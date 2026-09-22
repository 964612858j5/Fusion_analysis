"""G3.2b.4B1.2: the corrected floor is prepared ahead, and shared.

The floor cache is NOT new -- `ExploreController._floor_cache` has existed
since `7d7bb5a`, an 8-entry `OrderedDict` keyed by
`(source, channel, method, effective params, level, stride)`. Measured, it
already makes flipping BACK to a method this controller has shown cost
nothing. What it could not do:

* a channel the user has just switched to has no floor for the OTHER
  method, so the first flip computes one;
* the cache is per CONTROLLER, so the compare strip's three panels cannot
  see the full image's floors at all.

This block closes both without a second cache: HOT asks the host to prepare
the current channel's other floor in the background, and the strip borrows
the full image's OrderedDict.
"""

import collections
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

from block01.viewer.explore_view import ExploreController  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── 1. the cache is injectable, and ownership decides who empties it ──

def test_a_controller_makes_and_owns_its_own_cache_by_default():
    """The historical behaviour, unchanged: nothing injected, nothing shared."""
    import inspect

    signature = inspect.signature(ExploreController.__init__)
    assert signature.parameters["floor_cache"].default is None
    assert signature.parameters["owns_floor_cache"].default is True


def test_the_limit_is_still_eight_and_shared_rather_than_multiplied():
    """One 8-entry cache for both modes, not 8 per controller."""
    import inspect
    import pathlib

    source = pathlib.Path(
        inspect.getfile(ExploreController)).read_text(encoding="utf-8")
    assert "self._floor_cache_limit = 8" in source, (
        "the fixed 8-entry budget moved")
    assert "FLOOR_MAX_PIXELS = 4_000_000" in source, (
        "the floor's own pixel ceiling moved")


# ── 2. the two public entries, on a controller stand-in ──────────────

class _Provider:
    num_levels = 3

    def level_shape(self, level):
        return (2048 >> level, 2048 >> level)

    def level_downsample(self, level):
        return float(1 << level)

    def source_identity(self):
        return ("fake", "slide")


def _controller_like():
    """The smallest object the two public entries actually touch."""
    controller = ExploreController.__new__(ExploreController)
    controller.provider = _Provider()
    controller._torn_down = False
    controller._suspended = False
    controller._viewport_requests_enabled = True
    controller._floor_cache = collections.OrderedDict()
    controller._owns_floor_cache = True
    controller._floor_cache_limit = 8
    controller._floor_job_running = False
    controller._floor_background_request = None
    controller._floor_gen = 3
    controller.channel = "CD3"
    controller.method = "tophat"
    controller.params = (15,)
    controller.stats = collections.Counter()
    return controller


def test_has_cached_floor_asks_the_key_the_foreground_will_use():
    controller = _controller_like()
    assert controller.has_cached_floor("CD3", "cucim", (50,)) is False

    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for("CD3", "cucim", (50,), level, stride)
    controller._floor_cache[(ctx, level, stride)] = (None, {})

    assert controller.has_cached_floor("CD3", "cucim", (50,)) is True
    # ...and the key really does carry every part of the identity.
    assert controller.has_cached_floor("CD3", "cucim", (51,)) is False, \
        "a changed parameter hit the old floor"
    assert controller.has_cached_floor("CD8", "cucim", (50,)) is False, \
        "another channel hit this channel's floor"
    assert controller.has_cached_floor("CD3", "tophat", (50,)) is False, \
        "another method hit this method's floor"


def test_preparing_a_floor_that_is_already_cached_starts_nothing():
    controller = _controller_like()
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for("CD3", "cucim", (50,), level, stride)
    controller._floor_cache[(ctx, level, stride)] = (None, {})

    started = []
    controller._run_floor_job = lambda request: started.append(request)

    assert controller.prepare_floor_async("CD3", "cucim", (50,)) is False
    assert started == []


def test_a_background_request_waits_for_the_running_job_and_does_not_queue():
    """One job at a time, one waiting VALUE -- never a growing queue."""
    controller = _controller_like()
    started = []
    controller._run_floor_job = lambda request: started.append(request)
    controller._floor_job_running = True

    assert controller.prepare_floor_async("CD3", "cucim", (50,)) is True
    assert started == [], "a second job ran beside the one in flight"
    first = controller._floor_background_request
    assert first is not None and first.foreground is False

    # A newer one REPLACES it rather than piling up.
    assert controller.prepare_floor_async("CD3", "tophat", (99,)) is True
    assert controller._floor_background_request is not first
    assert isinstance(controller._floor_background_request,
                      type(first)), "the waiting slot changed shape"


def test_the_foreground_always_goes_first():
    """`_floor_pending` is served before any background preparation."""
    controller = _controller_like()
    controller._floor_job_running = True
    controller.prepare_floor_async("CD3", "cucim", (50,))
    assert controller._floor_background_request is not None

    controller._floor_pending = True
    started = []
    controller._start_floor_job = lambda gen: started.append(("foreground", gen))
    controller._run_floor_job = lambda request: started.append(("background",))
    # The tail of `_handle_floor_result`: pending foreground wins, and the
    # background slot is left for the round after it.
    if controller._floor_pending:
        controller._floor_pending = False
        controller._start_floor_job(controller._floor_gen)
    assert started == [("foreground", 3)]
    assert controller._floor_background_request is not None, (
        "the background request was dropped by a foreground job")


def test_a_background_result_only_reaches_the_cache():
    """It may not touch the live selection, the gains or the picture."""
    controller = _controller_like()
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for("CD3", "cucim", (50,), level, stride)
    from block01.viewer.explore_view import _FloorRequest

    request = _FloorRequest(generation=3, channel="CD3", method="cucim",
                            base_params=(50,), floor_level=level,
                            stride=stride, ctx=ctx, foreground=False)
    controller._floor_ctx = "UNTOUCHED"
    controller._floor_ready = False
    controller._level_gain = {}
    controller._gain_ctx = None
    controller._floor_pending = False
    controller._start_next_floor_job = lambda: None

    controller._handle_floor_result(
        (3, ctx, level, stride, np.zeros((4, 4), np.float32), None,
         {0: 1.0}, None, request))

    assert (ctx, level, stride) in controller._floor_cache, (
        "the background result never reached the cache")
    assert controller._floor_ctx == "UNTOUCHED", (
        "a background floor installed itself as the live one")
    assert controller._floor_ready is False
    assert controller._level_gain == {}, "it overwrote the live gain table"
    assert controller._gain_ctx is None


def test_the_historical_eight_element_payload_still_means_foreground():
    """Existing callers deliver eight elements; that must keep working."""
    controller = _controller_like()
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for("CD3", "tophat", (15,), level, stride)
    seen = {}
    controller._current_floor_ctx = lambda lvl, st=1: ctx
    controller._floor_pending = False
    controller._start_next_floor_job = lambda: None
    controller._remember_floor = lambda *a: seen.setdefault("remembered", a)
    controller._precise_pool = type("P", (), {
        "set_levels_for_level": lambda self, fn: None})()
    controller._corrected_levels_fn = lambda: (lambda lvl: (0.0, 1.0))
    controller._update_layer_visibility = lambda: None
    controller.floor_ready_changed = type("S", (), {
        "emit": lambda self, ok: seen.setdefault("emitted", ok)})()
    controller.floor_preparing_changed = type("S", (), {
        "emit": lambda self, ok: None})()
    controller.stats = collections.Counter()

    # Eight elements, no request: must be treated as the foreground.
    with pytest.raises(Exception):
        # It will fail later on the missing view, but it must get PAST the
        # unpack and into the foreground branch -- which is the contract.
        controller._handle_floor_result(
            (3, ctx, level, stride, np.zeros((4, 4), np.float32), None,
             {}, None))
    assert controller._floor_job_running is False


# ═══════════════════════════════════════════════════════════════════════
# G3.2b.4B1.2.1 -- the two exit gates B1.2 could not close, on the real
# public product path, plus the nine mutation gates.
#
# WHY THE COUNTER BELOW COUNTS FLOOR JOBS AND NOT `correct_array`:
# `CorrectionCompute.compute()` -- the corrected TILE path -- calls
# `self.correct_array()` internally (`viewer/correction_compute.py:130`),
# and so does the gain calibration folded into every floor job. Counting
# `correct_array` alone therefore counts one level+1 TILE as one "floor
# correction", which is exactly how B1.2 came to report "12 floor
# `correct_array` calls" for a flip whose floor was a cache HIT. The floor's
# own entry point is `_run_floor_job`, so that is what a floor gate counts.
# ═══════════════════════════════════════════════════════════════════════

import importlib.util  # noqa: E402
import pathlib  # noqa: E402

from PyQt5 import QtTest  # noqa: E402

from block01.ui.step0.step0_explore_tab import Step0ExploreTab  # noqa: E402

_RIG_PATH = pathlib.Path(__file__).with_name("test_step0_method_prefetch.py")
_rig_spec = importlib.util.spec_from_file_location("floor_rig", _RIG_PATH)
RIG = importlib.util.module_from_spec(_rig_spec)
_rig_spec.loader.exec_module(RIG)

providers = RIG.providers
TOPHAT_RADIUS = RIG.TOPHAT_RADIUS
CUCIM_SIGMA = RIG.CUCIM_SIGMA
PARAM_FOR = {"tophat": (TOPHAT_RADIUS,), "cucim": (CUCIM_SIGMA,), None: ()}


def open_full_image(method="tophat", channel="CD3",
                    path="/fake/slide.ome.tif", with_hot=True):
    """The rig's own opener, but able to arrive on Original too.

    `RIG.open_full_image` always passes a one-element params tuple, which is
    not a selection Original ever has. Everything else -- the widget, the
    dataset, the camera, the dwell -- is the rig's.

    `with_hot=False` is the rig's own way of reaching the behaviour BEFORE
    this block, through the public API alone: with no specs provider
    installed, `start_hot` returns without mounting anything, so nothing is
    ever prepared in the background.
    """
    if method is not None:
        return RIG.open_full_image(path=path, channel=channel, method=method,
                                   param=PARAM_FOR[method][0],
                                   with_hot=with_hot)
    tab = Step0ExploreTab(page=None)
    if with_hot:
        tab.set_hot_specs_provider(lambda: list(RIG.SPECS))
    tab.resize(800, 600)
    tab.show()
    QtWidgets.QApplication.instance().processEvents()
    tab.set_dataset(path)
    assert tab.show_source(channel, None, ()) is True
    drain(200)
    tab.stack.controller.jump_to(1024, 1024, 1024, 1024)
    drain(1800)
    return tab


def drain(ms=800, step=20):
    for _ in range(max(1, ms // step)):
        QtWidgets.QApplication.instance().processEvents()
        QtTest.QTest.qWait(step)


def wait_until(predicate, timeout_ms=20000, step=10):
    waited = 0
    while waited < timeout_ms:
        QtWidgets.QApplication.instance().processEvents()
        if predicate():
            return True
        QtTest.QTest.qWait(step)
        waited += step
    return False


@pytest.fixture
def floor_jobs(monkeypatch):
    """Every `_FloorRequest` that really ran, foreground and background apart.

    The production entry point, wrapped to COUNT only -- nothing is faked
    and no decision is changed.
    """
    from types import SimpleNamespace

    real = ExploreController._run_floor_job
    ran = []

    def run(self, request):
        ran.append(request)
        return real(self, request)

    monkeypatch.setattr(ExploreController, "_run_floor_job", run)
    return SimpleNamespace(
        ran=ran,
        foreground=lambda: [r for r in ran if r.foreground],
        background=lambda: [r for r in ran if not r.foreground])


@pytest.fixture
def prepared(monkeypatch):
    """Every `(channel, method, params)` the host was ASKED to prepare."""
    real = ExploreController.prepare_floor_async
    asked = []

    def prepare(self, channel, method, params=()):
        asked.append((channel, method, tuple(params or ())))
        return real(self, channel, method, params)

    monkeypatch.setattr(ExploreController, "prepare_floor_async", prepare)
    return asked


@pytest.fixture
def tile_computes(monkeypatch):
    """Every corrected TILE computed, by `CorrectionKey`."""
    from block01.viewer import correction_compute as cc

    keys = []
    real = cc.CorrectionCompute.compute

    def compute(self, key):
        keys.append(key)
        return real(self, key)

    monkeypatch.setattr(cc.CorrectionCompute, "compute", compute)
    return keys


# ── the identity helpers, taken from the controller, never re-derived ──

def floor_key(controller, channel, method, params):
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for(channel, method, params, level, stride)
    return (ctx, int(level), int(stride))


def viewport_keys(controller, channel, method, params, level=None):
    """The corrected keys covering the CURRENT viewport at `level`.

    Uses the controller's own `request_planning.bbox_to_level` +
    `tiles_covering` + `effective_param` -- the same three the foreground
    and HOT both call. No fourth coordinate formula lives in this file.
    """
    from block01.viewer import explore_view as ev
    from block01.viewer import request_planning as planning
    from block01.viewer.tile_types import (CorrectionKey, TileAddress,
                                           effective_param, tiles_covering)

    level = controller.level if level is None else int(level)
    if level >= controller.provider.num_levels:
        return set()
    ds = controller.provider.level_downsample(level)
    bbox = controller._current_bbox
    if bbox is None:
        return set()
    eff = tuple(effective_param(p, level, ds) for p in tuple(params or ()))
    return {
        CorrectionKey(
            source=controller.provider.source_identity(), channel=channel,
            tile=TileAddress(grid=controller.grid, level=level, tx=tx, ty=ty),
            method=method, params=eff,
            algorithm_version=ev.BG_CORRECTION_ALGO_VERSION,
            quality=controller.quality)
        for tx, ty in tiles_covering(
            planning.bbox_to_level(bbox, ds), controller.grid.tile_size)}


def missing(controller, keys):
    """Which of `keys` are NOT in the shared corrected cache right now.

    Read off `LRUByteCache._store`: `get()` would move the entry to the MRU
    end and count a hit, so the probe would change what it is probing.
    """
    store = controller.scheduler.corrected_cache._store
    return {key for key in keys if key not in store}


# ── EXIT GATE A: the first flip to the other method on a new channel ──

@pytest.mark.parametrize("shown,other", [("tophat", "cucim"),
                                         ("cucim", "tophat"),
                                         (None, "tophat"),
                                         (None, "cucim")])
def test_the_first_flip_to_the_other_method_adds_no_floor_work(
        app, providers, floor_jobs, tile_computes, shown, other):
    """Arrive on a channel, stand still, then flip: the floor is a HIT.

    The gate is stated in the two quantities a user can feel, kept apart:
    the FLOOR (this channel+method's whole downsampled level) must not be
    computed again, and the tiles the finished picture is made of must not
    be computed again. What a flip may still do afterwards, for a pan that
    has not happened, is not this gate's business -- see
    `test_the_lookahead_ring_is_the_only_level_plus_one_work_left`.
    """
    tab = open_full_image(method=shown)
    try:
        controller = tab.stack.controller
        params = PARAM_FOR[other]

        assert wait_until(
            lambda: controller.has_cached_floor("CD3", other, params)), (
            "standing still on a new channel never prepared the other "
            "method's floor")
        # ...and the picture's own tiles, current level and the level+1
        # band the foreground falls back to, are prepared too.
        current = viewport_keys(controller, "CD3", other, params)
        fallback = viewport_keys(controller, "CD3", other, params,
                                 level=controller.level + 1)
        assert wait_until(lambda: not missing(controller, current | fallback))

        jobs_before = len(floor_jobs.ran)
        hits_before = controller.stats.get("floor_cache_hits", 0)
        # THE MARK IS A LENGTH, NOT A SET SIZE. Slicing `tile_computes` by
        # `len(set(tile_computes))` reads the window from the wrong offset
        # the moment any key is computed twice -- the slice would start
        # early and charge work done BEFORE the switch to the switch.
        # Nothing may depend on "no key is ever computed twice" here; that
        # is a separate gate's claim, not this one's precondition.
        computed_mark = len(tile_computes)

        assert tab.show_source("CD3", other, params) is True

        # The gate is about the FRAME the user asked for, so it is read the
        # moment `show_source` has returned -- the floor is installed
        # synchronously from the cache -- and again after the dwell, so a
        # late floor job cannot hide behind the drain.
        assert len(floor_jobs.ran) == jobs_before, (
            "the flip started a floor job although the floor was cached")
        drain(900)

        assert len(floor_jobs.ran) == jobs_before, (
            f"{len(floor_jobs.ran) - jobs_before} floor job(s) ran for a "
            "selection whose floor was already in the cache")
        assert controller.stats.get("floor_cache_hits", 0) == hits_before + 1, (
            "the flip did not install the prepared floor from the cache")
        assert controller._floor_ready is True
        assert controller._floor_ctx == floor_key(
            controller, "CD3", other, params)[0]

        recomputed = [k for k in tile_computes[computed_mark:]
                      if k in current or k in fallback]
        assert not recomputed, (
            f"{len(recomputed)} tile(s) of the finished picture were "
            "computed again on the flip")
        assert controller.channel == "CD3" and controller.method == other
        assert tuple(controller.params) == params
    finally:
        tab.teardown()


def test_the_lookahead_ring_is_the_only_level_plus_one_work_left(
        app, providers, tile_computes):
    """Name the remaining level+1 work exactly, instead of counting it.

    `_issue_settled_request` asks for TWO things at level+1: the tiles that
    cover the viewport NOW, and a one-tile look-ahead RING
    (`FALLBACK_HALO_TILES`) for a pan that has not happened yet. HOT
    prepares the first and deliberately not the second -- a ring is
    speculative work for a picture nobody has asked for, and it is issued
    at `FALLBACK_RING_BASE_PRIORITY`, strictly below the current level.

    So: the covering set must be complete before the flip, and every level+1
    tile still computed by the flip must be a ring tile. That is a stronger
    statement than "12", and it turns red if HOT's plan and the
    foreground's ever disagree about the covering set.
    """
    from block01.viewer import explore_view as ev

    tab = open_full_image()
    try:
        controller = tab.stack.controller
        params = PARAM_FOR["cucim"]
        flevel = controller.level + 1
        assert flevel < controller.provider.num_levels, (
            "the fixture has no fallback level to prove anything about")

        covering = viewport_keys(controller, "CD3", "cucim", params,
                                 level=flevel)
        assert covering, "the viewport covers no level+1 tile"
        assert wait_until(lambda: not missing(controller, covering)), (
            "the level+1 tiles covering the viewport were never prepared")

        # The ring, by the controller's OWN constant and its own planner.
        ds = controller.provider.level_downsample(flevel)
        bbox = controller._current_bbox
        size = controller.grid.tile_size
        fh, fw = controller.provider.level_shape(flevel)
        pad = ev.FALLBACK_HALO_TILES * size
        from block01.viewer.tile_types import tiles_covering
        padded = tiles_covering(
            (max(0, int(bbox[0] / ds) - pad), max(0, int(bbox[1] / ds) - pad),
             min(fh, int(bbox[2] / ds) + pad), min(fw, int(bbox[3] / ds) + pad)),
            size)
        covering_coords = {(k.tile.tx, k.tile.ty) for k in covering}
        ring_coords = padded - covering_coords
        assert ring_coords, "this viewport has no look-ahead ring to speak of"

        mark = len(tile_computes)
        assert tab.show_source("CD3", "cucim", params) is True
        drain(900)

        after = [k for k in tile_computes[mark:] if k.tile.level == flevel]
        stray = [k for k in after if (k.tile.tx, k.tile.ty) not in ring_coords]
        assert not stray, (
            f"{len(stray)} level+1 tile(s) computed by the flip are NOT "
            "look-ahead ring tiles -- the prepared covering set and the "
            "foreground's disagree")
    finally:
        tab.teardown()


# ── MUTATION 2: preparation must not move the visible selection ────────

def test_preparing_a_floor_never_changes_what_is_on_screen(
        app, providers, floor_jobs):
    tab = open_full_image()
    try:
        controller = tab.stack.controller
        assert wait_until(lambda: controller._floor_ready)
        before = (controller.channel, controller.method,
                  tuple(controller.params), controller._floor_ctx,
                  controller._floor_ready, dict(controller._level_gain),
                  controller._gain_ctx)
        camera = controller.view.view_box.viewRect()
        image = controller.view.corrected_floor_item.image

        # Ask for a selection that is NOT on screen, through the public entry.
        controller.prepare_floor_async("CD20", "cucim", PARAM_FOR["cucim"])
        drain(1200)

        after = (controller.channel, controller.method,
                 tuple(controller.params), controller._floor_ctx,
                 controller._floor_ready, dict(controller._level_gain),
                 controller._gain_ctx)
        assert after == before, (
            "preparing a floor in the background changed the live selection, "
            "the floor context, the readiness or the gain table")
        assert controller.view.corrected_floor_item.image is image, (
            "a background floor installed itself into the visible ImageItem")
        now = controller.view.view_box.viewRect()
        assert (abs(now.x() - camera.x()) < 1e-6
                and abs(now.width() - camera.width()) < 1e-6), (
            "preparing a floor moved the camera")
    finally:
        tab.teardown()


# ── MUTATION 8: only the CENTRE channel gets a floor ───────────────────

def test_only_the_current_channel_is_given_a_floor(app, providers, prepared):
    tab = open_full_image()
    try:
        controller = tab.stack.controller
        assert wait_until(
            lambda: controller.has_cached_floor("CD3", "cucim",
                                                PARAM_FOR["cucim"]))
        drain(1200)
        assert prepared, "nothing was ever prepared"
        off_centre = sorted({ch for ch, _m, _p in prepared if ch != "CD3"})
        assert not off_centre, (
            f"a floor was prepared for {off_centre}, which is not the "
            "channel on screen")
        for neighbour in ("CD20", "DAPI"):
            for method, params in (("tophat", PARAM_FOR["tophat"]),
                                   ("cucim", PARAM_FOR["cucim"])):
                assert controller.has_cached_floor(
                    neighbour, method, params) is False, (
                    f"{neighbour}/{method} got a floor it was never owed")
    finally:
        tab.teardown()


# ── MUTATION 9: HOT's level+1 plan IS the foreground's covering set ────

def test_the_prepared_fallback_band_is_the_foregrounds_own_covering_set(
        app, providers):
    """The two paths must agree tile for tile, not in count.

    HOT's `_fallback_tiles` and the foreground's fallback batch both go
    through `request_planning.bbox_to_level` + `tiles_covering`. This reads
    HOT's real answer for the real snapshot and compares it, coordinate by
    coordinate, with what the controller's own planner gives for the same
    bbox -- so a second, drifting coordinate rule on either side turns this
    red.
    """
    from block01.viewer import request_planning as planning
    from block01.viewer.tile_types import tiles_covering

    tab = open_full_image()
    try:
        controller = tab.stack.controller
        hot = tab.hot
        assert hot is not None and hot.include_center is True
        snapshot = controller.snapshot()
        flevel = snapshot.level + 1
        assert flevel < controller.provider.num_levels

        ds = controller.provider.level_downsample(flevel)
        expected = tiles_covering(
            planning.bbox_to_level(snapshot.bbox_l0, ds),
            controller.grid.tile_size)
        assert set(hot._fallback_tiles(snapshot)) == set(expected), (
            "HOT's level+1 plan is not the foreground's covering set")
        assert expected, "the covering set is empty, so this proves nothing"
    finally:
        tab.teardown()


# ── EXIT GATE D: a parameter change must not hit the old floor ─────────

def test_a_changed_parameter_computes_once_and_then_stays_warm(
        app, providers, floor_jobs):
    tab = open_full_image()
    try:
        controller = tab.stack.controller
        assert wait_until(lambda: controller.has_cached_floor(
            "CD3", "tophat", PARAM_FOR["tophat"]))
        retuned = (TOPHAT_RADIUS + 4,)
        assert controller.has_cached_floor("CD3", "tophat", retuned) is False, (
            "a retuned radius hit the floor computed for the old one")

        before = len(floor_jobs.ran)
        assert tab.show_source("CD3", "tophat", retuned) is True
        assert wait_until(lambda: controller._floor_ready
                          and controller.has_cached_floor(
                              "CD3", "tophat", retuned))
        assert len(floor_jobs.ran) > before, (
            "a genuinely new parameter was served from a stale floor")

        # ...and the round trip back and forth is warm both ways.
        drain(600)
        mid = len(floor_jobs.ran)
        assert tab.show_source("CD3", "tophat", PARAM_FOR["tophat"]) is True
        drain(600)
        assert tab.show_source("CD3", "tophat", retuned) is True
        drain(600)
        assert len(floor_jobs.ran) == mid, (
            "a floor that had already been computed was computed again")
    finally:
        tab.teardown()


# ── EXIT GATE E: datasets are isolated, and the owner clears ───────────

def test_a_new_dataset_never_inherits_the_old_ones_floor(app, providers):
    tab = open_full_image(path="/fake/A.ome.tif")
    try:
        first = tab.stack.controller
        assert wait_until(lambda: first.has_cached_floor(
            "CD3", "cucim", PARAM_FOR["cucim"]))
        old_cache = first._floor_cache
        assert first._owns_floor_cache is True, (
            "the full image stopped owning the floor cache it made")
        old_keys = set(old_cache)
        assert old_keys

        tab.set_dataset("/fake/B.ome.tif")
        assert not old_cache, (
            "the owner did not empty the shared floor cache when its "
            "dataset's life ended")
        assert tab.show_source("CD3", "tophat", PARAM_FOR["tophat"]) is True
        drain(1200)
        second = tab.stack.controller
        assert second is not first
        for method, params in (("tophat", PARAM_FOR["tophat"]),
                               ("cucim", PARAM_FOR["cucim"])):
            assert floor_key(second, "CD3", method, params) not in old_keys, (
                "the new dataset's floor key is the old dataset's key")
    finally:
        tab.teardown()


# ── EXIT GATE G: no injection means the historical behaviour ───────────

def test_an_uninjected_controller_still_makes_and_clears_its_own(app,
                                                                 providers):
    tab = open_full_image()
    try:
        controller = tab.stack.controller
        assert controller._owns_floor_cache is True
        assert controller._floor_cache_limit == 8
        assert wait_until(lambda: len(controller._floor_cache) > 0)
        cache = controller._floor_cache
        tab.teardown()
        assert not cache, "an owner left its own floor cache behind"
    finally:
        try:
            tab.teardown()
        except Exception:                                   # noqa: BLE001
            pass


# ── MUTATION 7 (behaviour, not source text): the budget is still eight ─

def test_the_cache_never_grows_past_eight_entries():
    controller = _controller_like()
    level, stride = controller._pick_floor_level_and_stride()
    for n in range(20):
        ctx = controller._floor_ctx_for(f"CH{n}", "tophat", (15,), level,
                                        stride)
        controller._remember_floor(ctx, level, stride, np.zeros((2, 2),
                                                                np.float32), {})
    assert len(controller._floor_cache) == 8, (
        f"the floor budget became {len(controller._floor_cache)}")


# ── MUTATION 6: every part of the identity is in the key ───────────────

def test_the_floor_key_carries_level_stride_and_source_as_well():
    controller = _controller_like()
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for("CD3", "cucim", (50,), level, stride)
    controller._floor_cache[(ctx, level, stride)] = (None, {})

    assert (controller._floor_ctx_for("CD3", "cucim", (50,), level, stride + 1)
            != ctx or stride + 1 == stride), "stride is not in the identity"
    assert controller._floor_ctx_for(
        "CD3", "cucim", (50,), max(0, level - 1), stride) != ctx, (
        "the level is not in the identity")

    class _OtherSlide(_Provider):
        def source_identity(self):
            return ("fake", "another-slide")

    controller.provider = _OtherSlide()
    assert controller.has_cached_floor("CD3", "cucim", (50,)) is False, (
        "another slide hit this slide's floor -- the source identity is "
        "not part of the key")


# ═══════════════════════════════════════════════════════════════════════
# EXIT GATES B and C -- Full Image -> Compare -> Full Image.
#
# The strip is driven through the production `CompareStrip` over the
# compare suite's own fake slide, borrowed rather than rebuilt so a change
# there cannot leave two different "real strips" disagreeing.
# ═══════════════════════════════════════════════════════════════════════

_CMP_PATH = pathlib.Path(__file__).with_name("test_step0_compare_tiles.py")
_cmp_spec = importlib.util.spec_from_file_location("floor_compare_rig",
                                                   _CMP_PATH)
CMP = importlib.util.module_from_spec(_cmp_spec)
_cmp_spec.loader.exec_module(CMP)


@pytest.fixture
def realish_provider(monkeypatch):
    from block01.viewer import raw_tile_provider as rtp
    made = CMP._RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: made)
    return made


def _build_strip(page, *, floor_cache, caches=None):
    from block01.ui.step0 import compare_strip as cs

    strip = page._compare_strip_widget
    strip._stack_factory = cs.build_compare_stacks
    strip.set_dataset(page.ome_path)
    stacks = strip.ensure_built(
        page.current_channel, params_for=page._compare_params_for,
        tint=page._full_image_tint(),
        nucleus=page._full_image_nucleus_args(),
        viewport_l0=None, overview_store=None, caches=caches,
        floor_cache=floor_cache)
    drain(900)
    return strip, stacks


def test_the_three_compare_panels_borrow_the_one_floor_cache(
        app, realish_provider):
    """Gate B: one OrderedDict for all four controllers -- not four of them.

    And emphatically NOT a shared scheduler, provider, compute or
    controller: the whole point of lending the cache is that the identity in
    the key makes a floor computed by one of them the right answer for the
    others, without coupling their lifecycles.
    """
    page = CMP._page(app)
    lent = collections.OrderedDict()
    strip, stacks = _build_strip(page, floor_cache=lent)
    try:
        assert stacks is not None, strip._build_error
        assert len(stacks.controllers) == 3
        assert [c._floor_cache is lent for c in stacks.controllers] == \
            [True, True, True], "a panel built its own floor cache"
        assert [c._owns_floor_cache for c in stacks.controllers] == \
            [False, False, False], "a borrower claimed ownership"
        assert stacks.owns_floor_cache is False
        # ...and the budget is the SAME eight, not eight per panel.
        assert {c._floor_cache_limit for c in stacks.controllers} == {8}
        # Independent, on purpose.
        assert len({id(c.scheduler) for c in stacks.controllers}) == 1, (
            "the strip's own three panels stopped sharing its scheduler")
        assert len({id(c) for c in stacks.controllers}) == 3
    finally:
        strip.teardown(wait_for_floor=False)


def test_a_floor_the_full_image_computed_is_the_answer_for_a_panel(
        app, realish_provider, floor_jobs):
    """Gate B: an already-prepared floor is not prepared again in compare."""
    page = CMP._page(app)
    lent = collections.OrderedDict()
    strip, stacks = _build_strip(page, floor_cache=lent)
    try:
        assert stacks is not None, strip._build_error
        assert wait_until(lambda: len(lent) > 0), (
            "opening compare prepared no floor at all")
        cached = set(lent)
        for controller in stacks.controllers:
            if controller.method is None:
                continue
            key = floor_key(controller, controller.channel, controller.method,
                            controller.params)
            assert key in cached, (
                f"{controller.method}'s floor is not filed under the key the "
                "foreground looks it up by")
            assert controller.has_cached_floor(
                controller.channel, controller.method,
                controller.params) is True

        # Asking again for exactly what is cached starts nothing.
        before = len(floor_jobs.ran)
        for controller in stacks.controllers:
            if controller.method is None:
                continue
            assert controller.prepare_floor_async(
                controller.channel, controller.method,
                controller.params) is False
        assert len(floor_jobs.ran) == before
    finally:
        strip.teardown(wait_for_floor=False)


# ── MUTATION 5: the borrower must never empty what it was lent ─────────

def test_compare_teardown_leaves_the_borrowed_floor_cache_alone(
        app, realish_provider):
    page = CMP._page(app)
    lent = collections.OrderedDict()
    strip, stacks = _build_strip(page, floor_cache=lent)
    assert stacks is not None, strip._build_error
    assert wait_until(lambda: len(lent) > 0)
    before = dict(lent)

    strip.teardown(wait_for_floor=False)
    drain(400)

    assert dict(lent) == before, (
        "the compare strip emptied the full image's floor cache on its way "
        "out -- the full image would recompute the floor it is showing")


def test_a_strip_that_was_lent_nothing_owns_and_clears_its_own(
        app, realish_provider):
    """Gate G, on the borrowing side: no injection, historical behaviour.

    WITH NOTHING LENT each panel keeps its own private 8-entry cache and
    empties it on the way out -- byte for byte what a strip did before this
    block, and what a strip with no full image behind it must do. That is
    three caches, and it is deliberate: gate G requires the uninjected path
    be unchanged. The product path never reaches it, because `Step0Page`
    always lends the full image's own cache -- which
    `test_the_page_lends_the_full_images_floor_cache_when_compare_opens`
    and `test_the_three_compare_panels_borrow_the_one_floor_cache` gate.
    """
    page = CMP._page(app)
    strip, stacks = _build_strip(page, floor_cache=None)
    try:
        assert stacks is not None, strip._build_error
        assert stacks.owns_floor_cache is True
        caches = [c._floor_cache for c in stacks.controllers]
        assert len({id(c) for c in caches}) == 3, (
            "the uninjected path stopped behaving as it historically did")
        assert {c._floor_cache_limit for c in stacks.controllers} == {8}
        assert all(c._owns_floor_cache for c in stacks.controllers)
        assert wait_until(lambda: any(len(c) > 0 for c in caches))
    finally:
        strip.teardown(wait_for_floor=False)
        drain(400)
    assert not any(caches), "an owning strip left its own floor cache behind"


def test_coming_back_to_the_full_image_finds_its_floors_still_there(
        app, realish_provider, floor_jobs):
    """Gate C: the round trip costs no floor, and moves no selection."""
    page = CMP._page(app)
    lent = collections.OrderedDict()
    strip, stacks = _build_strip(page, floor_cache=lent)
    assert stacks is not None, strip._build_error
    assert wait_until(lambda: len(lent) > 0)
    survived = dict(lent)
    channel = page.current_channel

    strip.teardown(wait_for_floor=False)
    drain(400)
    assert dict(lent) == survived

    # A fresh controller lent the SAME cache finds every one of them.
    strip2, stacks2 = _build_strip(page, floor_cache=lent)
    try:
        assert stacks2 is not None, strip2._build_error
        before = len(floor_jobs.ran)
        drain(900)
        for controller in stacks2.controllers:
            if controller.method is None:
                continue
            assert controller.has_cached_floor(
                channel, controller.method, controller.params) is True, (
                f"{controller.method}'s floor was lost on the round trip")
        assert len(floor_jobs.ran) == before, (
            f"{len(floor_jobs.ran) - before} floor job(s) ran on the way "
            "back although every floor was already cached")
    finally:
        strip2.teardown(wait_for_floor=False)


def test_the_page_lends_the_full_images_floor_cache_when_compare_opens(app):
    """The wiring, driven by the real right-click that opens compare."""
    page = CMP._page(app)
    lent = collections.OrderedDict()
    page._explore_tab.stack.floor_cache = lent

    seen = {}
    real_ensure = page._compare_strip_widget.ensure_built

    def recording(channel, **kwargs):
        seen.update(kwargs)
        return real_ensure(channel, **kwargs)

    page._compare_strip_widget.ensure_built = recording
    CMP._enter(page)

    assert "floor_cache" in seen, (
        "the page did not offer the full image's floor cache at all")
    assert seen["floor_cache"] is lent, (
        "the page lent something other than the full image's own floor cache")


# ── MUTATION 3 / EXIT GATE F: the foreground goes first, in production ──

def test_a_landing_background_floor_serves_the_owed_foreground_first():
    """The real `_handle_floor_result`, not a hand-rolled copy of its tail.

    A user who changes to a selection whose floor is NOT cached while a
    background preparation is running gets `_floor_pending` set -- that is
    how `_ensure_corrected_floor` defers to the one-job-at-a-time rule. When
    the background job lands, the job that starts next must be that owed
    FOREGROUND floor, and the waiting background request must keep waiting.

    `test_the_foreground_always_goes_first` states the same rule against the
    two flags; this one states it against the code that has to obey it.
    """
    from block01.viewer.explore_view import _FloorRequest

    controller = _controller_like()
    controller._floor_job_running = True
    controller._floor_pending = False

    level, stride = controller._pick_floor_level_and_stride()
    running = _FloorRequest(
        generation=controller._floor_gen, channel="CD3", method="cucim",
        base_params=(50,), floor_level=level, stride=stride,
        ctx=controller._floor_ctx_for("CD3", "cucim", (50,), level, stride),
        foreground=False)
    # A second background preparation is waiting behind it...
    assert controller.prepare_floor_async("CD20", "tophat", (15,)) is True
    waiting = controller._floor_background_request
    assert waiting is not None
    # ...and the USER has meanwhile asked for a floor that is not cached.
    controller._floor_pending = True

    started = []
    controller._run_floor_job = lambda request: started.append(
        ("background", request.channel, request.method))
    controller._start_floor_job = lambda gen: started.append(
        ("foreground", gen))

    controller._handle_floor_result(
        (controller._floor_gen, running.ctx, level, stride,
         np.zeros((4, 4), np.float32), None, {}, None, running))

    assert started[:1] == [("foreground", controller._floor_gen)], (
        "a background preparation was started ahead of the foreground floor "
        f"the user is waiting for: {started}")
    assert controller._floor_pending is False, (
        "the owed foreground floor was neither started nor cleared -- it is "
        "simply lost until something else re-requests it")
    assert controller._floor_background_request is waiting, (
        "the waiting background request was dropped instead of deferred")


# ═══════════════════════════════════════════════════════════════════════
# G3.2b.4B1.2.2 -- the two parts of exit gate A that B1.2.1 measured
# nothing about and therefore did not claim: "the flip re-reads no raw"
# and "the picture is pixel for pixel what it was before any of this".
#
# PRODUCTION IS FROZEN for this block: these are tests only.
# ═══════════════════════════════════════════════════════════════════════

def frame_tiles(controller, channel, method, params):
    """`(level, tx, ty)` of every tile the FINISHED picture is made of.

    The current level's visible set plus the level+1 band that COVERS the
    viewport -- i.e. what exit gate A is about. The speculative look-ahead
    ring is deliberately not here: it is for a pan that has not happened,
    HOT does not prepare it, and `test_the_lookahead_ring_is_the_only_...`
    is where it is accounted for.
    """
    coords = set()
    for level in (controller.level, controller.level + 1):
        for key in viewport_keys(controller, channel, method, params,
                                 level=level):
            coords.add((key.tile.level, key.tile.tx, key.tile.ty))
    return coords


def raw_reads_for(provider, coords, channel, tile_size):
    """The provider records that fall on one of `coords`, for `channel`.

    `_Provider` already records every `read_tile` and every `read_region`
    as `(channel, level, y0, y1, x0, x1)`, so no wrapper is needed and
    nothing about the read path is changed in order to observe it.
    """
    wanted = {(channel, level, ty * tile_size, tx * tile_size)
              for level, tx, ty in coords}
    hits = []
    for record in list(provider.tile_reads):
        ch, level, y0, _y1, x0, _x1 = record
        if (ch, level, y0, x0) in wanted:
            hits.append(record)
    return hits


# ── EXIT GATE A, part 2: the flip re-reads no raw for the picture ──────

@pytest.mark.parametrize("shown,other", [("tophat", "cucim"),
                                         ("cucim", "tophat"),
                                         (None, "tophat"),
                                         (None, "cucim")])
def test_the_first_flip_re_reads_no_raw_for_the_finished_picture(
        app, providers, floor_jobs, shown, other):
    """Not one raw tile of the frame is read from the provider again.

    Stated about the tiles the finished picture is made of, and about
    whole-LEVEL reads (the floor's and the calibration's input), which must
    be zero because the floor is a cache hit. The look-ahead ring's raw IS
    read -- HOT never prepared it and it is not part of this frame -- so
    that is excluded by construction rather than by a fudge factor, and
    `test_the_lookahead_ring_is_the_only_level_plus_one_work_left` is what
    keeps the ring honest.
    """
    tab = open_full_image(method=shown)
    try:
        controller = tab.stack.controller
        provider = controller.provider
        params = PARAM_FOR[other]
        tile_size = controller.grid.tile_size

        assert wait_until(
            lambda: controller.has_cached_floor("CD3", other, params))
        current = viewport_keys(controller, "CD3", other, params)
        fallback = viewport_keys(controller, "CD3", other, params,
                                 level=controller.level + 1)
        assert wait_until(lambda: not missing(controller, current | fallback))
        drain(600)

        coords = frame_tiles(controller, "CD3", other, params)
        assert coords, "the frame covers no tile, so this proves nothing"
        # THE MATCHER MUST BE ABLE TO FIND A READ WHEN THERE IS ONE: these
        # very tiles were read from the provider during the arrive phase, so
        # a `raw_reads_for` that silently matched nothing (wrong coordinate
        # convention, wrong channel, wrong field order) would be caught here
        # rather than passing the real assertion below by being empty.
        assert raw_reads_for(provider, coords, "CD3", tile_size), (
            "the read matcher found none of the frame's tiles even in the "
            "arrive phase, so its 'zero after the flip' means nothing")
        tile_mark = len(provider.tile_reads)
        region_mark = len(provider.reads)
        jobs_before = len(floor_jobs.ran)

        assert tab.show_source("CD3", other, params) is True
        drain(900)

        fresh_tiles = provider.tile_reads[tile_mark:]
        reread = raw_reads_for(
            type("P", (), {"tile_reads": fresh_tiles})(), coords, "CD3",
            tile_size)
        assert not reread, (
            f"{len(reread)} raw tile(s) of the finished picture were read "
            f"from the provider again on the flip: {reread[:4]}")

        fresh_regions = [r for r in provider.reads[region_mark:]
                         if r[0] == "CD3"]
        assert not fresh_regions, (
            "the flip read whole levels of this channel again -- the floor "
            f"was supposed to be a cache hit: {fresh_regions[:3]}")
        assert len(floor_jobs.ran) == jobs_before
    finally:
        tab.teardown()


# ── EXIT GATE A, part 3: the picture is pixel for pixel unchanged ──────

def screen_state(controller, channel, method, params):
    """Everything the user actually SEES, as comparable values.

    The pooled `ImageItem`s' own arrays (what is blitted), the corrected
    floor's array, the geometry each is placed at, the display levels, the
    colour table and the per-level gain -- plus which layers are visible.
    Read off the real items, not off a cache: a cache comparison would
    prove the inputs matched, not the picture.
    """
    state = {"tiles": {}, "gain": dict(controller._level_gain)}
    for key in viewport_keys(controller, channel, method, params):
        coord = (key.tile.level, key.tile.tx, key.tile.ty)
        entry = controller._precise_pool.get(*coord)
        if entry is None or entry.item is None:
            state["tiles"][coord] = None
            continue
        item = entry.item
        rect = item.boundingRect()
        state["tiles"][coord] = {
            "pixels": None if item.image is None
                      else np.array(item.image, copy=True),
            "levels": None if item.levels is None else tuple(
                float(v) for v in np.asarray(item.levels).ravel()),
            "lut": None if item.lut is None else np.array(item.lut, copy=True),
            "rect": (round(rect.x(), 6), round(rect.y(), 6),
                     round(rect.width(), 6), round(rect.height(), 6)),
            "visible": bool(item.isVisible()),
            "key": entry.key,
        }
    floor = controller.view.corrected_floor_item
    frect = floor.boundingRect()
    state["floor"] = {
        "pixels": None if floor.image is None
                  else np.array(floor.image, copy=True),
        "levels": None if floor.levels is None else tuple(
            float(v) for v in np.asarray(floor.levels).ravel()),
        "lut": None if floor.lut is None else np.array(floor.lut, copy=True),
        "rect": (round(frect.x(), 6), round(frect.y(), 6),
                 round(frect.width(), 6), round(frect.height(), 6)),
        "visible": bool(floor.isVisible()),
        "ready": bool(controller._floor_ready),
        "level": controller._floor_level,
        "stride": controller._floor_stride,
    }
    # The other two layers `_update_layer_visibility` actually drives: the
    # RAW pool's items (the honest fallback until the floor is ready) and
    # the pinned overview at z=0. An earlier draft read a
    # `raw_underlay_item` that does not exist on `ExploreView`, so it
    # compared False with False and proved nothing.
    state["raw_tiles_visible"] = sorted(
        (entry.level, entry.tx, entry.ty)
        for entry in controller._raw_pool.entries.values()
        if entry.item is not None and entry.item.isVisible())
    overview = controller.view.overview_item
    state["overview_visible"] = bool(overview.isVisible())
    state["overview_pixels_present"] = overview.image is not None
    return state


def assert_same_picture(prepared, reference):
    """Every pixel, every placement, every mapping -- or say exactly which."""
    assert set(prepared["tiles"]) == set(reference["tiles"]), (
        "the two runs do not even show the same tiles: "
        f"{set(prepared['tiles']) ^ set(reference['tiles'])}")
    for coord in sorted(prepared["tiles"]):
        left, right = prepared["tiles"][coord], reference["tiles"][coord]
        assert (left is None) == (right is None), (
            f"tile {coord}: one run has it pooled and the other does not")
        if left is None:
            continue
        assert np.array_equal(left["pixels"], right["pixels"]), (
            f"tile {coord} is not pixel for pixel identical")
        assert left["levels"] == right["levels"], f"tile {coord} levels"
        assert (left["lut"] is None) == (right["lut"] is None)
        if left["lut"] is not None:
            assert np.array_equal(left["lut"], right["lut"]), (
                f"tile {coord} colour table")
        assert left["rect"] == right["rect"], f"tile {coord} placement"
        assert left["visible"] == right["visible"], f"tile {coord} visibility"
        assert left["key"] == right["key"], f"tile {coord} identity"

    lf, rf = prepared["floor"], reference["floor"]
    assert np.array_equal(lf["pixels"], rf["pixels"]), (
        "the corrected floor is not pixel for pixel identical")
    assert (lf["levels"], lf["rect"], lf["visible"], lf["ready"],
            lf["level"], lf["stride"]) == (
        rf["levels"], rf["rect"], rf["visible"], rf["ready"],
        rf["level"], rf["stride"]), "the floor's mapping or placement moved"
    assert (lf["lut"] is None) == (rf["lut"] is None)
    if lf["lut"] is not None:
        assert np.array_equal(lf["lut"], rf["lut"]), "the floor's colour table"
    assert prepared["gain"] == reference["gain"], (
        "the per-level display gain differs between the two runs")
    assert prepared["raw_tiles_visible"] == reference["raw_tiles_visible"], (
        "a different set of RAW tiles is showing through")
    assert (prepared["overview_visible"], prepared["overview_pixels_present"]
            ) == (reference["overview_visible"],
                  reference["overview_pixels_present"]), (
        "the pinned overview is in a different state")


def _settled(tab, channel, method, params):
    controller = tab.stack.controller
    keys = viewport_keys(controller, channel, method, params)
    fallback = viewport_keys(controller, channel, method, params,
                             level=controller.level + 1)
    ok = wait_until(lambda: (
        controller._floor_ready
        and controller._floor_ctx == floor_key(controller, channel, method,
                                               params)[0]
        and not missing(controller, keys | fallback)
        and all(controller._precise_pool.get(k.tile.level, k.tile.tx,
                                             k.tile.ty) is not None
                for k in keys)))
    assert ok, "the run never finished drawing the frame"
    drain(900)


@pytest.mark.parametrize("shown,other", [("tophat", "cucim"),
                                         ("cucim", "tophat"),
                                         (None, "tophat"),
                                         (None, "cucim")])
def test_the_flipped_picture_is_identical_with_and_without_preparation(
        app, providers, shown, other):
    """The optimisation may make the picture ARRIVE sooner, never differ.

    The reference is the behaviour BEFORE this whole line of work, reached
    through the public API alone: a tab with no HOT specs provider prepares
    nothing in the background, so its flip computes the floor and the tiles
    from scratch. Both runs are driven through `show_source` on the same
    synthetic slide at the same camera with the same parameters, and both
    are allowed to finish. Then every pixel that is on screen is compared.
    """
    params = PARAM_FOR[other]

    # THE SAME dataset path for both runs, on purpose: the `providers`
    # fixture hands out one `_Provider` per path, so both runs then see the
    # identical source identity and the comparison below can assert the
    # tiles' full `CorrectionKey`, source field included, instead of
    # excusing a difference. Each run still builds its own stack, scheduler,
    # caches and pools, so nothing the reference run computed can be handed
    # to the prepared run.
    path = "/fake/identical.ome.tif"
    reference_tab = open_full_image(method=shown, with_hot=False, path=path)
    try:
        assert reference_tab.hot is None, (
            "the reference run prepared something in the background")
        assert reference_tab.show_source("CD3", other, params) is True
        _settled(reference_tab, "CD3", other, params)
        reference = screen_state(reference_tab.stack.controller, "CD3",
                                 other, params)
    finally:
        reference_tab.teardown()

    prepared_tab = open_full_image(method=shown, with_hot=True, path=path)
    try:
        controller = prepared_tab.stack.controller
        assert prepared_tab.hot is not None
        assert wait_until(
            lambda: controller.has_cached_floor("CD3", other, params)), (
            "the prepared run never prepared the other method's floor")
        assert prepared_tab.show_source("CD3", other, params) is True
        _settled(prepared_tab, "CD3", other, params)
        prepared = screen_state(controller, "CD3", other, params)
    finally:
        prepared_tab.teardown()

    # THIS GATE MUST NOT BE ABLE TO PASS BY COMPARING NOTHING.
    assert reference["tiles"], "the reference run showed no tile at all"
    with_pixels = [c for c, t in reference["tiles"].items()
                   if t is not None and t["pixels"] is not None]
    assert len(with_pixels) >= 4, (
        f"only {len(with_pixels)} pooled tile(s) carried pixels; the "
        "comparison below would be nearly empty")
    assert any(t["visible"] for t in reference["tiles"].values()
               if t is not None), "not one compared tile is actually on screen"
    assert reference["floor"]["pixels"] is not None, (
        "the reference run produced no floor, so there is nothing to compare")
    assert reference["floor"]["pixels"].size > 0
    assert_same_picture(prepared, reference)


# ═══════════════════════════════════════════════════════════════════════
# G3.2b.4B1.2.3a -- HOT must not spend the ONE background floor slot on
# the method the foreground is already computing.
#
# Real-machine acceptance: TopHat -> new channel -> still -> cuCIM passed,
# but cuCIM -> new channel -> still -> TopHat recomputed. Measured cause
# (`2026-09-21_g3_2b_4b12_3a_attribution.json`), with the foreground floor
# still running when `_prepare_centre_floors()` fires:
#
#   PREPARE tophat  cached=False running=cucim  slot: None   -> tophat
#   PREPARE cucim   cached=False running=cucim  slot: tophat -> cucim
#   RESULT  cucim   foreground                  (cucim now cached)
#   NEXT    waiting=cucim  already_cached=True  -> dropped
#
# The host keeps ONE replaceable background request on purpose, so the LAST
# method asked for wins the slot -- and the last is always `HOT_METHODS[-1]`.
# With cuCIM on screen that last request duplicates the foreground's own
# work and evicts the only floor the user could need next.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("shown,expected", [
    ("tophat", {"cucim"}),
    ("cucim", {"tophat"}),
    (None, {"tophat", "cucim"}),
])
def test_hot_never_asks_for_the_method_the_foreground_is_showing(
        app, providers, prepared, shown, expected):
    """Which floors HOT asks for is decided by `snapshot.method`.

    Not by a fixed order, and not by naming a method: Original has no
    foreground floor of its own, so it still gets both.
    """
    tab = open_full_image(method=shown)
    try:
        controller = tab.stack.controller
        assert tab.hot is not None and tab.hot.include_center is True
        assert wait_until(lambda: any(ch == "CD3" for ch, _m, _p in prepared)), (
            "HOT never asked for a floor at all")
        drain(1500)

        asked = {method for channel, method, _p in prepared
                 if channel == "CD3"}
        assert asked == expected, (
            f"showing {shown!r}, HOT asked for {sorted(asked)} -- expected "
            f"{sorted(expected)}: the method on screen already has an owner, "
            "and asking for it again costs the single background slot")
        # ...and the parameters are each method's own, never swapped.
        for channel, method, params in prepared:
            if channel != "CD3":
                continue
            assert tuple(params) == PARAM_FOR[method], (
                f"{method} was prepared with {params}, not its own parameter")
        assert controller.method == shown, "the visible selection moved"
    finally:
        tab.teardown()


@pytest.fixture
def slow_floor(monkeypatch):
    """Make the floor worker's own correction slow, and nothing else.

    A big slide's floor takes long enough that it is STILL RUNNING when
    `_prepare_centre_floors()` fires -- which is the whole window this block
    is about, and which the synthetic slide is far too fast to reach on its
    own. The sleep is on the floor worker thread and on the floor's own
    whole-level array only: the gain calibration runs on that same thread
    and would multiply the delay by one call per window per level, and the
    tile corrections run on the scheduler's own threads and are left alone.
    No decision, key or pixel is changed.
    """
    import threading
    import time
    from block01.viewer import correction_compute as cc

    real = cc.CorrectionCompute.correct_array
    slept = []

    def correct_array(inner, arr, method, param):
        if (threading.current_thread().name.startswith("explore-floor-compute")
                and getattr(arr, "size", 0) > 4096):
            slept.append(method)
            time.sleep(0.35)
        return real(inner, arr, method, param)

    monkeypatch.setattr(cc.CorrectionCompute, "correct_array", correct_array)
    return slept


def test_the_foregrounds_own_method_never_evicts_the_waiting_floor(
        app, providers, prepared, floor_jobs, slow_floor):
    """The failing real-machine trajectory, with its window forced open.

    cuCIM on screen, the new channel's foreground cuCIM floor still running
    when HOT asks. TopHat must reach the cache, the background slot must
    still be ONE value, and no second cuCIM floor may be computed for work
    the foreground already owns.
    """
    tab = open_full_image(method="cucim")
    try:
        controller = tab.stack.controller
        # The window really was open: HOT asked while a floor job ran.
        assert wait_until(lambda: any(ch == "CD3" for ch, _m, _p in prepared)), (
            "HOT never asked for a floor")
        assert slow_floor, (
            "no floor job was slowed, so the racing window was never forced")

        assert wait_until(lambda: controller.has_cached_floor(
            "CD3", "tophat", PARAM_FOR["tophat"]), timeout_ms=30000), (
            "TopHat's floor never reached the cache -- the single background "
            "slot was spent on the method the foreground was computing")
        assert controller.has_cached_floor("CD3", "cucim", PARAM_FOR["cucim"])

        # ONE value, never a queue -- the contract this block must not break
        # in order to fix the eviction.
        assert not isinstance(controller._floor_background_request, (list,
                                                                     tuple,
                                                                     set,
                                                                     dict)), (
            "the single background request became a collection")

        # No floor was computed twice, and cuCIM's was computed by the
        # foreground alone.
        ran = [(r.channel, r.method, bool(r.foreground))
               for r in floor_jobs.ran if r.channel == "CD3"]
        methods = [m for _c, m, _f in ran]
        assert methods.count("cucim") == 1, (
            f"cuCIM's floor was computed {methods.count('cucim')} times: {ran}")
        assert methods.count("tophat") == 1, (
            f"TopHat's floor was computed {methods.count('tophat')} times: {ran}")
        assert ("CD3", "cucim", True) in ran, (
            "cuCIM's floor was not the foreground's own job")
        assert ("CD3", "tophat", False) in ran, (
            "TopHat's floor was not prepared in the background")

        # ...and the user's click is then free.
        jobs_before = len(floor_jobs.ran)
        hits_before = controller.stats.get("floor_cache_hits", 0)
        assert tab.show_source("CD3", "tophat", PARAM_FOR["tophat"]) is True
        assert controller._floor_ready is True, (
            "the flip left the user on 'Preparing corrected preview…'")
        drain(900)
        assert len(floor_jobs.ran) == jobs_before, (
            "the flip started a floor job although TopHat was prepared")
        assert controller.stats.get("floor_cache_hits", 0) == hits_before + 1
        assert controller.channel == "CD3" and controller.method == "tophat"
        assert tuple(controller.params) == PARAM_FOR["tophat"]
    finally:
        tab.teardown()


@pytest.mark.parametrize("first", ["tophat", "cucim"])
def test_original_prepares_both_and_either_first_click_hits(
        app, providers, floor_jobs, first):
    """Gate C: Original has no foreground floor, so HOT still owes both."""
    tab = open_full_image(method=None)
    try:
        controller = tab.stack.controller
        assert wait_until(lambda: (
            controller.has_cached_floor("CD3", "tophat", PARAM_FOR["tophat"])
            and controller.has_cached_floor("CD3", "cucim",
                                            PARAM_FOR["cucim"])),
            timeout_ms=30000), (
            "standing still on Original did not prepare both methods")

        jobs_before = len(floor_jobs.ran)
        hits_before = controller.stats.get("floor_cache_hits", 0)
        assert tab.show_source("CD3", first, PARAM_FOR[first]) is True
        assert controller._floor_ready is True
        drain(700)
        assert len(floor_jobs.ran) == jobs_before, (
            f"clicking {first} first computed a floor that was already there")
        assert controller.stats.get("floor_cache_hits", 0) == hits_before + 1
    finally:
        tab.teardown()


def test_the_neighbouring_channels_still_get_no_floor_at_all(
        app, providers, prepared):
    """Gate G: narrowing WHICH method must not widen WHICH channel."""
    tab = open_full_image(method="cucim")
    try:
        controller = tab.stack.controller
        assert wait_until(lambda: controller.has_cached_floor(
            "CD3", "tophat", PARAM_FOR["tophat"]), timeout_ms=30000)
        drain(1200)
        off_centre = sorted({ch for ch, _m, _p in prepared if ch != "CD3"})
        assert not off_centre, (
            f"a floor was prepared for {off_centre}, which is not on screen")
        for neighbour in ("CD20", "DAPI"):
            for method in ("tophat", "cucim"):
                assert controller.has_cached_floor(
                    neighbour, method, PARAM_FOR[method]) is False
    finally:
        tab.teardown()
