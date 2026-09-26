"""Block 4b: Step3's label supply (`ui/step3_label_binding.py`).

A fake controller (the two camera signals and `snapshot()`) and a fake GPU
layer (records every `set_labels`) around the REAL reader thread and the
real `core.step3_masks` on synthetic projects:

  * only the current level's visible tiles and one ring are asked for,
    centre first; what arrives equals `read_label_tile`;
  * a camera move drops the queued requests it no longer wants (counted);
    a result for an older generation or an unwanted tile is dropped (counted);
  * zooming in and out: the other level's tiles stay, drawn BEFORE the
    target level (farthest first); tiles off the view are released;
  * a coarse level without a pyramid is not requested, and the status says
    why; level 0 still reads; a missing pyramid is built first, then coarse
    levels are served;
  * pause asks for nothing and cancels a build; resume restarts it and
    catches up once; dispose ends the thread and clears the layer;
  * the budget: old levels, then the ring, then the far visible tiles go;
    the thread never holds more than the budget.

Synthetic projects in the test's temporary directory only.
"""

import importlib.util
import os
import threading
import time
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core import label_pyramid as lp  # noqa: E402
from block01.core import step3_masks as sm  # noqa: E402
from block01.ui import step3_label_binding as lb  # noqa: E402
from block01.ui.step1_gpu_layer import LABEL_FILL  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_masks_helpers", os.path.join(os.path.dirname(__file__), "test_step3_masks.py"))
mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mh)

SHAPES = mh.SHAPES                  # (401, 523), (101, 131), (26, 33)
BBOX = mh.BBOX
T = 48


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeController(QtCore.QObject):
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.current = snap(0, 0, set())

    def snapshot(self):
        return self.current

    def move(self, level, tiles, kind="PAN"):
        self.current = snap(self.current.epoch + 1, level, tiles)
        self.interaction_event.emit(kind, self.current)
        return self.current


class FakeLayer:
    labels_enabled = True

    def __init__(self):
        self.snapshots = []

    def set_labels(self, snapshot):
        self.snapshots.append(snapshot)

    @property
    def last(self):
        return self.snapshots[-1]


def snap(epoch, level, tiles):
    return types.SimpleNamespace(epoch=epoch, level=level, visible_tiles=frozenset(tiles),
                                 source="slide")


def wait(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    app.processEvents()
    return cond()


def _sources(tmp_path, pyramid=True, method="cellpose_wholecell_fusion", **kw):
    rdir = mh._workspace(tmp_path)
    run = mh._run(rdir, "seg_x", method, pyramid=pyramid, **kw)
    got = sm.resolve_masks(mh._only(rdir), "ROI_1", BBOX, SHAPES)
    return got, run


def _binding(app, controller=None, layer=None, **kw):
    controller = controller or FakeController()
    layer = layer or FakeLayer()
    made = lb.Step3LabelBinding(controller=controller, layer=layer, level_shapes=SHAPES,
                                tile_size=T, **kw)
    return made, controller, layer


def _idle(binding):
    st = binding.stats()
    return st["queued"] == 0 and st["reading"] == 0


@pytest.fixture
def made(app):
    created = []

    def build(**kw):
        b = _binding(app, **kw)
        created.append(b[0])
        return b
    yield build
    for b in created:
        b.dispose()


# ── what is asked for ────────────────────────────────────────────────────

def test_only_the_visible_tiles_and_one_ring_are_read(app, tmp_path, made):
    srcs, run = _sources(tmp_path)
    b, ctl, layer = made()
    b.set_sources(srcs)
    visible = {(3, 2), (4, 2), (3, 3), (4, 3)}
    ctl.move(0, visible)
    assert wait(app, lambda: _idle(b) and b.stats()["accepted"] == 16)
    keys = {k[3:] for k in b._resident}
    ring = {(x, y) for x in range(2, 6) for y in range(1, 5)} - visible
    assert keys == visible | ring
    assert all(k[2] == 0 and k[1] == "cell" for k in b._resident)
    # what arrived is what `read_label_tile` gives
    cell = srcs["cell"]
    for key, plane in b._resident.items():
        want = sm.read_label_tile(cell, 0, key[3], key[4], T, SHAPES)
        np.testing.assert_array_equal(plane.ids, want.labels)
        assert plane.world_rect == pytest.approx(want.world_rect)
    last = layer.last.layers
    assert [l.kind for l in last] == ["cell"] and len(last[0].planes) == 16


def test_the_centre_is_read_first(app, tmp_path, made, monkeypatch):
    srcs, _ = _sources(tmp_path)
    order, gate = [], threading.Event()
    real = sm.read_label_tile

    def slow(source, level, tx, ty, *a):
        gate.wait(5)
        order.append((tx, ty))
        return real(source, level, tx, ty, *a)
    monkeypatch.setattr(sm, "read_label_tile", slow)
    b, ctl, _ = made()
    b.set_sources(srcs)
    ctl.move(0, {(x, y) for x in range(2, 7) for y in range(2, 5)})
    gate.set()
    assert wait(app, lambda: _idle(b))
    assert order[0] == (4, 3)                           # the middle of the view
    visible_done = order[:15]
    assert set(visible_done) == {(x, y) for x in range(2, 7) for y in range(2, 5)}


def test_a_camera_move_drops_what_it_no_longer_wants(app, tmp_path, made, monkeypatch):
    srcs, _ = _sources(tmp_path)
    gate = threading.Event()
    real = sm.read_label_tile
    monkeypatch.setattr(sm, "read_label_tile",
                        lambda *a: (gate.wait(5), real(*a))[1])
    b, ctl, layer = made()
    b.set_sources(srcs)
    ctl.move(0, {(0, 0), (1, 0)}, kind="NAVIGATOR_JUMP")
    assert wait(app, lambda: b.stats()["reading"] == 1)
    queued_before = b.stats()["queued"]
    ctl.move(0, {(8, 7), (9, 7)}, kind="NAVIGATOR_JUMP")      # far away
    st = b.stats()
    assert st["stale_dropped"] >= queued_before
    gate.set()
    assert wait(app, lambda: _idle(b))
    st = b.stats()
    assert st["late_dropped"] >= 1                     # the one being read on the old spot
    assert {k[3:] for k in b._resident} <= {(x, y) for x in range(7, 11) for y in range(6, 9)}


def test_motion_is_throttled_and_a_quiet_camera_catches_up(app, tmp_path, made):
    srcs, _ = _sources(tmp_path)
    b, ctl, _ = made(motion_interval_ms=10_000)
    b.set_sources(srcs)
    ctl.move(0, {(0, 0)})
    first = b._target_level, set(b._wanted)
    ctl.move(0, {(5, 5)})                              # inside the throttle window
    assert set(b._wanted) == first[1]
    ctl.gesture_quiet.emit(ctl.current)
    assert (b.style("cell").visible and
            {k[3:] for k in b._wanted} >= {(5, 5)})


# ── levels ───────────────────────────────────────────────────────────────

def _level_order(layer_snapshot):
    return [p.identity[2] for p in layer_snapshot.layers[0].planes]


def test_zooming_in_and_out_keeps_the_other_level_under_the_target(app, tmp_path, made,
                                                                   monkeypatch):
    srcs, _ = _sources(tmp_path)
    b, ctl, layer = made()
    b.set_sources(srcs)
    # jumps: planned at once (a ZOOM inside the 33 ms motion window would
    # wait for the throttle)
    ctl.move(1, {(0, 0), (1, 0)}, kind="NAVIGATOR_JUMP")   # level 1 covers x 0..~381
    assert wait(app, lambda: _idle(b))
    gate = threading.Event()
    real = sm.read_label_tile
    monkeypatch.setattr(sm, "read_label_tile", lambda *a: (gate.wait(5), real(*a))[1])
    ctl.move(0, {(2, 1), (3, 1)}, kind="NAVIGATOR_JUMP")  # zoom in: level 0 not here yet
    levels = _level_order(layer.last)
    assert levels and set(levels) == {1}                # the old level fills the screen
    gate.set()
    assert wait(app, lambda: _idle(b))
    levels = _level_order(layer.last)
    assert 0 in levels and 1 in levels
    assert levels == sorted(levels, reverse=True)       # level 1 first, target 0 last
    # zoom out: level 0 now stays under the arriving level 2 target
    gate.clear()
    ctl.move(2, {(0, 0)}, kind="NAVIGATOR_JUMP")
    levels = _level_order(layer.last)
    assert levels and 2 not in levels
    gate.set()
    assert wait(app, lambda: _idle(b))
    levels = _level_order(layer.last)
    assert levels[-1] == 2 and levels.count(2) == 1
    assert levels[:-1] == sorted(levels[:-1], key=lambda l: -abs(l - 2))  # farthest first


def test_tiles_that_leave_the_view_are_released(app, tmp_path, made):
    srcs, _ = _sources(tmp_path)
    b, ctl, _ = made()
    b.set_sources(srcs)
    ctl.move(0, {(0, 0)}, kind="NAVIGATOR_JUMP")
    assert wait(app, lambda: _idle(b))
    ctl.move(0, {(9, 7)}, kind="NAVIGATOR_JUMP")
    assert wait(app, lambda: _idle(b))
    assert all(abs(k[3] - 9) <= 1 and abs(k[4] - 7) <= 1 for k in b._resident)


# ── pyramids ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anyway")
def test_without_a_pyramid_coarse_levels_are_not_asked_for(app, tmp_path, made):
    srcs, run = _sources(tmp_path, pyramid=False)
    os.chmod(run.dir, 0o555)                       # nor can one be built, nor in memory:
    try:
        orig = lp.build_in_memory
        lp.build_in_memory = lambda *a, **k: (_ for _ in ()).throw(MemoryError("test"))
        b, ctl, layer = made()
        b.set_sources(srcs)
        assert wait(app, lambda: not b.mask_status()["cell"]["building"])
        ctl.move(1, {(0, 0), (1, 0)})
        assert wait(app, lambda: _idle(b))
        assert b.stats()["requested"] == 0 and not b._resident
        status = b.mask_status()["cell"]
        assert status["coarse_unavailable"] and "cannot be written" in status["coarse_unavailable"]
        assert status["unavailable_here"]
        ctl.move(0, {(3, 3)})
        assert wait(app, lambda: _idle(b) and b._resident)
        assert all(k[2] == 0 for k in b._resident)
    finally:
        lp.build_in_memory = orig
        os.chmod(run.dir, 0o755)


def test_a_missing_pyramid_is_built_first_then_coarse_levels_show(app, tmp_path, made):
    srcs, run = _sources(tmp_path, pyramid=False)
    b, ctl, layer = made()
    b.set_sources(srcs)
    ctl.move(1, {(0, 0), (1, 0)})
    assert wait(app, lambda: b.stats()["pyramids_built"] == 1)
    assert os.path.isdir(lp.pyramid_path_for(run.mask))
    assert wait(app, lambda: _idle(b) and any(k[2] == 1 for k in b._resident))
    assert b.mask_status()["cell"]["coarse_unavailable"] is None


def test_a_level0_only_source_reads_level_0_while_nothing_is_built(app, tmp_path, made):
    srcs, _ = _sources(tmp_path, method="cellpose_nuclei_dapi")
    assert srcs["cell"] is None and srcs["nucleus"] is not None
    b, ctl, layer = made()
    b.set_sources(srcs)
    ctl.move(0, {(2, 2)})
    assert wait(app, lambda: _idle(b) and b._resident)
    assert {k[1] for k in b._resident} == {"nucleus"}
    assert [l.kind for l in layer.last.layers] == ["nucleus"]


def test_both_masks_are_drawn_nucleus_last(app, tmp_path, made):
    srcs, _ = _sources(tmp_path, method="mesmer_nuclear_guided", nuclei=True)
    b, ctl, layer = made()
    b.set_sources(srcs)
    b.set_style("cell", mode=LABEL_FILL, alpha=0.4)
    ctl.move(0, {(2, 2)})
    assert wait(app, lambda: _idle(b) and len(b._resident) == 18)
    kinds = [l.kind for l in layer.last.layers]
    assert kinds == ["cell", "nucleus"]
    assert layer.last.layers[0].mode == LABEL_FILL and layer.last.layers[0].alpha == 0.4
    pushes = b.stats()["requested"]
    b.set_style("nucleus", visible=False)               # drawing only: nothing read
    assert b.stats()["requested"] == pushes
    assert layer.last.layers[1].visible is False


# ── lifecycle ────────────────────────────────────────────────────────────

def test_pause_asks_for_nothing_and_resume_catches_up(app, tmp_path, made):
    srcs, _ = _sources(tmp_path)
    b, ctl, _ = made()
    b.set_sources(srcs)
    b.pause()
    ctl.move(0, {(3, 3)})
    ctl.gesture_quiet.emit(ctl.current)
    wait(app, lambda: False, timeout=0.2)
    assert b.stats()["requested"] == 0 and not b._resident
    b.resume()
    assert wait(app, lambda: _idle(b) and b._resident)
    assert {k[3:] for k in b._resident} >= {(3, 3)}


def test_pause_cancels_a_build_and_resume_restarts_it(app, tmp_path, made, monkeypatch):
    srcs, run = _sources(tmp_path, pyramid=False)
    started, release = threading.Event(), threading.Event()
    real = lp._write_levels

    def slow(group, src, shapes, bbox, cancel_check):
        started.set()
        release.wait(5)
        return real(group, src, shapes, bbox, cancel_check)
    monkeypatch.setattr(lp, "_write_levels", slow)
    b, ctl, _ = made()
    b.set_sources(srcs)
    assert started.wait(5)
    b.pause()
    release.set()
    assert wait(app, lambda: not b.mask_status()["cell"]["building"])
    dest = lp.pyramid_path_for(run.mask)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".partial")
    assert b.stats()["pyramids_built"] == 0
    b.resume()
    assert wait(app, lambda: b.stats()["pyramids_built"] == 1)
    assert lp.read(dest) is not None


def test_dispose_ends_the_thread_and_clears_the_layer(app, tmp_path):
    srcs, _ = _sources(tmp_path)
    b, ctl, layer = _binding(app)
    b.set_sources(srcs)
    ctl.move(0, {(3, 3)})
    out = b.dispose()
    assert out["thread_alive"] is False
    assert layer.last.layers == ()
    before = b.stats()["accepted"]
    ctl.move(0, {(4, 4)})
    wait(app, lambda: False, timeout=0.2)
    assert b.stats()["accepted"] == before
    assert b.dispose()["already_disposed"]


def test_new_sources_drop_everything_of_the_old_ones(app, tmp_path, made):
    srcs, _ = _sources(tmp_path / "a")
    other, _ = _sources(tmp_path / "b")
    b, ctl, layer = made()
    b.set_sources(srcs)
    ctl.move(0, {(3, 3)})
    b.set_sources(other)
    assert wait(app, lambda: _idle(b) and b._resident)
    assert all(k[0] == b.stats()["generation"] for k in b._resident)
    np.testing.assert_array_equal(
        b._resident[(b.stats()["generation"], "cell", 0, 3, 3)].ids,
        sm.read_label_tile(other["cell"], 0, 3, 3, T, SHAPES).labels)


# ── the budget ───────────────────────────────────────────────────────────

def test_over_budget_the_ring_goes_then_the_far_visible_tiles(app, tmp_path, made):
    srcs, _ = _sources(tmp_path)
    one = T * T * 4
    visible = {(x, y) for x in range(2, 5) for y in range(2, 5)}          # 9 tiles
    b, ctl, layer = made(budget_bytes=12 * one)                           # 9 + part of the ring
    b.set_sources(srcs)
    ctl.move(0, visible)
    assert wait(app, lambda: _idle(b))
    assert {k[3:] for k in b._resident} == visible                        # no ring
    assert b.mask_status()["budget"] is None
    b2, ctl2, _ = made(budget_bytes=5 * one)
    b2.set_sources(srcs)
    ctl2.move(0, visible)
    assert wait(app, lambda: _idle(b2))
    got = {k[3:] for k in b2._resident}
    assert len(got) == 5 and (3, 3) in got
    assert "mask memory is full" in b2.mask_status()["budget"]
    assert b2.stats()["resident_bytes"] <= b2.budget_bytes


def test_the_thread_never_holds_more_than_the_budget(app, tmp_path, made, monkeypatch):
    srcs, _ = _sources(tmp_path)
    one = T * T * 4
    peak = []
    b, ctl, _ = made(budget_bytes=6 * one)
    real = sm.read_label_tile

    def watch(*a):
        with b._cv:
            peak.append(b._resident_bytes + b._pending_bytes)
        return real(*a)
    monkeypatch.setattr(sm, "read_label_tile", watch)
    b.set_sources(srcs)
    ctl.move(0, {(x, y) for x in range(2, 4) for y in range(2, 5)})      # 6 visible
    assert wait(app, lambda: _idle(b))
    assert peak and max(peak) <= 6 * one


def test_undelivered_results_count_against_the_budget(app, tmp_path, made, monkeypatch):
    """The GUI thread does not take results (no event processing) while the
    camera moves on: the thread must stop at the budget, not read on."""
    srcs, _ = _sources(tmp_path)
    one = T * T * 4
    b, ctl, _ = made(budget_bytes=4 * one)
    real = sm.read_label_tile
    held = []

    def watch(*a):
        with b._cv:
            held.append(b._resident_bytes + b._pending_bytes)
        return real(*a)
    monkeypatch.setattr(sm, "read_label_tile", watch)
    b.set_sources(srcs)
    ctl.move(0, {(2, 2), (3, 2), (2, 3), (3, 3)}, kind="NAVIGATOR_JUMP")   # fills the budget
    time.sleep(0.5)                                                         # read, not accepted
    ctl.move(0, {(8, 6), (9, 6), (8, 7), (9, 7)}, kind="NAVIGATOR_JUMP")   # four more wanted
    time.sleep(0.5)
    assert max(held) <= 4 * one
    with b._cv:
        assert b._pending_bytes <= 4 * one
    assert wait(app, lambda: _idle(b) and len(b._resident) == 4)
    assert {k[3:] for k in b._resident} == {(8, 6), (9, 6), (8, 7), (9, 7)}


@pytest.mark.parametrize("width,dpr,radius", [
    (0, 2.0, 0), (1, 1.0, 1), (1, 1.25, 1), (1, 1.5, 2), (1, 2.0, 2),
    (2, 1.5, 3), (3, 1.5, 5), (4, 2.0, 8), (4, 3.0, 8),
])
def test_the_screen_radius_is_the_logical_width_times_the_dpr_rounded(width, dpr, radius):
    from block01.ui.step1_gpu_layer import label_radius
    assert label_radius(width, dpr) == radius                # floor(w * dpr + 0.5), 0..8
