"""S0-C (docs/v17_step0_engine_plan.md §16): the compare strip's three panels
drawn by the Rust viewer core on the full image's GL host -- the real tab and
strip in one stacked window, a synthetic slide, real GL on the real display.

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

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.ui.step0 import engine_mount as em  # noqa: E402
from block01.ui.step0.compare_strip import CompareStrip  # noqa: E402
from block01.ui.step0.step0_explore_tab import Step0ExploreTab  # noqa: E402

from test_step0_engine_mount import _wait, app  # noqa: E402,F401

H, W = 3000, 2500
NAMES = ["DAPI", "CD3", "CD8"]
BACKGROUND = 2000          # CD3 everywhere (plus dots); CD8 300 flat
DOT = 9000


@pytest.fixture(autouse=True)
def _rust(monkeypatch):
    monkeypatch.setenv("BLOCK01_STEP1_BACKEND", "rust")
    monkeypatch.delenv("BLOCK01_STEP1_DISPLAY_OMETIFF", raising=False)
    monkeypatch.delenv("BLOCK01_RUST_TEST_FAULT", raising=False)
    monkeypatch.setattr(em, "TEST_CONFIG", {})


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    root = tmp_path_factory.mktemp("s0cmp")
    path = root / "dots.ome.tif"
    data = np.zeros((3, H, W), np.uint16)
    data[0] = 1500
    data[1] = BACKGROUND
    for y in range(32, H, 64):
        for x in range(32, W, 64):
            data[1, y - 1:y + 2, x - 1:x + 2] = DOT
    data[2] = 300
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": NAMES}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _params_for(source):
    return (15,) if source == "tophat" else (20,)


class _Rig:
    """The page's arrangement: the full image and the strip on one stacked
    page of one window, the strip borrowing the full image's host."""

    def __init__(self, app, path):
        self.app = app
        self.window = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(self.window)
        self.stacked = QtWidgets.QStackedWidget(self.window)
        lay.addWidget(self.stacked)
        self.tab = Step0ExploreTab()
        self.strip = CompareStrip()
        self.stacked.addWidget(self.tab)
        self.stacked.addWidget(self.strip)
        self.window.resize(1200, 600)
        self.window.show()
        self.tab.set_dataset(path)
        app.processEvents()
        self.tab.show_source("CD3", None, (), nucleus=None, tint=(1.0, 1.0, 1.0))
        self.tab.stack.view.view_box.setRange(xRange=(0, W), yRange=(0, H), padding=0)
        assert _wait(app, lambda: self.full_on(), 30), "the full image never came up on the engine"
        self.strip.set_dataset(path)
        self.strip.set_engine_host_provider(self._host)

    def _host(self):
        engine = self.tab.engine
        return None if engine is None or engine.refused else engine.host

    def full_on(self):
        e = self.tab.engine
        return e is not None and e.picture == em.ENGINE

    def enter(self):
        """As `_enter_compare_mode`: built first, switched after, resumed."""
        stacks = self.strip.ensure_built("CD3", params_for=_params_for, tint=(1.0, 1.0, 1.0))
        assert stacks is not None
        self.stacked.setCurrentWidget(self.strip)
        self.strip.resume()
        self.app.processEvents()
        self.strip.set_display_mapping(0, DOT, 1.0, channel="CD3")
        self.strip.set_display_mapping(0, DOT, 1.0, channel="CD8")
        box = self.strip.view_boxes[0]
        self.app.processEvents()
        self.strip.set_camera(W / 2.0, H / 2.0, float(box.width()) / W)
        return stacks

    def leave(self):
        self.strip.suspend("full image")
        self.stacked.setCurrentWidget(self.tab)
        self.app.processEvents()

    @property
    def engine(self):
        return self.strip.engine

    def compare_on(self):
        e = self.engine
        return e is not None and e.picture == em.ENGINE and e.shows_wanted()

    def close(self):
        self.strip.teardown()
        self.tab.teardown()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()


@pytest.fixture
def rig(app, slide):
    r = _Rig(app, slide)
    yield r
    r.close()


def _states(engine):
    """Per panel, what its published picture is: (channel, method, params)."""
    out = []
    for mount in engine.mounts:
        slot = mount.panel.current
        if slot is None or mount.panel.published is None:
            out.append(None)
            continue
        if slot.adapter is not None and slot.picture is not None and slot.picture >= 0x8000:
            out.append(slot.adapter.selection_of(slot.picture))
        else:
            names = {i: n for n, i in mount._index.items()}
            out.append((names.get(slot.picture), None, ()))
    return out


def _record(host, engine):
    """The three panels' published states at every presented frame."""
    seen = []
    real = host._present

    def present(width, height):
        real(width, height)
        if host.isVisible() and host.mode == "compare":
            seen.append(_states(engine))
    host._present = present
    return seen


# ── coming up (§16.4 4) ─────────────────────────────────────────────
def test_the_three_panels_come_up_on_the_engine_together(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90), "the compare panels never came up on the engine"
    host = rig.engine.host
    assert host.mode == "compare" and host.layout == rig.engine.panels
    assert host.full.paused, "the full image's panel still runs under the strip"
    assert all(p.published is not None for p in rig.engine.panels)
    assert [s[0] for s in _states(rig.engine)] == ["CD3"] * 3
    assert [s[1] for s in _states(rig.engine)] == [None, "tophat", "cucim"]
    # the panels' rectangles are their ViewBoxes', side by side
    rects = [p.rect for p in rig.engine.panels]
    assert rects[0].right() < rects[1].left() and rects[1].right() < rects[2].left()


# ── one publication (§16.5) ─────────────────────────────────────────
def test_a_channel_switch_publishes_all_three_together(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    seen = _record(rig.engine.host, rig.engine)
    published = []
    rig.strip.publication_changed.connect(lambda: published.append(rig.strip.displayed_channel))
    rig.strip.set_channel("CD8", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert rig.strip.pending and rig.strip.displayed_channel == "CD3"
    assert _wait(app, lambda: rig.compare_on() and not rig.strip.pending, 90)
    app.processEvents()
    assert rig.strip.displayed_channel == "CD8" and published == ["CD8"]
    channels = [tuple(s[0] if s else None for s in states) for states in seen]
    assert channels, "no frame was presented"
    for row in channels:
        assert row in (("CD3",) * 3, ("CD8",) * 3), f"a mixed frame: {row}"
    assert channels[-1] == ("CD8",) * 3
    first = channels.index(("CD8",) * 3)
    assert all(row == ("CD8",) * 3 for row in channels[first:]), "the old picture came back"


def test_a_parameter_edit_switches_its_panel_alone(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    before = _states(rig.engine)
    edited = []
    rig.strip.params_published.connect(lambda: edited.append(1))
    rig.strip.set_params(lambda s: (25,) if s == "tophat" else (20,), method="tophat")
    assert _wait(app, lambda: rig.compare_on() and _states(rig.engine)[1] == ("CD3", "tophat", (25,)), 90)
    after = _states(rig.engine)
    assert after[0] == before[0] and after[2] == before[2]
    assert _wait(app, lambda: bool(edited), 5), "the page was not told to measure again"


def test_prepare_now_puts_an_edit_on_screen_before_returning(app, rig):
    """§16.13 2: the edit is synced in this turn and published -- no event
    loop between the edit and the check."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    rig.strip.set_params(lambda s: (21,) if s == "tophat" else (20,), method="tophat")
    assert rig.engine.prepare_now() is True
    assert _states(rig.engine)[1] == ("CD3", "tophat", (21,))


# ── full <-> compare (§16.6, 16.13 3) ───────────────────────────────
def test_back_and_forth_keeps_both_pictures(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    host = rig.engine.host
    rig.leave()
    assert host.mode == "full" and host.parent() is rig.tab.stack.view.graphics.viewport()
    assert _wait(app, rig.full_on, 30), "the full image did not come back on the engine"
    assert all(p.paused for p in rig.engine.panels)
    rig.stacked.setCurrentWidget(rig.strip)
    rig.strip.resume()
    app.processEvents()
    assert host.mode == "compare"
    assert _wait(app, rig.compare_on, 30), "the compare panels did not come back"


def test_a_failure_hands_both_modes_to_python(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    session = rig.engine.panels[1].current.session
    real = session.paint
    session.paint = lambda *a: dict(real(*a), error="injected")
    rig.strip.set_channel("CD8", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert _wait(app, lambda: bool(rig.tab.engine.refused), 30)
    assert _wait(app, lambda: not rig.strip.pending, 10), "the strip still waits for the engine"
    assert rig.engine.picture == "python"
    assert rig.strip.displayed_channel == "CD8"


def test_the_full_image_closing_first_closes_the_panels(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    engine = rig.engine
    cores = [s.core for p in engine.panels for s in p.cores()]
    assert cores
    rig.tab.teardown()                                  # disposes the host
    assert engine._closed and not any(p.cores() for p in engine.panels)


def test_each_panel_is_presented_in_its_rectangle(app, rig):
    """§16.4 3: Original's raw background bright in the first rectangle,
    TopHat's removed in the second; between them the views' background."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    host = rig.engine.host

    def ok():
        app.processEvents()
        frame = host.presented_rgba_for_test()
        rects = [p.rect for p in rig.engine.panels]

        def median_at(rect):
            cy, cx = rect.center().y(), rect.center().x()
            # between dots: the dots repeat every 64 slide px
            patch = frame[cy - 2:cy + 3, cx - 2:cx + 3, :3]
            return int(np.median(patch))
        bright, removed = median_at(rects[0]), median_at(rects[1])
        y = rects[0].center().y()
        # the view's own margin just right of the first panel, and the
        # strip's spacing halfway between the first two
        margin = frame[y, rects[0].right() + 3, :3].astype(int)
        spacing = frame[y, (rects[0].right() + rects[1].left()) // 2, :3].astype(int)

        def rgb(colour):
            return np.array([colour.red(), colour.green(), colour.blue()])
        view_bg = rgb(rig.strip.views[0].graphics.backgroundBrush().color())
        panes = rig.strip.panes_widget
        strip_bg = rgb(panes.palette().color(panes.backgroundRole()))
        return (bright > 30 and removed < bright // 3
                and np.all(np.abs(margin - view_bg) <= 2) and np.all(np.abs(spacing - strip_bg) <= 2))
    assert _wait(app, ok, 60), "a panel or a gap shows the wrong picture"


# ── codex S0-C code review 1 ────────────────────────────────────────
def test_back_to_the_channel_on_screen_before_the_other_was_published(app, rig, monkeypatch):
    """P2-1: A -> B -> A while B is still being prepared: done at once, no
    pending left behind."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    em.TEST_CONFIG["test_read_delay_ms"] = 200.0       # B cannot be ready at once
    rig.strip.set_channel("CD8", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    app.processEvents()
    rig.strip.set_channel("CD3", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert _wait(app, lambda: not rig.strip.pending, 30), "a target already on screen stays pending"
    assert rig.strip.displayed_channel == "CD3" and rig.compare_on()


def test_a_failure_while_building_hands_both_modes_to_python(app, slide, monkeypatch):
    """P2-2: a compare core that cannot be made (not a failed paint) sends
    every mount on the host to the Python picture, the full image's too."""
    r = _Rig(app, slide)
    try:
        import fusion_viewer_core as fvc

        def broken(*_a, **_k):
            raise RuntimeError("injected")
        monkeypatch.setattr(fvc, "ViewerCore", broken)
        stacks = r.strip.ensure_built("CD3", params_for=_params_for, tint=(1.0, 1.0, 1.0))
        assert stacks is not None
        r.stacked.setCurrentWidget(r.strip)
        r.strip.resume()
        assert _wait(app, lambda: bool(r.tab.engine.refused), 20), "the full image was not told"
        engine = r.engine
        assert engine is None or engine.picture == "python"
    finally:
        r.close()


def test_the_paused_panels_stay_counted_on_the_full_image(app, rig):
    """P2-3: back on the full image, the compare panels' cores and pictures
    are still the host's (memory, context)."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    host = rig.engine.host
    rig.leave()
    assert host.layout == [host.full]
    held = set(id(s) for p in rig.engine.panels for s in p.cores())
    assert held and held <= set(id(s) for s in host._all_cores())
    assert all(p in host.panels() for p in rig.engine.panels)


def test_nothing_is_measured_while_an_edit_waits(app, rig):
    """P2-4: while the edited panel's new picture is not published the
    strip is pending (the page plans no measurement then)."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    em.TEST_CONFIG["test_read_delay_ms"] = 200.0
    rig.strip.set_params(lambda s: (31,) if s == "tophat" else (20,), method="tophat")
    app.processEvents()
    seen = []
    assert _wait(app, lambda: seen.append(rig.strip.pending) or (
        rig.compare_on() and _states(rig.engine)[1] == ("CD3", "tophat", (31,))), 90)
    assert True in seen, "never pending while the edit was prepared"
    assert not rig.strip.pending


@pytest.fixture(scope="module")
def saved_slide(tmp_path_factory):
    """The dots slide with a saved TopHat 15 product for CD3 (as the full
    image's tests make one)."""
    zarr = pytest.importorskip("zarr")
    from block01.core import saved_correction
    root = tmp_path_factory.mktemp("s0cmpsaved")
    path = root / "dots.ome.tif"
    data = np.zeros((3, H, W), np.uint16)
    data[0] = 1500
    data[1] = BACKGROUND
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
    arr[:] = 800.0
    arr.attrs.update({"correction_method": "tophat", "correction_param_value": 15,
                      "source_identity": "synthetic-save-cmp", "roi_bbox_fullres": [0, H, 0, W],
                      "source_shape": [H, W]})

    class _Loader:
        filepath = str(path)
        _corrected_zarr_path = store
        _corrected_decisions = {"CD3": "tophat"}

    loader = _Loader()
    saved_correction.register_loader(loader)
    yield str(path)


def test_an_enter_from_a_saved_result_is_prepared_too(app, saved_slide):
    """P2-5: TopHat shows the saved product (its own core); a new radius is
    a candidate raw core -- prepared and published before `prepare_now`
    returns, like an edit on the same core."""
    r = _Rig(app, saved_slide)
    try:
        r.enter()
        assert _wait(app, r.compare_on, 90)
        tophat = r.engine.mounts[1]
        assert tophat.panel.current.identity[1] == "tophat", "the saved product is not drawn"
        r.strip.set_params(lambda s: (16,) if s == "tophat" else (20,), method="tophat")
        assert r.engine.prepare_now() is True
        assert _states(r.engine)[1] == ("CD3", "tophat", (16,))
        assert tophat.panel.current.identity == em.RAW_CORE
    finally:
        r.close()


def test_an_edit_taken_back_is_measured_again(app, rig, monkeypatch):
    """codex S0-C code review 2: 15 -> 25 -> 15 before 25 is published --
    no publication comes, the page is still told to measure again."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    from block01.viewer import correction_compute
    real = correction_compute.CorrectionCompute.compute

    def slow(self, key):
        time.sleep(0.3)
        return real(self, key)
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", slow)
    told = []
    rig.strip.params_published.connect(lambda: told.append(1))
    rig.strip.set_params(lambda s: (25,) if s == "tophat" else (20,), method="tophat")
    assert _wait(app, lambda: rig.engine.waiting(), 10), "25 never waited"
    rig.strip.set_params(lambda s: (15,) if s == "tophat" else (20,), method="tophat")
    assert _wait(app, lambda: bool(told) and not rig.strip.pending, 30), "never measured again"
    assert _states(rig.engine)[1] == ("CD3", "tophat", (15,))


# ── the camera (user acceptance, 2026-10-29) ────────────────────────
def test_channel_switches_keep_the_magnification(app, rig):
    """A run of channel switches leaves the three panels where they were:
    the words "preparing" coming and going must not change the panels'
    size and walk the view out."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    box = rig.strip.view_boxes[0]
    sizes = []
    box.sigResized.connect(lambda b: sizes.append((b.width(), b.height())))
    rig.strip.set_camera(W / 2.0, H / 2.0, 4.0 * float(box.width()) / W)
    app.processEvents()
    before = rig.strip.camera(0)
    for channel in ["CD8", "CD3"] * 4:
        rig.strip.set_channel(channel, params_for=_params_for, tint=(1.0, 1.0, 1.0))
        assert _wait(app, lambda: rig.compare_on() and not rig.strip.pending, 90)
        app.processEvents()
    after = rig.strip.camera(0)
    assert sizes == [], f"the panels changed size: {sizes}"
    assert after[2] == pytest.approx(before[2], rel=1e-3), f"zoom walked: {before} -> {after}"
    assert after[0] == pytest.approx(before[0], abs=1.0) and after[1] == pytest.approx(before[1], abs=1.0)


def test_a_panel_published_early_keeps_its_python_picture_live(app, rig, monkeypatch):
    """User acceptance 2026-10-29: until all three have a picture, the
    Python panels are the picture -- one published early must not stop
    reading or repainting (it froze for 20 s under a drag)."""
    # the cuCIM panel starts its core 3 s late (as one whose overview comes
    # late): the other two have pictures first
    real_sync = em.Step0EngineMount.sync
    t0 = time.monotonic()

    def late(self):
        if getattr(self.panel, "name", "") == "compare2" and time.monotonic() - t0 < 3.0:
            QtCore.QTimer.singleShot(100, self.sync)
            return
        return real_sync(self)
    monkeypatch.setattr(em.Step0EngineMount, "sync", late)
    rig.enter()
    seen = []

    def look():
        e = rig.engine
        if e is None or not e.usable:
            return False
        early = [m for m in e.mounts if m.panel.published is not None]
        if early and not e.host.isVisible():
            seen.append([m._scene_hold is None for m in early])
        return e.host.isVisible()
    assert _wait(app, look, 90), "the panels never came up on the engine"
    assert seen, "no panel was published before the others (the test saw nothing)"
    assert all(all(row) for row in seen), "a panel's Python picture stopped before the host showed"
    app.processEvents()
    assert all(m._scene_hold is not None for m in rig.engine.mounts), "the Python panels still paint under the host"


# ── prepared ahead (codex review 2026-10-30, a trial) ───────────────
def _prepare_ahead(app, rig, channel="CD3"):
    """As the page's `_prepare_compare_ahead`: built on the full image's
    page, put away without waiting, the engine's cores painted off screen."""
    built = rig.strip.built
    assert rig.strip.ensure_built(channel, params_for=_params_for, tint=(1.0, 1.0, 1.0)) is not None
    rig.strip.set_display_mapping(0, DOT, 1.0, channel="CD3")
    rig.strip.set_display_mapping(0, DOT, 1.0, channel="CD8")
    if not built:
        rig.strip.suspend("full image", badge="paused", wait=False)
    assert rig.strip.prepare_engine_ahead(400, 500)
    engine = rig.engine
    assert engine is not None and engine._ahead is not None
    return engine


def _ahead_done(engine):
    return engine._ahead is None and all(m.ahead_ready() for m in engine.mounts)


def _choose_on_full_image(app, rig, channel):
    """As a row click on the full image: the full image first, then the
    put-away strip (`_sync_full_image_to_channel`, `_sync_compare_to_channel`)."""
    rig.tab.show_source(channel, None, (), nucleus=None, tint=(1.0, 1.0, 1.0))
    assert _wait(app, lambda: rig.full_on() and rig.tab.engine.shows_wanted(), 30)
    rig.strip.set_channel(channel, params_for=_params_for, tint=(1.0, 1.0, 1.0))


def _enter_prepared(app, rig):
    rig.stacked.setCurrentWidget(rig.strip)
    rig.strip.resume()
    app.processEvents()


def test_prepared_ahead_the_entry_shows_the_panels_at_once(app, rig):
    engine = _prepare_ahead(app, rig)
    assert _wait(app, lambda: _ahead_done(engine), 60), "the panels were never prepared"
    assert not rig.strip.isVisible() and rig.tab.engine.host.mode == "full"
    assert all(m.panel.paused for m in engine.mounts), "the prepared cores were not paused"
    _enter_prepared(app, rig)
    assert engine.host.isVisible() and engine.picture == em.ENGINE, "the entry waited"
    assert [s[0] for s in _states(engine)] == ["CD3"] * 3


def test_a_channel_chosen_after_preparing_is_never_shown_under_the_old_picture(app, rig):
    """P2-3: prepared for CD3, CD8 chosen on the full image, then entered
    with nothing prepared again -- the host waits for CD8 (the Python
    panels are the picture), it never puts CD3 up under CD8's labels."""
    engine = _prepare_ahead(app, rig)
    assert _wait(app, lambda: _ahead_done(engine), 60)
    _choose_on_full_image(app, rig, "CD8")
    seen = _record(engine.host, engine)
    _enter_prepared(app, rig)
    assert _wait(app, lambda: rig.compare_on() and bool(seen), 90), "the panels never came up with CD8"
    channels = [tuple(s[0] if s else None for s in states) for states in seen]
    assert channels and all(row == ("CD8",) * 3 for row in channels), f"another picture was shown: {channels[:3]}"


def test_a_channel_chosen_after_preparing_is_prepared_again(app, rig):
    engine = _prepare_ahead(app, rig)
    assert _wait(app, lambda: _ahead_done(engine), 60)
    _choose_on_full_image(app, rig, "CD8")
    ready = [(m.controller.channel, m.ahead_ready()) for m in engine.mounts]
    assert ready == [("CD8", False)] * 3, ready
    # (the page waits for the full image to go still; the record is shared)
    assert _wait(app, lambda: rig.strip.prepare_engine_ahead(400, 500), 30)
    assert _wait(app, lambda: _ahead_done(engine), 60), "the switch was not prepared off screen"
    assert [s[0] for s in _states(engine)] == ["CD8"] * 3
    _enter_prepared(app, rig)
    assert engine.host.isVisible() and engine.shows_wanted(), "the entry waited"


def test_moving_the_full_image_stops_the_paints_ahead(app, rig):
    engine = _prepare_ahead(app, rig)
    assert rig.strip.stop_engine_ahead() is True
    assert engine._ahead is None and all(m.panel.paused for m in engine.mounts)
    assert rig.strip.stop_engine_ahead() is False
    # started again, it finishes what was begun
    assert rig.strip.prepare_engine_ahead(400, 500)
    assert _wait(app, lambda: _ahead_done(engine), 60)


def test_a_production_run_while_preparing_takes_the_hidden_panels_work_too(app, rig):
    """P2-1: the paints ahead stop, the panels' Python sources go and their
    tiles are given back, and the strip's work is waited out."""
    engine = _prepare_ahead(app, rig)
    assert _wait(app, lambda: any(s.adapter is not None and s.adapter.sources
                                  for m in engine.mounts for s in m.panel.cores()), 60)
    done = rig.strip.release_hidden_for_production("test run")
    assert engine._ahead is None
    assert all(not (s.adapter is not None and s.adapter.sources)
               for m in engine.mounts for s in m.panel.cores()), "a Python source still reads"
    assert done.wait(30), "the strip's work never ended"


def test_the_hand_off_waiters_end_with_the_stacks(app, rig):
    """P2-2: the put-away's waiter threads give up once the stacks are torn
    down (a scheduler shut down never calls them back)."""
    import threading
    _prepare_ahead(app, rig)
    gone = rig.strip.stacks_gone()
    assert gone() is False
    rig.strip.teardown()
    assert gone() is True

    def waiters():
        return [t for t in threading.enumerate() if t.name == "explore-production-handoff"]
    assert _wait(app, lambda: not waiters(), 10), f"{len(waiters())} waiter thread(s) still alive"


def test_a_switch_left_unpublished_is_superseded_by_a_hidden_choice(app, rig):
    """Codex round 2 P2: CD3 shown, CD8 chosen, compare left before CD8 is
    published, DAPI chosen on the full image -- the strip shows and says
    DAPI on the way back, never CD8."""
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    rig.strip.set_channel("CD8", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert rig.strip._engine_target is not None
    rig.leave()
    _choose_on_full_image(app, rig, "DAPI")
    assert rig.strip._engine_target is None
    _enter_prepared(app, rig)
    assert _wait(app, lambda: rig.compare_on() and not rig.strip.pending, 90)
    app.processEvents()
    assert rig.strip.displayed_channel == "DAPI"
    assert [s[0] for s in _states(rig.engine)] == ["DAPI"] * 3


def test_an_entry_on_screen_releases_nothing_hidden(app, rig):
    rig.enter()
    assert _wait(app, rig.compare_on, 90)
    assert rig.strip.release_hidden_for_production("test run").is_set()
    assert rig.compare_on()


# ── the slide opened ahead, off the GUI thread (Fable 5b) ─────────────
@pytest.fixture
def opens(monkeypatch):
    """Every open of the slide: the thread it ran on and what it returned,
    whose `close` is counted."""
    from block01.viewer import source_tile_provider as stp
    real = stp.open_viewer_source
    seen = []

    def counted(path, *a, **k):
        import threading
        provider = real(path, *a, **k)
        closes = []
        orig_close = provider.close

        def close():
            closes.append(1)
            return orig_close()

        provider.close = close
        seen.append((threading.current_thread().name, provider, closes))
        return provider

    monkeypatch.setattr(stp, "open_viewer_source", counted)
    return seen


def test_the_slide_opened_ahead_is_the_one_the_build_uses(app, rig, opens):
    ready = []
    rig.strip.source_ready.connect(lambda: ready.append(1))
    assert rig.strip.open_source_ahead() is True
    assert rig.strip.opening_source
    assert rig.strip.open_source_ahead() is True, "a second open was started"
    assert _wait(app, lambda: ready, 30), "the open never came back"
    assert [name for name, _p, _c in opens] == ["compare-open-source"], opens
    assert rig.strip.open_source_ahead() is False, "opened, but it would open again"

    stacks = rig.strip.ensure_built("CD3", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert stacks is not None
    assert len(opens) == 1, "the build opened the slide again"
    assert stacks.provider is opens[0][1]
    assert rig.strip.open_source_ahead() is False


def test_an_open_overtaken_by_the_build_or_a_new_dataset_is_closed(app, rig, opens):
    import threading

    # the user enters compare while the slide is still being opened
    assert rig.strip.open_source_ahead() is True
    stacks = rig.strip.ensure_built("CD3", params_for=_params_for, tint=(1.0, 1.0, 1.0))
    assert stacks is not None
    assert _wait(app, lambda: len(opens) == 2 and not any(
        t.name == "compare-open-source" for t in threading.enumerate()), 30)
    app.processEvents()
    late = [p for name, p, c in opens if name == "compare-open-source"]
    assert len(late) == 1 and stacks.provider is not late[0]
    assert [c for name, p, c in opens if p is late[0]][0], "the late slide was left open"

    # ...and one that landed unused is closed with the dataset it was for
    rig.strip.teardown()
    del opens[:]
    assert rig.strip.open_source_ahead() is True
    assert _wait(app, lambda: rig.strip._opened_source is not None, 30)
    rig.strip.set_dataset(None)
    assert opens[0][2], "an unused opened slide outlived its dataset"


def test_a_strip_deleted_while_its_slide_opens_closes_the_slide(app, slide, opens, monkeypatch):
    """The open lands after the strip is gone: no crash, the slide closed
    (codex r4 P1: the strip's own signal was emitted on a deleted object)."""
    import threading
    import time as _time
    from block01.viewer import source_tile_provider as stp

    counted = stp.open_viewer_source

    def slow(path, *a, **k):
        _time.sleep(0.4)
        return counted(path, *a, **k)

    monkeypatch.setattr(stp, "open_viewer_source", slow)
    strip = CompareStrip()
    strip.set_dataset(slide)
    assert strip.open_source_ahead() is True
    strip.deleteLater()
    app.processEvents()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    del strip
    assert _wait(app, lambda: opens and not any(
        t.name == "compare-open-source" for t in threading.enumerate()), 30)
    assert _wait(app, lambda: opens[0][2], 10), "the slide of a deleted strip was left open"
