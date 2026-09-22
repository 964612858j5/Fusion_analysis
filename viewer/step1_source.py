"""Which pixels Step1's whole-slide viewer draws, per channel and per tile.

Block B of `docs/step1_rework_plan.md`. Step1 shows the slide the user is
about to fuse, so what it draws has to be what the pipeline decided, not what
Step0 happens to be previewing:

* a channel counts as CORRECTED when its FINAL decision is `tophat` or
  `cucim` AND the corrected product for it can actually be opened -- the same
  pair of questions `core/io_loader.read_region` asks. A decision with no
  product is not a source;
* the corrected product is ROI-shaped (`root[<roi>][<channel>]`, with the
  ROI's own `bbox_fullres`), so it answers for the ROI and for nothing else.
  Outside the ROI there are NO PIXELS -- not raw ones, not zeros. A tile that
  straddles the boundary draws the inside and leaves the rest empty (user
  ruling, 2026-09-16);
* a channel whose decision is `original` reads the raw pyramid INSIDE THE
  SAME ROI. The ROI is the analysis region, not a property of the corrected
  product: outside it Step1 draws nothing whatever the channel's source is,
  and a tile across the boundary reads only the part inside -- the raw
  reader is handed the clipped rectangle, never the whole tile;
* a channel whose decision says corrected but whose product will not open is
  REFUSED rather than quietly served raw: the viewer says which channel and
  why, and draws nothing for it.

This module is the rule and the reader, with no Qt in it: the tiles it
returns are `(values, valid)` pairs, and the host turns them into pixels.

COARSE LEVELS, and the two different answers they get:

* a RAW channel reads the OME's own pyramid at that level. Reading level 0
  and averaging it here would multiply the I/O by the square of the
  downsample for pixels the file already has -- the opposite of what a
  whole-slide viewer is for. Only the ROI is converted into level-k
  coordinates, so the region still decides what may be drawn;
* a CORRECTED channel has no pyramid -- the product is one ROI-shaped array,
  and the raw pyramid holds different numbers -- so the mean is computed
  here, IN BOUNDED CHUNKS. A whole-slide overview covers billions of level-0
  pixels; materialising that rectangle to average it would be gigabytes, so
  the reduction walks the region in slabs whose size is fixed
  (`MAX_REDUCTION_ELEMENTS`) and keeps only the running sums.

Both share the grid rule: blocks are anchored at the LEVEL-0 ORIGIN, so a
tile's blocks do not shift with the tile, and each block is normalised by its
number of VALID samples -- an ROI edge must not be darkened by averaging in
the emptiness outside it.
"""

import numpy as np

#: The two decisions that mean "a corrected product was published".
CORRECTED_DECISIONS = ("tophat", "cucim")

#: The most level-0 pixels a corrected reduction may hold at once. The
#: running sums are per OUTPUT block, so the peak is this plus the output --
#: never the whole viewport.
MAX_REDUCTION_ELEMENTS = 4 * 1024 * 1024

#: What `source_of` answers.
SOURCE_CORRECTED = "corrected"
SOURCE_RAW = "raw"
SOURCE_MISSING = "missing"

#: The PERSISTED COARSE PLANE (G3.2b.4E).
#:
#: A corrected product has no pyramid, so the coarsest level of a corrected
#: channel is reduced from level 0 on demand -- and because that level is
#: the whole region, the first tick of a cuCIM channel waited for a whole
#: region to be averaged: 8.9-10.5 s measured on a real 59040x35520 slide,
#: while the tiles the viewport actually needed were in hand in 38 ms.
#:
#: This is that same plane, written once beside the product and read back
#: instead of recomputed. It is NOT a cache and NOT another authority:
#: nothing at runtime writes it, there is no eviction and no generation, and
#: every question it answers is checked against the product's own identity
#: first. A plane that is missing, stale, incomplete or for another level is
#: not used at all -- the runtime reduction below is still the definition.
COARSE_SIDECAR_DIRNAME = "corrected_coarse.zarr"

#: Bumped when the numbers a plane holds would change. A plane stamped with
#: another version is stale by definition.
COARSE_PLANE_FORMAT_VERSION = 1

#: Copied from the product's own array attrs when a plane is written, and
#: compared field by field when one is read. Any of them moving -- another
#: method, another parameter, another correction algorithm version, another
#: ROI, a reshaped product -- retires the plane.
COARSE_IDENTITY_FIELDS = (
    "channel_name", "correction_method", "correction_param_name",
    "correction_param_value", "bg_correction_algo_version", "roi_name",
    "roi_bbox_fullres", "source_shape",
    # THE ONE FIELD THE STATIC ONES CANNOT REPLACE. Everything above is the
    # SETTING a channel was computed with, and a second Save with the same
    # settings produces the same values for all of them -- while the pixels
    # underneath may be different (a changed raw slide, a re-run after an
    # interrupted write, a fixed polygon). `source_identity` is minted anew
    # by the writer every time a channel's level 0 is actually recomputed,
    # so a plane left over from the previous write cannot match the product
    # it now sits beside. It is the same field `_product_token` already
    # reads, not a second authority.
    "source_identity",
)


def coarse_block_range(bbox, stride):
    """The blocks of the global level-0 grid that cover `bbox`.

    `(origin, shape)`. The grid is anchored at the level-0 origin, so an ROI
    that does not start on a block boundary still lands on the same blocks a
    tile read would use -- which is the whole point of anchoring it there.
    """
    y0, y1, x0, x1 = (int(v) for v in bbox)
    stride = max(1, int(stride))
    by0, bx0 = y0 // stride, x0 // stride
    by1, bx1 = -(-y1 // stride), -(-x1 // stride)
    return (by0, bx0), (max(0, by1 - by0), max(0, bx1 - bx0))


def coarse_plane_identity(array_attrs, stride, level=None):
    """The attrs a plane must carry to be usable for this product."""
    identity = {field: array_attrs.get(field) for field in COARSE_IDENTITY_FIELDS}
    identity["stride"] = int(stride)
    identity["format_version"] = COARSE_PLANE_FORMAT_VERSION
    if level is not None:
        identity["level"] = int(level)
    return identity


class CoarsePlane:
    """One channel's persisted coarse plane, already checked against its
    product.

    It answers a tile by SLICING, never by reducing: the block grid it holds
    is the same grid `reduce_corrected` anchors at the level-0 origin, so a
    tile's blocks are `rect` itself, offset by where the plane starts.
    """

    def __init__(self, array, stride, origin):
        self.array = array
        self.stride = int(stride)
        self.origin = (int(origin[0]), int(origin[1]))

    @property
    def shape(self):
        return tuple(int(v) for v in self.array.shape)

    def tile(self, rect):
        """`(values, valid)` for a LEVEL-k rectangle, or None to fall back.

        `valid` is where the plane has blocks, which is exactly where the
        runtime reduction counts at least one sample: the plane covers the
        blocks the ROI covers and nothing else.
        """
        y0, y1, x0, x1 = (int(v) for v in rect)
        height, width = max(0, y1 - y0), max(0, x1 - x0)
        values = np.zeros((height, width), np.float32)
        valid = np.zeros((height, width), bool)
        if not height or not width:
            return values, valid
        by0, bx0 = self.origin
        ph, pw = self.shape
        sy0, sy1 = max(y0 - by0, 0), min(y1 - by0, ph)
        sx0, sx1 = max(x0 - bx0, 0), min(x1 - bx0, pw)
        if sy1 <= sy0 or sx1 <= sx0:
            return values, valid          # north or west of the region
        block = np.asarray(self.array[sy0:sy1, sx0:sx1], dtype=np.float32)
        oy, ox = sy0 + by0 - y0, sx0 + bx0 - x0
        values[oy:oy + block.shape[0], ox:ox + block.shape[1]] = block
        valid[oy:oy + block.shape[0], ox:ox + block.shape[1]] = True
        return values, valid


class MissingProduct:
    """Why a channel the decisions call corrected cannot be drawn."""

    NO_STORE = "no corrected store for this project"
    NO_ARRAY = "this ROI has no corrected array for the channel"

    def __init__(self, channel, reason):
        self.channel = str(channel)
        self.reason = str(reason)

    def __eq__(self, other):
        return (isinstance(other, MissingProduct)
                and (self.channel, self.reason) == (other.channel, other.reason))

    def __hash__(self):
        return hash((self.channel, self.reason))

    def __repr__(self):
        return f"MissingProduct({self.channel!r}, {self.reason!r})"


class CorrectedRegion:
    """One ROI's corrected pixels for one channel, in level-0 coordinates."""

    def __init__(self, array, bbox_fullres):
        self.array = array
        y0, y1, x0, x1 = (int(v) for v in bbox_fullres)
        self.bbox = (y0, y1, x0, x1)

    def contains(self, y0, y1, x0, x1):
        by0, by1, bx0, bx1 = self.bbox
        return by0 <= y0 and y1 <= by1 and bx0 <= x0 and x1 <= bx1

    def overlaps(self, y0, y1, x0, x1):
        by0, by1, bx0, bx1 = self.bbox
        return not (y1 <= by0 or y0 >= by1 or x1 <= bx0 or x0 >= bx1)

    def read(self, y0, y1, x0, x1):
        """The overlap of this rectangle with the ROI, and where it sits.

        Returns `(values, (oy0, oy1, ox0, ox1))` in level-0 coordinates, or
        `(None, None)` when the rectangle misses the ROI entirely.
        """
        by0, by1, bx0, bx1 = self.bbox
        oy0, oy1 = max(y0, by0), min(y1, by1)
        ox0, ox1 = max(x0, bx0), min(x1, bx1)
        if oy1 <= oy0 or ox1 <= ox0:
            return None, None
        values = np.asarray(
            self.array[oy0 - by0:oy1 - by0, ox0 - bx0:ox1 - bx0],
            dtype=np.float32)
        return values, (oy0, oy1, ox0, ox1)


class Step1SourceTable:
    """The per-channel answer, resolved once for a handoff.

    `open_corrected` is the opener (`utils.calibration_source
    .open_corrected_channel_array` in production, a stub in tests) and
    `roi_name` the ROI whose group is preferred.
    """

    def __init__(self, decisions=None, corrected_zarr_path="", roi_name="",
                 open_corrected=None, roi_bbox=None, handoff_revision=""):
        # THE ANALYSIS REGION, in its own right. It is not read off a
        # corrected product: a project whose channels are all `original` has
        # an ROI too, and "outside the ROI there are no pixels" is a rule
        # about the region, not about a product. `None` means the whole
        # slide -- a full-WSI project, where every tile is inside.
        self._roi_bbox = (tuple(int(v) for v in roi_bbox)
                          if roi_bbox and len(roi_bbox) == 4 else None)
        #: Whatever the handoff calls this version of itself. It is part of
        #: the cache identity: republishing a handoff can change the pixels
        #: without changing a path.
        self._handoff_revision = str(handoff_revision or "")
        self._decisions = {str(ch): str(m or "").strip().lower()
                           for ch, m in dict(decisions or {}).items()}
        self._path = str(corrected_zarr_path or "")
        self._roi_name = str(roi_name or "")
        if open_corrected is None:
            from ..utils.calibration_source import open_corrected_channel_array
            open_corrected = open_corrected_channel_array
        self._open = open_corrected
        self._regions = {}
        self._missing = {}
        #: Resolved persisted coarse planes, and the channels that have none.
        #: Asked once per channel per stride: a product without a plane must
        #: not pay a directory lookup per tile.
        self._coarse_planes = {}

    # ── identity ──────────────────────────────────────────────────────
    def identity_token(self):
        """What makes a Step1 tile's meaning, beyond the slide's own path.

        A cache keyed on the dataset alone would hand a raw tile back for a
        channel whose decision has since become `tophat`, or an old corrected
        tile after the product was regenerated -- same path, same channel,
        different pixels. The token below moves whenever any of that does, so
        the keys move with it.
        """
        parts = [f"roi={self._roi_bbox}", f"handoff={self._handoff_revision}"]
        for channel in sorted(self._decisions):
            decision = self._decisions[channel]
            parts.append(f"{channel}={decision}")
            if decision in CORRECTED_DECISIONS:
                parts.append(f"{channel}#{self._product_token(channel)}")
        return "|".join(parts)

    def _product_token(self, channel):
        """A corrected product's own identity: shape, dtype and its attrs."""
        if self.source_of(channel) != SOURCE_CORRECTED:
            return "absent"
        array = self._regions[channel].array
        attrs = dict(getattr(array, "attrs", {}) or {})
        stamp = attrs.get("source_identity") or attrs.get("written_at") or ""
        return (f"{tuple(getattr(array, 'shape', ()))}"
                f":{getattr(array, 'dtype', '')}"
                f":{attrs.get('correction_method', '')}"
                f":{attrs.get('roi_name', '')}:{stamp}")

    # ── the region ────────────────────────────────────────────────────
    def roi_bbox(self):
        """The analysis region in level-0 pixels, or None for a whole slide."""
        return self._roi_bbox

    def clip_to_roi(self, rect, stride=1):
        """`rect` cut down to the ROI, or None when it misses it entirely.

        `stride` is the level's downsample: with it, `rect` is read as
        LEVEL-k coordinates and the region is converted into them, so a raw
        channel can be clipped without ever leaving its own pyramid level.
        The ROI's edges are taken OUTWARD (floor the start, ceil the end) so
        a pixel that is partly inside the region is kept rather than dropped.
        """
        y0, y1, x0, x1 = (int(v) for v in rect)
        if self._roi_bbox is None:
            return (y0, y1, x0, x1)
        stride = max(1, int(stride))
        by0, by1, bx0, bx1 = self._roi_bbox
        if stride > 1:
            by0, bx0 = by0 // stride, bx0 // stride
            by1 = -(-by1 // stride)
            bx1 = -(-bx1 // stride)
        cy0, cy1 = max(y0, by0), min(y1, by1)
        cx0, cx1 = max(x0, bx0), min(x1, bx1)
        if cy1 <= cy0 or cx1 <= cx0:
            return None
        return (cy0, cy1, cx0, cx1)

    # ── the rule ──────────────────────────────────────────────────────
    def decision(self, channel):
        return self._decisions.get(str(channel), "original")

    def source_of(self, channel):
        """`corrected`, `raw` or `missing`, resolved and remembered."""
        channel = str(channel)
        if self.decision(channel) not in CORRECTED_DECISIONS:
            return SOURCE_RAW
        if channel in self._regions:
            return SOURCE_CORRECTED
        if channel in self._missing:
            return SOURCE_MISSING
        if not self._path:
            self._missing[channel] = MissingProduct(channel,
                                                    MissingProduct.NO_STORE)
            return SOURCE_MISSING
        array = self._open(self._path, channel, self._roi_name or None)
        bbox = None
        if array is not None:
            bbox = (dict(getattr(array, "attrs", {}) or {})
                    .get("roi_bbox_fullres"))
        if array is None or not bbox or len(bbox) != 4:
            self._missing[channel] = MissingProduct(channel,
                                                    MissingProduct.NO_ARRAY)
            return SOURCE_MISSING
        self._regions[channel] = CorrectedRegion(array, bbox)
        return SOURCE_CORRECTED

    def coarse_plane(self, channel, stride):
        """The persisted coarse plane for `channel` at `stride`, or None.

        None means "reduce it the way we always did" and is the answer for
        every product written before this existed, every plane whose
        identity does not match the product beside it, every incomplete one
        and every level that is not the one it was written for.
        """
        stride = max(1, int(stride))
        key = (str(channel), stride)
        if key in self._coarse_planes:
            return self._coarse_planes[key]
        plane = None
        try:
            plane = self._resolve_coarse_plane(str(channel), stride)
        except Exception:                                   # noqa: BLE001
            # A plane is an optimisation. Nothing it can do -- a missing
            # directory, an unreadable store, an attr of the wrong type --
            # may stop the tile being produced the old way.
            plane = None
        self._coarse_planes[key] = plane
        return plane

    def _resolve_coarse_plane(self, channel, stride):
        if stride <= 1 or self.source_of(channel) != SOURCE_CORRECTED:
            return None
        if not self._path:
            return None
        import os
        sidecar = os.path.join(os.path.dirname(self._path),
                               COARSE_SIDECAR_DIRNAME)
        if not os.path.isdir(sidecar):
            return None
        array = self._open(sidecar, channel, self._roi_name or None)
        if array is None:
            return None
        attrs = dict(getattr(array, "attrs", {}) or {})
        if not attrs.get("complete"):
            # Written last, after the pixels. A run that was cancelled or
            # died leaves the pixels without it, and they are not read.
            return None
        region = self._regions[channel]
        product = dict(getattr(region.array, "attrs", {}) or {})
        if not str(product.get("source_identity") or "").strip():
            # A product that cannot say WHICH WRITE it is gets no plane. The
            # static fields would match a plane left over from an earlier
            # write of the same channel with the same settings and different
            # pixels, and there would be no way to tell. Products written
            # before this existed therefore keep reducing at runtime -- which
            # is correct, just as slow as it was.
            return None
        expected = coarse_plane_identity(product, stride)
        for field, value in expected.items():
            if _as_plain(attrs.get(field)) != _as_plain(value):
                return None
        origin, shape = coarse_block_range(region.bbox, stride)
        if tuple(int(v) for v in array.shape) != tuple(shape):
            return None
        if (int(attrs.get("block_origin", (-1, -1))[0]) != origin[0]
                or int(attrs.get("block_origin", (-1, -1))[1]) != origin[1]):
            return None
        return CoarsePlane(array, stride, origin)

    def region(self, channel):
        """The corrected region, or None when this channel is not corrected."""
        return (self._regions.get(str(channel))
                if self.source_of(channel) == SOURCE_CORRECTED else None)

    def missing(self):
        """Every channel refused for want of a product, in name order."""
        for channel in list(self._decisions):
            self.source_of(channel)
        return [self._missing[ch] for ch in sorted(self._missing)]


def _as_plain(value):
    """Compare attrs by value, not by the container zarr handed back."""
    if isinstance(value, (list, tuple)):
        return [_as_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def box_downsample_valid(values, valid, stride, phase=(0, 0)):
    """Mean of each `stride x stride` block, over the VALID samples only.

    `phase` is where this array's top-left corner sits inside the global
    grid -- `(y0 % stride, x0 % stride)` for a rectangle read at `(y0, x0)`.
    Blocks are therefore anchored at the LEVEL-0 ORIGIN and do not move with
    the tile, which is what keeps a coarse tile's pixels the same wherever it
    is read from.

    Returns `(mean, valid_out)`; a block with no valid sample is 0 and not
    valid.
    """
    stride = max(1, int(stride))
    if stride == 1:
        return np.asarray(values, np.float32), np.asarray(valid, bool)
    values = np.asarray(values, np.float32)
    valid = np.asarray(valid, bool)
    py, px = int(phase[0]) % stride, int(phase[1]) % stride
    # Pad the front so the first block starts on the global grid, and the
    # back so the last block is whole.
    h, w = values.shape
    pad_y1 = (-(py + h)) % stride
    pad_x1 = (-(px + w)) % stride
    values = np.pad(values, ((py, pad_y1), (px, pad_x1)))
    valid = np.pad(valid, ((py, pad_y1), (px, pad_x1)))
    bh, bw = values.shape[0] // stride, values.shape[1] // stride
    blocks = values.reshape(bh, stride, bw, stride)
    counts = valid.reshape(bh, stride, bw, stride).sum(axis=(1, 3))
    sums = (blocks * valid.reshape(bh, stride, bw, stride)).sum(axis=(1, 3))
    out_valid = counts > 0
    out = np.zeros((bh, bw), np.float32)
    np.divide(sums, np.maximum(counts, 1), out=out, where=out_valid)
    return out, out_valid


def _isqrt(value):
    """Integer square root, without trusting a float round-trip."""
    value = max(0, int(value))
    root = int(np.sqrt(value))
    while root > 0 and root * root > value:
        root -= 1
    while (root + 1) * (root + 1) <= value:
        root += 1
    return root


def _slab_geometry(stride, max_elements):
    """`(rows, band)`: how much of the region one pass may hold, in pixels.

    Both are WHOLE MULTIPLES OF `stride`, so the walk steps along the global
    block grid and every slab but the first and last on each axis is already
    block-aligned.

    The budget is spent on `(rows + stride) x (band + stride)`, not on
    `rows x band`. The region's own first and last row/column need not sit on
    the block grid, so one slab per axis may have to be padded out by up to a
    block before it can be reshaped -- and it would be no use bounding the
    READ if the padded temporary were then allowed to exceed the same limit.
    """
    budget = max(1, int(max_elements))
    blocks = max(1, budget // (stride * stride))
    side = max(1, _isqrt(blocks))
    band_blocks = max(1, side - 1)
    rows_blocks = max(1, blocks // (band_blocks + 1) - 1)
    return rows_blocks * stride, band_blocks * stride


def _span_per_block(front, length, stride, blocks):
    """How many of `length` real samples land in each of `blocks` blocks.

    The padding a slab needed to reach whole blocks contributes pixels to a
    sum (zeros, which add nothing) but must never contribute to a COUNT, or
    the block at an ROI edge would be divided by samples that do not exist
    and the edge would go dark. This is that count, as two small vectors
    instead of a mask the size of the slab.
    """
    starts = np.arange(blocks, dtype=np.int64) * stride
    lo = np.maximum(starts, int(front))
    hi = np.minimum(starts + stride, int(front) + int(length))
    return np.maximum(hi - lo, 0)


def _accumulate_blocks(sums, counts, slab, placed, stride, base_by, base_bx):
    """Add one slab into the global block sums and counts, VECTORISED.

    The slab is padded out to whole blocks OF THE GLOBAL GRID -- the pad is
    zeros, which add nothing to a sum -- and then reshaped so both block axes
    are summed at once. No level-0 pixel is ever scattered individually.

    The phase comes from where the slab actually sits (`placed`), never from
    where the walk asked it to be, so a region that hands back less than was
    asked for still lands on the right blocks.
    """
    values = np.asarray(slab, np.float32)
    if values.ndim != 2 or not values.size:
        return
    sy0, _sy1, sx0, _sx1 = placed
    height, width = values.shape
    front_y, front_x = int(sy0) % stride, int(sx0) % stride
    pad_y = (-(front_y + height)) % stride
    pad_x = (-(front_x + width)) % stride
    if front_y or front_x or pad_y or pad_x:
        values = np.pad(values, ((front_y, pad_y), (front_x, pad_x)))
    bh = values.shape[0] // stride
    bw = values.shape[1] // stride
    if not bh or not bw:
        return
    block_sums = values.reshape(bh, stride, bw, stride).sum(axis=(1, 3),
                                                            dtype=np.float64)
    rows_in = _span_per_block(front_y, height, stride, bh)
    cols_in = _span_per_block(front_x, width, stride, bw)
    # A block's index is its own position on the level-0 grid minus the
    # rectangle's first block -- see `reduce_corrected`.
    oy = int(sy0) // stride - base_by
    ox = int(sx0) // stride - base_bx
    sums[oy:oy + bh, ox:ox + bw] += block_sums
    counts[oy:oy + bh, ox:ox + bw] += rows_in[:, None] * cols_in[None, :]


def reduce_corrected(region, rect, stride, max_elements=MAX_REDUCTION_ELEMENTS):
    """Mean of a corrected product over `rect`, in BOUNDED chunks.

    `rect` is in level-0 pixels and may be the whole slide: the overview asks
    for exactly that. Materialising it would be gigabytes, so the region's
    own overlap is walked in slabs of at most `max_elements` level-0 pixels
    and only the per-output-block sums and counts are kept. The blocks are
    anchored at the level-0 origin, so the answer does not depend on how the
    walk was cut.

    Returns `(mean, valid)` at `stride`, shaped like the rectangle's own
    coarse grid.

    TWO PATHS, because they are two different questions.

    * `stride == 1` IS NOT A REDUCTION. A block of one pixel is that pixel,
      so there is nothing to average: the overlap is read once and copied
      into place. The old path still built a block index per pixel and
      scattered every value through `np.add.at` to divide it by one, which
      cost about six times what the read itself costs.
    * `stride > 1` sums whole blocks with a reshape rather than scattering
      level-0 pixels one at a time. Same grid, same valid-sample
      normalisation, same bound -- see `_accumulate_blocks`.

    Neither path changes what a block MEANS: the mean over the valid samples
    of a grid anchored at the level-0 origin. A sample whose value is 0 is a
    sample; a position outside the region is not.
    """
    y0, y1, x0, x1 = (int(v) for v in rect)
    stride = max(1, int(stride))
    height, width = max(0, y1 - y0), max(0, x1 - x0)
    budget = max(1, int(max_elements))

    if stride == 1 and height * width <= budget:
        # The rectangle IS the block grid. `overlaps` is asked first so a
        # tile that misses the region entirely reads nothing at all.
        values = np.zeros((height, width), np.float32)
        valid = np.zeros((height, width), bool)
        if (region is not None and height and width
                and region.overlaps(y0, y1, x0, x1)):
            pixels, placed = region.read(y0, y1, x0, x1)
            if pixels is not None:
                pixels = np.asarray(pixels, np.float32)
                oy0, _oy1, ox0, _ox1 = placed
                # Sized from the pixels that came back, not from the
                # rectangle that was asked for.
                ph, pw = pixels.shape
                ry, rx = int(oy0) - y0, int(ox0) - x0
                values[ry:ry + ph, rx:rx + pw] = pixels
                valid[ry:ry + ph, rx:rx + pw] = True
        return values, valid

    py, px = y0 % stride, x0 % stride
    out_h = -(-(py + height) // stride)
    out_w = -(-(px + width) // stride)
    sums = np.zeros((out_h, out_w), np.float64)
    counts = np.zeros((out_h, out_w), np.int64)
    if region is not None and out_h and out_w:
        by0, by1, bx0, bx1 = region.bbox
        oy0, oy1 = max(y0, by0), min(y1, by1)
        ox0, ox1 = max(x0, bx0), min(x1, bx1)
        if oy1 > oy0 and ox1 > ox0:
            # WHOLE BLOCKS IN BOTH AXES, and never more than the budget. A
            # single row of blocks across a whole slide is itself gigabytes
            # (256 x 40000 at an overview stride), so the walk is a grid of
            # slabs, not a stack of full-width strips.
            rows, band = _slab_geometry(stride, budget)
            # `y0 - y0 % stride` is the rectangle's first block on the
            # level-0 grid, so a level-0 row `r` belongs to output block
            # `r // stride - base_by`.
            base_by, base_bx = y0 // stride, x0 // stride
            cursor_y = oy0
            while cursor_y < oy1:
                # STEP TO A BLOCK BOUNDARY, so only the very first and very
                # last slab on an axis can be off the grid.
                stop_y = min(oy1, (cursor_y // stride) * stride + rows)
                cursor_x = ox0
                while cursor_x < ox1:
                    stop_x = min(ox1, (cursor_x // stride) * stride + band)
                    slab, placed = region.read(cursor_y, stop_y,
                                               cursor_x, stop_x)
                    cursor_x = stop_x
                    if slab is None:
                        continue
                    _accumulate_blocks(sums, counts, slab, placed, stride,
                                       base_by, base_bx)
                cursor_y = stop_y
    valid = counts > 0
    mean = np.where(valid, sums / np.maximum(counts, 1), 0.0).astype(np.float32)
    return mean, valid


def read_tile(table, channel, rect, stride=1, read_raw=None,
              max_elements=MAX_REDUCTION_ELEMENTS):
    """One tile of `channel`, as `(values, valid)`.

    `rect` is in LEVEL-k coordinates when `stride` is that level's
    downsample: a RAW channel is read from the pyramid at that very level --
    `read_raw(channel, rect)` is handed level-k coordinates -- and a
    CORRECTED one is reduced from its product, which has only level 0.

    A corrected channel is drawn INSIDE ITS ROI ONLY: outside, `valid` is
    False and the values are 0 -- which is not the same as black, and the
    host must not paint it.
    """
    y0, y1, x0, x1 = (int(v) for v in rect)
    stride = max(1, int(stride))
    source = table.source_of(channel)
    if source == SOURCE_MISSING:
        return None, None

    if source == SOURCE_RAW:
        # THE PYRAMID'S OWN LEVEL. Reading level 0 and averaging it here
        # would cost stride^2 times the I/O for pixels the file already
        # holds. Only the region is converted into this level's
        # coordinates.
        height, width = max(0, y1 - y0), max(0, x1 - x0)
        values = np.zeros((height, width), np.float32)
        valid = np.zeros((height, width), bool)
        clipped = table.clip_to_roi((y0, y1, x0, x1), stride=stride)
        if clipped is not None:
            cy0, cy1, cx0, cx1 = clipped
            pixels = np.asarray(read_raw(channel, clipped), np.float32)
            values[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] = pixels
            valid[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] = True
        return values, valid

    # CORRECTED: one ROI-shaped array with no pyramid, reduced in bounded
    # chunks from the level-0 rectangle this tile covers -- unless that
    # exact plane was written beside the product, in which case this tile is
    # a slice of it. The two are the same numbers: `rect` in level-k
    # coordinates IS the block range, because the blocks are anchored at the
    # level-0 origin and `y0 * stride % stride == 0`.
    plane = table.coarse_plane(channel, stride) if stride > 1 else None
    if plane is not None:
        try:
            return plane.tile((y0, y1, x0, x1))
        except Exception:                                   # noqa: BLE001
            pass            # a read that failed is not an answer; reduce it
    level0 = (y0 * stride, y1 * stride, x0 * stride, x1 * stride)
    return reduce_corrected(table.region(channel), level0, stride,
                            max_elements=max_elements)
