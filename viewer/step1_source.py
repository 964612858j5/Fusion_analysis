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

    def region(self, channel):
        """The corrected region, or None when this channel is not corrected."""
        return (self._regions.get(str(channel))
                if self.source_of(channel) == SOURCE_CORRECTED else None)

    def missing(self):
        """Every channel refused for want of a product, in name order."""
        for channel in list(self._decisions):
            self.source_of(channel)
        return [self._missing[ch] for ch in sorted(self._missing)]


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
    """
    y0, y1, x0, x1 = (int(v) for v in rect)
    stride = max(1, int(stride))
    py, px = y0 % stride, x0 % stride
    out_h = -(-(py + max(0, y1 - y0)) // stride)
    out_w = -(-(px + max(0, x1 - x0)) // stride)
    sums = np.zeros((out_h, out_w), np.float64)
    counts = np.zeros((out_h, out_w), np.int64)
    if region is not None and out_h and out_w:
        by0, by1, bx0, bx1 = region.bbox
        oy0, oy1 = max(y0, by0), min(y1, by1)
        ox0, ox1 = max(x0, bx0), min(x1, bx1)
        if oy1 > oy0 and ox1 > ox0:
            budget = max(1, int(max_elements))
            # WHOLE BLOCKS IN BOTH AXES, and never more than the budget. A
            # single row of blocks across a whole slide is itself gigabytes
            # (256 x 40000 at an overview stride), so the walk is a grid of
            # slabs, not a stack of full-width strips.
            band = max(stride, int(np.sqrt(budget)) // stride * stride)
            rows = max(stride, (budget // band) // stride * stride)
            cursor_y = oy0
            while cursor_y < oy1:
                stop_y = min(oy1, cursor_y + rows)
                cursor_x = ox0
                while cursor_x < ox1:
                    stop_x = min(ox1, cursor_x + band)
                    slab, placed = region.read(cursor_y, stop_y,
                                               cursor_x, stop_x)
                    cursor_x = stop_x
                    if slab is None:
                        continue
                    sy0, sy1, sx0, sx1 = placed
                    block_y = (np.arange(sy0, sy1) - y0 + py) // stride
                    block_x = (np.arange(sx0, sx1) - x0 + px) // stride
                    flat = (block_y[:, None] * out_w
                            + block_x[None, :]).ravel()
                    np.add.at(sums.ravel(), flat,
                              slab.ravel().astype(np.float64))
                    np.add.at(counts.ravel(), flat, 1)
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
    # chunks from the level-0 rectangle this tile covers.
    level0 = (y0 * stride, y1 * stride, x0 * stride, x1 * stride)
    return reduce_corrected(table.region(channel), level0, stride,
                            max_elements=max_elements)
