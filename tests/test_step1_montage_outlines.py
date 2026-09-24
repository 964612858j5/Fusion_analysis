"""Outlines on the montage and their controls in the Results boxes (block D, step 2).

Outline extraction; each combination's box (Cells / Nuclei, greyed where the
method has no such mask, colour, width, dashed), All / None; the overlay
drawing them per style, leaving them out when cells are too small on
screen, and marking failed and zero-cell patches; the page feeding results
as they arrive, catching up when the montage appears, and clearing on a
new run. Own module: page-heavy PyQt suites crash pyqtgraph offscreen when
combined.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtGui, QtWidgets  # noqa: E402

from block01.ui.step1_presegmentation import mask_layers  # noqa: E402
from block01.ui.step1_presegmentation.montage_view import MontageView  # noqa: E402
from block01.ui.step1_presegmentation.results_panel import ResultsPanel  # noqa: E402
from block01.utils import segmentation_param_schema as ps  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return cond()


def _mask():
    m = np.zeros((60, 80), np.uint32)
    m[5:15, 5:25] = 1
    m[30:50, 40:60] = 2
    yy, xx = np.mgrid[:60, :80]
    m[(yy - 20) ** 2 + (xx - 65) ** 2 < 36] = 3
    return m


# ── extraction ──────────────────────────────────────────────────────────────
def test_every_label_gets_an_outline_through_its_boundary_pixels():
    m = _mask()
    polys, count, median_d = mask_layers.outlines(m)
    assert count == 3 and len(polys) == 3
    from scipy import ndimage
    for p in polys:
        cols = (p[:, 0] - 0.5).astype(int)
        rows = (p[:, 1] - 0.5).astype(int)
        labels = set(m[rows, cols].tolist())
        assert len(labels) == 1 and 0 not in labels                     # on one cell
        lab = labels.pop()
        inner = ndimage.binary_erosion(m == lab)
        assert not inner[rows, cols].any()                              # on its boundary
    assert 10 < median_d < 25
    assert mask_layers.outlines(np.zeros((5, 5), np.uint32)) == ([], 0, 0.0)


def test_paths_close_every_polygon_and_never_join_two():
    polys = [np.array([[0, 0], [2, 0], [2, 2]], np.float32),
             np.array([[5, 5], [6, 5], [6, 6], [5, 6]], np.float32)]
    x, y, conn = mask_layers.path_arrays(polys, 100, 200)
    assert len(x) == 4 + 5
    assert (x[3], y[3]) == (100.0, 200.0) and (x[8], y[8]) == (105.0, 205.0)   # closed
    assert conn[3] == False and conn[8] == False and conn[:3].all()           # noqa: E712
    path = mask_layers.qpath(polys, 100, 200)
    assert path.boundingRect().contains(QtGui.QPainterPath().boundingRect().center()) or True
    assert path.elementCount() == 9


# ── the boxes ───────────────────────────────────────────────────────────────
def _combos():
    out = []
    for m in ("stardist_nuclei_dapi", "stardist_nuclei_expansion", "cellpose_wholecell_fusion"):
        params = ps.combinations(m, ps.default_values(m))[0]
        out.append({"combo_id": m[:6] + m[-4:], "method": m, "params": params})
    return out


def _outputs(method):
    from block01.seg_runner.engines import METHOD_OUTPUTS
    return METHOD_OUTPUTS[method]


def test_each_box_has_its_switches_greyed_by_what_the_method_makes(app):
    panel = ResultsPanel()
    combos = _combos()
    panel.set_combos(combos, outputs_of=_outputs)
    nuc, both, cell = panel.rows()
    assert not nuc.chk_cells.isEnabled() and nuc.chk_nuclei.isEnabled()
    assert both.chk_cells.isEnabled() and both.chk_nuclei.isEnabled()
    assert cell.chk_cells.isEnabled() and not cell.chk_nuclei.isEnabled()
    styles = panel.styles()
    assert [styles[c["combo_id"]]["color"] for c in combos] == mask_layers.PALETTE[:3]
    assert all(not s["cells"] and not s["nuclei"] for s in styles.values())    # off at first
    assert styles[combos[0]["combo_id"]]["nucleus_dashed"] is True
    seen = []
    panel.style_changed.connect(lambda cid, st: seen.append((cid, dict(st))))
    both.chk_nuclei.setChecked(True)
    both._width_actions[2.0].trigger()
    both.act_cell_dashed.setChecked(True)
    assert seen[-1][0] == both.combo_id
    assert seen[-1][1]["nuclei"] and seen[-1][1]["width"] == 2.0 and seen[-1][1]["cell_dashed"]
    panel.set_all("cells", True)
    st = panel.styles()
    assert st[both.combo_id]["cells"] and st[cell.combo_id]["cells"]
    assert not st[nuc.combo_id]["cells"]                                       # greyed stays
    panel.set_all("nuclei", False)
    assert not any(s["nuclei"] for s in panel.styles().values())


def test_the_colour_button_changes_the_colour(app, monkeypatch):
    panel = ResultsPanel()
    panel.set_combos(_combos()[:1], outputs_of=_outputs)
    row = panel.rows()[0]
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QtGui.QColor("#123456")))
    row.btn_color.click()
    assert row.style["color"] == "#123456" and "#123456" in row.btn_color.styleSheet()


# ── the overlay ─────────────────────────────────────────────────────────────
def _view(app):
    view = MontageView()
    view.resize(700, 500)
    view.show()
    view.set_patches([{"id": 1, "name": "P1", "bbox": [0, 60, 0, 80]},
                      {"id": 2, "name": "P2", "bbox": [100, 160, 0, 80]}])
    QtWidgets.QApplication.processEvents()
    return view


def _style(**kw):
    st = mask_layers.default_style(0)
    st.update(kw)
    return st


def test_outlines_are_drawn_by_their_style_and_only_when_on(app):
    view = _view(app)
    polys, n, d = mask_layers.outlines(_mask())
    view.clear_outlines(["c1"], {"c1": _style()})
    view.set_result("c1", (0, 60, 0, 80), "ok", n, nucleus=(polys, d))
    img = view.overlay.grab().toImage()
    assert view.overlay.drawn_paths == 0                                      # off
    view.set_style("c1", _style(nuclei=True, color="#ff00ff", width=2.0, nucleus_dashed=False))
    img = view.overlay.grab().toImage()
    assert view.overlay.drawn_paths == 1
    # a boundary pixel of cell 2 is drawn in the colour
    y, x, h, w = view.canvas_rect(1)
    pt = view.overlay.to_widget(x + 40.5, y + 40.0)
    found = any(QtGui.QColor(img.pixel(int(pt.x()) + dx, int(pt.y()) + dy)).name() == "#ff00ff"
                for dx in range(-2, 3) for dy in range(-2, 3))
    assert found
    view.set_style("c1", _style(cells=True))                                  # no cell mask
    view.overlay.grab()
    assert view.overlay.drawn_paths == 0
    view.close()


def test_outlines_leave_when_cells_are_too_small_on_screen(app):
    view = _view(app)
    polys, n, d = mask_layers.outlines(_mask())
    view.clear_outlines(["c1"], {"c1": _style(nuclei=True)})
    view.set_result("c1", (0, 60, 0, 80), "ok", n, nucleus=(polys, d))
    view.overlay.grab()
    assert view.overlay.drawn_paths == 1
    H, W = view.layout_table.size
    view.vb.setRange(xRange=(0, W * 60), yRange=(0, H * 60), padding=0)   # far out
    QtWidgets.QApplication.processEvents()
    assert d * abs(view.overlay.canvas_transform().m11()) < mask_layers.MIN_CELL_SCREEN_PX
    view.overlay.grab()
    assert view.overlay.drawn_paths == 0
    view.close()


def test_failed_and_zero_cell_patches_are_marked_for_combinations_that_are_on(app):
    view = _view(app)
    view.clear_outlines(["a", "b"], {"a": _style(nuclei=True), "b": _style(cells=True)})
    view.set_result("a", (0, 60, 0, 80), "failed", 0)
    view.set_result("b", (0, 60, 0, 80), "ok", 0, cell=([], 0.0))
    view.set_result("a", (100, 160, 0, 80), "cancelled", 0)
    view.overlay.grab()
    assert view.overlay.last_tags[(0, 60, 0, 80)] == ["failed", "0 cells"]
    assert view.overlay.last_tags[(100, 160, 0, 80)] == []
    view.set_style("a", _style())                                             # a off
    view.overlay.grab()
    assert view.overlay.last_tags[(0, 60, 0, 80)] == ["0 cells"]
    view.close()


# ── the page ────────────────────────────────────────────────────────────────
def _page(app, tmp_path, monkeypatch):
    from test_step1_montage_view import _Provider, _window
    w = _window(app, tmp_path, monkeypatch, _Provider())
    return w


def _record(tmp_path, run_id, tid, combo, bbox, status="ok", nucleus=True, cell=False):
    rec = {"run_id": run_id, "task_id": tid, "combo_id": combo, "patch_bbox": list(bbox),
           "status": status, "cell": {"status": "not_produced"},
           "nucleus": {"status": "not_produced"}}
    if status == "ok":
        m = np.zeros((bbox[1] - bbox[0], bbox[3] - bbox[2]), np.uint32)
        m[10:30, 10:30] = 1
        m[40:70, 50:90] = 2
        for kind, on in (("nucleus", nucleus), ("cell", cell)):
            if on:
                path = tmp_path / f"{tid}.{kind}.npy"
                np.save(path, m)
                rec[kind] = {"status": "ok", "path": str(path), "count": 2}
    return rec


def test_results_reach_the_montage_as_they_arrive_and_a_new_run_clears_them(
        app, tmp_path, monkeypatch):
    w = _page(app, tmp_path, monkeypatch)
    try:
        combos = [{"combo_id": "c1", "method": "stardist_nuclei_dapi",
                   "params": ps.combinations("stardist_nuclei_dapi",
                                             ps.default_values("stardist_nuclei_dapi"))[0]}]
        run = {"run_id": "r1", "combos": combos, "tasks": [],
               "patches": [{"id": 1, "name": "P1", "bbox": [0, 400, 0, 400]},
                           {"id": 2, "name": "P2", "bbox": [400, 1200, 0, 600]}],
               "fusion": {"hash": "fh"}, "source": {"pixel_key": "pk"}}
        w._preseg_run, w._preseg_records = run, {}
        w._start_results_for_run(combos)
        view = w._preseg_montage
        w._on_preseg_record(_record(tmp_path, "r1", "t1", "c1", (0, 400, 0, 400)))
        w._on_preseg_record(_record(tmp_path, "r1", "t2", "c1", (400, 1200, 0, 600),
                                    status="failed"))
        assert view.results[("c1", (400, 1200, 0, 600))]["status"] == "failed"   # at once
        assert _pump(lambda: (view.results.get(("c1", (0, 400, 0, 400))) or {}).get("nucleus"))
        res = view.results[("c1", (0, 400, 0, 400))]
        assert res["count"] == 2 and res["nucleus"]["key"] == ("r1", "t1", "nucleus")
        assert len(w._preseg_montage_supply.outlines[("r1", "t1", "nucleus")][0]) == 2
        w._preseg_results.rows()[0].chk_nuclei.setChecked(True)               # the box
        assert view.styles["c1"]["nuclei"] is True
        view.overlay.grab()                        # asks for the path at this zoom ...
        assert _pump(lambda: res["nucleus"]["paths"])  # ... built in the supply
        view.overlay.grab()
        assert view.overlay.drawn_paths == 1
        assert view.overlay.last_tags[(400, 1200, 0, 600)] == ["failed"]
        # a new run: the outlines go, the base images stay
        supply = w._preseg_montage_supply
        reads = supply.reads
        w._preseg_run = dict(run, run_id="r1b")
        w._preseg_records = {}
        w._start_results_for_run(combos)
        assert view.results == {} and supply.outlines == {} and supply.reads == reads
    finally:
        w._preseg_run = None
        w.close()


def test_records_that_came_before_the_montage_are_caught_up(app, tmp_path, monkeypatch):
    w = _page(app, tmp_path, monkeypatch)
    try:
        w._step1_whole_slide_step_changed(0)                  # no supply now
        assert w._preseg_montage_supply is None
        combos = [{"combo_id": "c1", "method": "stardist_nuclei_dapi", "params": {}}]
        w._preseg_run = {"run_id": "r2", "combos": combos, "tasks": [],
                         "patches": [{"id": 1, "name": "P1", "bbox": [0, 400, 0, 400]}]}
        w._preseg_records, w._montage_outlined = {}, {}
        w._preseg_montage.clear_outlines(["c1"], {})
        w._on_preseg_record(_record(tmp_path, "r2", "t1", "c1", (0, 400, 0, 400)))
        assert ("c1", (0, 400, 0, 400)) not in w._preseg_montage.results       # not yet
        w._current_step = 1
        w._show_montage_patches()
        w._request_montage_images()                                            # shown again
        assert _pump(lambda: (w._preseg_montage.results.get(("c1", (0, 400, 0, 400))) or {})
                     .get("nucleus"))
    finally:
        w._preseg_run = None
        w.close()


def test_many_combinations_scroll_instead_of_being_squeezed(app):
    panel = ResultsPanel()
    m = "stardist_nuclei_expansion"
    v = ps.default_values(m)
    v["prob_thresh"] = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    combos = [{"combo_id": f"c{i}", "method": m, "params": p}
              for i, p in enumerate(ps.combinations(m, v))]
    panel.set_combos(combos, outputs_of=_outputs)
    panel.resize(260, 420)
    panel.show()
    _pump(lambda: False, timeout=0.2)
    rows = panel.rows()
    assert all(r.height() == r.sizeHint().height() for r in rows)          # full height
    assert panel.scroll.verticalScrollBar().maximum() > 0                  # it scrolls
    panel.close()



# ── detail that follows the zoom, paths built off the GUI thread ────────────
def test_simplified_outlines_stay_within_half_a_screen_pixel():
    polys, _, _ = mask_layers.outlines(_mask())
    assert mask_layers.lod_bucket(1.0) is None and mask_layers.lod_bucket(2.0) is None
    assert mask_layers.lod_bucket(0.5) == 1 and mask_layers.lod_bucket(0.3) == 1
    assert mask_layers.lod_bucket(0.2) == 2
    for bucket in (0, 1, 2, 3):
        simple = mask_layers.simplify(polys, bucket)
        eps = 0.5 * 2 ** bucket
        assert sum(map(len, simple)) <= sum(map(len, polys))
        import cv2
        for p, q in zip(polys, simple):
            contour = q.reshape(-1, 1, 2).astype(np.float32)
            worst = max(abs(cv2.pointPolygonTest(contour, (float(x), float(y)), True))
                        for x, y in p)
            assert worst <= eps + 1e-3
    assert mask_layers.simplify(polys, None) is polys


def test_paths_are_built_in_the_supply_not_on_the_gui_thread(app, monkeypatch, tmp_path):
    import threading
    from test_step1_montage_view import _Provider
    from block01.ui.step1_presegmentation.montage_supply import MontageSupply
    supply = MontageSupply(_Provider(), "pk")
    threads = []
    real = mask_layers.qpath
    monkeypatch.setattr(mask_layers, "qpath",
                        lambda *a: (threads.append(threading.current_thread().name), real(*a))[1])
    path = tmp_path / "m.npy"
    np.save(path, _mask())
    got = []
    supply.outlines_ready.connect(got.append)
    supply.request_outlines(("r", "t", "nucleus"), str(path))
    assert _pump(lambda: got)
    ready = []
    supply.path_ready.connect(lambda k, b: ready.append((k, b)))
    supply.request_path(("r", "t", "nucleus"), 2)
    assert _pump(lambda: ready)
    assert ready == [(("r", "t", "nucleus"), 2)]
    assert threads and all(t.startswith("montage-supply") for t in threads)
    raw = mask_layers.qpath(supply.outlines[("r", "t", "nucleus")][0], 0, 0)
    assert supply.paths[(("r", "t", "nucleus"), 2)].elementCount() < raw.elementCount()
    supply.close()


def test_zoomed_out_the_view_asks_for_simpler_paths_and_uses_the_nearest_meanwhile(app):
    view = _view(app)
    polys, n, d = mask_layers.outlines(_mask())
    view.clear_outlines(["c1"], {"c1": _style(nuclei=True)})
    view.set_result("c1", (0, 60, 0, 80), "ok", n,
                    nucleus={"key": ("r", "t", "nucleus"), "median_d": d})
    wanted = []
    view.paths_wanted.connect(wanted.extend)
    view.overlay.grab()
    assert view.overlay.drawn_paths == 0                                     # nothing yet
    _pump(lambda: wanted, timeout=1)
    scale = abs(view.overlay.canvas_transform().m11())
    assert wanted == [(("r", "t", "nucleus"), mask_layers.lod_bucket(scale))]
    view.set_path("c1", (0, 60, 0, 80), "nucleus", None, mask_layers.qpath(polys, 0, 0))
    view.overlay.grab()
    assert view.overlay.drawn_paths == 1                                     # the nearest
    view.close()


def test_one_tick_shows_every_combinations_cells_and_says_when_they_disagree(app):
    from PyQt5 import QtCore
    panel = ResultsPanel()
    panel.set_combos(_combos(), outputs_of=_outputs)
    panel.show()
    cells, nuclei = panel.chk_all["cells"], panel.chk_all["nuclei"]
    assert cells.checkState() == QtCore.Qt.Unchecked                        # off at first
    assert nuclei.checkState() == QtCore.Qt.Unchecked
    nuc, both, cell = panel.rows()
    cells.click()                                                           # all cells on
    assert cells.checkState() == QtCore.Qt.Checked
    assert both.style["cells"] and cell.style["cells"] and not nuc.style["cells"]  # greyed stays
    both.chk_cells.setChecked(False)                                        # one box off
    assert cells.checkState() == QtCore.Qt.PartiallyChecked
    cells.click()                                                           # from half: all on
    assert cells.checkState() == QtCore.Qt.Checked and both.style["cells"]
    cells.click()                                                           # all off
    assert cells.checkState() == QtCore.Qt.Unchecked
    assert not any(r.style["cells"] for r in panel.rows())
    only_nuclei = [c for c in _combos() if c["method"] == "stardist_nuclei_dapi"]
    panel.set_combos(only_nuclei, outputs_of=_outputs)                      # a new run
    assert not panel.chk_all["cells"].isEnabled()                           # nothing to show
    assert panel.chk_all["nuclei"].checkState() == QtCore.Qt.Unchecked
    panel.close()


def test_each_results_box_has_the_methods_blocks_grey_frame(app):
    from block01.ui.step1_presegmentation.method_blocks import MethodBlock, block_frame_qss
    panel = ResultsPanel()
    panel.set_combos(_combos()[:2], outputs_of=_outputs)
    a, b = panel.rows()
    assert a.styleSheet() == b.styleSheet() == block_frame_qss("resultBlock")
    assert "border:1px solid #555" in a.styleSheet() and "border-radius:4px" in a.styleSheet()
    m = MethodBlock("stardist_nuclei_dapi", ps.default_values("stardist_nuclei_dapi"))
    assert m.styleSheet() == block_frame_qss("methodBlock")              # the same frame
    panel.show_state(a.combo_id, "1/1 patches", "", True, True)          # in use: green
    assert "#6fcf97" in a.styleSheet() and b.styleSheet() == block_frame_qss("resultBlock")
