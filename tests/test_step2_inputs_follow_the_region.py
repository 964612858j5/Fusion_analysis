"""User ruling 2026-11-15 (Windows acceptance): after a Step0 Save that
removed the ROI (Overwrite: the same workspace, the correct run rewritten in
place), Step2 still showed the fused input Generated for the deleted ROI. A
fuse run made for another region than the one Step0 now records is no Step2
input any more; one for the same region, and one too old to record its
region, still are. Nothing is deleted.
"""
import os

import pytest

pytest.importorskip("block01.qt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.utils import run_store as rs  # noqa: E402

from test_v16_a6_workspace import app  # noqa: E402,F401  (fixture)
from test_v16_rm1_run_store import T0, _correct_run, _fuse_run, _rm_window  # noqa: E402

from datetime import timedelta  # noqa: E402

ROI_A = {"name": "R1", "bbox_fullres": [100, 600, 200, 900],
         "polygon_fullres": [[200, 100], [900, 100], [900, 600], [200, 600]]}
ROI_B = {"name": "R1", "bbox_fullres": [0, 300, 0, 300],
         "polygon_fullres": [[0, 0], [300, 0], [300, 300], [0, 300]]}
FULL = {"name": "Full WSI", "bbox_fullres": [0, 2000, 0, 2000], "polygon_fullres": None}


def _step0_region(correct, rois, kind="roi"):
    params = rs.read_params(correct)
    params["geometry"] = {"analysis_region_type": kind, "rois": rois, "patches": [],
                          "geometry_revision": 1}
    rs.write_params(correct, params)


def _made_for(fuse, roi):
    params = rs.read_params(fuse)
    params["geometry"] = {"regions": [{"roi_name": roi["name"], "bbox_fullres": roi["bbox_fullres"],
                                       "polygon_fullres": roi["polygon_fullres"]}]}
    rs.write_params(fuse, params)


def test_only_fuse_runs_for_the_current_region_are_inputs(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        _step0_region(c, [ROI_A])
        old = _fuse_run(ws, c, now=T0 + timedelta(minutes=1))
        _made_for(old, ROI_B)                                    # an earlier region
        legacy = _fuse_run(ws, c, now=T0 + timedelta(minutes=2))  # records no region
        same = _fuse_run(ws, c, now=T0 + timedelta(minutes=3))
        _made_for(same, ROI_A)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_bind_step2()
        listed = [i["run"] for i in w._step2._fuse_items]
        assert listed == [same, legacy]                          # newest first; `old` left out
        assert w._step2._zarr_edit.text() == os.path.join(same, "fused_R1.zarr")
        # The ROI is deleted and Step0 saved over the same workspace: the
        # whole slide now. Neither the ROI's run nor the earlier one is an
        # input; the unjudgeable legacy run still is.
        _step0_region(c, [FULL], kind="full_wsi")
        w._rm_bind_step2()
        assert [i["run"] for i in w._step2._fuse_items] == [legacy]
        # ...and with no legacy run there is no input at all
        rs.write_params(legacy, dict(rs.read_params(legacy), geometry={"regions": [
            {"roi_name": "R1", "bbox_fullres": ROI_A["bbox_fullres"],
             "polygon_fullres": ROI_A["polygon_fullres"]}]}))
        w._rm_bind_step2()
        assert w._step2._fuse_items == [] and w._step2._zarr_edit.text() == ""
        # Nothing was deleted
        assert all(rs.is_done(r) for r in (old, legacy, same))
    finally:
        w.close()


def test_a_rename_alone_keeps_the_input(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        _step0_region(c, [dict(ROI_A, name="Tumour")])
        f = _fuse_run(ws, c)
        _made_for(f, ROI_A)                                      # Generated as "R1"
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert [i["run"] for i in w._step2._fuse_items] == [f]
    finally:
        w.close()


def test_a_correct_run_without_a_region_record_judges_nothing(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)                                     # no geometry recorded
        f = _fuse_run(ws, c)
        _made_for(f, ROI_B)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert [i["run"] for i in w._step2._fuse_items] == [f]
    finally:
        w.close()
