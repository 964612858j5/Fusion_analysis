"""Block A2c: Step4 and Step1 fusion read their image data through the
PixelSource contract (docs/v16_A2c_application.md).

The pixels themselves are proven bitwise against the pre-migration path by
`scripts/diagnose_v16_a2c_oracle.py` on the representative dataset and by the
existing JobReader / fusion tests; these tests pin what is NEW: which
sources the consumers use, and that a corrected read is fail-closed.
"""

import os

import numpy as np
import pytest

pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core import quant_sources as qs  # noqa: E402
from block01.sources import CorrectedZarrSource, OmeTiffSource  # noqa: E402
from test_quant_sources import ROI_BBOX, build_project  # noqa: E402


# ── 2/3 Step4 ────────────────────────────────────────────────────────────

def test_step4_reads_through_the_contract(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        assert isinstance(r._raw, OmeTiffSource)
        assert isinstance(r._corrected, CorrectedZarrSource)
        assert r.raw_mode == "tiff_tiles"          # the provenance keeps its meaning
        raw = [c for c in job.channels if c.kind == "raw"]
        assert r.channel_dtype(raw[0]) == np.uint8
    finally:
        r.close()


def test_a_corrected_tile_the_product_does_not_cover_is_refused_not_read_raw(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    cd3 = next(c for c in job.channels if c.name == "CD3")
    r = qs.JobReader(job, read_threads=2)
    try:
        # a tile running past the region's (and the group's) edge
        h = ROI_BBOX[1] - ROI_BBOX[0]
        with pytest.raises(qs.QuantSourceError):
            r.channels([cd3], h - 5, h + 5, 0, 10)
    finally:
        r.close()


def test_a_project_without_corrected_channels_opens_no_corrected_source(tmp_path):
    p = build_project(tmp_path, decisions={"CD3": "original", "CD8": "original",
                                           "PanCK": "original"})
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        assert r._corrected is None
    finally:
        r.close()


# ── 3/3 FullFusionWorker ─────────────────────────────────────────────────

REMAP = {"DAPI": {"min": 0.0, "max": 255.0, "gamma": 1.0},
         "CD3": {"min": 0.0, "max": 50.0, "gamma": 1.0},
         "CD8": {"min": 0.0, "max": 255.0, "gamma": 1.0}}


def _fuse(p, out, *, sources, loader=None, rois=None, decisions=None, zpath=None, hook=None):
    from PyQt5 import QtCore
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0.overview_panel import FullFusionWorker
    decisions = {"CD3": "tophat"} if decisions is None else decisions
    zpath = p["zarr"] if zpath is None else zpath
    if loader is None:
        loader = OMETIFFLoader(p["slide"])
        loader.set_corrected_zarr_store(zpath, decisions)
    rois = rois if rois is not None else [{"name": "Full WSI", "bbox_fullres": list(ROI_BBOX),
                                           "polygon_fullres": None}]
    cfg = {"ome_tiff": p["slide"], "output_dir": str(out), "nucleus": {"channel": "DAPI", "weight": 1.0},
           "groups": {"markers": {"group_weight": 1.0, "channels": {"CD3": 1.0, "CD8": 1.0}}},
           "channel_remap_params": REMAP, "artifact_kind": "test", "config_hash": "test"}
    kw = ({"corrected_zarr_path": zpath, "corrected_decisions": decisions, "use_pixel_sources": True}
          if sources else {})
    w = FullFusionWorker(loader=loader, fusion_cfg=cfg, n_rows=2, n_cols=2, rois=rois, **kw)
    if hook is not None:
        hook(w, loader)
    errs, done = [], []
    w.error.connect(errs.append)
    w.finished.connect(done.append)
    w.run()
    return errs, (zarr.open(done[0], mode="r")[...] if done else None)


def test_fusion_through_sources_equals_the_loader_path(tmp_path):
    p = build_project(tmp_path)
    errs_a, a = _fuse(p, tmp_path / "a", sources=False)
    errs_b, b = _fuse(p, tmp_path / "b", sources=True)
    assert errs_a == [] and errs_b == []
    assert a is not None and a.any() and np.array_equal(a, b)


def test_fusion_reads_corrected_channels_from_the_product(tmp_path):
    p = build_project(tmp_path)
    _, with_product = _fuse(p, tmp_path / "a", sources=True)
    _, raw_only = _fuse(p, tmp_path / "b", sources=True, decisions={})
    assert not np.array_equal(with_product, raw_only)       # CD3 came from the product


def test_a_region_the_corrected_group_does_not_cover_is_refused_not_read_raw(tmp_path):
    """Blocker (1): the loader served raw pixels here; the migrated path refuses."""
    p = build_project(tmp_path)
    wider = [{"name": "Full WSI", "bbox_fullres": [0, 150, 0, 170], "polygon_fullres": None}]
    errs_old, old = _fuse(p, tmp_path / "old", sources=False, rois=wider)
    assert errs_old == [] and old is not None          # the old path silently went on
    errs, out = _fuse(p, tmp_path / "a", sources=True, rois=wider)
    assert out is None and errs and "no fallback" in errs[0]


def test_a_change_to_the_loaders_store_during_the_run_does_not_reach_the_fusion(tmp_path):
    """Blocker (2): the migrated worker never reads pixels through the loader."""
    p = build_project(tmp_path)
    _, clean = _fuse(p, tmp_path / "a", sources=True)
    from block01.ui.step0.overview_panel import FullFusionWorker
    real = FullFusionWorker._read_one_source

    def meddle(w, loader):
        def read(*a, **k):
            loader.set_corrected_zarr_store("", {})        # the GUI thread changing it
            loader.set_correction_config({})
            return real(*a, **k)
        w._read_one_source = read
    _, meddled = _fuse(p, tmp_path / "b", sources=True, hook=meddle)
    assert np.array_equal(clean, meddled)


def test_a_decided_channel_without_a_product_is_refused(tmp_path):
    p = build_project(tmp_path)
    errs, out = _fuse(p, tmp_path / "a", sources=True, zpath="")
    assert out is None and errs and "no fallback" in errs[0]
