"""Block A8 (C-b): the CURRENT segmentation run as the code decides it today,
pinned before the migration (application §3.2, §6). Written and run on the
pre-A8 code first; the answers are copied into the execution record; the
migrated code must give the same answers -- except where §3.2 deliberately
changes one (named in each test).

Seams used: `_on_step2_complete` (Step2's finished result), `_go_to_step3`'s
named run (`_step3_requested_run`), `_step3_refresh_masks` / `_step3_on_run_chosen`
(Step3's list), `_step3_load_run` (Load…), `_rm_note_viewing` (a Generate /
fuse choice), `_step4_choice` (what Step4 quantifies).
"""

import json
import os

import pytest

from block01.utils import run_store as rs
from test_v16_rm1_run_store import (  # noqa: F401  (fixtures and helpers)
    app, _rm_window, _correct_run, _fuse_run, T0)
from test_v16_rm2_chain import _t

BBOX = [0, 100, 0, 120]


def _seg(ws, fuse, now, method="stardist_nuclei_dapi"):
    r = rs.new_run(ws, "segment", suffix=method, now=now)
    with open(os.path.join(r, "segmentation_meta.json"), "w") as f:
        json.dump({"run_id": os.path.basename(r), "method": method,
                   "created_at": now.isoformat(),
                   "rois": [{"roi_name": "R1", "bbox_fullres": BBOX}]}, f)
    rs.write_params(r, {"kind": "segment", "summary": method,
                        "segmentation_config": {"method": method}})
    rs.write_inputs(r, fuse)
    rs.publish(r)
    return r


def _viewing(ws):
    v = rs.session_pointers(ws).get("viewing") or ""
    return os.path.realpath(v) if v else ""


def _rel(_ws, run):
    return os.path.realpath(run)


@pytest.fixture
def chain(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    w._active_roi = {"name": "R1", "bbox_fullres": BBOX}
    c = _correct_run(ws)
    f = _fuse_run(ws, c, regions=("R1",))
    a = _seg(ws, f, _t(10))
    b = _seg(ws, f, _t(20), method="cellpose_wholecell_fusion")
    yield w, ws, f, a, b
    w.close()


def _current_run_dir(w):
    run_dir, _roi = w._step4_choice()
    return os.path.realpath(run_dir) if run_dir else ""


def test_c1_a_finished_step2_run_is_what_step4_takes(chain):
    w, ws, f, a, b = chain
    w._on_step2_complete(b)
    assert _current_run_dir(w) == os.path.realpath(b)
    assert _viewing(ws) == ""                     # Step2 completion writes no `viewing`


def test_c2_step3_shows_the_run_step2_named_and_records_it(chain):
    w, ws, f, a, b = chain
    entry = w._step3_refresh_masks(requested_dir=a)
    assert entry is not None and os.path.realpath(entry.run.run_dir) == os.path.realpath(a)
    assert _viewing(ws) == _rel(ws, a)
    assert _current_run_dir(w) == os.path.realpath(a)


def test_c3_a_choice_in_step3_is_what_step4_takes(chain):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    w._step3_on_run_chosen(f"{b}\x1fR1")
    assert _current_run_dir(w) == os.path.realpath(b)
    assert _viewing(ws) == _rel(ws, b)


def test_c4_an_outside_result_is_current_but_not_recorded(chain, tmp_path, monkeypatch):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    other = tmp_path / "other"
    other_w, other_ws = None, None
    from test_v16_rm1_run_store import _rm_window as _win
    ow, ows = _win(w.__class__ and pytest.importorskip("PyQt5").QtWidgets.QApplication.instance(),
                   other)
    try:
        oc = _correct_run(ows)
        of = _fuse_run(ows, oc, regions=("R1",))
        x = _seg(ows, of, _t(30))
    finally:
        ow.close()
    monkeypatch.setattr(w, "_step3_pick_run_folder", lambda: x)
    monkeypatch.setattr(type(w), "_step3_slide_path", lambda self: "")
    import block01.core.step3_masks as sm
    monkeypatch.setattr(sm, "same_slide", lambda p, q: True)
    assert w._step3_load_run() is True
    assert _current_run_dir(w) == os.path.realpath(x)
    assert _viewing(ws) == _rel(ws, a)            # the outside run is not recorded


def test_c5_generate_keeps_the_current_segmentation_run(chain):
    """C-b: a Generate (a new fuse run noted as `viewing`) -- what happens to
    the current segmentation run is pinned here, on today's code."""
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    c = rs.upstream_of(f)
    f2 = _fuse_run(ws, c, regions=("R1",), now=_t(40))
    w._rm_note_viewing(f2)
    assert _viewing(ws) == _rel(ws, f2)
    assert _current_run_dir(w) == os.path.realpath(a)          # before Step3 is looked at
    w._step3_refresh_masks()
    assert _current_run_dir(w) == os.path.realpath(a)          # and after: KEPT


def test_c6_a_removed_current_run_falls_to_the_workspaces_next(chain):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=b)
    import shutil
    shutil.move(b, os.path.join(os.path.dirname(ws), "..", "moved_away"))   # gone, as a delete leaves it
    entry = w._step3_refresh_masks()
    assert entry is not None and os.path.realpath(entry.run.run_dir) == os.path.realpath(a)
    assert _current_run_dir(w) == os.path.realpath(a)


def test_c7_reopening_restores_the_viewed_run_in_step3(chain, app, tmp_path):
    """Today Step4 learns it only once Step3 has been looked at (§3.2 makes
    the restored run current at once -- a deliberate change)."""
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    assert _viewing(ws) == _rel(ws, a)
    import test_step1_fusion_isolation as iso
    w2 = iso._window(app, tmp_path / "second")
    w2.step0_output = dict(w2.step0_output, roi_dir=ws, roi_id="ws1",
                           step0_dir=os.path.join(ws, "settings", "step0"),
                           step1_dir=os.path.join(ws, "settings"))
    try:
        w2._active_roi = {"name": "R1", "bbox_fullres": BBOX}
        assert w2._step4_choice() == ("", None)                # today: nothing yet
        entry = w2._step3_refresh_masks()
        assert os.path.realpath(entry.run.run_dir) == os.path.realpath(a)
    finally:
        w2.close()


def test_c8_another_workspace_drops_the_choice(chain, tmp_path):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    assert w._step3_mask_key
    other = tmp_path / "proj" / "rois" / "ws2"
    (other / "settings" / "step0").mkdir(parents=True)
    (other / "roi_manifest.json").write_text("{}")
    w.step0_output = dict(w.step0_output, roi_dir=str(other), roi_id="ws2")
    entry = w._step3_refresh_masks()
    # ws2 has no runs of its own: the newest of the project's others is shown
    assert entry is None or os.path.realpath(entry.run.run_dir) in (
        os.path.realpath(a), os.path.realpath(b))


def test_c9_a_step3_choice_outlives_a_later_step2_run(chain):
    """Step3 shown (a choice exists), then Step2 finishes another run:
    Step4 keeps the Step3 choice; the new run becomes current only when it
    is opened in Step3 (Step2's "Open QC" names it)."""
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    w._on_step2_complete(b)
    assert _current_run_dir(w) == os.path.realpath(a)


def test_c10_without_step3_the_newest_step2_run_is_taken(chain):
    w, ws, f, a, b = chain
    w._on_step2_complete(a)
    w._on_step2_complete(b)
    assert _current_run_dir(w) == os.path.realpath(b)
