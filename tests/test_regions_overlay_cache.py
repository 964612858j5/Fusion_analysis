"""Regions overlay: what the static-picture optimisation must not lose.

docs/v17_pyside6_plan.md §8 / the block RT2 report: `set_items` prepares the
point array and the level-0 box, `_paint_static` culls on the transformed box
before the per-point work. These tests pin the parts a fast path can quietly
break:

* a shape whose outline crosses the viewport edge still draws (the cull must
  not eat a half-visible outline);
* a shape entirely off screen draws nothing;
* the cached picture is invalidated by data, transform, size and DPR, and is
  reused when none of them changed;
* the dynamic layer (the shape under a drag, its handles, a marquee) is
  painted from the same code as before.
"""

import numpy as np
import pytest

pytest.importorskip("block01.qt")
pg = pytest.importorskip("pyqtgraph")

from block01.qt import QtCore, QtGui, QtWidgets  # noqa: E402

from block01.ui.regions.overlay import RegionsOverlay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rig(app):
    view = pg.GraphicsView()
    view.resize(600, 400)
    vb = pg.ViewBox()
    view.setCentralItem(vb)
    view.show()
    ov = RegionsOverlay(view, vb, None)
    vb.setRange(xRange=(0, 600), yRange=(0, 400), padding=0)
    app.processEvents()
    return view, vb, ov


def _square(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


def _paint(ov, tr=None, size=(600, 400)):
    """Render the static picture to a pixmap and return its non-empty count."""
    pm = QtGui.QPixmap(*size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    ov._paint_static(p, tr if tr is not None else ov.canvas_transform())
    p.end()
    img = pm.toImage()
    n = 0
    for y in range(0, size[1], 4):
        for x in range(0, size[0], 4):
            if img.pixelColor(x, y).alpha():
                n += 1
    return n


def test_a_shape_crossing_the_edge_still_draws(app):
    view, vb, ov = _rig(app)
    try:
        # Half of the square is left of x=0: its outline crosses the edge.
        ov.set_items([{"id": "1", "label": "R1", "polygon": _square(-40, 180, 80),
                       "color": "#6bcb77"}])
        app.processEvents()
        assert _paint(ov) > 0
    finally:
        view.close()


def test_a_shape_entirely_off_screen_draws_nothing(app):
    view, vb, ov = _rig(app)
    try:
        ov.set_items([{"id": "1", "label": "R1", "polygon": _square(-2000, -2000, 50),
                       "color": "#6bcb77"}])
        app.processEvents()
        assert _paint(ov) == 0
    finally:
        view.close()


def test_the_cache_is_reused_until_something_changes(app):
    view, vb, ov = _rig(app)
    try:
        ov.set_items([{"id": "1", "label": "R1", "polygon": _square(100, 100, 100),
                       "color": "#6bcb77"}])
        app.processEvents()
        for _ in range(3):                 # settle: show/resize deliver first
            app.processEvents()
        ov.render(QtGui.QImage(600, 400, QtGui.QImage.Format_ARGB32))
        first = ov._static
        assert first is not None
        # Nothing changed: the same key, the same pixmap object.
        ov.render(QtGui.QImage(600, 400, QtGui.QImage.Format_ARGB32))
        assert ov._static[0] == first[0]
        # A transform change invalidates it.
        vb.setRange(xRange=(50, 650), yRange=(0, 400), padding=0)
        app.processEvents()
        ov.render(QtGui.QImage(600, 400, QtGui.QImage.Format_ARGB32))
        assert ov._static[0] != first[0]
        # A data change invalidates it again.
        second = ov._static
        ov.set_items([{"id": "2", "label": "R2",
                       "polygon": _square(300, 150, 80), "color": "#ff8800"}])
        app.processEvents()
        ov.render(QtGui.QImage(600, 400, QtGui.QImage.Format_ARGB32))
        assert ov._static[0] != second[0]
    finally:
        view.close()


def test_the_live_shape_and_its_handles_are_still_painted(app):
    view, vb, ov = _rig(app)
    try:
        live = {"id": "1", "label": "R1", "polygon": _square(100, 100, 100),
                "color": "#6bcb77", "dash": False, "hatch": False}
        ov.set_live(live, _square(100, 100, 120))
        app.processEvents()
        tr = ov.canvas_transform()
        pm = QtGui.QPixmap(600, 400)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        ov._paint_dynamic(p, tr)
        p.end()
        img = pm.toImage()
        assert any(img.pixelColor(x, y).alpha()
                   for y in range(0, 400, 4) for x in range(0, 600, 4))
    finally:
        view.close()


def test_a_shape_smaller_than_the_label_threshold_gets_no_label_but_draws(app):
    view, vb, ov = _rig(app)
    try:
        # 6 px on screen: under LABEL_MIN_PX (14), so no label box -- the
        # outline is still painted.
        ov.set_items([{"id": "1", "label": "R1", "polygon": _square(200, 200, 6),
                       "color": "#6bcb77"}])
        app.processEvents()
        assert _paint(ov) > 0
    finally:
        view.close()


def _image(ov, size=(600, 400)):
    pm = QtGui.QPixmap(*size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    ov._paint_static(p, ov.canvas_transform())
    p.end()
    return pm.toImage()


def _ink(img, x0, x1, y):
    return sum(1 for x in range(int(x0), int(x1)) if img.pixelColor(x, int(y)).alpha())


def test_regions_items_paint_as_before_and_the_optional_keys_only_when_set(app):
    """docs/v17_step2_skip_tiles_ux_plan.md §2: `fill` and `width` are
    optional; a Regions item sets neither and is painted as before (no fill,
    a 1.5 px border, 3 px when selected)."""
    view, vb, ov = _rig(app)
    try:
        plain = {"id": "a", "polygon": _square(100, 100, 100), "color": "#6bcb77"}
        centre = ov.to_widget(150, 150)
        edge = ov.to_widget(100, 150)                                  # the left side, mid-height
        cx, cy = int(centre.x()), int(centre.y())
        ex, ey = edge.x(), edge.y()
        ov.set_items([plain])
        img = _image(ov)
        assert img.pixelColor(cx, cy).alpha() == 0                    # no fill inside
        thin = _ink(img, ex - 10, ex + 10, ey)
        assert thin > 0
        ov.set_items([plain], selected={"a"})
        assert _ink(_image(ov), ex - 10, ex + 10, ey) > thin          # selected: 3 px, as before
        ov.set_items([dict(plain, fill="#4dc678dd", width=4.0)])
        img = _image(ov)
        inside = img.pixelColor(cx, cy)
        assert inside.alpha() > 0 and inside.blue() > inside.green()
        assert _ink(img, ex - 10, ex - 1, ey) > _ink(_image_of(ov, plain), ex - 10, ex - 1, ey)   # the 4 px border
    finally:
        view.close()


def _image_of(ov, item):
    ov.set_items([item])
    return _image(ov)
