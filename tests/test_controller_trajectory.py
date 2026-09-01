"""Characterization test for the controller's viewport planning output.

Before the migration, test_request_planning.py held differential tests that
compared `ExploreController`'s private methods against the pure functions,
which is what licensed moving each call site. Once a call site delegates,
that comparison degenerates into the new function being compared with
itself, so those tests were removed and this file replaced them: it pins
what LEAVES the controller, and a change in planning behaviour fails here.

Every expected value below was RECORDED from the controller at commit
37a3d9a -- before any planning call site moved -- and then written out as
explicit assertions. None of it is hand-computed. Re-record deliberately if
a behaviour change is ever intended and reviewed.

Four steps, chosen to exercise exactly what this migration touched: an
initial viewport, a pan at the same scale, a zoom that crosses the display
level, and a pan inside the new level. The motion path is driven
EXPLICITLY -- both timers stopped, `_issue_raw_requests` called directly --
so nothing here depends on a timer expiring or on wall-clock timing.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from block01.viewer.tile_types import CorrectionKey, RawKey  # noqa: E402

from test_explore_controller import (  # noqa: E402
    _pump,
    app,            # noqa: F401  (pytest fixture)
    make_controller,
)

# Tile coordinates are (tx, ty). Level 0 here is 4096x4096 with 512px
# tiles, so a 2048x2399 viewport covers 5 columns x 4 rows.
TILES_L0_20 = {(tx, ty) for tx in range(5) for ty in range(4)}
TILES_L0_30 = {(tx, ty) for tx in range(6) for ty in range(5)}
TILES_L1_4 = {(0, 0), (0, 1), (1, 0), (1, 1)}

# (name, requested view range, expected state after the step)
TRAJECTORY = [
    ("initial", (0.0, 0.0, 2048.0, 2048.0), {
        "level": 0, "bbox": (0, 0, 2048, 2399), "tiles": TILES_L0_20,
        "shrinking": False, "zooming": False, "events": ["PAN"],
        "n_raw": 20, "n_correction": 24, "req_levels": {0, 1},
        "generations": {("raw", 1), ("precise", 1)},
        "cancels": [("raw", 0), ("precise", 0)],
    }),
    ("pan", (256.0, 256.0, 2304.0, 2304.0), {
        "level": 0, "bbox": (256, 0, 2304, 2655), "tiles": TILES_L0_30,
        "shrinking": False, "zooming": False, "events": ["PAN"],
        "n_raw": 30, "n_correction": 38, "req_levels": {0, 1},
        # A pan also starts directional prefetch, which is why a third
        # generation appears here and nowhere else.
        "generations": {("raw", 2), ("precise", 2), ("dirprefetch", 1)},
        "cancels": [("raw", 1), ("precise", 1)],
    }),
    ("zoom_crosses_level", (0.0, 0.0, 3600.0, 3600.0), {
        "level": 1, "bbox": (0, 0, 3600, 4096), "tiles": TILES_L1_4,
        "shrinking": False, "zooming": True, "events": ["ZOOM"],
        "n_raw": 4, "n_correction": 4, "req_levels": {1},
        "generations": {("raw", 3), ("precise", 3)},
        # The level change cancels directional prefetch as well, and the
        # zoom gate cancels it a second time -- recorded, not tidied.
        "cancels": [("dirprefetch", 1), ("raw", 2), ("precise", 2),
                    ("dirprefetch", 2)],
    }),
    ("pan_in_new_level", (400.0, 400.0, 4000.0, 4000.0), {
        "level": 1, "bbox": (400, 0, 4000, 4096), "tiles": TILES_L1_4,
        "shrinking": False, "zooming": False, "events": ["PAN"],
        "n_raw": 4, "n_correction": 4, "req_levels": {1},
        "generations": {("raw", 4), ("precise", 4)},
        "cancels": [("raw", 3), ("precise", 3)],
    }),
]


def _gen(generation):
    return tuple(generation) if isinstance(generation, tuple) else generation


def test_controller_trajectory_planning_is_unchanged(app):
    ctrl, _provider, scheduler, view = make_controller(app, settle_ms=30)
    events = []
    ctrl.interaction_event.connect(lambda kind, _snap: events.append(kind))
    try:
        ctrl.load_overview()
        ctrl.set_selection(method="tophat", params=(10,))
        _pump(30)
        ctrl._motion_timer.stop()
        ctrl._settle_timer.stop()

        for name, (y0, x0, y1, x1), want in TRAJECTORY:
            del events[:]
            first_request = len(scheduler.requests)
            first_cancel = len(scheduler.cancelled_generations)

            view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1),
                                   padding=0)
            _pump(5)
            ctrl._motion_timer.stop()
            ctrl._settle_timer.stop()
            ctrl._issue_raw_requests()        # the 30ms tick, explicitly
            _pump(5)
            ctrl._motion_timer.stop()
            ctrl._settle_timer.stop()

            requests = [r for r, _cb in scheduler.requests[first_request:]]
            cancels = [_gen(g) for g
                       in scheduler.cancelled_generations[first_cancel:]]

            assert ctrl.level == want["level"], name
            assert ctrl._current_bbox == want["bbox"], name
            assert ctrl._visible_tiles == want["tiles"], name
            assert ctrl._viewport_shrinking is want["shrinking"], name
            assert ctrl._viewport_zooming is want["zooming"], name
            assert events == want["events"], name

            raw = [r for r in requests if isinstance(r.key, RawKey)]
            correction = [r for r in requests
                          if isinstance(r.key, CorrectionKey)]
            assert len(raw) == want["n_raw"], name
            assert len(correction) == want["n_correction"], name
            assert len(raw) + len(correction) == len(requests), (
                f"{name}: a request carried a key type this test does not "
                "know about")
            assert {r.key.tile.level for r in requests} == want["req_levels"], name
            assert {_gen(r.generation) for r in requests} == want["generations"], name
            assert cancels == want["cancels"], name
    finally:
        ctrl.teardown()
