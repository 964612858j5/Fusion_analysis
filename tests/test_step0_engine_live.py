"""S0-B (docs/v17_step0_engine_plan.md §11): Step0's live TopHat / cuCIM
preview drawn by the Rust engine -- a Python source on the channel's
Original core, its tiles computed by the Python correction code on demand.
The real tab, real stack, a synthetic slide; real GL on the real display.

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

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from block01.ui.step0 import engine_mount as em  # noqa: E402
from block01.ui.step0 import python_source as ps  # noqa: E402

from test_step0_engine_mount import _at, _tab, _wait, app, window  # noqa: E402,F401

H, W = 3000, 2500
NAMES = ["DAPI", "CD3", "CD8"]
BACKGROUND = 2000          # CD3 everywhere ...
DOT = 9000                 # ... plus 3x3 dots every 64 px: TopHat keeps the dots
ORIGINAL = em.RAW_CORE          # S0-C §16.16: one raw core for every channel
LIVE = ("CD3", "tophat", (15,))


@pytest.fixture(autouse=True)
def _rust(monkeypatch):
    monkeypatch.setenv("BLOCK01_STEP1_BACKEND", "rust")
    monkeypatch.delenv("BLOCK01_STEP1_DISPLAY_OMETIFF", raising=False)
    monkeypatch.delenv("BLOCK01_RUST_TEST_FAULT", raising=False)
    monkeypatch.setattr(em, "TEST_CONFIG", {})


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    root = tmp_path_factory.mktemp("s0live")
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
    return str(path), None


@pytest.fixture
def tab(app, window, slide):
    t = _tab(app, window, slide)
    yield t
    t.teardown()


def _show(tab, channel, method, params=()):
    return tab.show_source(channel, method, params, nucleus=None, tint=(1.0, 1.0, 1.0))


def _whole_slide(app, tab):
    tab.stack.view.view_box.setRange(xRange=(0, W), yRange=(0, H), padding=0)
    app.processEvents()


def _engine_on(tab, identity=ORIGINAL):
    e = tab.engine
    return (e is not None and e.picture == em.ENGINE and e.host.current is not None
            and e.host.current.identity == identity)


def _live_on(tab, selection=LIVE):
    if not _engine_on(tab):
        return False
    slot = tab.engine.host.current
    return (slot.adapter is not None and slot.picture is not None and slot.picture >= 0x8000
            and slot.adapter.selection_of(slot.picture) == selection and slot.switch is None)


def _settled(app, tab, timeout=60):
    """The current core's Python work is done: nothing waiting, requested
    or ready, and the core wants nothing more."""
    def done():
        slot = tab.engine.host.current
        st = slot.adapter.stats() if slot.adapter is not None else {}
        return (not st.get("waiting") and not st.get("ready") and not st.get("open_requests")
                and slot.core.python_stats()["handed"] == 0)
    ok = _wait(app, done, timeout)
    if not ok:
        slot = tab.engine.host.current
        print("NOT SETTLED", slot.adapter.stats() if slot.adapter else None,
              dict(slot.core.python_stats()), flush=True)
    return ok


def _capture(host):
    """Every presented frame from now on, as RGBA arrays (copied out right
    after the real blit, in the same paint)."""
    frames = []
    real = host._present

    def capture(width, height):
        real(width, height)
        if not host.isVisible() or host._published is None:
            return
        copy = QtGui.QOpenGLFramebufferObject(width, height, QtGui.QOpenGLFramebufferObjectFormat())
        rect = QtCore.QRect(0, 0, width, height)
        QtGui.QOpenGLFramebufferObject.blitFramebuffer(copy, rect, None, rect)
        image = copy.toImage().convertToFormat(QtGui.QImage.Format_RGBA8888)
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        frames.append(np.frombuffer(ptr, np.uint8).reshape(
            image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()].copy())
    host._present = capture
    return frames


def _background(tab, frame):
    """Medians at two background points, left and right (between dots)."""
    return [int(np.median(_at(tab, frame, x, H // 2 + 16, 2)[..., :3])) for x in (W // 4 + 16, 3 * W // 4 + 16)]


# ── the payload ─────────────────────────────────────────────────────
def test_the_engine_holds_exactly_what_the_python_correction_computes(app, tab):
    """§4 item 3: a Python source's tile in the engine's cache is
    `CorrectionCompute`'s output, byte for byte."""
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    slot = tab.engine.host.current
    adapter, core = slot.adapter, slot.core
    source = adapter.sources[slot.picture]
    c = tab.stack.controller
    checked = 0
    for level in range(c.provider.num_levels):
        h, w = c.provider.level_shape(level)
        for ty in range((h + 511) // 512):
            for tx in range((w + 511) // 512):
                got = core.cached_tile_for_test(slot.picture, level, tx, ty)
                if got is None:
                    continue
                data, rows, cols = got
                expected = c.compute.compute(adapter._key(source, level, tx, ty)).pixels.handle
                assert expected.shape == (rows, cols)
                assert data == np.ascontiguousarray(expected, np.float32).tobytes(), (level, tx, ty)
                checked += 1
    assert checked > 0


def test_the_live_picture_is_the_corrected_one(app, tab):
    _show(tab, "CD3", None)
    _whole_slide(app, tab)
    tab.stack.controller.set_display_mapping(0, DOT, 1.0, channel="CD3")
    assert _wait(app, lambda: _engine_on(tab))
    host = tab.engine.host
    original = host.presented_rgba_for_test()
    assert min(_background(tab, original)) > 30, "the Original background is not bright"
    _show(tab, *LIVE)
    tab.stack.controller.set_display_mapping(0, DOT, 1.0, channel="CD3")
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    live = host.presented_rgba_for_test()
    assert max(_background(tab, live)) <= 6, "the background was not removed"


# ── the switch (§11.5) ──────────────────────────────────────────────
def test_a_switch_never_presents_a_mixed_frame(app, tab):
    _show(tab, "CD3", None)
    _whole_slide(app, tab)
    tab.stack.controller.set_display_mapping(0, DOT, 1.0, channel="CD3")
    assert _wait(app, lambda: _engine_on(tab))
    host = tab.engine.host
    frames = _capture(host)
    _show(tab, *LIVE)
    tab.stack.controller.set_display_mapping(0, DOT, 1.0, channel="CD3")
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    app.processEvents()
    kinds = []
    for frame in frames:
        left, right = _background(tab, frame)
        bright = [v > 30 for v in (left, right)]
        assert bright[0] == bright[1], f"a mixed frame: {left} / {right}"
        kinds.append(bright[0])
    assert kinds and kinds[-1] is False, "the live picture was never presented"
    # once the new picture is up the old one never comes back
    first_new = kinds.index(False)
    assert not any(kinds[first_new:])


def test_while_preparing_the_old_picture_follows_the_camera(app, tab, monkeypatch):
    """§11.5: no frozen frame -- every paint during preparation draws the
    old rows at the current view and publishes them."""
    _show(tab, "CD3", None)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _engine_on(tab))
    host = tab.engine.host
    held = []
    monkeypatch.setattr(ps.PythonSourceAdapter, "_hand_over", lambda self: held.append(1) and False)
    _show(tab, *LIVE)
    assert _wait(app, lambda: host.current.switch is not None, 10)
    before = host.published_view
    vb = tab.stack.view.view_box
    vb.setRange(xRange=(200, W - 200), yRange=(300, H - 300), padding=0)
    assert _wait(app, lambda: host.published_view is not None and host.published_view != before, 10), \
        "the old picture did not follow the camera while the new one was prepared"
    assert host.current.switch is not None and held
    monkeypatch.undo()
    assert _wait(app, lambda: _live_on(tab), 60)


def test_a_newer_selection_replaces_the_one_being_prepared(app, tab):
    _show(tab, "CD3", None)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _engine_on(tab))
    for params in (11, 13, 17, 19):
        _show(tab, "CD3", "tophat", (params,))
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (19,))), 60)
    adapter = tab.engine.host.current.adapter
    assert list(adapter.sources) == [tab.engine.host.current.picture]
    assert _settled(app, tab)
    assert adapter.credit() == (0, 0)


# ── resources (§11.3, §11.4) ────────────────────────────────────────
def test_retired_sources_leave_nothing_and_do_not_grow(app, tab):
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    slot = tab.engine.host.current
    adapter, core = slot.adapter, slot.core
    layers = core.python_stats()["page_layers"]
    retired = []
    for n in range(1000):
        sid = adapter.register("CD3", "cucim", (n + 1,))
        adapter.retire(sid)
        retired.append(sid)
    for params in (21, 23, 25):                  # real switches, with real requests
        _show(tab, "CD3", "tophat", (params,))
        assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (params,))), 60)
    assert _settled(app, tab)
    stats = core.python_stats()
    assert stats["live"] == [slot.picture]
    assert stats["page_layers"] <= layers + 1, stats
    assert all(not core.holds_python_source(sid) for sid in retired[::97])
    assert adapter.credit() == (0, 0)
    # every cancelled S0-B token was forgotten once it ended (other
    # consumers' tokens are theirs)
    assert not [g for g in tab.stack.scheduler._stale_gens if g and g[0] == "s0b"], \
        "cancelled tokens were not forgotten"


def test_the_credit_bounds_the_work_while_panning(app, tab, monkeypatch):
    monkeypatch.setattr(ps, "MAX_TILES", 8)
    _show(tab, *LIVE)
    vb = tab.stack.view.view_box
    vb.setRange(xRange=(0, 600), yRange=(0, 600), padding=0)
    assert _wait(app, lambda: _live_on(tab), 60)
    adapter = tab.engine.host.current.adapter
    peak = 0
    for k in range(30):
        x = (k * 70) % (W - 600)
        vb.setRange(xRange=(x, x + 600), yRange=(x // 2, x // 2 + 600), padding=0)
        app.processEvents()
        peak = max(peak, adapter.credit()[0])
        time.sleep(0.01)
    assert peak <= 8
    assert _settled(app, tab)
    assert adapter.credit() == (0, 0)


# ── lifecycle (§11.7) ───────────────────────────────────────────────
def test_a_production_run_retires_the_sources_and_resume_prepares_again(app, tab):
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    host = tab.engine.host
    old = host.current.picture
    tab.engine.release_for_production()
    assert host.current.adapter.sources == {} and host.current.picture is None
    assert not host.current.core.holds_python_source(old)
    assert host.has_picture, "the published picture should stay"
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert host.current.picture != old
    # again: no retired number survives a release / resume cycle anywhere
    retired = [old, host.current.picture]
    tab.engine.release_for_production()
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    core = host.current.core
    for sid in retired:
        assert not core.holds_python_source(sid), sid
    assert all(int(r[0]) not in retired for r in host.current.shown_rows)


def test_a_fallback_gives_the_controller_its_floor_back(app, tab):
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    c = tab.stack.controller
    assert c.engine_owns_view
    tab.engine.fail("injected")
    assert not c.engine_owns_view and c.viewport_requests_enabled
    # the floor is back for the live selection (drawn or under finer tiles,
    # as the Python picture always decides)
    ok = _wait(app, lambda: c._floor_ready and not c._floor_pending
               and c._floor_ctx == c._current_floor_ctx(c._floor_level, c._floor_stride), 60)
    assert ok, "the Python picture's floor was not restored"
    assert tab.engine.picture == em.PYTHON


def test_a_failing_tile_backs_off_and_finally_hands_over(app, tab, monkeypatch):
    _show(tab, "CD3", None)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _engine_on(tab))
    from block01.viewer import correction_compute

    def broken(self, key):
        raise RuntimeError("injected compute failure")
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", broken)
    monkeypatch.setattr(tab.stack.scheduler.corrected_cache, "get", lambda *_a, **_k: None, raising=False)
    _show(tab, "CD3", "tophat", (27,))
    assert _wait(app, lambda: bool(tab.engine.refused), 60), "the failures never handed over"
    assert tab.engine.picture == em.PYTHON


def test_a_nucleus_switched_on_while_preparing_is_prepared_too(app, tab, monkeypatch):
    """codex S0-B code review 1, P2-2: a row the switch newly brings is a
    row being prepared, never drawn half-loaded into the old picture."""
    nucleus = {"nucleus_channel": "DAPI", "nucleus_tint": (0.0, 0.0, 1.0), "nucleus_enabled": False}
    tab.show_source("CD3", None, (), nucleus=nucleus, tint=(1.0, 1.0, 1.0))
    _whole_slide(app, tab)
    assert _wait(app, lambda: _engine_on(tab))
    host = tab.engine.host
    monkeypatch.setattr(ps.PythonSourceAdapter, "_hand_over", lambda self: False)
    tab.show_source(*LIVE, nucleus=nucleus, tint=(1.0, 1.0, 1.0))
    assert _wait(app, lambda: host.current.switch is not None, 10)
    ov = tab.stack.overlay
    ov.set_enabled(True, host=tab.stack.controller)          # the DAPI switch, as the page flips it
    dapi = tab.engine._index["DAPI"]
    assert _wait(app, lambda: host.current.switch is not None and dapi in host.current.switch[0], 10), \
        (host.current.switch, ov.effective_enabled, ov.calibrated, ov.channel, getattr(ov, "_enabled", None))
    assert all(int(r[0]) != dapi for r in host.current.shown_rows), "DAPI entered the old picture"
    monkeypatch.undo()
    assert _wait(app, lambda: _live_on(tab), 60)


def test_the_picture_being_replaced_keeps_its_gains(app, tab, monkeypatch):
    """codex S0-B code review 1, P2-3: while a new selection is prepared the
    old one is drawn with the gains it was calibrated with."""
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    c = tab.stack.controller
    assert _wait(app, lambda: _live_on(tab) and c._gain_ctx is not None, 60)
    slot = tab.engine.host.current
    old = slot.picture
    before = list(tab.engine._gains_for(slot)[old])
    monkeypatch.setattr(ps.PythonSourceAdapter, "_hand_over", lambda self: False)
    _show(tab, "CD3", "tophat", (23,))
    assert _wait(app, lambda: slot.switch is not None, 10)
    app.processEvents()
    assert tab.engine._gains_for(slot)[old] == before
    # a table of another selection is "not calibrated" for this one
    real = c._gain_ctx
    c._gain_ctx = ("another selection",)
    assert c.current_gains()[0] is None
    c._gain_ctx = real
    monkeypatch.undo()
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (23,))), 60)


def test_completions_while_paused_are_cached_without_starting_reads(app, tab, monkeypatch):
    """codex S0-B code review 3: Step0 left with Python tiles under way --
    they still go in, but no raw read starts until Step0 is back."""
    from block01.viewer import correction_compute
    real = correction_compute.CorrectionCompute.compute

    def slow(self, key):
        time.sleep(0.3)                           # work really under way at the pause
        return real(self, key)
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", slow)
    nucleus = {"nucleus_channel": "DAPI", "nucleus_tint": (0.0, 0.0, 1.0), "nucleus_enabled": True}
    tab.show_source("CD3", None, (), nucleus=nucleus, tint=(1.0, 1.0, 1.0))
    _whole_slide(app, tab)
    assert _wait(app, lambda: _engine_on(tab))
    host = tab.engine.host
    tab.show_source("CD3", "tophat", (29,), nucleus=nucleus, tint=(1.0, 1.0, 1.0))
    adapter = None
    assert _wait(app, lambda: host.current.adapter is not None
                 and host.current.adapter.stats().get("open_requests", 0) > 0, 30)
    adapter, core = host.current.adapter, host.current.core
    tab.engine.set_running(False)                 # Step0 left
    app.processEvents()
    reads = int(core.stats()["reads"])
    handed = adapter.counts["handed"]
    assert _wait(app, lambda: adapter.stats().get("open_requests", 0) == 0
                 and not adapter.stats().get("ready"), 60)
    assert adapter.counts["handed"] > handed, "the work under way did not go in"
    time.sleep(0.3)
    app.processEvents()
    assert int(core.stats()["reads"]) == reads, "a paused core started reading"
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (29,))), 60)


# ── §13: the patch hand-off keeps a picture the cache could publish ──
def _count_computes(monkeypatch):
    from block01.viewer import correction_compute
    real = correction_compute.CorrectionCompute.compute
    calls = []

    def counted(self, key):
        calls.append(key)
        return real(self, key)
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", counted)
    return calls


def _count_paints(monkeypatch, host):
    real = host._paint_slot
    calls = []

    def counted(*a, **k):
        calls.append(a[0])
        return real(*a, **k)
    monkeypatch.setattr(host, "_paint_slot", counted)
    return calls


def test_a_same_label_change_is_published_from_the_cache_and_kept(app, tab, monkeypatch):
    """At the whole slide the coarse radius is scaled (round(r / ds)): 15
    and 16 are the same label there -- the new picture comes from the
    cache, is published before the hand-off returns, and stays on screen
    through the run with no core paint and no computation."""
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    host = tab.engine.host
    computes = _count_computes(monkeypatch)
    requested = host.current.adapter.counts["requested"]
    host.repaint()                                 # the window was composed just now ...
    presented = host.presented_now
    _show(tab, "CD3", "tophat", (16,))             # ... and Enter: the selection, then the hand-off
    assert tab.engine.release_for_production(keep_picture=True) is True
    # on screen before the hand-off returned (the run's thread starts next),
    # although Qt throttles a second composition within one refresh
    assert host.presented_now == presented + 1
    slot = host.current
    assert slot.switch is None and slot.adapter.selection_of(slot.picture) == ("CD3", "tophat", (16,))
    assert slot.adapter.counts["requested"] == requested, "the fill asked Python for work"
    assert host.hidden and slot.adapter.sources, "kept: paused, its sources stay"
    paints = _count_paints(monkeypatch, host)
    frames = _capture(host)
    host.update()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.5:
        app.processEvents()
    assert paints == [], "a core paint during the run"
    assert frames, "the kept picture was not presented"
    assert computes == []
    kept = frames[-1]
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (16,))), 60)
    assert _settled(app, tab)
    after = _capture(host)
    host.update()
    assert _wait(app, lambda: bool(after), 10)
    # the kept picture is the corrected one at its settled pixels
    assert _background(tab, kept) == _background(tab, after[-1])
    assert max(_background(tab, kept)) <= 6, "the kept picture is not the corrected one"


def test_a_change_the_cache_cannot_serve_is_computed_and_published_before_the_hand_off(app, tab, monkeypatch):
    """§14.5 2 / 15.5: a new label is computed right away -- the hand-off
    returns with the new picture published, kept, and on screen."""
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    host = tab.engine.host
    computes = _count_computes(monkeypatch)
    presented = host.presented_now
    _show(tab, "CD3", "tophat", (61,))
    assert tab.engine.release_for_production(keep_picture=True) is True
    assert computes, "nothing was computed before the hand-off"
    assert _live_on(tab, ("CD3", "tophat", (61,))), "the new picture was not published before the run"
    assert host.presented_now == presented + 1, "published but not on screen before the run"
    assert host.hidden and host.current.adapter.sources
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (61,))), 60)
    assert _settled(app, tab)


def test_an_engine_not_running_is_never_kept(app, tab):
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    tab.engine.set_running(False)                  # e.g. the compare view
    assert tab.engine.release_for_production(keep_picture=True) is False
    assert tab.engine.host.current.adapter.sources == {}


def test_requests_cancelled_for_a_kept_hand_off_are_asked_for_once_after(app, tab, monkeypatch):
    """§13.6-13.7: queued work is dropped, running work ends stale, the
    credit comes back; each still-wanted tile is requested exactly once
    when the adapter runs again."""
    from block01.viewer import correction_compute
    real = correction_compute.CorrectionCompute.compute

    def slow(self, key):
        time.sleep(0.2)
        return real(self, key)
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", slow)
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    host = tab.engine.host
    _show(tab, "CD3", "tophat", (37,))
    adapter = host.current.adapter
    assert _wait(app, lambda: adapter.stats().get("open_requests", 0) > 1, 30)
    asked = []
    sched = tab.stack.scheduler
    real_request = sched.request

    def request(req, callback):
        if isinstance(req.generation, tuple) and req.generation[:1] == ("s0b",):
            asked.append(req.generation[2:6])
        return real_request(req, callback)
    monkeypatch.setattr(sched, "request", request)
    adapter.suspend_requests()
    tab.engine.set_running(False)
    assert _wait(app, lambda: adapter.stats().get("open_requests", 0) == 0
                 and not adapter.stats().get("ready"), 60)
    assert asked == [], "a request while paused"
    assert adapter.credit() == (0, 0)
    waiting = adapter.stats()["waiting"]
    assert waiting > 0
    tab.engine.set_running(True)
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (37,))), 60)
    assert _settled(app, tab)
    assert len(asked) == len(set(asked)), "a tile asked for twice"
    assert adapter.credit() == (0, 0) and not adapter._by_tile


def test_paint_now_reaches_the_screen_even_right_after_a_composition(app, tab):
    """codex S13 code review 2: Qt turns a repaint() within one refresh of
    the last composition into a posted update -- `paint_now` delivers it."""
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    host = tab.engine.host
    for _ in range(5):
        host.repaint()
        before = host.presented_now
        assert host.paint_now()
        assert host.presented_now == before + 1


# ── §14.6: the old picture holds still until the new one is as sharp ──
def _slow_fine(monkeypatch, delay, level=None):
    """Corrections of `level` (None: all) take `delay` s longer."""
    from block01.viewer import correction_compute
    real = correction_compute.CorrectionCompute.compute

    def slow(self, key):
        if level is None or key.tile.level == level:
            time.sleep(delay)
        return real(self, key)
    monkeypatch.setattr(correction_compute.CorrectionCompute, "compute", slow)


def test_while_a_switch_waits_the_old_picture_is_presented_unchanged(app, tab, monkeypatch):
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    host = tab.engine.host
    old = _capture(host)
    host.update()
    assert _wait(app, lambda: bool(old), 10)
    reference = old[-1]
    _slow_fine(monkeypatch, 0.4)
    frames = _capture(host)
    _show(tab, "CD3", "tophat", (43,))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.3:
        host.update()
        app.processEvents()
    waiting = list(frames)
    assert waiting, "nothing presented while the switch waited"
    assert all(np.array_equal(f, reference) for f in waiting), "the old picture changed while waiting"
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (43,))), 60)
    assert _settled(app, tab)
    # every frame: the old picture, or the new one complete (no third kind)
    final = frames[-1]
    assert all(np.array_equal(f, reference) or np.array_equal(f, final) for f in frames[:-1]) or \
        all(np.array_equal(f, reference) for f in frames[:-2])


def _deep(app, tab):
    """A view on level 0 (the slide's finest), well inside the slide."""
    tab.stack.view.view_box.setRange(xRange=(800, 1400), yRange=(1000, 1400), padding=0)
    app.processEvents()


def test_a_coarse_stand_in_never_publishes_a_switch(app, tab, monkeypatch):
    """Zoomed in: the gate level (0) is slow, the coarser ones fast -- no
    frame shows the new source until level 0 covers the view."""
    _show(tab, *LIVE)
    _deep(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    host = tab.engine.host
    assert host.published_level == 0, host.published_level
    _slow_fine(monkeypatch, 0.5, level=0)
    switched = []
    real = host.on_switched
    host.on_switched = lambda slot, token: (switched.append(time.monotonic()), real(slot, token))
    t0 = time.monotonic()
    _show(tab, "CD3", "tophat", (47,))
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (47,))), 60)
    assert switched and switched[0] - t0 >= 0.45, "published before the gate level had come"
    assert host.published_level == 0


def test_a_switch_held_by_a_coarser_admission_goes_through_after_the_stall(app, tab, monkeypatch):
    """§14.7: no mouse, no update(): the timer alone lets it through."""
    from block01.ui.step0 import gl_host
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    host = tab.engine.host
    monkeypatch.setattr(gl_host, "GATE_STALL_S", 0.4)
    host.published_level = 0                       # as if the picture on screen were finer
    marks = []
    monkeypatch.setattr(gl_host.perf_trace, "mark", lambda name, **k: marks.append(name))
    _show(tab, "CD3", "tophat", (49,))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.3:
        app.processEvents()
    assert not _live_on(tab, ("CD3", "tophat", (49,))), "went through before the stall"
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (49,))), 10)
    assert "step0.gate_budget_limited" in marks


def test_a_preparation_wider_than_the_credit_still_publishes_before_the_hand_off(app, tab, monkeypatch):
    monkeypatch.setattr(ps, "MAX_TILES", 2)
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    _show(tab, "CD3", "tophat", (53,))
    assert tab.engine.release_for_production(keep_picture=True) is True
    assert _live_on(tab, ("CD3", "tophat", (53,)))
    tab.engine.set_running(True)
    assert _settled(app, tab)


def test_the_occluded_python_scene_is_not_painted_while_the_engine_is_the_picture(app, tab, monkeypatch):
    """§15.5 3: a whole-window repaint does not paint the Python image items
    the engine covers; back on the Python picture they paint again."""
    import pyqtgraph as pg
    painted = []
    real = pg.ImageItem.paint
    monkeypatch.setattr(pg.ImageItem, "paint", lambda self, *a: (painted.append(1), real(self, *a))[1])
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    app.processEvents()
    painted.clear()
    tab.engine.host.window().repaint()
    app.processEvents()
    assert painted == [], "an occluded image item was painted"
    tab.engine.fail("injected")                        # back to the Python picture
    app.processEvents()
    assert tab.engine._scene_hold is None
    assert _wait(app, lambda: bool(painted), 10), "the Python picture does not paint again"


def _waiting_switch(host, token, admitted=2):
    slot = type("S", (), {})()
    slot.switch, slot.admitted = ((), (), token), admitted
    host.current = slot
    return slot


def test_the_budget_exception_publishes_only_at_the_admission_level(app, monkeypatch):
    """codex §15 code 1, P1-2: past the stall a picture goes through only
    covering the view at its admission level, never at any coarser one."""
    from block01.ui.step0 import gl_host
    monkeypatch.setattr(gl_host, "GATE_STALL_S", 0.0)
    host = gl_host.Step0GlHost()
    try:
        owner = gl_host._owner_of(_waiting_switch(host, 1))
        assert host._gate_open(False, False, 2, 0, owner=owner) is False
        assert host._gate_open(False, True, 2, 0, owner=owner) is True
        # nothing on screen: covered at the admission level or not at all
        assert host._gate_open(False, True, 2, None, owner=owner) is False
        assert host._gate_open(True, True, 2, None, owner=owner) is True
    finally:
        host.current = None
        host.deleteLater()


def test_a_new_switch_does_not_inherit_the_stall_clock(app, monkeypatch):
    """codex §15 code 1, P2-4: a switch held back after another one was
    given up waits its own `GATE_STALL_S`."""
    from block01.ui.step0 import gl_host
    monkeypatch.setattr(gl_host, "GATE_STALL_S", 0.2)
    host = gl_host.Step0GlHost()
    try:
        slot = _waiting_switch(host, 1)
        assert host._gate_open(False, True, 2, 0, owner=gl_host._owner_of(slot)) is False
        time.sleep(0.25)
        host.request_switch(slot, (), (), 2)
        assert host._gate_open(False, True, 2, 0, owner=gl_host._owner_of(slot)) is False
        time.sleep(0.25)
        assert host._gate_open(False, True, 2, 0, owner=gl_host._owner_of(slot)) is True
    finally:
        host.current = None
        host.deleteLater()


def test_each_waiting_picture_keeps_its_own_stall_clock(app, monkeypatch):
    """codex §15 code 2, P2-1: an old switch still tried every paint does
    not restart the candidate's clock (nor the candidate the switch's)."""
    from block01.ui.step0 import gl_host
    monkeypatch.setattr(gl_host, "GATE_STALL_S", 0.2)
    host = gl_host.Step0GlHost()
    try:
        switch = gl_host._owner_of(_waiting_switch(host, 1))
        candidate_slot = type("S", (), {"switch": None, "admitted": 2})()
        host.candidate = candidate_slot
        candidate = gl_host._owner_of(candidate_slot)
        assert host._gate_open(False, True, 2, 0, owner=candidate) is False
        for _ in range(5):                                  # the old switch, every paint
            assert host._gate_open(False, False, 2, 0, owner=switch) is False
            assert host._gate_open(False, False, 0, 0, owner=switch) is False
            time.sleep(0.06)
        assert host._gate_open(False, True, 2, 0, owner=candidate) is True
        host.published_level = 0
        assert host.gate_levels(candidate_slot) == (0, 2)
    finally:
        host.current = host.candidate = None
        host.deleteLater()


def test_the_gate_level_is_the_finer_of_screen_and_admission(app, monkeypatch):
    """codex §15 code 1, P1-1: requests go first for the finer of the level
    on screen and the admission; past the stall the admission level too."""
    from block01.ui.step0 import gl_host
    host = gl_host.Step0GlHost()
    try:
        slot = _waiting_switch(host, 1)
        assert host.gate_levels(slot) == (2,)          # nothing on screen
        host.published_level = 0
        assert host.gate_levels(slot) == (0,)
        host.published_level, slot.admitted = 2, 0
        assert host.gate_levels(slot) == (0,)
        host.published_level, slot.admitted = 0, 2
        monkeypatch.setattr(gl_host, "GATE_STALL_S", 0.0)
        host._gate_open(False, False, 2, 0, owner=gl_host._owner_of(slot))
        assert host.gate_levels(slot) == (0, 2)
    finally:
        host.current = None
        host.deleteLater()


def test_paints_beside_the_host_still_reach_the_viewport(app, monkeypatch):
    """codex §15 code 1 P2-5, code 2 P2-2: what lies under the host is not
    painted; the margin around it is, pixel for pixel as before, and
    without painting the image items the host hides."""
    import pyqtgraph as pg
    from PyQt5 import QtTest
    widget = pg.GraphicsLayoutWidget()
    view_box = widget.addViewBox()
    view_box.addItem(pg.ImageItem(np.random.default_rng(0).random((300, 300))))
    widget.resize(400, 300)
    widget.show()
    try:
        assert QtTest.QTest.qWaitForWindowExposed(widget)
        view_box.setRange(xRange=(50, 150), yRange=(50, 150), padding=0)
        app.processEvents()
        viewport = widget.viewport()
        scene = view_box.mapRectToScene(view_box.rect())
        corner = widget.mapFromScene(scene.topLeft())
        cover = QtCore.QRect(corner.x(), corner.y(), int(round(scene.width())), int(round(scene.height())))
        assert QtCore.QRect(viewport.rect()) != cover          # there is a margin
        host = QtWidgets.QWidget(viewport)               # stands where the GL host would
        host.setGeometry(cover)
        host.show()
        viewport.installEventFilter(em._NoPaint(viewport, host, widget, view_box.childGroup))
        painted = []
        real = pg.ImageItem.paint
        monkeypatch.setattr(pg.ImageItem, "paint", lambda self, *a: (painted.append(1), real(self, *a))[1])
        viewport.repaint(cover.adjusted(5, 5, -5, -5))         # wholly under the host
        viewport.repaint()                                      # under it and beside it
        _wait(app, lambda: False, 0.3)                          # the margin's own paint
        assert painted == [], "a hidden image item was painted"
        monkeypatch.setattr(pg.ImageItem, "paint", real)

        def drawn():                     # the view drawn by itself (no paint event)
            image = QtGui.QImage(viewport.size(), QtGui.QImage.Format_ARGB32)
            image.fill(0)
            painter = QtGui.QPainter(image)
            QtWidgets.QGraphicsView.render(widget, painter, QtCore.QRectF(image.rect()), viewport.rect())
            painter.end()
            return image
        shown = drawn()
        view_box.childGroup.setOpacity(0.0)
        hidden = drawn()
        view_box.childGroup.setOpacity(1.0)
        assert shown != hidden                                   # the content is drawn under the host
        seam = cover.adjusted(-1, -1, 1, 1)     # the ViewBox edge falls between pixels
        for y in range(viewport.height()):                      # ... and never in the margin
            for x in range(viewport.width()):
                if not seam.contains(x, y):
                    assert shown.pixel(x, y) == hidden.pixel(x, y), (x, y)
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_a_published_switch_asks_for_the_tiles_held_back(app, tab, monkeypatch):
    """codex §15 code 1, P2-3: the tiles deferred for the switch are asked
    for right after it is published, with no other paint to wake them."""
    asked = []
    real = ps.PythonSourceAdapter.pump_soon
    monkeypatch.setattr(ps.PythonSourceAdapter, "pump_soon", lambda self: (asked.append(1), real(self))[1])
    _show(tab, *LIVE)
    _whole_slide(app, tab)
    assert _wait(app, lambda: _live_on(tab), 60)
    assert _settled(app, tab)
    asked.clear()
    _show(tab, "CD3", "tophat", (51,))
    assert _wait(app, lambda: _live_on(tab, ("CD3", "tophat", (51,))), 60)
    assert asked, "no request round after the switch was published"
