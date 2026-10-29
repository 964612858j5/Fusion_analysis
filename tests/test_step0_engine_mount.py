"""S0-A (docs/v17_step0_engine_plan.md §10): Step0's full image on the Rust
viewer core -- the real tab, the real stack factory, a synthetic slide and
a synthetic saved product; real GL on the real display. What is checked is
what the host PRESENTS (`grabFramebuffer`), not the core's readback.

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import os
import time

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)
pytest.importorskip("fusion_viewer_core")
tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtGui, QtWidgets  # noqa: E402

from block01.core import saved_correction  # noqa: E402
from block01.ui.step0 import engine_mount as em  # noqa: E402
from block01.ui.step0.step0_explore_tab import Step0ExploreTab  # noqa: E402

H, W = 3000, 2500
NAMES = ["DAPI", "CD3", "CD8"]
RAW_LEFT = 5000            # CD3's raw value in the left half (right half 0)
SAVED = 800                # CD3's saved TopHat 15 product, everywhere
DAPI = 2000                # DAPI's raw value, everywhere


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _rust(monkeypatch):
    monkeypatch.setenv("BLOCK01_STEP1_BACKEND", "rust")
    monkeypatch.delenv("BLOCK01_STEP1_DISPLAY_OMETIFF", raising=False)
    monkeypatch.delenv("BLOCK01_RUST_TEST_FAULT", raising=False)
    monkeypatch.setattr(em, "TEST_CONFIG", {})


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    """3 uint16 channels, 3000 x 2500, 512-px deflate tiles, three levels;
    flat values so a presented pixel can be predicted; a saved TopHat 15
    product for CD3 covering the whole slide."""
    root = tmp_path_factory.mktemp("s0engine")
    path = root / "synth.ome.tif"
    data = np.zeros((3, H, W), np.uint16)
    data[0] = DAPI
    data[1, :, :W // 2] = RAW_LEFT
    data[2] = 300
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": NAMES}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    store = str(root / "corrected_channels.zarr")
    group = zarr.open_group(store, mode="w")
    arr = group.create_group("Full_WSI").create_dataset("CD3", shape=(H, W), chunks=(512, 512), dtype="f4")
    arr[:] = float(SAVED)
    arr.attrs.update({"correction_method": "tophat", "correction_param_value": 15,
                      "source_identity": "synthetic-save-1", "roi_bbox_fullres": [0, H, 0, W],
                      "source_shape": [H, W]})

    class _Loader:
        filepath = str(path)
        _corrected_zarr_path = store
        _corrected_decisions = {"CD3": "tophat"}

    loader = _Loader()
    saved_correction.register_loader(loader)
    yield str(path), loader


def _wait(app, cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def window(app):
    w = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(w)
    w.resize(900, 700)
    w.show()
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def _tab(app, window, slide, nucleus=False):
    path, _loader = slide
    t = Step0ExploreTab()
    window.layout().addWidget(t)
    t.set_dataset(path)
    t._test_nucleus = ({"nucleus_channel": "DAPI", "nucleus_tint": (0.0, 0.0, 1.0),
                        "nucleus_enabled": True} if nucleus else None)
    app.processEvents()
    return t


@pytest.fixture
def tab(app, window, slide):
    t = _tab(app, window, slide)
    yield t
    t.teardown()


def _show(tab, channel, method, params=()):
    return tab.show_source(channel, method, params, nucleus=getattr(tab, "_test_nucleus", None),
                           tint=(1.0, 1.0, 1.0))


def _engine_on(tab, identity=None):
    e = tab.engine
    if e is None or e.picture != em.ENGINE:
        return False
    return identity is None or (e.host.current is not None and e.host.current.identity == identity)


ORIGINAL = ("CD3", None, (), None)
SAVED_ID = ("CD3", "tophat", (15,), "synthetic-save-1")


def _whole_slide(app, tab):
    tab.stack.view.view_box.setRange(xRange=(0, W), yRange=(0, H), padding=0)
    app.processEvents()


def _at(tab, picture, x, y, r=6):
    """The presented pixels around slide point (x, y) (level 0)."""
    from PyQt5 import QtCore
    view = tab.stack.view
    host = tab.engine.host
    scene = view.view_box.mapViewToScene(QtCore.QPointF(x, y))
    pt = view.graphics.mapFromScene(scene) - host.pos()
    ratio = float(host.devicePixelRatioF()) or 1.0
    px, py = int(pt.x() * ratio), int(pt.y() * ratio)
    return picture[max(0, py - r):py + r, max(0, px - r):px + r].astype(int)


def _halves(tab, picture, channel=None):
    """Median of the slide's left-half and right-half centres."""
    sel = (lambda a: a[..., :3]) if channel is None else (lambda a: a[..., channel])
    return (int(np.median(sel(_at(tab, picture, W // 4, H // 2)))),
            int(np.median(sel(_at(tab, picture, 3 * W // 4, H // 2)))))


def _centre(picture):
    h, w = picture.shape[:2]
    return picture[h // 4:3 * h // 4, w // 4:3 * w // 4, :3].astype(int)


def _mapping(tab, lo, hi):
    tab.stack.controller.set_display_mapping(lo, hi, 1.0, channel="CD3")


# ── sources ──────────────────────────────────────────────────────────
def test_original_is_drawn_by_the_engine(app, tab):
    assert _show(tab, "CD3", None)
    assert tab.backend == "rust"
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL)), tab.engine.refused
    host = tab.engine.host
    assert host.candidate is None
    assert not tab.stack.controller.viewport_requests_enabled
    assert host.isVisible()


def _live_on(tab, selection):
    """The engine shows a Python source for `selection` on the current core."""
    e = tab.engine
    if not _engine_on(tab, ORIGINAL if selection[0] == "CD3" else None):
        return False
    slot = e.host.current
    return (slot.adapter is not None and slot.picture is not None and slot.picture >= 0x8000
            and slot.adapter.selection_of(slot.picture) == selection and slot.switch is None)


def test_saved_original_and_live_are_all_engine(app, tab):
    """S0-B (§11.5): a live method is a Python source on the channel's
    Original core -- the engine's picture, the controller only calibrating."""
    assert _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    assert tab.engine.host.candidate is None
    # no saved product for 16: live, on the Original core (the standby)
    _show(tab, "CD3", "tophat", (16,))
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (16,))), 60)
    c = tab.stack.controller
    assert not c.viewport_requests_enabled and c.engine_owns_view
    host = tab.engine.host
    assert host.standby.identity == SAVED_ID
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL) and host.current.picture == 1)
    assert not c.viewport_requests_enabled
    assert len(host.cores()) <= 2


def test_another_channel_with_a_method_has_no_saved_product(app, tab):
    """No saved product: that channel's Original core, a live source on it."""
    assert _show(tab, "CD8", "tophat", (15,))
    assert tab.engine.identity_for("CD8", "tophat", (15,)) == ("CD8", None, (), None)
    assert tab.engine.identity_for("CD3", "cucim", (15,)) == ORIGINAL
    assert _wait(app, lambda: _engine_on(tab, ("CD8", None, (), None))
                 and tab.engine.host.current.picture is not None
                 and tab.engine.host.current.picture >= 0x8000, 60)


def test_python_backend_builds_no_engine(app, tab, monkeypatch):
    monkeypatch.setenv("BLOCK01_STEP1_BACKEND", "python")
    assert _show(tab, "CD3", None)
    assert tab.engine is None and tab.backend == "python"
    assert tab.stack.controller.viewport_requests_enabled


def test_an_injected_stack_factory_builds_no_engine(app, window, slide):
    from block01.ui.step0.step0_explore_tab import build_default_stack

    def factory(*args, **kwargs):
        return build_default_stack(*args, **kwargs)

    t = Step0ExploreTab(stack_factory=factory)
    window.layout().addWidget(t)
    t.set_dataset(slide[0])
    try:
        assert t.show_source("CD3", None)
        assert t.engine is None and t.backend == "python"
    finally:
        t.teardown()


# ── pixels ───────────────────────────────────────────────────────────
def test_presented_pixels_follow_the_mapping(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    _whole_slide(app, tab)
    _mapping(tab, 0, 10000)
    host = tab.engine.host

    def settled():
        app.processEvents()
        left, right = _halves(tab, host.presented_rgba_for_test())
        return abs(left - 127) <= 2 and right <= 1
    assert _wait(app, settled), "left half 5000/10000 -> ~127, right half black"
    # the saved product: 800 of 1000 -> ~204, everywhere
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    _mapping(tab, 0, 1000)

    def saved_shows():
        app.processEvents()
        return abs(int(np.median(_centre(host.presented_rgba_for_test()))) - 204) <= 2
    assert _wait(app, saved_shows)


def test_display_changes_rebuild_no_core(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    _whole_slide(app, tab)
    _mapping(tab, 0, 10000)
    host = tab.engine.host
    slot, count = host.current, host.published_count
    tab.stack.controller.set_tint((1.0, 0.0, 0.0))

    def red():
        app.processEvents()
        p = host.presented_rgba_for_test()
        return _halves(tab, p, 0)[0] > 100 and _halves(tab, p, 1)[0] <= 1
    assert _wait(app, red)
    tab.stack.controller.set_marker_visible(False)
    assert _wait(app, lambda: int(host.presented_rgba_for_test()[..., :3].max()) <= 1)
    tab.stack.controller.set_marker_visible(True)
    _mapping(tab, 0, 5000)
    assert _wait(app, lambda: _halves(tab, host.presented_rgba_for_test(), 0)[0] >= 250)
    assert host.current is slot and host.published_count == count


def test_the_nucleus_is_added_and_never_doubled(app, window, slide):
    t = _tab(app, window, slide, nucleus=True)
    try:
        _show(t, "CD3", None)
        assert _wait(app, lambda: _engine_on(t, ORIGINAL))
        _whole_slide(app, t)
        _mapping(t, 0, 10000)
        overlay = t.stack.overlay
        overlay.set_display_mapping(0, 4000, 1.0)
        host = t.engine.host

        def both():
            app.processEvents()
            p = host.presented_rgba_for_test()
            return abs(_halves(t, p, 2)[1] - 127) <= 3 and _halves(t, p, 0)[1] <= 1
        assert _wait(app, both), "DAPI 2000/4000 in blue where CD3 is 0"
        rows = t.engine._rows_for_display()
        assert len(rows) == 2
        # the marker IS the nucleus: one row, not two
        _show(t, "DAPI", None)
        assert _wait(app, lambda: _engine_on(t, ("DAPI", None, (), None)))
        assert len(t.engine._rows_for_display()) == 1
    finally:
        t.teardown()


# ── publication ──────────────────────────────────────────────────────
def test_a_new_source_replaces_the_old_picture_whole(app, tab):
    """While the saved product's tiles are slow, every presented frame is
    the old picture whole or the new one whole -- never a mixture."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    _whole_slide(app, tab)
    _mapping(tab, 0, 10000)
    host = tab.engine.host
    assert _wait(app, lambda: abs(_halves(tab, host.presented_rgba_for_test())[0] - 127) <= 2)
    em.TEST_CONFIG["test_read_delay_ms"] = 40.0
    _show(tab, "CD3", "tophat", (15,))          # 800 of 10000 -> ~20, everywhere
    frames = []
    end = time.monotonic() + 20
    while time.monotonic() < end and not _engine_on(tab, SAVED_ID):
        app.processEvents()
        frames.append(host.presented_rgba_for_test())
        time.sleep(0.01)
    assert _engine_on(tab, SAVED_ID)
    for _ in range(5):
        app.processEvents()
        frames.append(host.presented_rgba_for_test())
    assert len(frames) > 5
    for p in frames:
        left, right = _halves(tab, p)
        old = abs(left - 127) <= 2 and right <= 1
        new = abs(left - 20) <= 2 and abs(right - 20) <= 2
        assert old or new, (left, right)


def test_moving_the_view_while_preparing_needs_the_new_view_covered(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    view_box = tab.stack.view.view_box
    view_box.setRange(xRange=(0, 600), yRange=(0, 450), padding=0)
    app.processEvents()
    _mapping(tab, 0, 1000)                       # old: 5000 left (255), 0 right; new: 800 (204)
    em.TEST_CONFIG["test_read_delay_ms"] = 60.0
    _show(tab, "CD3", "tophat", (15,))
    host = tab.engine.host
    frames = []
    for xr in ((1800, 2400), (0, 600), (1800, 2400)):        # B -> A -> B, right half: old is black
        view_box.setRange(xRange=xr, yRange=(2400, 2850), padding=0)
        for _ in range(5):
            app.processEvents()
            if host.isVisible():
                frames.append((tuple(view_box.viewRange()[0]), host.presented_rgba_for_test()))
            time.sleep(0.01)
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    for xr, p in frames:
        h, w = p.shape[:2]
        inner = p[h // 4:3 * h // 4, w // 4:3 * w // 4, :3].astype(int)
        lo, hi = int(inner.min()), int(inner.max())
        old = (hi <= 1) if xr[0] >= 1250 else (lo >= 253)
        new = lo >= 201 and hi <= 207
        assert old or new, (xr, lo, hi)
    view, _w, _h = host.published_view
    (x0, x1), (y0, y1) = view_box.viewRange()
    assert host.current.core.view_covered(tuple(view))
    assert abs(view[0] - x0) < 1 and abs(view[1] - x1) < 1


def test_resizing_while_preparing_publishes_at_the_new_size(app, tab, window):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    em.TEST_CONFIG["test_read_delay_ms"] = 60.0
    _show(tab, "CD3", "tophat", (15,))
    window.resize(700, 520)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.01)
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    host = tab.engine.host
    _wait(app, lambda: host.published_view[1:] == host._device_size(), 5)
    view, w, h = host.published_view
    assert (w, h) == host._device_size()
    assert host.current.core.view_covered(tuple(view))


# ── the standby (§10.5.6, user ruling 2026-10-27) ─────────────────────
def test_going_back_resumes_the_previous_core(app, tab, monkeypatch):
    """Original -> saved -> Original -> saved: the second visit of each
    source is the SAME core, resumed -- no core is built for it."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    original = host.current.core
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    saved = host.current.core
    assert host.standby.core is original
    import fusion_viewer_core as fvc
    built = []
    real = fvc.ViewerCore
    monkeypatch.setattr(fvc, "ViewerCore", lambda *a, **k: built.append(1) or real(*a, **k))
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    assert host.current.core is original and host.standby.core is saved
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    assert host.current.core is saved and host.standby.core is original
    assert built == []
    assert len(host.cores()) == 2


def test_original_live_original_stays_on_one_core(app, tab, monkeypatch):
    """S0-B: Original -> a live method -> Original never builds a core:
    the switches are prepared and published on the Original core."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    original = host.current.core
    import fusion_viewer_core as fvc
    built = []
    real = fvc.ViewerCore
    monkeypatch.setattr(fvc, "ViewerCore", lambda *a, **k: built.append(1) or real(*a, **k))
    _show(tab, "CD3", "tophat", (16,))
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (16,))), 60)
    assert host.current.core is original and host.isVisible()
    _show(tab, "CD3", None)
    assert _wait(app, lambda: host.current.picture == 1 and host.current.switch is None, 30)
    assert host.current.core is original and built == []
    assert host.current.adapter.sources == {}, "the live source was not retired"


def test_a_resumed_core_shows_what_changed_while_it_was_parked(app, tab, window):
    """Original parked as the standby; meanwhile the colour, the range and
    the window size change; back to Original: the resumed core's first
    presented picture has the new colour and range at the new size."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    _whole_slide(app, tab)
    _mapping(tab, 0, 10000)
    host = tab.engine.host
    original = host.current.core
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    assert host.standby.core is original
    tab.stack.controller.set_tint((1.0, 0.0, 0.0))
    _mapping(tab, 0, 5000)
    window.resize(760, 600)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.01)
    _whole_slide(app, tab)
    tab.show_source("CD3", None, (), tint=(1.0, 0.0, 0.0))     # the page passes the colour along
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    assert host.current.core is original
    assert _wait(app, lambda: host.published_view[1:] == host._device_size(), 5)

    def red_full():
        app.processEvents()
        p = host.presented_rgba_for_test()
        return (_halves(tab, p, 0)[0] >= 250 and _halves(tab, p, 1)[0] <= 1
                and _halves(tab, p, 0)[1] <= 1)
    assert _wait(app, red_full)


def test_a_third_source_drops_the_standby_first(app, tab):
    """Original (current) + saved (standby) -> another channel: the standby
    goes before the new core is built -- never three."""
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    assert host.standby is not None and len(host.cores()) == 2
    em.TEST_CONFIG["test_read_delay_ms"] = 60.0
    _show(tab, "CD8", None)
    seen = []
    assert _wait(app, lambda: seen.append(len(host.cores())) or host.candidate is not None
                 or _engine_on(tab, ("CD8", None, (), None)))
    assert max(seen) <= 2
    assert host.standby is None or host.standby.identity != SAVED_ID
    assert _wait(app, lambda: _engine_on(tab, ("CD8", None, (), None)))
    assert len(host.cores()) <= 2 and host.standby.identity == ORIGINAL


def test_a_failure_leaves_no_standby(app, tab, monkeypatch):
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    tab.engine.fail("injected")
    assert host.cores() == [] and tab.engine.picture == em.PYTHON


def test_at_most_two_cores(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    em.TEST_CONFIG["test_read_delay_ms"] = 80.0
    host = tab.engine.host
    for _ in range(3):
        _show(tab, "CD3", "tophat", (15,))
        assert len(host.cores()) <= 2
        _show(tab, "CD3", None)
        assert len(host.cores()) <= 2
        app.processEvents()


def test_the_first_published_frame_is_whole(app, window, slide):
    """Python picture first (a live method), then Original: the host stays
    hidden until its first picture covers the view."""
    t = _tab(app, window, slide)
    try:
        _show(t, "CD3", "tophat", (16,))
        assert t.engine is not None and t.engine.picture == em.PYTHON
        _whole_slide(app, t)
        _mapping(t, 0, 10000)
        em.TEST_CONFIG["test_read_delay_ms"] = 40.0
        host = t.engine.host
        # the FIRST frame Qt presents: right after the real blit of the first
        # visible `paintGL`, the widget's own framebuffer is copied out (in
        # the same paint; nothing paints again in between)
        from PyQt5 import QtCore
        first = []
        real_present = host._present

        def capture(width, height):
            real_present(width, height)
            if first or not host.isVisible() or host._published is None:
                return
            copy = QtGui.QOpenGLFramebufferObject(width, height, QtGui.QOpenGLFramebufferObjectFormat())
            rect = QtCore.QRect(0, 0, width, height)
            QtGui.QOpenGLFramebufferObject.blitFramebuffer(copy, rect, None, rect)
            image = copy.toImage().convertToFormat(QtGui.QImage.Format_RGBA8888)
            ptr = image.constBits()
            ptr.setsize(image.byteCount())
            first.append(np.frombuffer(ptr, np.uint8).reshape(
                image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()].copy())
        host._present = capture
        _show(t, "CD3", None)
        seen_hidden = False
        end = time.monotonic() + 20
        while not _engine_on(t, ORIGINAL) and time.monotonic() < end:
            app.processEvents()
            if not host.isVisible():
                seen_hidden = True
                assert t.stack.controller.viewport_requests_enabled   # Python stays whole
            time.sleep(0.002)
        assert seen_hidden and _engine_on(t, ORIGINAL)
        assert _wait(app, lambda: bool(first)), "no visible frame was presented"
        # the very first presented frame is the whole new picture, over the
        # whole slide (a grid of points in each half, not one sample)
        for fx in (0.05, 0.15, 0.25, 0.35, 0.45):
            for fy in (0.05, 0.3, 0.5, 0.7, 0.95):
                left = int(np.median(_at(t, first[0], fx * W, fy * H, 2)[..., :3]))
                right = int(np.median(_at(t, first[0], (fx + 0.5) * W, fy * H, 2)[..., :3]))
                assert abs(left - 127) <= 2 and right <= 1, (fx, fy, left, right)
        view, _w, _h = host.published_view
        assert host.current.core.view_covered(tuple(view))
    finally:
        t.teardown()


def test_engine_to_live_starts_the_floor(app, tab):
    """§11.6: the engine draws a live method, the controller only
    calibrates -- the floor and its gains still complete, nothing is drawn
    into the Python layers, and the gains reach the engine's rows."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    c = tab.stack.controller
    for params in ((16,), (15,)):            # (15,) via the saved engine picture first
        if params == (15,):
            _show(tab, "CD3", "tophat", (15,))
            assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
            params = (17,)
        _show(tab, "CD3", "tophat", params)
        assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", params)), 60)
        assert c.engine_owns_view
        assert _wait(app, lambda: not c._floor_pending and not c._floor_job_running
                     and c._floor_ctx == c._current_floor_ctx(c._floor_level, c._floor_stride), 60), \
            "the live method's floor never completed"
        assert c.stats["gain_calibrated"], "its gain calibration never completed"
        assert not c.view.corrected_floor_item.isVisible(), "the floor was drawn under the engine"
        slot = tab.engine.host.current
        assert tab.engine._gains_for(slot).get(slot.picture), "the gains did not reach the engine"


# ── failures ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("fault", ["paint:3", "all:200"])
def test_a_failing_candidate_hands_over_to_python(app, tab, monkeypatch, fault):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    monkeypatch.setenv("BLOCK01_RUST_TEST_FAULT", fault)
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: bool(tab.engine.refused), 30), "no fallback"
    assert tab.engine.picture == em.PYTHON
    assert tab.stack.controller.viewport_requests_enabled
    assert tab.engine.host.cores() == []
    # not tried again for this dataset
    monkeypatch.delenv("BLOCK01_RUST_TEST_FAULT")
    _show(tab, "CD3", None)
    app.processEvents()
    assert tab.engine.picture == em.PYTHON


def test_a_failing_current_core_never_publishes_a_ready_candidate(app, tab, monkeypatch):
    """The current core's next paint fails in the same `paintGL` that would
    advance a candidate: nothing is published, the Python picture takes over."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    count = host.published_count
    session = host.current.session
    real = session.paint
    monkeypatch.setattr(session, "paint", lambda *a: dict(real(*a), error="injected"))
    advanced = []
    original_advance = host._advance_candidate
    monkeypatch.setattr(host, "_advance_candidate", lambda *a: advanced.append(1) or original_advance(*a))
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: bool(tab.engine.refused))
    assert host.published_count == count
    assert not advanced, "a candidate was advanced in a paint whose current core failed"
    assert tab.engine.picture == em.PYTHON


def test_a_candidate_that_cannot_attach_hands_over(app, tab, monkeypatch):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    from block01.ui.core_gl_session import CoreGlSession
    made = []
    real_init = CoreGlSession.__init__

    def track(self, *a, **k):
        real_init(self, *a, **k)
        made.append(self)

    def broken(self, _context):
        raise RuntimeError("no GL names")
    monkeypatch.setattr(CoreGlSession, "__init__", track)
    monkeypatch.setattr(CoreGlSession, "attach_gl", broken)
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: bool(tab.engine.refused))
    assert "could not start drawing" in tab.engine.refused
    # the host's own cleanup ran: the half-made core is closed, its sockets too
    assert made and made[-1]._closed and made[-1]._sockets is None
    assert host.candidate is None
    assert tab.engine.picture == em.PYTHON and tab.stack.controller.viewport_requests_enabled


def test_a_channel_whose_coarse_never_fits_hands_over(app, tab, monkeypatch):
    """A per-channel coarse budget too small for the slide: the channel is
    `unavailable` and could never be drawn -- no endless wait."""
    em.TEST_CONFIG["max_coarse_bytes_per_channel"] = 1024
    _show(tab, "CD3", None)
    assert _wait(app, lambda: bool(tab.engine.refused), 30), "waited forever"
    assert "cannot be drawn" in tab.engine.refused or "exceeds" in tab.engine.refused
    assert tab.engine.picture == em.PYTHON


def test_capacity_exceeded_hands_over(app, tab, monkeypatch):
    """Step0 draws at most two channels, so a real sampler shortage is out of
    reach here (Step1's test makes one with eight corrected channels); the
    report the core gives then is injected into the candidate's paint."""
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    real_add = host.add_candidate

    def add(*a, **k):
        slot = real_add(*a, **k)
        real = slot.session.paint
        slot.session.paint = lambda *pa: dict(real(*pa), capacity_exceeded=True)
        return slot
    monkeypatch.setattr(host, "add_candidate", add)
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: bool(tab.engine.refused), 30)
    assert "GPU blocks" in tab.engine.refused
    assert tab.engine.picture == em.PYTHON


def test_an_roi_only_product_is_not_used(app, tab, slide):
    path, loader = slide
    arr = zarr.open_group(loader._corrected_zarr_path, mode="r+")["Full_WSI"]["CD3"]
    arr.attrs["roi_bbox_fullres"] = [0, H // 2, 0, W]
    saved_correction.register_loader(loader)           # a new Save
    try:
        _show(tab, "CD3", "tophat", (15,))
        app.processEvents()
        # not the saved product: live, on the Original core
        assert tab.engine.identity_for("CD3", "tophat", (15,)) == ORIGINAL
        assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (15,))), 60)
    finally:
        arr.attrs["roi_bbox_fullres"] = [0, H, 0, W]
        saved_correction.register_loader(loader)


# ── lifecycle ────────────────────────────────────────────────────────
def test_hidden_and_production_stop_the_engine(app, tab, window):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    engine = tab.engine
    tab.hide()
    app.processEvents()
    assert not engine.running and engine.host.hidden
    # a production run that ends while the view is hidden must not wake it
    tab.release_for_production("test run")
    assert not engine.running
    tab.resume_from_production()
    assert not engine.running
    tab.show()
    assert _wait(app, lambda: engine.running)
    assert not engine.host.hidden
    # shown while a run is still going: stays stopped
    tab.release_for_production("test run")
    assert not engine.running
    tab.hide()
    tab.show()
    app.processEvents()
    assert not engine.running
    tab.resume_from_production()
    assert engine.running
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))


def test_a_new_dataset_closes_the_engine(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    engine = tab.engine
    host = engine.host
    tab.set_dataset(None)
    assert tab.engine is None and engine.host is None
    assert host._disposed


def test_memory_accounting_counts_every_core_and_framebuffer(app, tab):
    _show(tab, "CD3", None)
    assert _wait(app, lambda: _engine_on(tab, ORIGINAL))
    host = tab.engine.host
    em.TEST_CONFIG["test_read_delay_ms"] = 200.0
    _show(tab, "CD3", "tophat", (15,))
    assert _wait(app, lambda: host.candidate is not None and host.candidate.fbo is not None
                 and host.candidate.core.gpu_bytes() > 0)
    assert len(host.cores()) == 2

    def exact():
        cores = sum(int(s.core.gpu_bytes()) for s in host.cores())
        fbos = [f for f in [host._published] + [s.fbo for s in host.cores()] if f is not None]
        return fbos and host.gpu_bytes() == cores + sum(f.width() * f.height() * 4 for f in fbos)
    assert exact()
    # with a standby: the paused core is counted too
    assert _wait(app, lambda: _engine_on(tab, SAVED_ID))
    assert host.standby is not None and host.standby.core.gpu_bytes() > 0
    assert exact()


def test_quitting_closes_the_engine(app, window, slide):
    t = _tab(app, window, slide)
    _show(t, "CD3", None)
    assert _wait(app, lambda: _engine_on(t, ORIGINAL))
    host = t.engine.host
    app.aboutToQuit.emit()
    assert t.engine is None and host._disposed
