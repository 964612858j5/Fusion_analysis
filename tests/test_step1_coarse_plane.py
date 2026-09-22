"""The persisted coarse plane answers exactly what the reduction answered.

G3.2b.4E, read side. A corrected product has no pyramid, so its coarsest
level was reduced from level 0 on every first tick -- 9-11.6 s on a real
59040x35520 product, measured. The plane is that same result written once
beside the product; these gates say it is the SAME result and that anything
doubtful is refused in favour of the reduction that was always there.

What is deliberately NOT asserted here: speed. A test that timed a read
would pass on a fast day. What is asserted is that no level-0 pixel is
touched when the plane answers, which is the thing that made it slow.
"""

import numpy as np
import pytest

from block01.viewer import step1_source as sources

TILE = 512


class _Array:
    """A zarr-shaped array that counts how much of it is read."""

    def __init__(self, data, attrs):
        self._data = np.asarray(data)
        self.attrs = dict(attrs)
        self.reads = 0
        self.read_pixels = 0

    @property
    def shape(self):
        return self._data.shape

    @property
    def dtype(self):
        return self._data.dtype

    def __getitem__(self, item):
        out = self._data[item]
        self.reads += 1
        self.read_pixels += int(np.asarray(out).size)
        return out


class _Store:
    """The opener's answer: product arrays here, plane arrays there."""

    def __init__(self):
        self.product = {}
        self.plane = {}

    def opener(self, path, channel, roi_name=None):
        table = self.plane if path.endswith(sources.COARSE_SIDECAR_DIRNAME) \
            else self.product
        return table.get(channel)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A product, its plane, and a table that reads both through the opener."""
    monkeypatch.setattr("os.path.isdir", lambda path: True, raising=True)
    return _Store()


def _product_attrs(bbox, shape, method="cucim", param=50, algo="7",
                   token="write-1"):
    """`source_identity` is WHICH WRITE produced these pixels. Step0 mints a
    new one every time it recomputes a channel, and a plane must carry the
    same one -- without it the static fields below would happily match a
    plane from an earlier write of the same settings."""
    return {"channel_name": "CD22", "correction_method": method,
            "correction_param_name": "cucim_sigma",
            "correction_param_value": int(param),
            "bg_correction_algo_version": algo,
            "roi_name": "ROI_1",
            "roi_bbox_fullres": [int(v) for v in bbox],
            "source_shape": [int(shape[0]), int(shape[1])],
            "source_identity": token}


def _plane_attrs(product_attrs, stride, origin, overrides=None):
    """The stamp a written plane carries. `overrides` corrupts one field of
    it -- it is applied LAST, so it can rewrite `stride` itself."""
    attrs = sources.coarse_plane_identity(product_attrs, stride, level=3)
    attrs["block_origin"] = [int(origin[0]), int(origin[1])]
    attrs["complete"] = True
    attrs.update(dict(overrides or {}))
    return attrs


def _build(store, bbox=(31328, 31328 + 4096, 3104, 3104 + 3072), stride=64,
           seed=3, plane_overrides=None):
    """`plane_overrides` is a dict ON PURPOSE: as `**kwargs` an override
    named `stride` would have rebuilt the whole rig at that stride instead
    of corrupting the plane's stamp, and the gate would have passed for the
    wrong reason (it did, once)."""
    """A product with pixels, and the plane those pixels reduce to."""
    y0, y1, x0, x1 = bbox
    rng = np.random.default_rng(seed)
    data = (rng.random((y1 - y0, x1 - x0), dtype=np.float32) * 500.0)
    data[:37, :] = 0.0                       # the polygon mask's own zeros
    product_attrs = _product_attrs(bbox, data.shape)
    store.product["CD22"] = _Array(data, product_attrs)

    region = sources.CorrectedRegion(store.product["CD22"], bbox)
    (by0, bx0), (bh, bw) = sources.coarse_block_range(bbox, stride)
    plane = np.zeros((bh, bw), np.float32)
    for row in range(bh):
        rect = ((by0 + row) * stride, (by0 + row + 1) * stride,
                bx0 * stride, (bx0 + bw) * stride)
        values, _valid = sources.reduce_corrected(region, rect, stride)
        plane[row, :] = values[0, :]
    store.plane["CD22"] = _Array(
        plane, _plane_attrs(product_attrs, stride, (by0, bx0),
                            overrides=plane_overrides))
    return bbox, stride, (by0, bx0)


def _table(store, bbox, path="/tmp/g324e/step0/corrected_channels.zarr"):
    return sources.Step1SourceTable(
        decisions={"CD22": "cucim"}, corrected_zarr_path=path,
        roi_name="ROI_1", roi_bbox=list(bbox), handoff_revision="r1",
        open_corrected=store.opener)


def _tiles(bbox, stride, limit=None):
    """Level-k rectangles covering the region, partial edges included.

    Asserted non-empty: several gates below compare inside a loop over this,
    and an empty list would make them pass without comparing anything.
    """
    y0, y1, x0, x1 = bbox
    (by0, bx0), (bh, bw) = sources.coarse_block_range(bbox, stride)
    out = []
    for ty in range((by0 + bh + TILE - 1) // TILE):
        for tx in range((bx0 + bw + TILE - 1) // TILE):
            out.append((ty * TILE, ty * TILE + TILE,
                        tx * TILE, tx * TILE + TILE))
    assert out, "no tiles cover this region"
    return out[:limit] if limit else out


# ── the plane answers, and answers the same thing ────────────────────

def test_the_plane_is_used_and_is_bit_identical_to_the_reduction(rig):
    bbox, stride, _origin = _build(rig)
    table = _table(rig, bbox)
    region = table.region("CD22")

    assert table.coarse_plane("CD22", stride) is not None
    for rect in _tiles(bbox, stride):
        values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
        level0 = tuple(v * stride for v in rect)
        want, want_valid = sources.reduce_corrected(region, level0, stride)
        assert np.array_equal(valid, want_valid)
        assert np.array_equal(values[valid], want[want_valid])


def test_answering_from_the_plane_reads_no_level_zero_pixel(rig):
    bbox, stride, _origin = _build(rig)
    table = _table(rig, bbox)
    table.region("CD22")                      # resolve before counting
    product = rig.product["CD22"]
    table.coarse_plane("CD22", stride)
    before = product.read_pixels

    for rect in _tiles(bbox, stride):
        sources.read_tile(table, "CD22", rect, stride=stride)

    assert product.read_pixels == before, (
        "the plane answered but level 0 was read anyway")
    assert rig.plane["CD22"].reads > 0


def test_a_tile_north_west_of_the_region_is_empty_and_invalid(rig):
    bbox, stride, origin = _build(rig)
    table = _table(rig, bbox)
    # ABOVE the region's first block, not tile (0,0): this ROI starts at
    # block 489, which tile (0,0) does contain.
    values, valid = sources.read_tile(
        table, "CD22", (0, origin[0] - 1, 0, origin[1] + 4), stride=stride)
    assert not valid.any()
    assert not values.any()


def test_the_partial_right_and_bottom_tiles_match(rig):
    """The clipped rectangles `_level_rect` produces at the slide's edge."""
    bbox, stride, _origin = _build(rig)
    table = _table(rig, bbox)
    region = table.region("CD22")
    (by0, bx0), (bh, bw) = sources.coarse_block_range(bbox, stride)
    rect = (by0 + bh - 3, by0 + bh + 5, bx0 + bw - 7, bx0 + bw + 2)
    values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
    want, want_valid = sources.reduce_corrected(
        region, tuple(v * stride for v in rect), stride)
    assert np.array_equal(valid, want_valid)
    assert np.array_equal(values[valid], want[want_valid])


@pytest.mark.parametrize("bbox", [
    (31328, 31328 + 4096, 3104, 3104 + 3072),      # unaligned origin
    (0, 4096, 0, 3072),                            # aligned origin
    (31330, 31330 + 4095, 3105, 3105 + 3070),      # nothing divides
])
def test_every_roi_phase_matches(rig, bbox):
    bbox, stride, _origin = _build(rig, bbox=bbox)
    table = _table(rig, bbox)
    region = table.region("CD22")
    for rect in _tiles(bbox, stride):
        values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
        want, want_valid = sources.reduce_corrected(
            region, tuple(v * stride for v in rect), stride)
        assert np.array_equal(valid, want_valid)
        assert np.array_equal(values[valid], want[want_valid])


# ── and refuses itself whenever anything does not line up ────────────

@pytest.mark.parametrize("overrides,reason", [
    ({"source_identity": "write-2"}, "another write of the same settings"),
    ({"correction_method": "tophat"}, "another method"),
    ({"correction_param_value": 30}, "another parameter"),
    ({"bg_correction_algo_version": "6"}, "an older correction algorithm"),
    ({"roi_bbox_fullres": [0, 4096, 0, 3072]}, "another ROI"),
    ({"source_shape": [4096, 3071]}, "a reshaped product"),
    ({"stride": 16}, "another level"),
    ({"format_version": 0}, "an older plane format"),
    ({"channel_name": "CD163"}, "another channel"),
])
def test_a_plane_that_does_not_match_its_product_is_not_used(rig, overrides,
                                                             reason):
    bbox, stride, _origin = _build(rig, plane_overrides=overrides)
    table = _table(rig, bbox)
    assert table.coarse_plane("CD22", stride) is None, reason
    # and the tile is still produced, by the reduction
    region = table.region("CD22")
    rect = _tiles(bbox, stride)[0]
    values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
    want, want_valid = sources.reduce_corrected(
        region, tuple(v * stride for v in rect), stride)
    assert np.array_equal(valid, want_valid)
    assert np.array_equal(values[valid], want[want_valid])


def test_a_plane_without_its_completion_mark_is_not_used(rig):
    bbox, stride, _origin = _build(rig, plane_overrides={"complete": False})
    assert _table(rig, bbox).coarse_plane("CD22", stride) is None


def test_a_plane_whose_shape_is_not_the_regions_block_range_is_not_used(rig):
    bbox, stride, origin = _build(rig)
    plane = rig.plane["CD22"]
    rig.plane["CD22"] = _Array(np.zeros((plane.shape[0] - 1, plane.shape[1]),
                                        np.float32), plane.attrs)
    assert _table(rig, bbox).coarse_plane("CD22", stride) is None


def test_a_plane_placed_at_the_wrong_block_origin_is_not_used(rig):
    bbox, stride, origin = _build(rig)
    rig.plane["CD22"].attrs["block_origin"] = [origin[0] + 1, origin[1]]
    assert _table(rig, bbox).coarse_plane("CD22", stride) is None


def test_a_product_with_no_write_token_is_never_answered_from_a_plane(rig):
    """An old product cannot say which write it is, so no plane may speak
    for it -- it reduces at runtime, exactly as it did before.

    The token is removed from BOTH sides on purpose. Removing it only from
    the product leaves the two stamps unequal, and the plane would then be
    refused by the ordinary identity comparison -- the gate would pass
    without the rule it exists for ever running (a mutation that deleted
    the rule kept it green until this was fixed).
    """
    bbox, stride, _origin = _build(rig, plane_overrides={"source_identity": ""})
    attrs = dict(rig.product["CD22"].attrs)
    attrs["source_identity"] = ""
    rig.product["CD22"].attrs = attrs
    assert _table(rig, bbox).coarse_plane("CD22", stride) is None


def test_stride_one_never_uses_a_plane(rig):
    bbox, _stride, _origin = _build(rig)
    assert _table(rig, bbox).coarse_plane("CD22", 1) is None


def test_a_plane_that_fails_to_read_falls_back_to_the_reduction(rig):
    bbox, stride, _origin = _build(rig)
    table = _table(rig, bbox)
    region = table.region("CD22")
    plane = table.coarse_plane("CD22", stride)
    assert plane is not None

    class _Broken:
        shape = plane.shape
        attrs = {}

        def __getitem__(self, item):
            raise OSError("the plane's chunk is gone")

    plane.array = _Broken()
    rect = _tiles(bbox, stride)[0]
    values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
    want, want_valid = sources.reduce_corrected(
        region, tuple(v * stride for v in rect), stride)
    assert np.array_equal(valid, want_valid)
    assert np.array_equal(values[valid], want[want_valid])


def test_a_product_with_no_sidecar_at_all_behaves_exactly_as_before(rig,
                                                                    monkeypatch):
    bbox, stride, _origin = _build(rig)
    monkeypatch.setattr("os.path.isdir", lambda path: False, raising=True)
    table = _table(rig, bbox)
    assert table.coarse_plane("CD22", stride) is None
    region = table.region("CD22")
    for rect in _tiles(bbox, stride):
        values, valid = sources.read_tile(table, "CD22", rect, stride=stride)
        want, want_valid = sources.reduce_corrected(
            region, tuple(v * stride for v in rect), stride)
        assert np.array_equal(valid, want_valid)
        assert np.array_equal(values[valid], want[want_valid])


def test_a_raw_channel_is_untouched_by_any_of_this(rig):
    bbox, stride, _origin = _build(rig)
    table = sources.Step1SourceTable(
        decisions={"CD22": "original"}, corrected_zarr_path="/tmp/x.zarr",
        roi_name="ROI_1", roi_bbox=list(bbox), handoff_revision="r1",
        open_corrected=rig.opener)
    assert table.coarse_plane("CD22", stride) is None
    assert table.source_of("CD22") == sources.SOURCE_RAW
