"""What Step1's whole-slide viewer draws, per channel and per tile.

Block B of `docs/step1_rework_plan.md`, B.1 and B.2. The rules under test:

* corrected means FINAL DECISION in {tophat, cucim} AND a product that opens;
* the corrected product answers for its ROI and nowhere else -- outside it
  there are no pixels, and a tile across the boundary draws the inside only;
* a decision with no product is REFUSED, with a reason, not served raw;
* coarse levels average the corrected pixels themselves, on a grid anchored
  at the level-0 origin, over the valid samples alone.

No Qt here: this is the rule and the reader.
"""

import numpy as np
import pytest

from block01.viewer.step1_source import (
    MAX_REDUCTION_ELEMENTS, SOURCE_CORRECTED, SOURCE_MISSING, SOURCE_RAW,
    MissingProduct, Step1SourceTable, _slab_geometry, box_downsample_valid,
    read_tile,
)


class _Array:
    """A corrected product: ROI-shaped pixels plus the ROI's own bbox."""

    def __init__(self, bbox, fill=None):
        y0, y1, x0, x1 = bbox
        h, w = y1 - y0, x1 - x0
        if fill is None:
            fill = np.arange(h * w, dtype=np.float32).reshape(h, w)
        self._pixels = np.asarray(fill, np.float32)
        self.attrs = {"roi_bbox_fullres": [y0, y1, x0, x1]}
        self.shape = self._pixels.shape
        self.dtype = self._pixels.dtype

    def __getitem__(self, key):
        return self._pixels[key]


def _table(decisions, arrays=None, path="/tmp/corrected.zarr", roi="ROI_1",
           roi_bbox=None):
    arrays = dict(arrays or {})

    def _open(_path, channel, _roi):
        return arrays.get(channel)

    return Step1SourceTable(decisions=decisions, corrected_zarr_path=path,
                            roi_name=roi, open_corrected=_open,
                            roi_bbox=roi_bbox)


class _RawReader:
    """The raw pyramid, recording exactly which rectangles it was asked for."""

    def __init__(self, pixels=None):
        self.calls = []
        self._pixels = pixels

    def __call__(self, channel, rect):
        self.calls.append((channel, tuple(rect)))
        y0, y1, x0, x1 = rect
        if self._pixels is None:
            return np.full((y1 - y0, x1 - x0), 7.0, np.float32)
        return np.asarray(self._pixels[y0:y1, x0:x1], np.float32)


ROI = (100, 200, 100, 200)


# ── 1. the rule ───────────────────────────────────────────────────────

def test_a_decision_of_original_reads_raw():
    table = _table({"CD3": "original"})
    assert table.source_of("CD3") == SOURCE_RAW


def test_a_channel_nobody_decided_reads_raw():
    assert _table({}).source_of("CD8") == SOURCE_RAW


@pytest.mark.parametrize("method", ["tophat", "cucim", "TopHat", " cuCIM "])
def test_a_decision_with_a_product_is_corrected(method):
    table = _table({"CD3": method}, {"CD3": _Array(ROI)})
    assert table.source_of("CD3") == SOURCE_CORRECTED
    assert table.region("CD3").bbox == ROI


def test_a_decision_without_a_product_is_refused_not_served_raw():
    table = _table({"CD3": "tophat"}, {})
    assert table.source_of("CD3") == SOURCE_MISSING
    assert table.missing() == [MissingProduct("CD3", MissingProduct.NO_ARRAY)]
    assert table.region("CD3") is None


def test_a_project_with_no_store_refuses_the_same_way():
    table = _table({"CD3": "cucim"}, {"CD3": _Array(ROI)}, path="")
    assert table.source_of("CD3") == SOURCE_MISSING
    assert table.missing() == [MissingProduct("CD3", MissingProduct.NO_STORE)]


def test_a_product_without_an_roi_bbox_is_refused():
    array = _Array(ROI)
    array.attrs = {}
    table = _table({"CD3": "tophat"}, {"CD3": array})
    assert table.source_of("CD3") == SOURCE_MISSING


def test_the_answer_is_resolved_once():
    opened = []

    def _open(_path, channel, _roi):
        opened.append(channel)
        return _Array(ROI)

    table = Step1SourceTable({"CD3": "tophat"}, "/tmp/c.zarr", "ROI_1", _open)
    for _ in range(4):
        assert table.source_of("CD3") == SOURCE_CORRECTED
    assert opened == ["CD3"]


# ── 2. the three kinds of tile ────────────────────────────────────────

def _corrected_table():
    values = np.arange(100 * 100, dtype=np.float32).reshape(100, 100)
    return _table({"CD3": "tophat"}, {"CD3": _Array(ROI, values)}), values


def test_a_tile_inside_the_roi_is_the_corrected_pixels():
    table, pixels = _corrected_table()
    values, valid = read_tile(table, "CD3", (120, 140, 130, 150))
    assert valid.all()
    assert np.array_equal(values, pixels[20:40, 30:50])


def test_a_tile_outside_the_roi_has_no_pixels_at_all():
    table, _ = _corrected_table()
    values, valid = read_tile(table, "CD3", (0, 20, 0, 20))
    assert not valid.any(), "the outside of the ROI was drawn"
    assert values.shape == (20, 20)


def test_a_tile_across_the_boundary_draws_the_inside_only():
    table, pixels = _corrected_table()
    values, valid = read_tile(table, "CD3", (90, 110, 90, 110))

    inside = valid[10:, 10:]
    assert inside.all(), "the ROI's own corner was left empty"
    assert np.array_equal(values[10:, 10:], pixels[0:10, 0:10])
    assert not valid[:10, :].any(), "pixels appeared above the ROI"
    assert not valid[:, :10].any(), "pixels appeared left of the ROI"


def test_a_refused_channel_yields_no_tile():
    table = _table({"CD3": "tophat"}, {})
    values, valid = read_tile(table, "CD3", (0, 10, 0, 10))
    assert values is None and valid is None


def test_a_raw_channel_comes_from_the_raw_reader():
    table = _table({"CD3": "original"})
    raw = _RawReader()

    values, valid = read_tile(table, "CD3", (0, 8, 0, 8), read_raw=raw)

    assert raw.calls == [("CD3", (0, 8, 0, 8))]
    assert valid.all() and (values == 7.0).all()


# ── 2b. the ROI rules the raw channels too ────────────────────────────
#
# The region is the analysis region, not a property of a corrected product:
# a project whose channels are all `original` has an ROI as well, and
# "outside it there are no pixels" is the same rule for both sources.

def test_a_raw_tile_inside_the_roi_is_the_raw_pixels():
    pixels = np.arange(200 * 200, dtype=np.float32).reshape(200, 200)
    table = _table({"CD3": "original"}, roi_bbox=ROI)
    raw = _RawReader(pixels)

    values, valid = read_tile(table, "CD3", (120, 140, 130, 150),
                              read_raw=raw)

    assert valid.all()
    assert np.array_equal(values, pixels[120:140, 130:150])
    assert raw.calls == [("CD3", (120, 140, 130, 150))]


def test_a_raw_tile_outside_the_roi_reads_nothing_at_all():
    table = _table({"CD3": "original"}, roi_bbox=ROI)
    raw = _RawReader()

    values, valid = read_tile(table, "CD3", (0, 20, 0, 20), read_raw=raw)

    assert not valid.any(), "the outside of the ROI was drawn"
    assert values.shape == (20, 20)
    assert raw.calls == [], "the raw pyramid was read outside the ROI"


def test_a_raw_tile_across_the_boundary_reads_the_clipped_rectangle():
    pixels = np.arange(200 * 200, dtype=np.float32).reshape(200, 200)
    table = _table({"CD3": "original"}, roi_bbox=ROI)
    raw = _RawReader(pixels)

    values, valid = read_tile(table, "CD3", (90, 110, 90, 110), read_raw=raw)

    assert raw.calls == [("CD3", (100, 110, 100, 110))], (
        "the raw reader was handed the whole tile instead of the ROI part")
    assert valid[10:, 10:].all()
    assert np.array_equal(values[10:, 10:], pixels[100:110, 100:110])
    assert not valid[:10, :].any()
    assert not valid[:, :10].any()


def test_a_coarse_raw_tile_reads_the_pyramid_at_that_level():
    """The OME already holds the coarse pixels: read THEM.

    Reading level 0 and averaging here would cost stride^2 the I/O for
    numbers the file has. What is converted into level-k coordinates is the
    REGION, so the ROI still decides what may be drawn.
    """
    pixels = np.full((200, 200), 100.0, np.float32)
    table = _table({"CD3": "original"}, roi_bbox=(400, 568, 400, 568))
    raw = _RawReader(pixels)

    # A level-2 tile (stride 4) covering level-2 pixels 96..112.
    values, valid = read_tile(table, "CD3", (96, 112, 96, 112), stride=4,
                              read_raw=raw)

    assert len(raw.calls) == 1
    _channel, asked = raw.calls[0]
    assert asked == (100, 112, 100, 112), (
        f"the raw pyramid was not asked at this level: {asked}")
    assert values.shape == (16, 16), "the raw tile was downsampled again"
    assert not valid[:4, :].any(), "pixels appeared outside the region"
    assert valid[4:, 4:].all()


def test_a_project_with_no_roi_draws_the_whole_slide_raw():
    """Full-WSI mode: there is no region to be outside of."""
    table = _table({"CD3": "original"}, roi_bbox=None)
    raw = _RawReader()

    values, valid = read_tile(table, "CD3", (0, 8, 0, 8), read_raw=raw)

    assert valid.all() and raw.calls == [("CD3", (0, 8, 0, 8))]


def test_the_raw_reader_is_never_asked_for_a_corrected_channel():
    table, _ = _corrected_table()

    def _read_raw(*_a, **_k):
        raise AssertionError("the raw reader was asked for corrected pixels")

    read_tile(table, "CD3", (120, 140, 120, 140), read_raw=_read_raw)


# ── 3. coarse levels ──────────────────────────────────────────────────

def test_a_coarse_tile_is_the_mean_of_the_corrected_pixels():
    """The product has no pyramid, so the mean is computed here."""
    table, pixels = _corrected_table()
    # Level-2 coordinates: 25..27 covers level-0 100..108.
    values, valid = read_tile(table, "CD3", (25, 27, 25, 27), stride=4)

    assert values.shape == (2, 2) and valid.all()
    expected = pixels[0:8, 0:8].reshape(2, 4, 2, 4).mean(axis=(1, 3))
    assert np.allclose(values, expected)


def test_the_coarse_grid_is_anchored_at_the_level_0_origin():
    """A block may not move with the tile it is read in."""
    table, pixels = _corrected_table()
    whole, _ = read_tile(table, "CD3", (25, 29, 25, 29), stride=4)
    right, _ = read_tile(table, "CD3", (25, 29, 27, 29), stride=4)

    assert np.allclose(right, whole[:, 2:]), "the coarse grid shifted"


def test_a_world_block_reads_the_same_from_any_tile_that_covers_it():
    """The coarse grid is anchored at the level-0 origin, not at the tile.

    Both tiles below start OFF the grid (102 and 98 with a stride of 4), so a
    grid that started at the tile would cut the same world pixels into
    different blocks and the two would disagree.
    """
    values = np.arange(80 * 80, dtype=np.float32).reshape(80, 80)
    roi = (100, 180, 100, 180)
    table = _table({"CD3": "tophat"}, {"CD3": _Array(roi, values)})

    # The world block 108..112 x 108..112 is whole inside both tiles.
    # Level-2 tiles whose level-0 origins are 104 and 96 -- both inside the
    # ROI, and the world block 108..112 is whole in each.
    a, _ = read_tile(table, "CD3", (26, 34, 26, 34), stride=4)
    b, _ = read_tile(table, "CD3", (24, 32, 24, 32), stride=4)

    ia = (108 - 104) // 4, (108 - 104) // 4
    ib = (108 - 96) // 4, (108 - 96) // 4
    expected = values[8:12, 8:12].mean()

    assert a[ia] == pytest.approx(expected), "tile A cut the world block"
    assert b[ib] == pytest.approx(expected), "tile B cut the world block"
    assert a[ia] == pytest.approx(b[ib])


def test_the_roi_edge_is_not_darkened_by_the_emptiness_outside_it():
    """The mean is over VALID samples: a 0 from outside must not join it.

    The ROI starts at 102, so the block covering 100..104 is HALF outside it:
    a mean over the whole block would read 50 where the pixels are 100.
    """
    pixels = np.full((40, 40), 100.0, np.float32)
    roi = (102, 142, 102, 142)
    table = _table({"CD3": "tophat"}, {"CD3": _Array(roi, pixels)})

    # Level-2 coordinates 24..28 cover level-0 96..112.
    values, valid = read_tile(table, "CD3", (24, 28, 24, 28), stride=4)

    edge = values[1, 1]          # the block at 100..104, half inside the ROI
    assert valid[1, 1]
    assert edge == pytest.approx(100.0), (
        f"the ROI edge was averaged with the emptiness outside it: {edge}")
    assert not valid[0, 0], "a block with no valid sample was drawn"


def test_a_block_with_no_valid_sample_is_not_drawn():
    mean, valid = box_downsample_valid(np.zeros((4, 4), np.float32),
                                       np.zeros((4, 4), bool), 4)
    assert mean.shape == (1, 1) and not valid.any()


# ── 4. identity: what makes a Step1 tile's meaning ────────────────────

def test_the_token_moves_when_a_decision_does():
    """`original` -> `tophat` is different pixels under the same path."""
    array = _Array(ROI)
    before = _table({"CD3": "original"}, {"CD3": array}).identity_token()
    after = _table({"CD3": "tophat"}, {"CD3": array}).identity_token()
    assert before != after


def test_the_token_moves_when_the_product_is_regenerated():
    old = _Array(ROI)
    old.attrs = dict(old.attrs, written_at="2026-09-16T10:00:00")
    new = _Array(ROI)
    new.attrs = dict(new.attrs, written_at="2026-09-17T10:00:00")

    assert (_table({"CD3": "tophat"}, {"CD3": old}).identity_token()
            != _table({"CD3": "tophat"}, {"CD3": new}).identity_token())


def test_the_token_moves_when_the_region_does():
    array = _Array(ROI)
    a = _table({"CD3": "tophat"}, {"CD3": array}, roi_bbox=ROI)
    b = _table({"CD3": "tophat"}, {"CD3": array}, roi_bbox=(0, 300, 0, 300))
    assert a.identity_token() != b.identity_token()


def test_the_token_moves_when_the_handoff_is_republished():
    array = _Array(ROI)

    def _open(_path, channel, _roi):
        return array

    a = Step1SourceTable({"CD3": "tophat"}, "/tmp/c.zarr", "ROI_1", _open,
                         roi_bbox=ROI, handoff_revision="rev-1")
    b = Step1SourceTable({"CD3": "tophat"}, "/tmp/c.zarr", "ROI_1", _open,
                         roi_bbox=ROI, handoff_revision="rev-2")
    assert a.identity_token() != b.identity_token()


def test_the_token_stands_still_when_nothing_moved():
    array = _Array(ROI)
    a = _table({"CD3": "tophat"}, {"CD3": array})
    b = _table({"CD3": "tophat"}, {"CD3": array})
    assert a.identity_token() == b.identity_token()


# ── 5. bounded memory ─────────────────────────────────────────────────

class _HugeProduct:
    """A whole-slide-sized corrected product that REFUSES a large read.

    The overview asks for the whole rectangle; an implementation that
    materialises it would ask this array for billions of pixels at once.
    """

    LIMIT = 4 * 1024 * 1024

    def __init__(self, bbox, value=42.0):
        y0, y1, x0, x1 = bbox
        self.attrs = {"roi_bbox_fullres": list(bbox)}
        self.shape = (y1 - y0, x1 - x0)
        self.dtype = np.float32
        self._value = value
        self.largest_read = 0
        self.reads = 0

    def __getitem__(self, key):
        ys, xs = key
        height = ys.stop - ys.start
        width = xs.stop - xs.start
        size = height * width
        self.largest_read = max(self.largest_read, size)
        self.reads += 1
        if size > self.LIMIT:
            raise AssertionError(
                f"a single read of {size} pixels: the reduction is not bounded")
        return np.full((height, width), self._value, np.float32)


def test_an_overview_of_a_whole_slide_product_stays_within_its_budget():
    bbox = (0, 60000, 0, 40000)          # 2.4e9 level-0 pixels
    table = _table({"CD3": "tophat"}, {"CD3": _HugeProduct(bbox)},
                   roi_bbox=bbox)

    # One overview tile at stride 256: 234 x 156 output pixels.
    values, valid = read_tile(table, "CD3", (0, 234, 0, 157), stride=256)

    assert valid.any()
    assert np.allclose(values[valid], 42.0)


def test_the_bound_is_honoured_for_a_coarse_tile_too():
    bbox = (0, 60000, 0, 40000)
    product = _HugeProduct(bbox)
    table = _table({"CD3": "tophat"}, {"CD3": product}, roi_bbox=bbox)

    read_tile(table, "CD3", (0, 512, 0, 512), stride=16)

    assert product.largest_read <= _HugeProduct.LIMIT


def test_the_whole_slide_product_is_never_taken_in_one_read():
    """The bound has to BITE: one read of everything would also pass a
    `largest_read <= LIMIT` check if the walk had quietly stopped walking."""
    bbox = (0, 60000, 0, 40000)
    product = _HugeProduct(bbox)
    table = _table({"CD3": "tophat"}, {"CD3": product}, roi_bbox=bbox)

    read_tile(table, "CD3", (0, 234, 0, 157), stride=256)

    assert product.reads > 1, "the overview was read in a single slab"
    assert product.largest_read <= _HugeProduct.LIMIT


@pytest.mark.parametrize("stride", [1, 2, 4, 16, 64, 256])
def test_a_slab_still_fits_the_budget_after_the_padding_it_may_need(stride):
    """The read is not the only thing that has to stay inside the budget.

    A slab is padded out to whole blocks of the global grid before it is
    summed, which can add up to one block on each axis. `_HugeProduct` can
    only see READS, so this is the arithmetic that protects the temporary --
    the thing no product-side gate can observe.
    """
    rows, band = _slab_geometry(stride, MAX_REDUCTION_ELEMENTS)

    assert rows % stride == 0 and band % stride == 0
    assert rows * band <= MAX_REDUCTION_ELEMENTS
    assert (rows + stride) * (band + stride) <= MAX_REDUCTION_ELEMENTS


def test_the_reduction_budget_has_not_been_raised():
    """Speed may not be bought with memory (G3.2b.4A)."""
    assert MAX_REDUCTION_ELEMENTS == 4 * 1024 * 1024


def test_a_fine_corrected_tile_is_copied_rather_than_reduced():
    """`stride == 1` is not a reduction: a block of one pixel is the pixel.

    The reduction walk would cut this rectangle into slabs smaller than the
    budget, so the read count is what tells the copy apart from the walk.
    """
    pixels = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
    bbox = (0, 64, 0, 64)
    product = _CountingArray(bbox, pixels)
    table = _table({"CD3": "cucim"}, {"CD3": product}, roi_bbox=bbox)

    values, valid = read_tile(table, "CD3", (0, 64, 0, 64), stride=1)

    assert values.shape == (64, 64) and valid.all()
    assert np.array_equal(values, pixels)
    assert product.reads == 1, (
        f"a fine tile took {product.reads} reads; it is being reduced")


class _CountingArray(_Array):
    """`_Array`, plus how many times its pixels were actually asked for."""

    def __init__(self, bbox, fill=None):
        super().__init__(bbox, fill)
        self.reads = 0

    def __getitem__(self, key):
        self.reads += 1
        return super().__getitem__(key)
