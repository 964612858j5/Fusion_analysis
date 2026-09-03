"""`OMETIFFLoader.read_region_lowres` reads a downsampled region from the
pyramid and returns exactly what `read_region(..., downsample=ds)` returns
in SHAPE, so callers' coordinate maths is unchanged, without decoding the
region at full resolution.

Why it exists: after every Save the main window reads the nucleus channel
over the whole ROI at full resolution on the GUI thread and keeps every
33rd pixel -- 15.6 s of frozen window on a 59040x35520 slide, caught by the
GUI watchdog. The same tile budget from the pyramid is milliseconds.
"""

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

from block01.core.io_loader import OMETIFFLoader  # noqa: E402


def _write_pyramid(path, data, levels=3):
    """A 2-channel OME-TIFF with `levels` pyramid levels (SubIFDs), each
    a 2x box-mean of the previous."""
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=levels - 1, tile=(64, 64),
                 metadata={"axes": "CYX",
                           "Channel": {"Name": ["DAPI", "CD3"]}})
        cur = data
        for _ in range(levels - 1):
            c, h, w = cur.shape
            cur = cur[:, :h // 2 * 2, :w // 2 * 2].reshape(c, h // 2, 2, w // 2, 2)
            cur = cur.mean(axis=(2, 4)).astype(data.dtype)
            tw.write(cur, subfiletype=1, tile=(64, 64))


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    rng = np.random.default_rng(0)
    h, w = 520, 776
    yy, xx = np.mgrid[0:h, 0:w]
    ramp = (yy * 40 + xx * 20).astype(np.float64)
    data = np.stack([ramp, ramp[::-1] * 0.5], axis=0)
    data += rng.normal(0, 3, size=data.shape)
    data = np.clip(data, 0, 65535).astype(np.uint16)
    path = tmp_path_factory.mktemp("lowres") / "pyr.ome.tif"
    _write_pyramid(path, data, levels=3)
    return str(path), data


@pytest.mark.parametrize("bbox", [(0, 520, 0, 776), (37, 411, 123, 700),
                                  (500, 520, 760, 776), (0, 7, 0, 5)])
@pytest.mark.parametrize("ds", [2, 3, 4, 5, 9, 33])
def test_same_shape_and_same_pixels_up_to_the_pyramids_resolution(slide, bbox, ds):
    path, _data = slide
    loader = OMETIFFLoader(path)
    y0, y1, x0, x1 = bbox

    slow = loader.read_region("DAPI", y0, y1, x0, x1, downsample=ds, normalize=False)
    fast = loader.read_region_lowres("DAPI", y0, y1, x0, x1, ds, normalize=False)

    assert fast.shape == slow.shape, (fast.shape, slow.shape)
    assert fast.dtype == np.float32
    # The pyramid's pixel is a 2x2 (or 4x4) mean of the base pixels around the
    # sampled one; on this ramp (40/row, 20/col) that is within one or two
    # base-level steps plus noise.
    if fast.size:
        assert np.median(np.abs(fast - slow)) <= 130.0


def test_ds_one_and_a_corrected_channel_take_the_plain_path(slide, tmp_path):
    path, _data = slide
    loader = OMETIFFLoader(path)

    a = loader.read_region_lowres("CD3", 10, 100, 10, 100, 1, normalize=False)
    b = loader.read_region("CD3", 10, 100, 10, 100, downsample=1, normalize=False)
    assert np.array_equal(a, b)

    # A channel served corrected (on-the-fly config) must not come from the
    # raw pyramid.
    loader.set_correction_config({"channel_decisions": {"CD3": "tophat"},
                                  "method_params": {"tophat_radius": 3}})
    assert loader._channel_is_corrected("CD3") is True
    assert loader._channel_is_corrected("DAPI") is False
    corrected = loader.read_region("CD3", 0, 200, 0, 200, downsample=4, normalize=False)
    via_lowres = loader.read_region_lowres("CD3", 0, 200, 0, 200, 4, normalize=False)
    assert np.array_equal(corrected, via_lowres)


def test_normalisation_matches_read_region(slide):
    path, _data = slide
    loader = OMETIFFLoader(path)
    fast = loader.read_region_lowres("DAPI", 0, 520, 0, 776, 4)
    slow = loader.read_region("DAPI", 0, 520, 0, 776, downsample=4)
    assert fast.dtype == slow.dtype          # `_norm` decides, same as before
    assert fast.shape == slow.shape
    assert 0.0 <= float(fast.min()) and float(fast.max()) <= 1.0


def test_a_tiff_without_a_pyramid_falls_back(tmp_path):
    data = (np.arange(128 * 96, dtype=np.uint16).reshape(1, 128, 96))
    path = str(tmp_path / "flat.ome.tif")
    tifffile.imwrite(path, data, ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI"]}})
    loader = OMETIFFLoader(path)

    fast = loader.read_region_lowres("DAPI", 5, 100, 3, 90, 7, normalize=False)
    slow = loader.read_region("DAPI", 5, 100, 3, 90, downsample=7, normalize=False)

    assert np.array_equal(fast, slow)


def test_the_pyramid_is_what_gets_read(slide, monkeypatch):
    """The point of the method: the base level is not decoded."""
    path, _data = slide
    loader = OMETIFFLoader(path)
    calls = []
    real = loader._read_roi_zarr

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(loader, "_read_roi_zarr", spy)
    loader.read_region_lowres("DAPI", 0, 520, 0, 776, 8, normalize=False)
    assert calls == []
