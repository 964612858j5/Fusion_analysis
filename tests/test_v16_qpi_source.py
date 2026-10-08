"""Block v16 QP: PhenoCycler Fusion QPTIFF through the existing readers.

A QPTIFF has no OME-XML: names come from each channel page's
``<Biomarker>`` and the pixel size from the TIFF resolution tags
(`core/qpi_metadata.py`); the pixels are read by the readers unchanged.
Synthetic files in a temporary directory only. The fixture is a genuine QPI
layout for tifffile's ``_series_qpi``: top-level IFDs, channel pages, an RGB
thumbnail, then reduced levels of exactly ``//2`` (the last one in strips),
an overview and a label.
"""

import os
import shutil

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

from block01.core import qpi_metadata as qm  # noqa: E402
from block01.core import project_identity as pid  # noqa: E402
from block01.core.io_loader import OMETIFFLoader  # noqa: E402
from block01.sources.ome_tiff import OmeTiffSource  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

NAMES = ["DAPI", "CD3", "CD8"]
FILTERS = ["DAPI", "ATTO 550", "Cy5"]
SHAPE = (301, 203)                       # odd sizes: clipped edge tiles
UM = 0.5068606698203042
RES = (4294967295, 217695)               # px per cm, as Fusion writes it


def _qpi_xml(biomarker, name, image_type):
    bm = "<Biomarker />" if biomarker is None else f"<Biomarker>{biomarker}</Biomarker>"
    return ('<?xml version="1.0" encoding="utf-16"?>\n'
            "<PerkinElmer-QPI-ImageDescription>\n"
            "  <DescriptionVersion>6</DescriptionVersion>\n"
            f"  <ImageType>{image_type}</ImageType>\n"
            + (f"  <Name>{name}</Name>\n" if name is not None else "")
            + f"  {bm}\n"
            "</PerkinElmer-QPI-ImageDescription>")


def write_qpi(path, data, biomarkers=NAMES, levels=3, rgb=False, filters=FILTERS):
    """A QPTIFF of `data` (C, Y, X), or (Y, X, 3) pages with `rgb`."""
    kw = dict(software="PerkinElmer-QPI", metadata=None, compression="lzw",
              resolution=(RES[0] / RES[1], RES[0] / RES[1]), resolutionunit="CENTIMETER")
    thumb = np.zeros((20, 14, 3), np.uint8)
    with tifffile.TiffWriter(str(path), bigtiff=True) as tw:
        cur = data
        for lv in range(levels):
            image_type = "FullResolution" if lv == 0 else "ReducedResolution"
            tiling = dict(tile=(32, 32)) if lv < levels - 1 else dict(rowsperstrip=16)
            planes = [cur] if rgb else cur
            for c, plane in enumerate(planes):
                tw.write(np.ascontiguousarray(plane),
                         photometric="rgb" if rgb else "minisblack",
                         subfiletype=0 if lv == 0 else 1,
                         description=_qpi_xml(biomarkers[c] if not rgb else "RGB",
                                              filters[c % 3], image_type),
                         **tiling, **kw)
            if lv == 0:
                tw.write(thumb, photometric="rgb",
                         description=_qpi_xml(None, "Thumbnail", "Thumbnail"), **kw)
            h, w = cur.shape[-2:] if not rgb else cur.shape[:2]
            cur = (cur[:, :h // 2 * 2:2, :w // 2 * 2:2] if not rgb
                   else cur[:h // 2 * 2:2, :w // 2 * 2:2])
        for kind in ("Overview", "Label"):
            tw.write(np.zeros((30, 40, 3), np.uint8), photometric="rgb",
                     description=_qpi_xml(None, kind, kind), **kw)
    return str(path)


def write_ome_twin(path, data, levels=3):
    """The same pixels and the same ``//2`` pyramid as an OME-TIFF."""
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=levels - 1, tile=(32, 32),
                 metadata={"axes": "CYX", "Channel": {"Name": NAMES}})
        cur = data
        for _ in range(levels - 1):
            h, w = cur.shape[-2:]
            cur = np.ascontiguousarray(cur[:, :h // 2 * 2:2, :w // 2 * 2:2])
            tw.write(cur, subfiletype=1, tile=(32, 32))
    return str(path)


def _data(seed=0, shape=SHAPE, n=len(NAMES)):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(n,) + shape, dtype=np.uint8)


@pytest.fixture(scope="module")
def qpi(tmp_path_factory):
    data = _data()
    return write_qpi(tmp_path_factory.mktemp("qp") / "slide.qptiff", data), data


# ── the fixture is what tifffile calls QPI ──────────────────────────────

def test_the_fixture_is_a_qpi_pyramid(qpi):
    path, data = qpi
    with tifffile.TiffFile(path) as tf:
        assert tf.is_qpi and not tf.ome_metadata and qm.is_qpi(tf)
        s = tf.series[0]
        assert s.axes == "CYX" and s.shape == data.shape
        assert [lv.shape for lv in s.levels] == [(3, 301, 203), (3, 150, 101), (3, 75, 50)]


# ── metadata ─────────────────────────────────────────────────────────────

def test_names_are_the_biomarkers_and_the_size_comes_from_the_tags(qpi):
    with tifffile.TiffFile(qpi[0]) as tf:
        assert qm.qpi_channel_names(tf) == NAMES
        dy, dx = qm.qpi_physical_size_um(tf)
    assert dy == pytest.approx(UM) and dx == pytest.approx(UM)


def test_a_missing_biomarker_falls_back_to_the_filter_then_the_index(tmp_path):
    p = write_qpi(tmp_path / "s.qptiff", _data(), biomarkers=["DAPI", None, ""])
    with tifffile.TiffFile(p) as tf:
        assert qm.qpi_channel_names(tf) == ["DAPI", "ATTO 550", "Cy5"]


def test_without_biomarker_and_name_the_channel_is_its_index(tmp_path):
    p = write_qpi(tmp_path / "s.qptiff", _data(), biomarkers=["DAPI", None, None],
                  filters=["DAPI", None, None])
    with tifffile.TiffFile(p) as tf:
        assert qm.qpi_channel_names(tf) == ["DAPI", "ch_01", "ch_02"]


def test_a_duplicate_made_by_the_fallback_is_an_error(tmp_path):
    # channel 1 has no Biomarker and falls back to its filter, "Cy5"
    p = write_qpi(tmp_path / "s.qptiff", _data(), biomarkers=["Cy5", None, "CD8"],
                  filters=["DAPI", "Cy5", "Cy5"])
    with tifffile.TiffFile(p) as tf, pytest.raises(qm.QpiLayoutError, match="Cy5"):
        qm.qpi_channel_names(tf)


def test_duplicate_names_are_an_error_everywhere(tmp_path):
    p = write_qpi(tmp_path / "dup.qptiff", _data(), biomarkers=["DAPI", "CD3", "CD3"])
    with pytest.raises(qm.QpiLayoutError, match="duplicate channel names: CD3"):
        OMETIFFLoader(p)
    with pytest.raises(qm.QpiLayoutError, match="duplicate"):
        RawTileProvider(p)                     # not swallowed into ch_NN
    from block01.core import quant_sources as qs
    with pytest.raises(qm.QpiLayoutError, match="duplicate"):
        qs._slide_channels(p)


def test_an_rgb_qptiff_is_refused(tmp_path):
    rgb = np.zeros((64, 48, 3), np.uint8)
    p = write_qpi(tmp_path / "bf.qptiff", rgb, biomarkers=["RGB"], levels=2, rgb=True)
    with pytest.raises(qm.QpiLayoutError):
        OMETIFFLoader(p)


# ── OMETIFFLoader: names, shape, region reads ───────────────────────────

def test_the_loader_reads_a_qptiff(qpi):
    path, data = qpi
    lo = OMETIFFLoader(path, {"CD8": "CD8a"})
    assert lo.shape == SHAPE
    assert lo.ch_map == {"DAPI": 0, "CD3": 1, "CD8a": 2}
    np.testing.assert_array_equal(lo.read_region("CD8a", 17, 250, 3, 199, normalize=False),
                                  data[2, 17:250, 3:199])
    low = lo.read_region_lowres("CD3", 0, SHAPE[0], 0, SHAPE[1], 4, normalize=False)
    assert low.shape == lo.read_region("CD3", 0, SHAPE[0], 0, SHAPE[1], downsample=4,
                                       normalize=False).shape
    # the same pyramid as an OME-TIFF gives the same low-resolution read
    twin = OMETIFFLoader(write_ome_twin(os.path.join(os.path.dirname(path), "twin.ome.tif"),
                                        data))
    np.testing.assert_array_equal(
        low, twin.read_region_lowres("CD3", 0, SHAPE[0], 0, SHAPE[1], 4, normalize=False))


def test_the_workers_open_a_qptiff_as_they_open_an_ome_tiff(qpi):
    """`mesmer_worker.run_mesmer_patch_preview` / `cellpose_worker.run_cellpose_process`
    build their loader exactly like this from the job's ``ome_path``."""
    path, data = qpi
    lo = OMETIFFLoader(path, {}, correction_config=None)
    lo.set_corrected_zarr_store(None, {})
    np.testing.assert_array_equal(lo.read_region("DAPI", 0, 64, 0, 64, normalize=False),
                                  data[0, :64, :64])


# ── PixelSource / RawTileProvider (A2) ──────────────────────────────────

def test_the_pixel_source_reads_every_level(qpi):
    path, data = qpi
    with OmeTiffSource(path) as src:
        assert src.channel_names() == NAMES
        assert src.physical_size() == pytest.approx((UM, UM))
        assert src.source_format() == "qptiff"
        assert src.level_count() == 3
        assert [src.native_tile_shape(lv) for lv in range(3)] == [(32, 32), (32, 32), (16, 50)]
        level = data
        for lv in range(3):
            h, w = src.level_shape(lv)
            arr, origin = src.read_region("CD3", lv, 0, h, 0, w)
            np.testing.assert_array_equal(arr, level[1])
            tile = src.read_native_tile("CD8", lv, 1, 0)
            th, tw = src.native_tile_shape(lv)
            np.testing.assert_array_equal(tile[0] if isinstance(tile, tuple) else tile,
                                          level[2, th:2 * th, :tw])
            ty, tx = (h - 1) // th, (w - 1) // tw                  # the clipped corner tile
            edge = src.read_native_tile("DAPI", lv, ty, tx)
            np.testing.assert_array_equal(edge[0] if isinstance(edge, tuple) else edge,
                                          level[0, ty * th:, tx * tw:])
            level = level[:, :h // 2 * 2:2, :w // 2 * 2:2]
        got, _ = src.read_regions(["DAPI", "CD8"], 0, 5, 290, 7, 200)
        np.testing.assert_array_equal(got, data[[0, 2], 5:290, 7:200])
        assert src.scan_reader_mode() == "tiff_tiles"


def test_the_viewers_provider_and_the_injected_source_agree(qpi):
    path, _ = qpi
    prov = RawTileProvider(path)
    try:
        assert prov.channel_index("CD8") == 2
        src = OmeTiffSource(path, provider=prov)
        assert src.channel_names() == NAMES and src.source_format() == "qptiff"
    finally:
        prov.close()


# ── identity: describe_slide, find_workspaces ───────────────────────────

def test_describe_slide_of_a_qptiff(qpi):
    path, _ = qpi
    sid, desc = pid.describe_slide(path)
    assert sid.startswith("slide_") and desc["slide_id_method"] == pid.SLIDE_ID_METHOD
    assert desc["kind"] == "qptiff" and desc["channel_count"] == 3
    assert desc["level_shapes_yx"] == [[301, 203], [150, 101], [75, 50]]
    assert desc["physical_size_yx_um"] == pytest.approx([UM, UM])
    assert desc["fingerprint"] == pid.fingerprint(path)
    assert pid.describe_slide(path)[0] == sid                       # stable
    entry = pid.transforms_entry(desc)
    assert entry["physical_um"]["source"] == "QPI XResolution/YResolution"


def test_an_ome_slide_keeps_its_kind_and_provenance(tmp_path):
    from test_v16_project_identity import _write_slide
    p = _write_slide(tmp_path / "s.ome.tif")
    _, desc = pid.describe_slide(p)
    assert desc["kind"] == "ome_tiff"
    assert pid.transforms_entry(desc)["physical_um"]["source"] == "OME PhysicalSizeY/X"


def test_workspaces_of_a_qptiff_are_found_by_slide_id_after_a_move(tmp_path, qpi):
    from block01.utils import roi_project, workspace_session as wsess
    from test_v16_a6_workspace import _commit_step0
    slide = shutil.copy(qpi[0], tmp_path / "a.qptiff")
    proj = str(tmp_path / "proj")
    ctx = roi_project.create_full_wsi_context(proj, SHAPE, slide)
    _commit_step0(ctx, decisions={"CD3": "original", "CD8": "original"})
    sid, found = wsess.find_workspaces(proj, slide)
    assert sid and [w.workspace_id for w in found] == [ctx["roi_id"]]
    moved = str(tmp_path / "moved.qptiff")
    os.rename(slide, moved)
    assert [w.workspace_id for w in wsess.find_workspaces(proj, moved)[1]] == [ctx["roi_id"]]


# ── downstream: channel store, HQ resolver, Step4 ───────────────────────

def test_the_shared_channel_store_reads_through_the_loader(qpi):
    from block01.utils.channel_cache import SharedChannelStore
    path, data = qpi
    store = SharedChannelStore()
    try:
        got = store.read_raw_ome(OMETIFFLoader(path), "CD3", 10, 60, 20, 90)
        np.testing.assert_array_equal(got, data[1, 10:60, 20:90])
    finally:
        store.close()


def test_the_hq_resolver_takes_a_qptiff_as_the_raw_source(qpi):
    from block01.workers import hq_source_resolver as hq
    out = hq.resolve_hq_marker_source(
        requested_channels=["CD3", "CD8"], multichannel_source_path="",
        raw_channel_source_path=qpi[0], roi_id="r", requested_roi_names=[],
        loader_factory=OMETIFFLoader, source_mode=hq.SOURCE_MODE_RAW_ONLY)
    assert out.kind == "raw_ome" and list(out.available_channels) == NAMES


def test_step4_quantifies_a_project_on_a_qptiff(tmp_path, monkeypatch):
    import test_quant_sources as tqs
    from block01.core import quant_sources as qs

    def write_as_qpi(path, data, tiled=True):
        write_qpi(path, data, biomarkers=list(tqs.NAMES[:data.shape[0]]), levels=2)
    monkeypatch.setattr(tqs, "write_slide", write_as_qpi)
    p = tqs.build_project(tmp_path)
    with tifffile.TiffFile(p["slide"]) as tf:
        assert qm.is_qpi(tf)
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        raw = [c for c in job.channels if c.kind == "raw"]
        y0, x0 = tqs.ROI_BBOX[0], tqs.ROI_BBOX[2]
        np.testing.assert_array_equal(
            r.channels(raw, 3, 50, 7, 90),
            p["slide_data"][[c.index for c in raw], y0 + 3:y0 + 50, x0 + 7:x0 + 90])
    finally:
        r.close()


def test_batch_discovery_lists_a_qptiff(tmp_path, qpi):
    from block01.ui import batch_step4_dialog as bd
    scan = tmp_path / "sample" / "Scan1"
    scan.mkdir(parents=True)
    shutil.copy(qpi[0], scan / "x.qptiff")
    assert bd._find_ome_tiff(str(tmp_path / "sample")) == str(scan / "x.qptiff")
