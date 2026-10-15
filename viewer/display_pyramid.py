"""v16_5 R7b B2 (docs/v16_5_r7b_corrected_read_plan.md B2.1-B2.3): the
display layers of a corrected channel.

A corrected product is one float32 level 0 in 1024 chunks; every coarser
level Step1 draws was reduced from it, at runtime or (stride 32 only) from
`corrected_coarse.zarr`. This writes the SAME reduction for every level of
the slide's pyramid -- blocks anchored at the level-0 origin, each the mean
of the samples inside the region (`box_downsample_valid`) -- rounded ONCE to
uint16, in 512 chunks, beside the product in `corrected_display.zarr`:

    <group>/<channel>/s<stride>        (the product is <group>/<channel>)

* ELIGIBLE: every sample the product holds (fill included) is finite, an
  integer and in 0..65535; one sample that is not, and the channel gets no
  display layers at all (logged). No scaling.
* EXACT SUMS: block sums are float64 of integers (exact), each coarser
  stride summed from the finer one's sums, so every layer is the mean of
  its level-0 samples rounded once (half up): source error <= 0.5.
* NOT AN AUTHORITY: identity copied from the product (`coarse_plane_identity`
  fields, `source_identity` included) plus the stride, dtype and block
  origin; `complete` is written last, on every stride at once. Step1 checks
  all of it (`accept`) and otherwise reads as before.
* BOUNDED: fed band by band (a band is a full-width run of rows; Step0
  passes its tiles in row order, the backfill script reads slabs), only one
  band's sums are held per stride.
"""

import os
import queue
import shutil
import threading
import time

import numpy as np

from . import step1_source as sources

DISPLAY_DIRNAME = "corrected_display.zarr"
DISPLAY_FORMAT_VERSION = 1
CHUNK = 512
#: working memory a builder may use per channel (B2.3), configurable
MAX_WORK_BYTES = 1 << 30


def level_strides(slide_path):
    """`round(h0/hk)` of every level k >= 1 of the slide's pyramid (> 1,
    sorted, unique) -- the strides Step1 asks a corrected channel for."""
    try:
        import tifffile
        with tifffile.TiffFile(str(slide_path)) as handle:
            heights = [int(level.shape[-2]) for level in handle.series[0].levels]
    except Exception:                                       # noqa: BLE001
        return []
    if len(heights) < 2 or heights[0] <= 0:
        return []
    return sorted({int(round(heights[0] / float(h))) for h in heights[1:] if h > 0} - {0, 1})


def identity(product_attrs, stride):
    """What a display array must carry to stand for this product."""
    out = sources.coarse_plane_identity(product_attrs, stride)
    out.update(kind="display", dtype="<u2", format_version=DISPLAY_FORMAT_VERSION)
    return out


def display_root(product_path):
    return os.path.join(os.path.dirname(str(product_path)), DISPLAY_DIRNAME)


def drop(root_path, group_name, channel):
    """Remove a channel's display layers (BEFORE its level 0 moves)."""
    shutil.rmtree(os.path.join(str(root_path), str(group_name), str(channel)), ignore_errors=True)


def accept(root_path, array_path, product_attrs, bbox, stride):
    """The display array for this product at `stride`, or None.

    `array_path` is the product's own path in its store (`<group>/<ch>`).
    Refused unless complete, of this product's identity (a product without
    `source_identity` gets none), `<u2`, and exactly the block range of
    `bbox` at `stride` with that origin."""
    if not str(product_attrs.get("source_identity") or "").strip():
        return None
    folder = os.path.join(str(root_path), str(array_path), f"s{int(stride)}")
    if not os.path.isfile(os.path.join(folder, ".zarray")):
        return None
    import zarr
    array = zarr.open_array(folder, mode="r")
    attrs = dict(array.attrs)
    if not attrs.get("complete") or array.dtype != np.dtype("<u2"):
        return None
    for field, value in identity(product_attrs, stride).items():
        if sources._as_plain(attrs.get(field)) != sources._as_plain(value):
            return None
    origin, shape = sources.coarse_block_range(bbox, stride)
    if tuple(int(v) for v in array.shape) != tuple(shape):
        return None
    if [int(v) for v in attrs.get("block_origin") or (-1, -1)] != [origin[0], origin[1]]:
        return None
    return array


class _Stride:
    """One stride's sums for the band under way, and its output array."""

    def __init__(self, stride, bbox, array):
        self.stride = stride
        (self.by0, self.bx0), (self.bh, self.bw) = sources.coarse_block_range(bbox, stride)
        self.array = array
        self.row0 = 0                          # output row the held sums start at
        self.sums = np.zeros((0, self.bw), np.float64)

    def add(self, block_sums, gby, gbx):
        """Block sums whose first block is global block (gby, gbx)."""
        oy, ox = gby - self.by0 - self.row0, gbx - self.bx0
        need = oy + block_sums.shape[0]
        if need > self.sums.shape[0]:
            self.sums = np.vstack([self.sums, np.zeros((need - self.sums.shape[0], self.bw))])
        self.sums[oy:need, ox:ox + block_sums.shape[1]] += block_sums

    def flush(self, upto, bbox):
        """Write the output rows before `upto` (output row index)."""
        n = min(upto, self.bh) - self.row0
        if n <= 0:
            return
        s = self.stride
        y0, y1, x0, x1 = bbox
        rows = np.arange(self.row0, self.row0 + n) + self.by0
        cols = np.arange(self.bw) + self.bx0
        rspan = np.minimum((rows + 1) * s, y1) - np.maximum(rows * s, y0)
        cspan = np.minimum((cols + 1) * s, x1) - np.maximum(cols * s, x0)
        counts = rspan[:, None].astype(np.float64) * cspan[None, :]
        held = self.sums[:n]
        if held.shape[0] < n:
            held = np.vstack([held, np.zeros((n - held.shape[0], self.bw))])
        mean = held / counts
        self.array[self.row0:self.row0 + n, :] = np.floor(mean + 0.5).astype("<u2")
        self.sums = self.sums[n:]
        self.row0 += n


def _block_sums(values, gy, gx, stride):
    """(sums, first global block row, col) of `values` at (gy, gx) level 0.

    Summed by adding strided slices (rows, then columns): exact in float32
    while a block's sum of uint16 samples stays below 2**24 (stride <= 16),
    float64 otherwise -- and much faster than a reshape sum."""
    fy, fx = gy % stride, gx % stride
    h, w = values.shape
    py, px = (-(fy + h)) % stride, (-(fx + w)) % stride
    if fy or fx or py or px:
        values = np.pad(values, ((fy, py), (fx, px)))
    if values.dtype == np.float32 and stride > 16:
        values = values.astype(np.float64)
    rows = values[0::stride].copy()
    for i in range(1, stride):
        rows += values[i::stride]
    sums = rows[:, 0::stride].copy()
    for i in range(1, stride):
        sums += rows[:, i::stride]
    return sums.astype(np.float64, copy=False), gy // stride, gx // stride


class DisplayBuilder:
    """One channel's display layers, written while its level 0 passes by.

    `add` takes level-0 values at global (gy, gx), in row order: a value at
    a row greater than the band under way starts a new band, and every
    output row the previous bands finished is written. `finish` publishes;
    `abort` removes what was written. `refused` says why nothing will be
    published (ineligible, over the memory bound, out of disk, a write
    error) -- a builder that refused only drops what it got.

    `threaded`: the work runs on a background thread fed through a queue of
    two tiles (Step0: the save loop is not held up; `finish` / `abort` wait
    for it). The tiles handed over must not change afterwards.
    """

    def __init__(self, root_path, group_name, channel, bbox, strides,
                 max_bytes=MAX_WORK_BYTES, log=print, threaded=False):
        self.root = str(root_path)
        self.group_name, self.channel = str(group_name), str(channel)
        self.bbox = tuple(int(v) for v in bbox)
        self.log = log
        self.refused = ""
        self._band = None
        self._strides = []
        self._queue = self._thread = None
        y0, y1, x0, x1 = self.bbox
        strides = sorted({int(s) for s in strides if int(s) > 1})
        if not strides or y1 <= y0 or x1 <= x0:
            self.refused = "no level to write"
            return
        width = x1 - x0 + 2 * strides[-1]
        if 4096 * width * 4 + (4096 // strides[0] + 2) * (width // strides[0] + 2) * 8 > max_bytes:
            self.refused = "a band would exceed the working-memory bound"
            return
        need = sum(int(np.prod(sources.coarse_block_range(self.bbox, s)[1])) * 2 for s in strides)
        probe = os.path.abspath(self.root)
        while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)              # the nearest folder that exists
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = 0
        if free < need * 1.1:
            self.refused = f"not enough disk ({free} free, {int(need * 1.1)} needed)"
            return
        try:
            import zarr
            from numcodecs import Blosc
            drop(self.root, self.group_name, self.channel)
            group = zarr.open_group(self.root, mode="a").require_group(
                self.group_name).require_group(self.channel)
            for s in strides:
                _origin, shape = sources.coarse_block_range(self.bbox, s)
                array = group.create_dataset(
                    f"s{s}", shape=shape, chunks=(CHUNK, CHUNK), dtype="<u2", fill_value=0,
                    compressor=Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE), overwrite=True)
                self._strides.append(_Stride(s, self.bbox, array))
            self._group = group
        except Exception as exc:                            # noqa: BLE001
            self._fail(f"could not create the layers: {exc}")
        if threaded and not self.refused:
            self._queue = queue.Queue(maxsize=2)
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"display {self.channel}")
            self._thread.start()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._consume(*item)                 # skips once refused

    def _join(self):
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()
            self._thread = None

    def _fail(self, why):
        if not self.refused:
            self.refused = why
            self.log(f"[display] {self.group_name}/{self.channel}: no display layers ({why})")
        self._strides = []

    def add(self, values, gy, gx):
        if self.refused:
            return
        if self._thread is not None:
            self._queue.put((values, gy, gx))
        else:
            self._consume(values, gy, gx)

    def _consume(self, values, gy, gx):
        if self.refused:
            return
        values = np.asarray(values, np.float32)
        if values.ndim != 2 or not values.size:
            return
        if not np.isfinite(values).all():
            return self._fail("a sample is not finite")
        lo, hi = float(values.min()), float(values.max())
        if lo < 0 or hi > 65535:
            return self._fail(f"a sample is outside 0..65535 ({lo}..{hi})")
        if not np.array_equal(values, np.floor(values)):
            return self._fail("a sample is not an integer")
        gy, gx = int(gy), int(gx)
        if self._band is not None and gy > self._band:
            self._flush_before(gy)
        self._band = gy
        try:
            sums, by, bx, prev = None, 0, 0, 1
            for st in self._strides:
                if sums is None or st.stride % prev:
                    sums, by, bx = _block_sums(values, gy, gx, st.stride)
                else:                          # coarser blocks from the finer sums
                    m = st.stride // prev
                    sums, by, bx = _block_sums(sums, by, bx, m)
                st.add(sums, by, bx)
                prev = st.stride
        except Exception as exc:                            # noqa: BLE001
            self._fail(f"accumulating failed: {exc}")

    def add_local(self, values, roi_y0, roi_x0):
        """`add` at ROI-local (y, x), as Step0 writes its tiles."""
        self.add(values, self.bbox[0] + int(roi_y0), self.bbox[2] + int(roi_x0))

    def _flush_before(self, gy):
        try:
            for st in self._strides:
                st.flush(gy // st.stride - st.by0, self.bbox)
        except Exception as exc:                            # noqa: BLE001
            self._fail(f"writing failed: {exc}")

    def finish(self, product_attrs):
        """Write what is left and publish; False (and nothing kept) if refused."""
        self._join()
        if not self.refused:
            for st in self._strides:
                try:
                    st.flush(st.bh, self.bbox)
                except Exception as exc:                    # noqa: BLE001
                    self._fail(f"writing failed: {exc}")
        if self.refused:
            self.abort()
            return False
        try:
            datasets = []
            for st in self._strides:
                for key, value in identity(dict(product_attrs), st.stride).items():
                    st.array.attrs[key] = value
                st.array.attrs["block_origin"] = [st.by0, st.bx0]
                st.array.attrs["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                datasets.append({"path": f"s{st.stride}", "coordinateTransformations": [
                    {"type": "scale", "scale": [float(st.stride)] * 2},
                    {"type": "translation", "translation": [float(st.by0 * st.stride),
                                                            float(st.bx0 * st.stride)]}]})
            self._group.attrs["multiscales"] = [{"version": "0.4", "name": self.channel,
                                                 "axes": [{"name": "y", "type": "space"},
                                                          {"name": "x", "type": "space"}],
                                                 "datasets": datasets}]
            for st in self._strides:
                st.array.attrs["complete"] = True          # LAST
        except Exception as exc:                            # noqa: BLE001
            self._fail(f"publishing failed: {exc}")
            self.abort()
            return False
        return True

    def abort(self):
        if self._thread is not None:
            self.refused = self.refused or "aborted"    # the thread skips the rest
            self._join()
        self._strides = []
        drop(self.root, self.group_name, self.channel)
