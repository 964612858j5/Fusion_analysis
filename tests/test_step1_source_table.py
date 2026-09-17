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
    SOURCE_CORRECTED, SOURCE_MISSING, SOURCE_RAW, MissingProduct,
    Step1SourceTable, box_downsample_valid, read_tile,
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


def test_a_coarse_raw_tile_across_the_boundary_averages_the_inside_only():
    pixels = np.full((200, 200), 100.0, np.float32)
    table = _table({"CD3": "original"}, roi_bbox=(102, 142, 102, 142))
    raw = _RawReader(pixels)

    values, valid = read_tile(table, "CD3", (96, 112, 96, 112), stride=4,
                              read_raw=raw)

    assert valid[1, 1]
    assert values[1, 1] == pytest.approx(100.0), (
        "the ROI edge was averaged with the emptiness outside it")
    assert not valid[0, 0]


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
    table, pixels = _corrected_table()
    values, valid = read_tile(table, "CD3", (100, 108, 100, 108), stride=4)

    assert values.shape == (2, 2) and valid.all()
    expected = pixels[0:8, 0:8].reshape(2, 4, 2, 4).mean(axis=(1, 3))
    assert np.allclose(values, expected)


def test_the_coarse_grid_is_anchored_at_the_level_0_origin():
    """A block may not move with the tile it is read in."""
    table, pixels = _corrected_table()
    whole, _ = read_tile(table, "CD3", (100, 116, 100, 116), stride=4)
    right, _ = read_tile(table, "CD3", (100, 116, 108, 116), stride=4)

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
    a, _ = read_tile(table, "CD3", (102, 134, 102, 134), stride=4)
    b, _ = read_tile(table, "CD3", (98, 130, 98, 130), stride=4)

    # Where that block sits in each tile's output: blocks start at the
    # multiple of 4 at or below the tile's own origin.
    ia = (108 - 100) // 4, (108 - 100) // 4
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

    values, valid = read_tile(table, "CD3", (96, 112, 96, 112), stride=4)

    edge = values[1, 1]          # the block at 100..104, half inside the ROI
    assert valid[1, 1]
    assert edge == pytest.approx(100.0), (
        f"the ROI edge was averaged with the emptiness outside it: {edge}")
    assert not valid[0, 0], "a block with no valid sample was drawn"


def test_a_block_with_no_valid_sample_is_not_drawn():
    mean, valid = box_downsample_valid(np.zeros((4, 4), np.float32),
                                       np.zeros((4, 4), bool), 4)
    assert mean.shape == (1, 1) and not valid.any()
