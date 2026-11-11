"""Step1 Pre-segmentation: the Methods list scrolls (backlog 9,
docs/v17_step1_methods_scroll_plan.md §4).

The tab's own column: Patches, Methods and Results, each in the product's
section box, Results with stretch 1. Every check is made once the layout has
settled. A block is never lower than its height for the width; the list
either scrolls or ends at its last block; the plan buttons and Run / Stop
stay in the column; the wheel over a block's own widgets scrolls the list;
a new, merged or edited block comes into view.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtCore, QtGui, QtWidgets  # noqa: E402
from block01.utils import segmentation_param_schema as ps  # noqa: E402

SDX = "stardist_nuclei_expansion"
HYB = "hybrid_cellpose_stardist"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Editor:
    """Stands in for the method dialog (the panel's `editor_class` seam)."""
    queue = []

    def __init__(self, parent=None, method=None, values=None, lock_method=False):
        self._method, self._values = self.queue.pop(0)

    def exec(self):
        return QtWidgets.QDialog.Accepted

    def method(self):
        return self._method

    def values(self):
        return self._values


def _settle(app, root, rounds=200):
    """Process events until the column's geometry stops changing."""
    last = None
    for _ in range(rounds):
        app.processEvents()
        now = tuple((w.geometry().getRect(), w.isVisible())
                    for w in root.findChildren(QtWidgets.QWidget))
        if now == last:
            return
        last = now
    raise AssertionError("the layout never settled")


def _column(app, monkeypatch, n_methods=0, results=False):
    from block01.ui.main_window import MainWindow
    from block01.ui.step1_presegmentation import method_blocks as mb
    from block01.ui.step1_presegmentation.patches_panel import PatchesPanel
    from block01.ui.step1_presegmentation.results_panel import ResultsPanel
    monkeypatch.setattr(mb.MethodsPanel, "editor_class", _Editor)
    root = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(root)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    methods = mb.MethodsPanel()
    res = ResultsPanel()
    box = MainWindow._section_box                 # the product's own frame
    lay.addWidget(box(None, "Patches", PatchesPanel()))
    lay.addWidget(box(None, "Methods", methods))
    lay.addWidget(box(None, "Results", res), 1)
    for m in (list(ps.UI_METHODS) * 2)[:n_methods]:
        methods._add_block(m, ps.default_values(m))
    methods._refresh()
    if results:
        v = ps.default_values(SDX)
        v["prob_thresh"] = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        res.set_combos([{"combo_id": f"c{i}", "method": SDX, "params": p}
                        for i, p in enumerate(ps.combinations(SDX, v))],
                       outputs_of=lambda _m: ("cell", "nucleus"))
    root.show()
    return root, methods


def _in_column(root, w):
    r = QtCore.QRect(w.mapTo(root, QtCore.QPoint(0, 0)), w.size())
    return w.isVisible() and r.height() > 0 and root.rect().contains(r)


def _check(root, methods):
    scroll = methods.list_scroll
    blocks = methods.blocks()
    for b in blocks:                                           # never squeezed
        assert b.height() >= b.heightForWidth(b.width()), b.method
    if blocks and scroll.verticalScrollBar().maximum() == 0:   # no empty band
        assert scroll.viewport().height() <= blocks[-1].geometry().bottom() + 1
    for b in (methods.btn_add, methods.btn_save, methods.btn_load, methods.total,
              methods.btn_run, methods.btn_stop):
        assert _in_column(root, b), b.text()


def _wheel_event(widget):
    pos = QtCore.QPointF(widget.width() / 2, widget.height() / 2)
    return QtGui.QWheelEvent(pos, QtCore.QPointF(widget.mapToGlobal(pos.toPoint())),
                             QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
                             QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                             QtCore.Qt.NoScrollPhase, False)


def _wheel(app, widget, viewport):
    """A wheel turn over `widget`. Qt hands a real turn to the widget under
    the cursor and on to its parents until one takes it; offscreen it cannot
    be made a real one, so each step is checked: every widget from `widget`
    up to the list leaves the turn, and the list then scrolls."""
    w = widget
    while w is not viewport:
        ev = _wheel_event(w)
        app.sendEvent(w, ev)
        assert not ev.isAccepted(), type(w).__name__
        w = w.parentWidget()
        assert w is not None, "the widget is not in the list"
    app.sendEvent(viewport, _wheel_event(viewport))
    app.processEvents()


# ── §4.1 many methods through width and height changes ─────────────────────

@pytest.mark.parametrize("results", [False, True])
def test_many_methods_scroll_and_are_never_squeezed(app, monkeypatch, results):
    root, methods = _column(app, monkeypatch, n_methods=8, results=results)
    try:
        scrolled = False
        for size in ((180, 1400), (500, 1400), (260, 1400),
                     (260, 500), (260, 1400), (260, 600)):
            root.resize(*size)
            _settle(app, root)
            _check(root, methods)
            scrolled |= methods.list_scroll.verticalScrollBar().maximum() > 0
        assert scrolled                       # a short column scrolls the list
    finally:
        root.close()


# ── §4.2, §4.3 few methods, none ───────────────────────────────────────────

def test_a_few_methods_take_their_height_and_no_more(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=2)
    try:
        root.resize(260, 1400)
        _settle(app, root)
        _check(root, methods)
        scroll = methods.list_scroll
        assert scroll.verticalScrollBar().maximum() == 0
        assert scroll.height() == methods.blocks()[-1].geometry().bottom() + 1
    finally:
        root.close()


def test_no_method_hides_the_list_and_says_so(app, monkeypatch):
    root, methods = _column(app, monkeypatch)
    try:
        root.resize(260, 900)
        _settle(app, root)
        assert not methods.list_scroll.isVisible()
        assert methods._empty.isVisible()
        _check(root, methods)
    finally:
        root.close()


# ── §4.4 the wheel over a block's own widgets ──────────────────────────────

def test_the_wheel_over_a_block_scrolls_the_list(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(260, 600)
        _settle(app, root)
        bar = methods.list_scroll.verticalScrollBar()
        assert bar.maximum() > 0 and bar.value() == 0
        vp = methods.list_scroll.viewport()
        _wheel(app, methods.blocks()[0].title, vp)
        assert bar.value() > 0
        first = bar.value()
        visible = next(b for b in methods.blocks()
                       if methods.list_scroll.viewport().rect().contains(
                           b.btn_edit.mapTo(methods.list_scroll.viewport(), QtCore.QPoint(1, 1))))
        _wheel(app, visible.btn_edit, vp)
        assert bar.value() > first
    finally:
        root.close()


# ── §4.5 a new, merged or tall block comes into view ───────────────────────

def _fully_in_view(methods, w):
    vp = methods.list_scroll.viewport()
    r = QtCore.QRect(w.mapTo(vp, QtCore.QPoint(0, 0)), w.size())
    return vp.rect().contains(r)


def test_a_new_block_comes_into_view(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(260, 600)
        _settle(app, root)
        assert methods.list_scroll.verticalScrollBar().value() == 0
        _Editor.queue[:] = [(SDX, ps.default_values(SDX))]
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
        blk = methods.add_method()                     # No: a block of its own
        assert blk is methods.blocks()[-1]
        _settle(app, root)
        assert _fully_in_view(methods, blk)
        _check(root, methods)
    finally:
        root.close()


def test_a_merge_into_a_block_out_of_view_brings_it_into_view(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(260, 600)
        _settle(app, root)
        bar = methods.list_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())                    # the first block is out of view
        _settle(app, root)
        first = methods.blocks()[0]
        assert not _fully_in_view(methods, first)
        _Editor.queue[:] = [(first.method, ps.default_values(first.method))]
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
        assert methods.add_method() is first           # merged into it
        _settle(app, root)
        assert _fully_in_view(methods, first)
    finally:
        root.close()


def test_a_block_taller_than_the_list_shows_its_top_line(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(180, 500)
        _settle(app, root)
        tall = max(methods.blocks(), key=lambda b: b.height())
        assert tall.height() > methods.list_scroll.viewport().height()
        methods.list_scroll.verticalScrollBar().setValue(0)
        _settle(app, root)
        _Editor.queue[:] = [(tall.method, tall.values)]
        methods._edit(tall.uid)                         # Edit, values kept
        _settle(app, root)
        for w in (tall.title, tall.btn_edit, tall.btn_remove):
            assert _fully_in_view(methods, w)
    finally:
        root.close()


# ── §4.6 an Edit that wraps more grows the list ────────────────────────────

def test_an_edit_that_wraps_more_grows_the_list(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=1)
    try:
        root.resize(260, 1400)
        _settle(app, root)
        blk = methods.blocks()[0]
        before = methods.list_scroll.height()
        values = dict(blk.values)
        long_list = [s for s in ps.specs(blk.method) if s.kind != "choice"]
        key = long_list[0].key
        values[key] = [round(0.05 * i, 2) for i in range(1, 19)]
        _Editor.queue[:] = [(blk.method, values)]
        methods._edit(blk.uid)
        _settle(app, root)
        assert methods.list_scroll.height() > before
        _check(root, methods)
    finally:
        root.close()


def test_an_edit_that_merges_into_a_block_out_of_view_brings_it_into_view(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(260, 600)
        _settle(app, root)
        bar = methods.list_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        _settle(app, root)
        first, last = methods.blocks()[0], methods.blocks()[-1]
        assert first.method != last.method and not _fully_in_view(methods, first)
        _Editor.queue[:] = [(first.method, ps.default_values(first.method))]
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
        methods._edit(last.uid)                        # Edit onto the first's method: merge
        _settle(app, root)
        assert last not in methods.blocks() and len(methods.blocks()) == 7
        assert _fully_in_view(methods, first)
        _check(root, methods)
    finally:
        root.close()


def test_a_block_removed_before_its_scroll_runs_is_left_alone(app, monkeypatch):
    root, methods = _column(app, monkeypatch, n_methods=8)
    try:
        root.resize(260, 600)
        _settle(app, root)
        _Editor.queue[:] = [(SDX, ps.default_values(SDX))]
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
        blk = methods.add_method()                     # its scroll is queued
        methods._remove(blk.uid)                       # gone before it runs
        _settle(app, root)
        assert blk not in methods.blocks() and len(methods.blocks()) == 8
        _check(root, methods)
    finally:
        root.close()
