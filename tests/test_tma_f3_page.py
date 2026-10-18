"""TMA F3 page (docs/tma_f3_page_application.md §2, §5), offscreen: the
public operations on a synthetic project -- choose the map sheet, find
cores, number by line / clicks / one core, merge, undo, lock, reopen read
only, the draft back after a reopen, a switched slide's late result dropped,
closing while a job runs.

The synthetic slide is a small pyramidal OME-TIFF with grainy discs on the
3 x 2 map of the F1 tests' workbook (position 6 empty).
"""

import importlib.util
import json
import os
import sys

import numpy as np
import pytest

pytest.importorskip("openpyxl")
tifffile = pytest.importorskip("tifffile")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from block01.core.tma import layout as L  # noqa: E402
from block01.core.tma import layout_lock as K  # noqa: E402

_F1 = os.path.join(os.path.dirname(__file__), "test_tma_f1.py")
_spec = importlib.util.spec_from_file_location("_tma_f1_helpers", _F1)
f1 = importlib.util.module_from_spec(_spec)
sys.modules["_tma_f1_helpers"] = f1
_spec.loader.exec_module(f1)

ROWS, COLS = f1.ROWS, f1.COLS          # 3 x 2, position 6 empty
D = 120
H, W = 900, 700


def _centre(x, y):
    return 200 + (y - 1) * 250, 220 + (x - 1) * 260      # (cy, cx); map as is


def _slide(path):
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.zeros((H, W), np.float32)
    for x in range(1, COLS + 1):
        for y in range(1, ROWS + 1):
            if (x - 1) * ROWS + y == 6:
                continue
            cy, cx = _centre(x, y)
            disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= (D / 2) ** 2
            img[disc] = rng.uniform(20, 200, disc.sum())
    base = np.stack([img.astype(np.uint8), np.zeros((H, W), np.uint8)])
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(base, subifds=1, tile=(128, 128),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]},
                           "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5})
        tw.write(base[:, ::2, ::2], subfiletype=1, tile=(128, 128))
    return str(path)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    slide = _slide(tmp_path / "tma.ome.tif")
    xlsx = str(tmp_path / "map.xlsx")
    f1._workbook(xlsx)
    # the map sheet says 1.0 mm; this slide's cores are 120 px at 0.5 um/px = 0.06 mm
    from block01.core.tma import map_sheet as ms
    real_parse = ms.parse

    def parse(p):
        s = real_parse(p)
        s.diameter_mm = D * 0.5 / 1000.0
        return s
    monkeypatch.setattr(ms, "parse", parse)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    yield str(proj), slide, xlsx
    # before the patches go: every TMA job ends and its result is delivered
    # here, not into a real dialog in a later test's teardown
    from block01.ui.tma import workers
    workers.shutdown(30000)
    app = QApplication.instance()
    for _ in range(20):
        app.processEvents()


def _wait(app, cond, ms=20000):
    import time
    t = time.time()
    while not cond():
        app.processEvents()
        if time.time() - t > ms / 1000:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _page(app, env, monkeypatch):
    from block01.ui.tma.tma_page import TmaPage
    proj, slide, xlsx = env
    page = TmaPage()
    page.resize(1200, 800)
    page.set_context(proj, slide)
    from PyQt5.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (xlsx, "")))
    page.btn_map.click()
    assert page.study is not None
    page.btn_find.click()
    _wait(app, lambda: not page._detecting)
    assert page.holder is not None and len(page.candidates["candidates"]) == 5
    return page


def _set_target(page, start_label, end_label, order_text):
    nb = page.numbering
    nb.order.setCurrentIndex([nb.order.itemText(i) for i in range(nb.order.count())].index(order_text))
    for box, lab in ((nb.start, start_label), (nb.end, end_label)):
        box.setCurrentIndex(next(i for i in range(box.count()) if box.itemText(i).startswith(lab)))


def _label_of(page, uid):
    key = page.holder.d["cores"][uid]["position_key"]
    return page._labels_by_key()[key]["label"] if key else None


def test_number_by_line_merge_undo_and_lock(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    # column 1 by a line from X1-Y01 to X1-Y03
    page._set_tool("line")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    page.ctl.map_drag("start", _centre(1, 1), _centre(1, 1))
    page.ctl.map_drag("finish", _centre(1, 1), _centre(1, 3))
    assert page.numbering.btn_confirm.isEnabled()
    page.numbering.btn_confirm.click()
    labels = sorted(filter(None, (_label_of(page, u) for u in page.holder.d["cores"])))
    assert labels == ["X1-Y01", "X1-Y02", "X1-Y03"]
    # column 2 by clicks; X2-Y03 (position 6) is empty, so the target is Y01..Y02
    page._set_tool("click")
    _set_target(page, "X2-Y01", "X2-Y02", "Along column")
    for y in (1, 2):
        page.ctl.map_click(*_centre(2, y), 0)
    page.numbering.btn_confirm.click()
    assert sum(1 for u in page.holder.d["cores"] if _label_of(page, u)) == 5
    # a drawn fragment merged into X1-Y01, then undone
    page._set_tool("draw")
    cy, cx = _centre(1, 1)
    page.ctl.map_drag("start", (cy, cx + 90), (cy, cx + 90))
    page.ctl.map_drag("finish", (cy, cx + 90), (cy, cx + 100))
    frag = page.selected
    assert page.holder.d["pieces"][frag]["source"] == "drawn"
    page._set_tool("select")
    page.ctl.map_click(cy, cx, 0)
    core = page.selected_core()
    page._set_tool("merge")
    page.ctl.map_click(cy, cx + 90, 0)
    assert page.holder.d["cores"][core]["mapping"] == "review"
    page.undo()
    assert page.holder.d["pieces"][frag]["owner"] is None
    page.edit(L.drop_piece, frag, "dust")
    # lock
    monkeypatch.setattr(type(page), "_confirm_coverage", lambda self: True)
    page.btn_lock.click()
    assert page.view_version == 1 and page.holder.read_only
    assert K.load_draft(page.project_dir) is None
    assert page.btn_lock.text() == "New draft from this version"
    with pytest.raises(Exception):
        page.holder.do(L.set_missing, next(iter(page.holder.d["positions"])))
    page.btn_lock.click()                                             # new draft based on v1
    assert page.view_version is None and page.holder.d["based_on"] == 1
    page.shutdown()


def test_single_number_conflict_and_draft_restored_on_reopen(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    page.ctl.map_click(*_centre(1, 1), 0)
    page.numbering.number.setText("X1-Y02")
    page.numbering.btn_bind.click()
    uid = page.selected_core()
    assert _label_of(page, uid) == "X1-Y02"
    # a second core asking for X1-Y02: rebind
    from PyQt5.QtWidgets import QMessageBox as MB
    monkeypatch.setattr(MB, "exec_", lambda self: (setattr(self, "_clicked", self.buttons()[0]), 0)[1])
    monkeypatch.setattr(MB, "clickedButton", lambda self: getattr(self, "_clicked", None))
    page.ctl.map_click(*_centre(1, 2), 0)
    page.numbering.number.setText("X1-Y02")
    page.numbering.btn_bind.click()
    assert _label_of(page, page.selected_core()) == "X1-Y02" and _label_of(page, uid) is None
    # reopen: the draft comes back as it was (the autosave waits for a pause)
    assert page._save_timer.isActive()
    page.flush_draft()
    from block01.ui.tma.tma_page import TmaPage
    again = TmaPage()
    again.set_context(page.project_dir, page.slide_path)
    assert again.holder is not None and again.view_version is None
    assert again.holder.d["cores"] == page.holder.d["cores"]
    page.shutdown()


def test_a_switched_slide_drops_the_old_result(app, env, monkeypatch, tmp_path):
    page = _page(app, env, monkeypatch)
    gen = page._gen
    page.set_context(None, None)                                      # the dataset went away
    page._detect_done(gen, page.candidates)                           # a late result of the old slide
    assert page.holder is None
    page._thumb_done(gen, np.zeros((10, 10), np.float32))
    assert page.shape is None
    page.shutdown()


def test_closing_while_finding_cores_waits_for_the_job(app, env, monkeypatch):
    from block01.ui.tma import workers
    page = _page(app, env, monkeypatch)
    page.btn_find.click()
    assert workers.LIVE
    assert page.shutdown()
    assert all(w.isFinished() for w in list(workers.LIVE))


# ── part 3 review (codex_tma_f3_p3_1.out) ──────────────────────────────

def test_map_clicks_before_any_draft_do_nothing(app, env):
    from block01.ui.tma.tma_page import TmaPage
    proj, slide, _xlsx = env
    page = TmaPage()
    page.set_context(proj, slide)
    assert page.holder is None
    page.ctl.map_click(100.0, 100.0, 0)
    page.ctl.map_context(100.0, 100.0, None)
    page.ctl.map_drag("start", (1.0, 1.0), (1.0, 1.0))
    page.shutdown()


def test_undoing_a_drawn_circle_inside_a_click_sequence(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("draw")
    page.ctl.map_drag("start", (50.0, 600.0), (50.0, 600.0))
    page.ctl.map_drag("finish", (50.0, 600.0), (50.0, 620.0))
    page._set_tool("click")
    page.ctl.map_click(50.0, 600.0, 0)
    page.ctl.map_click(*_centre(1, 1), 0)
    assert len(page.ctl.sequence) == 2
    page.undo()                                                       # the drawn circle goes
    assert len(page.ctl.sequence) == 1
    page.shutdown()


def test_a_corner_handle_resize_does_not_jump(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    cy, cx = _centre(1, 1)
    page.ctl.map_click(cy, cx, 0)
    pid = page.selected
    r = page.holder.d["pieces"][pid]["r"]
    corner = (cy - r, cx + r)                                          # a box corner handle
    monkeypatch.setattr(page.ctl, "handle_at", lambda y, x: True)
    page.ctl.map_drag("start", corner, corner)
    page.ctl.map_drag("move", corner, corner)
    assert abs(page.holder.d["pieces"][pid]["r"] - r) < 1e-6
    page.ctl.map_drag("finish", corner, (cy - 1.1 * r, cx + 1.1 * r))
    assert abs(page.holder.d["pieces"][pid]["r"] - 1.1 * r) < 1e-6
    page.shutdown()


def test_find_cores_asks_before_replacing_a_saved_draft(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.ctl.map_click(*_centre(1, 1), 0)
    page._run_toggled(next(iter(page.holder.d["positions"])), True)   # an edit with no operation entry
    before = page.holder.d
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    page.btn_find.click()
    _wait(app, lambda: not page._detecting)
    assert page.holder.d is before
    page.shutdown()


def test_a_context_switch_during_finding_frees_the_button(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.btn_find.click()
    assert page._detecting
    proj, slide, _x = env
    page.set_context(None, None)
    page.set_context(proj, slide)
    assert not page._detecting and page.btn_find.isEnabled()
    page.shutdown()


def test_enter_confirms_a_click_sequence_and_the_lock_dialog_needs_both_ticks(app, env, monkeypatch):
    from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox
    page = _page(app, env, monkeypatch)
    page._set_tool("click")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    for y in (1, 2, 3):
        page.ctl.map_click(*_centre(1, y), 0)
    page._enter_key()                                                 # ends the sequence, commits nothing
    assert sum(1 for c in page.holder.d["cores"].values() if c["position_key"]) == 0
    page.ctl.map_click(*_centre(2, 1), 0)                             # no more clicks taken
    assert len(page.ctl.sequence) == 3
    page.numbering.btn_confirm.click()
    assert sum(1 for c in page.holder.d["cores"].values() if c["position_key"]) == 3
    seen = {}

    def run(dlg):
        boxes = dlg.findChildren(QCheckBox)
        ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        seen["start"] = ok.isEnabled()
        boxes[0].setChecked(True)
        seen["one"] = ok.isEnabled()
        boxes[1].setChecked(True)
        seen["both"] = ok.isEnabled()
        return QDialog.Accepted
    monkeypatch.setattr(QDialog, "exec_", run)
    assert page._confirm_coverage()
    assert seen == {"start": False, "one": False, "both": True}
    page.shutdown()


# ── parts 3+4 re-review (codex_tma_f3_p34_2.out) ───────────────────────

def test_another_slide_in_the_same_project_is_refused(app, env, monkeypatch, tmp_path):
    page = _page(app, env, monkeypatch)
    proj = page.project_dir
    page.shutdown()
    other = tmp_path / "other"
    other.mkdir()
    rng = np.random.default_rng(9)
    tifffile.imwrite(str(other / "b.ome.tif"), (rng.random((2, 300, 300)) * 255).astype(np.uint8), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    from block01.ui.tma.tma_page import TmaPage
    b = TmaPage()
    b.set_context(proj, str(other / "b.ome.tif"))
    assert b.study is None and b.holder is None and b.study_mismatch
    assert not b.btn_find.isEnabled() and not b.btn_map.isEnabled()
    assert not b.version_box.isEnabled() and b.version_box.count() == 0
    b._version_chosen(0)                                               # even if reached: nothing opens
    assert b.holder is None
    b.shutdown()


def test_a_refused_close_leaves_find_cores_usable(app, env, monkeypatch):
    from block01.ui.tma import workers
    page = _page(app, env, monkeypatch)
    page.btn_find.click()
    workers.shutdown(timeout_ms=1)                                     # may time out: the close is refused
    _wait(app, lambda: not page._detecting)
    assert page.btn_find.isEnabled()
    page.shutdown()


# ── user feedback 2026-10-18 ───────────────────────────────────────────

def test_the_map_sheet_shows_its_table_before_find_cores(app, env, monkeypatch):
    from block01.ui.tma.tma_page import TmaPage
    from PyQt5.QtWidgets import QFileDialog
    proj, slide, xlsx = env
    page = TmaPage()
    page.set_context(proj, slide)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (xlsx, "")))
    page.btn_map.click()
    assert page.holder is None and page.table.table.rowCount() == ROWS * COLS
    page.shutdown()


def test_a_left_drag_pans_unless_it_edits_the_selected_piece(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    assert page.ctl.map_drag("start", (5.0, 5.0), (5.0, 5.0)) is False          # empty glass: pan
    assert page.ctl.map_drag("start", _centre(1, 1), _centre(1, 1)) is False      # not selected: pan
    page.ctl.map_click(*_centre(1, 1), 0)
    assert page.ctl.map_drag("start", _centre(1, 1), _centre(1, 1)) is True       # the selected one: move
    page.ctl.map_drag("finish", _centre(1, 1), _centre(1, 1))
    page._set_tool("clicks" if False else "click")
    assert page.ctl.map_drag("start", (5.0, 5.0), (5.0, 5.0)) is False
    page.shutdown()


def test_a_burst_of_edits_is_saved_once_after_a_pause(app, env, monkeypatch):
    from block01.core.tma import layout_lock as KK
    page = _page(app, env, monkeypatch)
    writes = []
    real = KK.save_draft
    monkeypatch.setattr(KK, "save_draft", lambda p, d: (writes.append(1), real(p, d))[1])
    for key in list(page.holder.d["positions"])[:5]:
        page._run_toggled(key, True)
    assert writes == []
    _wait(app, lambda: writes, ms=3000)
    assert len(writes) == 1
    page.shutdown()
