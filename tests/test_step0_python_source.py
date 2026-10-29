"""S0-B (docs/v17_step0_engine_plan.md §11.4): the Python-source adapter's
bookkeeping, against a fake core and a fake scheduler -- credit held until
a result is handed over, every request ended exactly once, ended tokens
forgotten and never cancelled again, tokens unique across adapters, and no
core call from inside a scheduler callback."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.step0 import python_source as ps  # noqa: E402
from block01.viewer.tile_types import PixelBuffer, TileGridSpec, TileResult  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeCore:
    def __init__(self):
        self.live, self.new, self.gone = set(), [], []
        self.completed, self.failed = [], []
        self.inside = False

    def add_python_source(self, sid):
        if len(self.live) >= 2:
            raise ValueError("two live")
        self.live.add(sid)

    def drop_python_source(self, sid):
        self.live.discard(sid)

    def drain_demands(self):
        assert not self.inside
        new, gone, self.new, self.gone = self.new, self.gone, [], []
        return new, gone

    def complete_python_tile(self, sid, level, tx, ty, data):
        assert not self.inside, "a core call inside a core call"
        self.completed.append((sid, level, tx, ty, len(data)))
        return True

    def fail_python_tile(self, sid, level, tx, ty):
        self.failed.append((sid, level, tx, ty))


class FakeScheduler:
    """Answers when told: `sync` -- inside `request` (a cache hit)."""

    def __init__(self, sync=False):
        self.sync = sync
        self.pending = {}
        self._stale_gens = set()
        self.cancelled = []

    def request(self, req, callback):
        assert req.notify_on_stale_completion
        if self.sync:
            callback(_ok(req))
            return
        self.pending[req.generation] = (req, callback)

    def finish(self, token, error=None):
        req, callback = self.pending.pop(token)
        callback(_ok(req) if error is None else TileResult(req, None, req.key.quality, False, {}, error))

    def cancel_generation(self, gen):
        self.cancelled.append(gen)
        self._stale_gens.add(gen)

    def forget_generation(self, gen):
        self._stale_gens.discard(gen)


def _ok(req):
    arr = np.ones((512, 512), np.float32)
    return TileResult(req, PixelBuffer("cpu", "float32", arr.shape, arr), req.key.quality, False, {})


class FakeProvider:
    num_levels = 3

    def level_shape(self, level):
        return (2048 >> level, 2048 >> level)

    def level_downsample(self, level):
        return float(2 ** level)

    def source_identity(self):
        from block01.viewer.tile_types import SourceIdentity
        return SourceIdentity("/fake.tif", "1:1", "raw")


class FakeController:
    provider = FakeProvider()
    grid = TileGridSpec()

    def correction_identity(self, level, channel, method, params):
        return tuple(params), "interactive", "v1"


def _adapter(app, sync=False):
    core, sched = FakeCore(), FakeScheduler(sync)
    a = ps.PythonSourceAdapter(core, FakeController(), sched)
    return a, core, sched


def _settle(app):
    for _ in range(5):
        app.processEvents()


def _demand(core, sid, *tiles):
    core.new.extend((sid, level, tx, ty) for level, tx, ty in tiles)


def test_a_result_keeps_its_credit_until_it_is_handed_over(app, monkeypatch):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0), (0, 1, 0))
    a.pump()
    assert a.credit()[0] == 2 and len(sched.pending) == 2
    monkeypatch.setattr(ps, "COMMIT_MS", 0.0)            # nothing handed over this round
    token = next(iter(sched.pending))
    sched.finish(token)
    _settle(app)
    assert a.credit()[0] == 2, "credit freed before the tile was handed over"
    assert a.stats()["ready"] == 1
    monkeypatch.setattr(ps, "COMMIT_MS", 4.0)
    a.pump()
    assert a.credit()[0] == 1 and core.completed


def test_cancelled_stale_and_errors_free_at_once_and_errors_are_reported(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0), (0, 1, 0), (0, 0, 1))
    a.pump()
    tokens = list(sched.pending)
    for token, error in zip(tokens, ("cancelled", "stale", "boom")):
        sched.finish(token, error)
    _settle(app)
    assert a.credit() == (0, 0)
    assert core.failed == [(sid, 0, 0, 1)] or len(core.failed) == 1
    assert not sched._stale_gens


def test_the_credit_caps_the_requests(app, monkeypatch):
    monkeypatch.setattr(ps, "MAX_TILES", 2)
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, *[(0, tx, 0) for tx in range(4)])
    a.pump()
    assert len(sched.pending) == 2 and a.stats()["waiting"] == 2
    for token in list(sched.pending):
        sched.finish(token)
    _settle(app)
    assert len(core.completed) == 2 and len(sched.pending) == 2, "the waiting ones went once credit freed"


def test_a_withdrawn_request_is_cancelled_and_ends_once(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    core.gone.append((sid, 0, 0, 0))
    a.pump()
    assert sched.cancelled == [token]
    assert a.credit()[0] == 1, "freed before the cancelled request ended"
    sched.finish(token, "stale")
    _settle(app)
    assert a.credit() == (0, 0) and token not in sched._stale_gens


def test_an_ended_token_is_never_cancelled_again(app, monkeypatch):
    monkeypatch.setattr(ps, "COMMIT_MS", 0.0)
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    sched.finish(token)
    _settle(app)                                          # ended, held as ready
    core.gone.append((sid, 0, 0, 0))
    monkeypatch.setattr(ps, "COMMIT_MS", 4.0)
    a.pump()
    assert sched.cancelled == [] and token not in sched._stale_gens
    # already computed and its source lives: it still goes into the core's
    # cache (§11.7), not thrown away
    assert a.credit() == (0, 0) and core.completed == [(sid, 0, 0, 0, 512 * 512 * 4)]


def test_a_cache_hit_answers_inside_request_without_reentering_the_core(app):
    a, core, sched = _adapter(app, sync=True)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    core.inside = True                                    # any core call now is a re-entry
    a._request_waiting  # noqa: B018 -- documented entry
    core.inside = False
    a.pump()
    assert not core.completed, "handed over inside the callback"
    _settle(app)
    assert core.completed and a.credit() == (0, 0)


def test_tokens_are_unique_across_adapters_and_reissues(app):
    a1, core1, s1 = _adapter(app)
    a2, core2, s2 = _adapter(app)
    sid1, sid2 = a1.register("CD3", "tophat", (15,)), a2.register("CD3", "tophat", (15,))
    assert sid1 == sid2                                   # each core numbers its own
    _demand(core1, sid1, (0, 0, 0))
    _demand(core2, sid2, (0, 0, 0))
    a1.pump()
    a2.pump()
    t1, t2 = next(iter(s1.pending)), next(iter(s2.pending))
    assert t1 != t2
    s1.finish(t1, "stale")
    _settle(app)
    _demand(core1, sid1, (0, 0, 0))
    a1.pump()
    assert next(iter(s1.pending)) != t1, "a re-request reused its token"


def test_retire_and_close_leave_nothing(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0), (0, 1, 0))
    a.pump()
    a.retire(sid)
    assert sid not in core.live and len(sched.cancelled) == 2
    for token in list(sched.pending):
        sched.finish(token, "stale")
    _settle(app)
    assert a.credit() == (0, 0) and not core.completed
    sid2 = a.register("CD3", "cucim", (30,))
    assert sid2 == sid + 1, "a number was reused"
    a.close()
    assert core.live == set()


def test_a_closed_adapter_lets_go_of_its_core_and_goes_once_its_work_ended(app):
    from PyQt5 import QtCore, sip
    a, core, sched = _adapter(app)
    a.setParent(None)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    a.close()
    assert a.core is None and a.controller is None
    assert not sip.isdeleted(a), "deleted while a cancelled request still runs"
    for token in list(sched.pending):
        sched.finish(token, "stale")
    _settle(app)
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    assert sip.isdeleted(a)


def test_paused_work_under_way_finishes_and_is_cached_but_nothing_new_starts(app):
    """§11.7 (codex S0-B code review 2, P2-3): Step0 left -- the requests
    running end normally and their tiles go in; no new demand is taken."""
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0), (0, 1, 0))
    a.pump()
    a.set_paused(True)
    _demand(core, sid, (0, 2, 0))
    for token in list(sched.pending):
        sched.finish(token)
    _settle(app)
    assert len(core.completed) == 2 and not sched.cancelled
    assert not sched.pending, "a new request while paused"
    a.set_paused(False)
    assert len(sched.pending) == 1


# ── §13: cache-only fill and the requests a kept hand-off cancels ────
def test_a_cache_fill_hands_hits_over_and_leaves_misses_waiting_unrequested(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0), (0, 1, 0))
    hit = np.ones((512, 512), np.float32)
    got = a.fill_from_cache(lambda key: hit if key.tile.tx == 0 else None)
    assert got == (1, 1)
    assert core.completed == [(sid, 0, 0, 0, 512 * 512 * 4)]
    assert not sched.pending and a.credit() == (0, 0), "the fill asked for work"
    assert a.stats()["waiting"] == 1
    a.pump()
    assert len(sched.pending) == 1, "the miss is asked for once the adapter runs"


def test_a_cancelled_request_still_wanted_waits_and_is_asked_for_once_it_ended(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    a.set_paused(True)
    a.suspend_requests()
    assert sched.cancelled == [token] and a.stats()["waiting"] == 1
    a.set_paused(False)
    assert len(sched.pending) == 1, "asked again before the cancelled one ended"
    sched.finish(token, "stale")
    _settle(app)
    assert len(sched.pending) == 1 and next(iter(sched.pending)) != token
    assert a.stats()["waiting"] == 0


def test_a_result_already_on_its_way_is_handed_over_once_and_not_asked_again(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    sched.finish(token)                                   # queued on the GUI thread ...
    a.set_paused(True)
    a.suspend_requests()                                  # ... before the cancel
    _settle(app)
    a.set_paused(False)
    _settle(app)
    assert len(core.completed) == 1 and not sched.pending
    assert a.stats()["waiting"] == 0 and a.credit() == (0, 0)


def test_a_failure_already_on_its_way_goes_to_the_core_not_the_waiting_list(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    sched.finish(token, "boom")
    a.set_paused(True)
    a.suspend_requests()
    _settle(app)
    a.set_paused(False)
    _settle(app)
    assert core.failed == [(sid, 0, 0, 0)]
    assert not sched.pending, "the failure bypassed the core's backoff"
    assert a.stats()["waiting"] == 0 and a.credit() == (0, 0)


def test_a_withdrawn_request_is_not_revived_by_a_kept_hand_off(app):
    a, core, sched = _adapter(app)
    sid = a.register("CD3", "tophat", (15,))
    _demand(core, sid, (0, 0, 0))
    a.pump()
    token = next(iter(sched.pending))
    core.gone.append((sid, 0, 0, 0))
    a.pump()                                              # withdrawn, its end still to come
    a.set_paused(True)
    a.suspend_requests()
    sched.finish(token, "stale")
    _settle(app)
    a.set_paused(False)
    assert not sched.pending and a.stats()["waiting"] == 0
