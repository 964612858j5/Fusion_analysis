"""Step0's Save keeps the picture (the user's item 2, 2026-11-12;
docs/v17_acceptance_notes_plan.md B18): the hand-off for the correction
run keeps the engine's picture (no prepare, nothing retired, nothing given
back to the coarse level); after the run the saved product is followed as
a candidate core painted off screen while the old picture stays, and every
presented frame is the old picture whole or the new one whole.

The engine tests' synthetic slide and saved product, real GL on the real
display (as `test_step0_engine_mount.py`).
"""

import os
import time

import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)
pytest.importorskip("fusion_viewer_core")

from block01.core import saved_correction  # noqa: E402
from block01.ui.step0 import engine_mount as em  # noqa: E402

from test_step0_engine_mount import (SAVED_ID, _halves, _live_on, _mapping, _rust,  # noqa: E402,F401
                                     _show, _tab, _wait, _whole_slide, app, slide, window)

LIVE = ("CD3", "tophat", (15,))


@pytest.fixture
def unsaved(slide):
    """The slide before its Save: no decision for CD3 (the live picture)."""
    _path, loader = slide
    loader._corrected_decisions = {}
    saved_correction.register_loader(loader)
    yield loader
    loader._corrected_decisions = {"CD3": "tophat"}
    saved_correction.register_loader(loader)


def test_save_keeps_the_picture_and_switches_to_the_saved_product_whole(app, window, slide, unsaved,
                                                                          monkeypatch):
    tab = _tab(app, window, slide)
    try:
        _show(tab, *LIVE)
        _whole_slide(app, tab)
        _mapping(tab, 0, 1000)
        assert _wait(app, lambda: _live_on(tab, LIVE), 60)
        host = tab.engine.host
        old = host.presented_rgba_for_test()
        old_halves = _halves(tab, old)
        coarse, prepared = [], []
        monkeypatch.setattr(tab.engine.panel, "release_to_coarse", lambda *a, **k: coarse.append(1))
        monkeypatch.setattr(tab.engine, "_prepare_switch", lambda: prepared.append(1) or False)
        done, _t = tab.begin_release_for_production("save", keep_picture=True)
        assert prepared == [], "Save's hand-off must not block on a prepare"
        assert coarse == [], "the picture was given back"
        slot = host.current
        assert slot is not None and slot.adapter is not None and slot.adapter.sources, "sources retired"
        assert _wait(app, done.is_set, 30)
        # the Save: the saved product now answers for this selection
        unsaved._corrected_decisions = {"CD3": "tophat"}
        saved_correction.register_loader(unsaved)
        em.TEST_CONFIG["test_read_delay_ms"] = 40.0          # its tiles arrive slowly
        frames = []
        tab.resume_from_production()
        end = time.monotonic() + 30
        while time.monotonic() < end and not (host.current is not None and host.current.identity == SAVED_ID):
            app.processEvents()
            frames.append(host.presented_rgba_for_test())
            time.sleep(0.01)
        assert host.current is not None and host.current.identity == SAVED_ID
        for _ in range(5):
            app.processEvents()
            frames.append(host.presented_rgba_for_test())
        new_halves = _halves(tab, frames[-1])
        assert abs(new_halves[0] - 204) <= 3 and abs(new_halves[1] - 204) <= 3   # 800 of 1000
        for p in frames:
            h = _halves(tab, p)
            whole_old = all(abs(a - b) <= 3 for a, b in zip(h, old_halves))
            whole_new = all(abs(a - b) <= 3 for a, b in zip(h, new_halves))
            assert whole_old or whole_new, (h, old_halves, new_halves)
        assert coarse == []
    finally:
        tab.teardown()


def test_without_keep_the_hand_off_still_gives_the_picture_back(app, window, slide, unsaved, monkeypatch):
    """Other production runs are unchanged (the patch correction has its
    own keep; everything else gives the tiles back)."""
    tab = _tab(app, window, slide)
    try:
        _show(tab, *LIVE)
        _whole_slide(app, tab)
        assert _wait(app, lambda: _live_on(tab, LIVE), 60)
        coarse = []
        monkeypatch.setattr(tab.engine.panel, "release_to_coarse", lambda *a, **k: coarse.append(1))
        done, _t = tab.begin_release_for_production("other")
        assert coarse == [1]
        assert _wait(app, done.is_set, 30)
        tab.resume_from_production()
    finally:
        tab.teardown()
