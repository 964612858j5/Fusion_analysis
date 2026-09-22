"""G3.2b.4A: the corrected reduction, against a reference that is obvious.

`viewer/step1_source.reduce_corrected` answers one question -- the mean of a
corrected product over a grid ANCHORED AT THE LEVEL-0 ORIGIN, taken over the
samples that really exist -- and it now answers it two ways: a copy when
`stride == 1`, and a reshape-and-sum over whole blocks when it is larger.

Both are checked here against `_reference`, a per-pixel loop written to be
read rather than to be fast. It is deliberately NOT the implementation with
different bookkeeping: it walks every level-0 pixel, decides for itself which
block the pixel belongs to, and divides by what it counted. An optimisation
that verifies itself proves nothing.

The contracts under test, in the order they are easy to break:

* the grid is anchored at the LEVEL-0 ORIGIN -- not at the tile, not at the
  slab the walk happened to cut. The same world block must read the same
  from every rectangle that contains it;
* a partial block at the region's edge is divided by the samples it ACTUALLY
  got, never by `stride**2`, or the edge goes dark;
* a sample whose value is 0 is a sample. Only a position outside the region
  is absent;
* how the walk is cut may not change the answer;
* no single read, and no temporary, exceeds `MAX_REDUCTION_ELEMENTS`.

No Qt in sections 1-4; section 5 uses the production provider and says so.
"""

import numpy as np
import pytest

from block01.viewer.step1_source import (
    MAX_REDUCTION_ELEMENTS, CorrectedRegion, Step1SourceTable,
    _slab_geometry, read_tile, reduce_corrected,
)


# ── the product, and what it was asked for ────────────────────────────

class _Product:
    """A corrected product that records every read it was asked to serve."""

    def __init__(self, bbox, pixels):
        y0, y1, x0, x1 = bbox
        self.attrs = {"roi_bbox_fullres": [y0, y1, x0, x1]}
        self._pixels = np.asarray(pixels, np.float32)
        self.shape = self._pixels.shape
        self.dtype = self._pixels.dtype
        self.reads = []

    def __getitem__(self, key):
        ys, xs = key
        self.reads.append(((ys.start, ys.stop), (xs.start, xs.stop)))
        return self._pixels[key]

    @property
    def largest_read(self):
        return max((abs(y1 - y0) * abs(x1 - x0)
                    for (y0, y1), (x0, x1) in self.reads), default=0)


def _region(bbox, pixels):
    product = _Product(bbox, pixels)
    return CorrectedRegion(product, bbox), product


def _reference(bbox, pixels, rect, stride):
    """Per-pixel, deliberately slow, deliberately obvious.

    A level-0 pixel at `(r, c)` belongs to block `(r // stride, c // stride)`
    of the GLOBAL grid; the rectangle's own first block is
    `(y0 // stride, x0 // stride)`, so that is what the output is offset by.
    A pixel outside the region is not a sample and is not counted.
    """
    y0, y1, x0, x1 = rect
    stride = max(1, int(stride))
    py, px = y0 % stride, x0 % stride
    out_h = -(-(py + max(0, y1 - y0)) // stride)
    out_w = -(-(px + max(0, x1 - x0)) // stride)
    sums = np.zeros((out_h, out_w), np.float64)
    counts = np.zeros((out_h, out_w), np.int64)
    by0, by1, bx0, bx1 = bbox
    for row in range(y0, y1):
        if not by0 <= row < by1:
            continue
        for col in range(x0, x1):
            if not bx0 <= col < bx1:
                continue
            i = row // stride - y0 // stride
            j = col // stride - x0 // stride
            sums[i, j] += float(pixels[row - by0, col - bx0])
            counts[i, j] += 1
    valid = counts > 0
    mean = np.where(valid, sums / np.maximum(counts, 1), 0.0).astype(np.float32)
    return mean, valid


def _assert_matches(bbox, pixels, rect, stride, **kwargs):
    region, _product = _region(bbox, pixels)
    got, got_valid = reduce_corrected(region, rect, stride, **kwargs)
    want, want_valid = _reference(bbox, pixels, rect, stride)
    assert got.shape == want.shape, f"shape {got.shape} != {want.shape}"
    assert got.dtype == np.float32 and got_valid.dtype == np.bool_
    assert (got_valid == want_valid).all(), "the valid mask moved"
    assert np.allclose(got[want_valid], want[want_valid],
                       rtol=1e-6, atol=1e-5), (
        "values differ by "
        f"{np.abs(got[want_valid] - want[want_valid]).max() if want_valid.any() else 0}")
    assert not got[~want_valid].any(), "an invalid block was given a value"
    return got, got_valid


#: A product with every awkward value in it: negatives, an exact zero that is
#: a real measurement, fractions that do not round nicely.
def _awkward(height, width, seed=11):
    rng = np.random.default_rng(seed)
    pixels = (rng.standard_normal((height, width)) * 12.5).astype(np.float32)
    pixels[0, 0] = 0.0
    pixels[height // 2, width // 3] = 0.0          # valid-dark, in the middle
    pixels[1, 2] = -47.75
    pixels[2, 1] = 1.0 / 3.0
    return pixels


# ── 1. stride == 1 is a copy, not a reduction ─────────────────────────

ROI1 = (100, 164, 200, 268)                        # unaligned on purpose


def test_a_stride_one_tile_inside_the_region_is_the_pixels_themselves():
    pixels = _awkward(64, 68)
    region, product = _region(ROI1, pixels)
    values, valid = reduce_corrected(region, (110, 142, 210, 250), 1)

    assert values.shape == (32, 40) and valid.all()
    assert values.dtype == np.float32 and valid.dtype == np.bool_
    assert np.array_equal(values, pixels[10:42, 10:50])
    assert len(product.reads) == 1, "a plain tile took more than one read"


def test_a_stride_one_tile_outside_the_region_reads_nothing_at_all():
    pixels = _awkward(64, 68)
    region, product = _region(ROI1, pixels)
    values, valid = reduce_corrected(region, (0, 32, 0, 32), 1)

    assert values.shape == (32, 32)
    assert not valid.any() and not values.any()
    assert product.reads == [], "a tile that misses the region still read it"


@pytest.mark.parametrize("rect,name", [
    ((80, 120, 210, 250), "top"),
    ((140, 180, 210, 250), "bottom"),
    ((110, 150, 180, 220), "left"),
    ((110, 150, 250, 290), "right"),
    ((80, 120, 180, 220), "top-left"),
    ((80, 120, 250, 290), "top-right"),
    ((140, 180, 180, 220), "bottom-left"),
    ((140, 180, 250, 290), "bottom-right"),
])
def test_a_stride_one_tile_across_the_boundary_draws_the_inside_only(rect, name):
    pixels = _awkward(64, 68)
    values, valid = _assert_matches(ROI1, pixels, rect, 1)

    assert valid.any(), f"{name}: nothing was drawn"
    assert not valid.all(), f"{name}: the outside was drawn too"


def test_a_stride_one_zero_is_a_sample_and_not_an_absence():
    pixels = np.zeros((16, 16), np.float32)
    bbox = (0, 16, 0, 16)
    values, valid = reduce_corrected(*_region(bbox, pixels)[:1],
                                     rect=(0, 16, 0, 16), stride=1)

    assert valid.all(), "an all-zero product was treated as absent"
    assert not values.any()


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_stride_one_tile_passes_nan_and_inf_straight_through(bad):
    pixels = np.ones((16, 16), np.float32)
    pixels[3, 4] = bad
    region, _product = _region((0, 16, 0, 16), pixels)
    values, valid = reduce_corrected(region, (0, 16, 0, 16), 1)

    assert valid.all(), "a non-finite sample was declared absent"
    assert np.array_equal(values, pixels, equal_nan=True)


class _ShortRegion:
    """A region that hands back LESS than the overlap it was asked for.

    Not a product this repository writes -- a defensive case. The reduction
    must place what it was given at the coordinates it was given, and leave
    the rest absent, rather than trusting the rectangle it asked for.
    """

    def __init__(self, bbox, pixels, shrink=3):
        self.bbox = bbox
        self._pixels = np.asarray(pixels, np.float32)
        self._shrink = shrink

    def overlaps(self, y0, y1, x0, x1):
        by0, by1, bx0, bx1 = self.bbox
        return not (y1 <= by0 or y0 >= by1 or x1 <= bx0 or x0 >= bx1)

    def read(self, y0, y1, x0, x1):
        by0, by1, bx0, bx1 = self.bbox
        oy0, oy1 = max(y0, by0), min(y1, by1)
        ox0, ox1 = max(x0, bx0), min(x1, bx1)
        oy1 = max(oy0, oy1 - self._shrink)
        ox1 = max(ox0, ox1 - self._shrink)
        if oy1 <= oy0 or ox1 <= ox0:
            return None, None
        return (self._pixels[oy0 - by0:oy1 - by0, ox0 - bx0:ox1 - bx0],
                (oy0, oy1, ox0, ox1))


def test_a_stride_one_tile_places_exactly_what_the_region_returned():
    pixels = _awkward(32, 32)
    region = _ShortRegion((0, 32, 0, 32), pixels, shrink=3)
    values, valid = reduce_corrected(region, (0, 32, 0, 32), 1)

    assert values.shape == (32, 32)
    assert valid[:29, :29].all(), "the pixels that arrived were not placed"
    assert not valid[29:, :].any() and not valid[:, 29:].any(), (
        "the reduction invented pixels the region never returned")
    assert np.array_equal(values[:29, :29], pixels[:29, :29])


def test_a_stride_one_rectangle_over_the_budget_still_stays_bounded():
    """The copy is only taken when it fits; otherwise the walk does it."""
    pixels = _awkward(64, 64)
    region, product = _region((0, 64, 0, 64), pixels)
    values, valid = reduce_corrected(region, (0, 64, 0, 64), 1,
                                     max_elements=1024)

    assert valid.all()
    assert np.array_equal(values, pixels), "the bounded walk changed stride 1"
    assert product.largest_read <= 1024, "a read broke the budget"
    assert len(product.reads) > 1, "nothing was actually split"


def test_a_stride_one_tile_is_read_once_rather_than_walked():
    """The fast path's own signature: ONE read for a tile that fits.

    The bounded walk would cut the same rectangle into a grid of slabs
    smaller than the budget, so the read count tells the two apart.
    """
    pixels = _awkward(64, 64)
    region, product = _region((0, 64, 0, 64), pixels)
    reduce_corrected(region, (0, 64, 0, 64), 1, max_elements=64 * 64)

    assert len(product.reads) == 1, (
        f"stride 1 took {len(product.reads)} reads; the reduction path is "
        "being used where a copy was enough")


# ── 2. stride > 1: the numbers, against the obvious reference ─────────

@pytest.mark.parametrize("stride", [2, 3, 4, 7, 16, 64])
@pytest.mark.parametrize("roi_origin", [(0, 0), (5, 3), (64, 64), (101, 97)])
def test_the_reduction_matches_the_reference_everywhere(stride, roi_origin):
    """Aligned and unaligned ROI, aligned and unaligned rectangles, edges."""
    ry, rx = roi_origin
    height, width = 71, 83
    bbox = (ry, ry + height, rx, rx + width)
    pixels = _awkward(height, width)
    starts_y = [ry - 9, ry, ry + 1, ry + stride, ry + height - 3, ry + height + 5]
    starts_x = [rx - 9, rx, rx + 2, rx + stride, rx + width - 1, rx + width + 7]
    sizes = [(1, 1), (stride, stride), (stride + 1, max(1, stride - 1)),
             (2 * stride + 3, 3 * stride + 2), (40, 46)]
    for y0 in starts_y:
        for x0 in starts_x:
            for dh, dw in sizes:
                _assert_matches(bbox, pixels, (y0, y0 + dh, x0, x0 + dw),
                                stride)


@pytest.mark.parametrize("stride", [3, 7, 16])
def test_a_rectangle_smaller_than_one_block_is_still_right(stride):
    bbox = (10, 60, 20, 70)
    pixels = _awkward(50, 50)
    for offset in range(stride):
        _assert_matches(bbox, pixels,
                        (20 + offset, 21 + offset, 30 + offset, 31 + offset),
                        stride)


@pytest.mark.parametrize("stride", [4, 7, 64])
def test_the_region_edge_is_divided_by_the_samples_it_really_got(stride):
    """A partial block must not be dimmed by the emptiness outside."""
    value = 100.0
    ry, rx = stride + 2, stride + 3            # region starts off the grid
    pixels = np.full((4 * stride, 4 * stride), value, np.float32)
    bbox = (ry, ry + 4 * stride, rx, rx + 4 * stride)
    rect = (0, ry + 5 * stride, 0, rx + 5 * stride)
    values, valid = _assert_matches(bbox, pixels, rect, stride)

    assert np.allclose(values[valid], value), (
        "a partial block was divided by stride**2 instead of its own samples")


def test_a_zero_inside_the_region_keeps_its_block_valid():
    stride = 4
    pixels = np.zeros((16, 16), np.float32)
    bbox = (8, 24, 8, 24)
    values, valid = _assert_matches(bbox, pixels, (8, 24, 8, 24), stride)

    assert valid.all(), "an all-zero corrected block was dropped as absent"
    assert not values.any()


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_a_non_finite_sample_propagates_into_its_block(bad):
    stride = 4
    pixels = np.ones((16, 16), np.float32)
    pixels[5, 6] = bad
    region, _product = _region((0, 16, 0, 16), pixels)
    values, valid = reduce_corrected(region, (0, 16, 0, 16), stride)

    assert valid.all()
    assert not np.isfinite(values[1, 1]), "the block swallowed a non-finite"
    assert np.allclose(values[0, 0], 1.0)


@pytest.mark.parametrize("stride", [3, 7, 16, 64])
def test_how_the_walk_is_cut_does_not_change_the_answer(stride):
    bbox = (13, 213, 29, 249)
    pixels = _awkward(200, 220, seed=3)
    rect = (5, 235, 11, 251)
    first = None
    for budget in (64, 1024, 9999, 250000, MAX_REDUCTION_ELEMENTS):
        region, _product = _region(bbox, pixels)
        values, valid = reduce_corrected(region, rect, stride,
                                         max_elements=budget)
        if first is None:
            first = (values, valid)
            continue
        assert (valid == first[1]).all(), f"budget {budget} moved the mask"
        assert np.allclose(values[valid], first[0][valid],
                           rtol=1e-6, atol=1e-5), f"budget {budget} moved values"


@pytest.mark.parametrize("stride", [4, 7, 16])
def test_a_world_block_reads_the_same_from_every_tile_that_covers_it(stride):
    """The grid is the level-0 origin's, so the seams have to agree."""
    height = width = 12 * stride
    ry, rx = 2 * stride + 1, 3 * stride + 2         # region off the grid
    bbox = (ry, ry + height, rx, rx + width)
    pixels = _awkward(height, width, seed=5)

    # A block that is whole inside the region, well away from every edge.
    block_y = (ry + 4 * stride) // stride
    block_x = (rx + 4 * stride) // stride
    expected = pixels[block_y * stride - ry:(block_y + 1) * stride - ry,
                      block_x * stride - rx:(block_x + 1) * stride - rx]
    expected = float(np.asarray(expected, np.float64).mean())

    seen = []
    for dy in range(stride):
        for dx in (0, 1, stride - 1):
            y0 = block_y * stride - 2 * stride - dy
            x0 = block_x * stride - 2 * stride - dx
            region, _product = _region(bbox, pixels)
            values, valid = reduce_corrected(
                region, (y0, y0 + 5 * stride, x0, x0 + 5 * stride), stride)
            i = block_y - y0 // stride
            j = block_x - x0 // stride
            assert valid[i, j]
            seen.append(float(values[i, j]))

    assert max(seen) - min(seen) <= 1e-4, (
        "the same world block read differently from different rectangles: "
        f"{min(seen)} .. {max(seen)}")
    assert seen[0] == pytest.approx(expected, rel=1e-6, abs=1e-5)


# ── 3. bounded memory ─────────────────────────────────────────────────

class _HugeProduct:
    """A whole-slide-sized product that REFUSES an oversized read."""

    LIMIT = MAX_REDUCTION_ELEMENTS

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
        size = (ys.stop - ys.start) * (xs.stop - xs.start)
        self.largest_read = max(self.largest_read, size)
        self.reads += 1
        if size > self.LIMIT:
            raise AssertionError(
                f"a single read of {size} pixels: the reduction is not bounded")
        # Never materialise the whole slide: only this slab exists.
        return np.full((ys.stop - ys.start, xs.stop - xs.start),
                       self._value, np.float32)


HUGE = (0, 60000, 0, 40000)                        # 2.4e9 level-0 pixels


@pytest.mark.parametrize("stride", [16, 64, 256])
def test_a_whole_slide_reduction_never_reads_more_than_the_budget(stride):
    product = _HugeProduct(HUGE)
    region = CorrectedRegion(product, HUGE)
    out_h = -(-HUGE[1] // stride)
    out_w = -(-HUGE[3] // stride)

    values, valid = reduce_corrected(region, HUGE, stride)

    assert values.shape == (out_h, out_w)
    assert valid.all()
    assert np.allclose(values, 42.0)
    assert product.largest_read <= MAX_REDUCTION_ELEMENTS
    assert product.reads > 1, "the whole slide was taken in one read"


def test_an_ordinary_coarse_tile_of_a_whole_slide_product_is_bounded_too():
    product = _HugeProduct(HUGE)
    region = CorrectedRegion(product, HUGE)

    reduce_corrected(region, (0, 512 * 16, 0, 512 * 16), 16)

    assert product.largest_read <= MAX_REDUCTION_ELEMENTS


@pytest.mark.parametrize("stride", [1, 2, 3, 4, 7, 16, 64, 256])
def test_the_slab_geometry_leaves_room_for_the_padding_it_may_need(stride):
    """The bound that protects the TEMPORARY, not just the read.

    A slab is padded out to whole blocks before it is reshaped, which can add
    up to one block on each axis. If the geometry spent the whole budget on
    the read, the padded array -- not the read -- would be what broke it, and
    no product-side gate would ever see it. This is that arithmetic, checked
    directly.
    """
    rows, band = _slab_geometry(stride, MAX_REDUCTION_ELEMENTS)

    assert rows % stride == 0 and band % stride == 0, (
        "a slab that is not a whole number of blocks puts every slab off the "
        "global grid, not just the first")
    assert rows >= stride and band >= stride
    assert rows * band <= MAX_REDUCTION_ELEMENTS, "the read itself is over budget"
    assert (rows + stride) * (band + stride) <= MAX_REDUCTION_ELEMENTS, (
        f"stride {stride}: a padded slab may reach "
        f"{(rows + stride) * (band + stride)} elements, over "
        f"{MAX_REDUCTION_ELEMENTS}")


def test_the_budget_constant_itself_has_not_been_raised():
    assert MAX_REDUCTION_ELEMENTS == 4 * 1024 * 1024


# ── 4. the rule around it is unchanged ────────────────────────────────

def _table(decisions, arrays, roi_bbox):
    def _open(_path, channel, _roi):
        return arrays.get(channel)

    return Step1SourceTable(decisions=decisions,
                            corrected_zarr_path="/tmp/corrected.zarr",
                            roi_name="ROI_1", open_corrected=_open,
                            roi_bbox=roi_bbox)


def test_read_tile_uses_the_copy_for_fine_and_the_reduction_for_coarse():
    bbox = (0, 128, 0, 128)
    pixels = _awkward(128, 128)
    product = _Product(bbox, pixels)
    table = _table({"CD3": "cucim"}, {"CD3": product}, bbox)

    fine, fine_valid = read_tile(table, "CD3", (0, 64, 0, 64), stride=1)
    reads_after_fine = len(product.reads)
    coarse, coarse_valid = read_tile(table, "CD3", (0, 16, 0, 16), stride=4)

    assert fine.shape == (64, 64) and fine_valid.all()
    assert np.array_equal(fine, pixels[:64, :64])
    assert reads_after_fine == 1, "the fine tile was reduced instead of copied"
    assert coarse.shape == (16, 16) and coarse_valid.all()
    want = pixels[:64, :64].astype(np.float64).reshape(16, 4, 16, 4).mean(axis=(1, 3))
    assert np.allclose(coarse, want, rtol=1e-6, atol=1e-5)


def test_a_raw_channel_is_untouched_by_any_of_this():
    calls = []

    def read_raw(channel, rect):
        calls.append((channel, tuple(rect)))
        y0, y1, x0, x1 = rect
        return np.full((y1 - y0, x1 - x0), 7.0, np.float32)

    table = _table({"CD3": "original"}, {}, (0, 128, 0, 128))
    values, valid = read_tile(table, "CD3", (0, 32, 0, 32), stride=4,
                              read_raw=read_raw)

    assert calls == [("CD3", (0, 32, 0, 32))], "the raw reader was re-routed"
    assert values.shape == (32, 32) and valid.all()
    assert np.allclose(values, 7.0)


def test_a_refused_channel_still_yields_no_tile():
    table = _table({"CD3": "cucim"}, {}, (0, 128, 0, 128))

    values, valid = read_tile(table, "CD3", (0, 16, 0, 16), stride=4)

    assert values is None and valid is None
    assert [m.channel for m in table.missing()] == ["CD3"]


def test_the_identity_token_is_unaffected_by_the_reduction_change():
    bbox = (0, 128, 0, 128)
    pixels = _awkward(128, 128)
    table = _table({"CD3": "cucim"}, {"CD3": _Product(bbox, pixels)}, bbox)

    before = table.identity_token()
    read_tile(table, "CD3", (0, 16, 0, 16), stride=4)
    read_tile(table, "CD3", (0, 64, 0, 64), stride=1)

    assert table.identity_token() == before
