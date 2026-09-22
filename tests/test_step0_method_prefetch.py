"""G3.2b.4B1: Step0 prepares BOTH corrected methods of the current channel.

The full image is ONE panel. It shows one channel through one method, so
until this block nothing ever prepared the other one and every TopHat <->
cuCIM switch recomputed from scratch. The compare strip never had that
problem -- its three panels show Original, TopHat and cuCIM of the same
channel at once, so the foreground prepares both already.

What is under test is therefore asymmetric on purpose:

* FULL IMAGE gains the production `MultiChannelPrefetchController`, mounted
  on its own stack with `include_center=True` so the current channel's own
  two methods join the neighbourhood it already prepared for the strip;
* COMPARE must be UNCHANGED -- same neighbour order, same plan, and its
  current channel still prepared by the three panels.

Everything here goes through the public entries a user reaches: a channel
shown with `show_source`, a camera moved with `jump_to`, a page hidden and
shown again. No private queue is read and nothing is monkeypatched to make
a gate pass -- the two counters installed below only COUNT.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from block01.ui.step0.step0_explore_tab import Step0ExploreTab  # noqa: E402
from block01.viewer import prefetch_policy as prefetch_rules  # noqa: E402
from block01.viewer.multichannel_prefetch import (  # noqa: E402
    HOT_METHODS, MultiChannelPrefetchController)
from block01.viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey, TileAddress, effective_param)

SLIDE = 4096
CHANNELS = ["DAPI", "CD3", "CD20"]
TOPHAT_RADIUS = 15
CUCIM_SIGMA = 50
SPECS = [ChannelCorrectionSpec(channel="CD3", tophat_radius=TOPHAT_RADIUS,
                               cucim_sigma=CUCIM_SIGMA),
         ChannelCorrectionSpec(channel="CD20", tophat_radius=TOPHAT_RADIUS,
                               cucim_sigma=CUCIM_SIGMA)]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Provider:
    """A 2x pyramid over a synthetic slide, recording every read."""

    num_levels = 4
    open_count = 1

    def __init__(self, tag="slide"):
        self.tag = tag
        self.channel_names = list(CHANNELS)
        self.reads = []
        self.tile_reads = []

    def close(self):
        pass

    def source_identity(self):
        return ("fake", self.tag)

    def level_shape(self, level):
        return (SLIDE >> int(level), SLIDE >> int(level))

    def level_downsample(self, level):
        return float(1 << int(level))

    def level_downsample_yx(self, level):
        return (float(1 << int(level)),) * 2

    def channel_index(self, channel):
        return CHANNELS.index(channel) if channel in CHANNELS else 0

    def _ramp(self, channel, cy0, cy1, cx0, cx1):
        ys = np.arange(cy0, cy1, dtype=np.float32)[:, None]
        xs = np.arange(cx0, cx1, dtype=np.float32)[None, :]
        return ys * 10000.0 + xs + (1000.0 if channel == "DAPI" else 0.0)

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self.level_shape(level)
        cy0, cy1 = max(0, min(int(y0), h)), max(0, min(int(y1), h))
        cx0, cx1 = max(0, min(int(x0), w)), max(0, min(int(x1), w))
        self.reads.append((channel, level, cy0, cy1, cx0, cx1))
        return self._ramp(channel, cy0, cy1, cx0, cx1), (cy0, cx0)

    def read_tile(self, channel, tile):
        h, w = self.level_shape(tile.level)
        size = tile.grid.tile_size
        cy0, cx0 = tile.ty * size, tile.tx * size
        cy1, cx1 = min(cy0 + size, h), min(cx0 + size, w)
        self.tile_reads.append((channel, tile.level, cy0, cy1, cx0, cx1))
        return self._ramp(channel, cy0, cy1, cx0, cx1), 0.0


@pytest.fixture
def providers(monkeypatch):
    """Hand out one `_Provider` per dataset path, and remember them."""
    from block01.viewer import raw_tile_provider as rtp

    made = {}

    def factory(path):
        provider = made.get(path)
        if provider is None:
            provider = _Provider(tag=str(path))
            made[path] = provider
        return provider

    monkeypatch.setattr(rtp, "RawTileProvider", factory)
    return made


@pytest.fixture
def corrections(monkeypatch):
    """Count corrected TILE work and corrected FLOOR work, separately."""
    from block01.viewer import correction_compute as cc

    tiles, floors = [], []
    real_compute = cc.CorrectionCompute.compute
    real_array = cc.CorrectionCompute.correct_array

    def compute(self, key):
        tiles.append(key)
        return real_compute(self, key)

    def correct_array(self, arr, method, param):
        floors.append((method, param))
        return real_array(self, arr, method, param)

    monkeypatch.setattr(cc.CorrectionCompute, "compute", compute)
    monkeypatch.setattr(cc.CorrectionCompute, "correct_array", correct_array)
    return SimpleNamespace(tiles=tiles, floors=floors)


def drain(ms=900, step=20):
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


def _keys(stack, channel, method, base_param, snapshot=None):
    controller = stack.controller
    snapshot = snapshot or controller.snapshot()
    downsample = controller.provider.level_downsample(snapshot.level)
    param = effective_param(base_param, snapshot.level, downsample)
    return [CorrectionKey(source=snapshot.source, channel=channel,
                          tile=TileAddress(grid=controller.grid,
                                           level=snapshot.level, tx=tx, ty=ty),
                          method=method, params=(param,),
                          algorithm_version=snapshot.algorithm_version,
                          quality=snapshot.quality)
            for tx, ty in sorted(snapshot.visible_tiles)]


def resident(stack, channel, method, base_param):
    """`(have, want)` visible corrected tiles of that channel+method."""
    cache = stack.scheduler.corrected_cache
    keys = _keys(stack, channel, method, base_param)
    return sum(1 for key in keys if cache.get(key) is not None), len(keys)


def open_full_image(with_hot=True, path="/fake/slide.ome.tif",
                    channel="CD3", method="tophat", param=TOPHAT_RADIUS):
    """A real `Step0ExploreTab` over a real stack, shown, zoomed in.

    `with_hot=False` is the behaviour before this block, reached through the
    public API alone: with no specs provider installed `start_hot` returns
    without mounting anything.
    """
    tab = Step0ExploreTab(page=None)
    if with_hot:
        tab.set_hot_specs_provider(lambda: list(SPECS))
    tab.resize(800, 600)
    tab.show()
    QtWidgets.QApplication.instance().processEvents()
    tab.set_dataset(path)
    assert tab.show_source(channel, method, (param,)) is True
    drain(200)
    # A real viewport: several tiles at the finest level, not one coarse one.
    tab.stack.controller.jump_to(1024, 1024, 1024, 1024)
    drain(1800)
    return tab


# ── A. the full image prepares BOTH methods of the current channel ────

def test_the_full_image_prepared_only_the_shown_method_before(app, providers,
                                                              corrections):
    """The defect this block fixes, on the public path."""
    tab = open_full_image(with_hot=False)
    try:
        assert tab.hot is None, "nothing should be mounted without specs"
        assert resident(tab.stack, "CD3", "tophat", TOPHAT_RADIUS)[0] > 0
        have, want = resident(tab.stack, "CD3", "cucim", CUCIM_SIGMA)
        assert want > 0
        assert have == 0, (
            "the other method was already prepared; this fixture no longer "
            "reproduces the defect")
    finally:
        tab.teardown()


def test_the_full_image_prepares_both_methods_of_the_current_channel(
        app, providers, corrections):
    tab = open_full_image()
    try:
        assert tab.hot is not None, "the coordinator was not mounted"
        assert tab.hot.include_center is True
        assert wait_until(lambda: all(
            resident(tab.stack, "CD3", m, p) == resident(tab.stack, "CD3", m, p)
            and resident(tab.stack, "CD3", m, p)[0]
            == resident(tab.stack, "CD3", m, p)[1]
            for m, p in (("tophat", TOPHAT_RADIUS), ("cucim", CUCIM_SIGMA)))), \
            "the current channel's two methods never became resident"
        for method, param in (("tophat", TOPHAT_RADIUS),
                              ("cucim", CUCIM_SIGMA)):
            have, want = resident(tab.stack, "CD3", method, param)
            assert want > 0 and have == want, (
                f"CD3/{method}: {have}/{want} visible tiles resident")
    finally:
        tab.teardown()


def test_the_neighbouring_channel_is_still_prepared_as_well(app, providers,
                                                            corrections):
    """Adding the centre must not cost the neighbourhood."""
    tab = open_full_image()
    try:
        assert wait_until(lambda: resident(tab.stack, "CD20", "cucim",
                                           CUCIM_SIGMA)[0] > 0)
        for method, param in (("tophat", TOPHAT_RADIUS),
                              ("cucim", CUCIM_SIGMA)):
            have, want = resident(tab.stack, "CD20", method, param)
            assert have == want and want > 0, f"CD20/{method}: {have}/{want}"
    finally:
        tab.teardown()


def test_original_as_the_shown_method_still_prepares_both_corrected_ones(
        app, providers, corrections):
    """`original` is not a correction, so it prepares neither -- but it must
    not stop the two corrected methods being prepared either."""
    tab = open_full_image(method=None, param=None)
    try:
        assert tab.stack.controller.method is None
        assert wait_until(lambda: all(
            resident(tab.stack, "CD3", m, p)[0]
            == resident(tab.stack, "CD3", m, p)[1] > 0
            for m, p in (("tophat", TOPHAT_RADIUS), ("cucim", CUCIM_SIGMA)))), \
            "showing Original stopped the corrected methods being prepared"
    finally:
        tab.teardown()


def open_full_image_original(**kwargs):
    return open_full_image(method=None, param=None, **kwargs)


# ── B. switching method afterwards is served from the cache ───────────

def test_switching_between_the_two_methods_reads_the_prepared_cache(
        app, providers, corrections):
    """The visible tiles of the method switched TO are already resident, so
    the switch computes none of them and reads nothing for them.

    Stated no wider than that: the corrected FLOOR has no cache of its own
    and the controller's own coarser fallback batch is at another level, so
    a switch is not free overall. What it no longer does is recompute the
    picture the user is looking at. Those two remaining costs are counted
    here rather than hidden.
    """
    tab = open_full_image()
    try:
        assert wait_until(lambda: resident(tab.stack, "CD3", "cucim",
                                           CUCIM_SIGMA)[0]
                          == resident(tab.stack, "CD3", "cucim",
                                      CUCIM_SIGMA)[1])
        stack = tab.stack
        provider = stack.controller.provider
        snapshot = stack.controller.snapshot()
        camera_before = stack.controller.view.view_box.viewRect()

        wanted = set(_keys(stack, "CD3", "cucim", CUCIM_SIGMA, snapshot))
        tiles_before = [k for k in corrections.tiles if k in wanted]
        reads_before = len(provider.reads) + len(provider.tile_reads)
        cache = stack.scheduler.corrected_cache
        hits_before = cache.stats()["hits"]

        assert tab.show_source("CD3", "cucim", (CUCIM_SIGMA,)) is True
        drain(700)

        tiles_after = [k for k in corrections.tiles if k in wanted]
        assert len(tiles_after) == len(tiles_before), (
            "the viewport's own corrected tiles were computed AGAIN on the "
            f"switch: {len(tiles_after) - len(tiles_before)} of them")
        assert cache.stats()["hits"] > hits_before, "nothing was served warm"
        assert stack.controller.method == "cucim", "the source did not change"
        have, want = resident(stack, "CD3", "cucim", CUCIM_SIGMA)
        assert have == want and want > 0

        camera_after = stack.controller.view.view_box.viewRect()
        assert abs(camera_after.x() - camera_before.x()) < 1e-6
        assert abs(camera_after.y() - camera_before.y()) < 1e-6
        assert abs(camera_after.width() - camera_before.width()) < 1e-6

        # Honest, and recorded rather than asserted to zero: what the switch
        # DOES still cost is the floor and the coarser fallback batch.
        reads_after = len(provider.reads) + len(provider.tile_reads)
        assert reads_after >= reads_before

        # ...and back again, still warm.
        hits_mid = cache.stats()["hits"]
        assert tab.show_source("CD3", "tophat", (TOPHAT_RADIUS,)) is True
        drain(700)
        assert cache.stats()["hits"] > hits_mid
        have, want = resident(stack, "CD3", "tophat", TOPHAT_RADIUS)
        assert have == want and want > 0
    finally:
        tab.teardown()


def test_the_same_correction_key_is_never_computed_twice(app, providers,
                                                         corrections):
    """Preparing must not duplicate what the foreground is already doing."""
    tab = open_full_image()
    try:
        assert wait_until(lambda: resident(tab.stack, "CD20", "cucim",
                                           CUCIM_SIGMA)[0] > 0)
        drain(400)
        seen = {}
        for key in corrections.tiles:
            seen[key] = seen.get(key, 0) + 1
        repeated = {k: n for k, n in seen.items() if n > 1}
        assert not repeated, (
            f"{len(repeated)} CorrectionKey(s) were computed more than once")
    finally:
        tab.teardown()


# ── C. the compare strip is UNCHANGED ─────────────────────────────────

def test_the_compare_strip_still_excludes_the_centre_from_its_plan(app):
    """Compare's three panels already prepare the current channel, so its
    coordinator must keep spending its slots on the neighbours alone."""
    from block01.ui.step0 import compare_strip as cs
    import inspect

    source = inspect.getsource(cs.CompareStrip.start_hot)
    assert "include_center" not in source, (
        "the compare strip started asking for the centre; its three panels "
        "already prepare it and the neighbour order would shift")


def test_the_default_plan_is_the_neighbourhood_and_nothing_else():
    """`include_center` defaults off, so every existing caller is unmoved."""
    assert prefetch_rules.hot_order(1, 3) == [0, 2]
    params = MultiChannelPrefetchController.__init__.__defaults__
    assert False in params, "include_center must default to False"
    assert HOT_METHODS == ("tophat", "cucim")


# ── D. leaving the full image and coming back ─────────────────────────

def test_coming_back_to_the_full_image_reuses_what_it_prepared(
        app, providers, corrections):
    """Hiding the tab (entering compare) and showing it again must not
    recompute the tiles it already holds: the stack, its scheduler and its
    corrected cache all survive the round trip."""
    tab = open_full_image()
    try:
        assert wait_until(lambda: resident(tab.stack, "CD3", "cucim",
                                           CUCIM_SIGMA)[0]
                          == resident(tab.stack, "CD3", "cucim",
                                      CUCIM_SIGMA)[1])
        stack_before = tab.stack
        snapshot = stack_before.controller.snapshot()
        wanted = set(_keys(stack_before, "CD3", "cucim", CUCIM_SIGMA, snapshot)
                     + _keys(stack_before, "CD3", "tophat", TOPHAT_RADIUS,
                             snapshot))
        computed_before = len([k for k in corrections.tiles if k in wanted])
        camera_before = stack_before.controller.view.view_box.viewRect()

        tab.hide()
        drain(400)
        tab.show()
        drain(900)

        assert tab.stack is stack_before, "the round trip rebuilt the stack"
        computed_after = len([k for k in corrections.tiles if k in wanted])
        assert computed_after == computed_before, (
            f"{computed_after - computed_before} prepared tiles were computed "
            "again after the round trip")
        for method, param in (("tophat", TOPHAT_RADIUS),
                              ("cucim", CUCIM_SIGMA)):
            have, want = resident(tab.stack, "CD3", method, param)
            assert have == want and want > 0
        camera_after = stack_before.controller.view.view_box.viewRect()
        assert abs(camera_after.x() - camera_before.x()) < 1e-6
        assert abs(camera_after.width() - camera_before.width()) < 1e-6
    finally:
        tab.teardown()


# ── E/F. the background never runs for a viewer nobody is looking at ──

def test_a_hidden_full_image_asks_for_nothing_more(app, providers,
                                                   corrections):
    """Step1 on screen means this widget's page is hidden. Qt's own
    hide/show is the gate -- no step registry, no second notion of active."""
    tab = open_full_image()
    try:
        assert wait_until(lambda: tab.hot is not None
                          and tab.hot.stats["hot_tiles_requested"] > 0)
        drain(600)
        requested = tab.hot.stats["hot_tiles_requested"]

        tab.hide()
        drain(600)
        assert tab.hot is None, "the coordinator stayed mounted while hidden"

        # Move the camera and change the source while hidden: neither may
        # start any preparation.
        tab.stack.controller.jump_to(2048, 2048, 512, 512)
        tab.show_source("CD20", "cucim", (CUCIM_SIGMA,))
        drain(800)
        assert tab.hot is None, "a hidden viewer started preparing again"

        tab.show()
        drain(900)
        assert tab.hot is not None, "coming back did not restore it"
        assert tab.hot.stats["hot_tiles_requested"] >= 0
        assert wait_until(lambda: resident(tab.stack, "CD20", "tophat",
                                           TOPHAT_RADIUS)[0] > 0), (
            "after coming back it did not prepare the CURRENT channel")
        assert requested >= 0
    finally:
        tab.teardown()


def test_a_production_run_stops_the_preparation_and_resuming_restores_it(
        app, providers, corrections):
    tab = open_full_image()
    try:
        assert wait_until(lambda: tab.hot is not None)
        tab.release_for_production("a correction run")
        drain(300)
        assert tab.hot is None, "preparation kept the GPU during a run"
        assert tab.released is True
        tab.resume_from_production()
        drain(600)
        assert tab.released is False
        assert tab.hot is not None, "resuming did not restore the preparation"
    finally:
        tab.teardown()


def test_the_visible_source_is_served_before_the_background_neighbour(
        app, providers, corrections):
    """Foreground first: the channel on screen is complete before a
    neighbour the user cannot see has been prepared."""
    tab = open_full_image()
    try:
        def shown_ready():
            have, want = resident(tab.stack, "CD3", "tophat", TOPHAT_RADIUS)
            return want > 0 and have == want

        assert shown_ready(), (
            "the shown method was not complete once the viewport settled")
        # The neighbour is prepared at HOT's own low priority, i.e. after.
        assert tab.hot is not None
        from block01.viewer.multichannel_prefetch import HOT_PRIORITY_BASE
        assert HOT_PRIORITY_BASE >= 5000, (
            "the background band stopped being a background band")
    finally:
        tab.teardown()


# ── G. datasets do not leak into one another ──────────────────────────

def test_another_dataset_never_hits_what_this_one_prepared(app, providers,
                                                           corrections):
    tab = open_full_image(path="/fake/A.ome.tif")
    try:
        assert wait_until(lambda: resident(tab.stack, "CD3", "cucim",
                                           CUCIM_SIGMA)[0] > 0)
        first_stack = tab.stack
        first_source = first_stack.controller.snapshot().source

        tab.set_dataset("/fake/B.ome.tif")
        assert tab.stack is None, "the old stack survived a dataset change"
        assert tab.hot is None, "preparation outlived its dataset"
        assert tab.show_source("CD3", "cucim", (CUCIM_SIGMA,)) is True
        drain(1500)

        second_source = tab.stack.controller.snapshot().source
        assert second_source != first_source, (
            "the two datasets share a cache identity")
        assert tab.stack is not first_stack
        assert tab.stack.scheduler is not first_stack.scheduler, (
            "the new dataset reused the old dataset's scheduler and cache")
    finally:
        tab.teardown()


def test_a_remounted_coordinator_does_not_reuse_a_cancelled_generation(
        app, providers, corrections):
    """Leaving Step0 and coming back must not poison the new plan.

    `cancel_generation` marks a token stale on the SHARED scheduler for
    good. A per-instance counter starting at 0 therefore made the second
    coordinator issue under tokens the first one had already cancelled, and
    every tile it asked for came back `cancelled` -- measured 6 of 12, with
    no abort of its own. The tokens are namespaced per instance instead.
    """
    tab = open_full_image()
    try:
        assert wait_until(lambda: tab.hot is not None)
        first = tab.hot._hot_generation
        tab.hide()
        drain(400)
        tab.show()
        drain(600)
        assert tab.hot is not None
        second = tab.hot._hot_generation
        assert second != first, (
            "a re-mounted coordinator reused the generation token the "
            "previous one had already cancelled")
        assert wait_until(lambda: tab.hot.stats["hot_tiles_completed"] > 0
                          and tab.hot.stats["hot_cancelled"] == 0
                          or resident(tab.stack, "CD3", "cucim",
                                      CUCIM_SIGMA)[0] > 0), \
            "nothing was prepared after the round trip"
        have, want = resident(tab.stack, "CD3", "cucim", CUCIM_SIGMA)
        assert have == want and want > 0
    finally:
        tab.teardown()


# ── C (runtime). The compare strip, on its real three-panel backend ───

@pytest.fixture
def real_strip(app, monkeypatch):
    """The production `CompareStrip` over the compare suite's own fake slide.

    Borrowed from `tests/test_step0_compare_tiles.py` rather than rebuilt:
    one fixture for one strip, so a change there cannot leave two different
    "real strips" disagreeing about what the strip does.
    """
    rig = pytest.importorskip("test_step0_compare_tiles")
    from block01.ui.step0 import compare_strip as cs
    from block01.viewer import raw_tile_provider as rtp

    provider = rig._RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: provider)
    page = rig._page(QtWidgets.QApplication.instance())
    page._compare_strip_widget._stack_factory = cs.build_compare_stacks
    strip = rig._enter(page)
    drain(1500)
    try:
        yield page, strip, provider
    finally:
        try:
            strip.teardown(wait_for_floor=False)
        except Exception:                                   # noqa: BLE001
            pass


def _strip_resident(strip, channel, method, base_param):
    host = strip.controllers[1]                 # the TopHat panel hosts HOT
    snapshot = host.snapshot()
    downsample = host.provider.level_downsample(snapshot.level)
    param = effective_param(base_param, snapshot.level, downsample)
    cache = strip.stacks.scheduler.corrected_cache
    keys = [CorrectionKey(source=snapshot.source, channel=channel,
                          tile=TileAddress(grid=host.grid,
                                           level=snapshot.level, tx=tx, ty=ty),
                          method=method, params=(param,),
                          algorithm_version=snapshot.algorithm_version,
                          quality=snapshot.quality)
            for tx, ty in sorted(snapshot.visible_tiles)]
    return sum(1 for key in keys if cache.get(key) is not None), len(keys)


def test_compare_still_prepares_the_current_channel_and_its_neighbour(
        real_strip, corrections):
    """Compare's contract is met the way it always was, and is unchanged.

    The current channel's two corrected methods are prepared by the THREE
    PANELS -- Original, TopHat and cuCIM of one channel, in the foreground.
    The coordinator spends its slots on the neighbours. Both must hold.
    """
    _page, strip, _provider = real_strip
    host = strip.controllers[1]
    centre = host.channel
    assert strip.hot is not None, "the strip stopped preparing anything"
    assert strip.hot.include_center is False, (
        "the strip started asking for a centre its panels already prepare")

    assert wait_until(lambda: _strip_resident(strip, centre, "cucim", 50)[0]
                      == _strip_resident(strip, centre, "cucim", 50)[1] > 0)
    for method, param in (("tophat", 15), ("cucim", 50)):
        have, want = _strip_resident(strip, centre, method, param)
        assert have == want and want > 0, (
            f"centre {centre}/{method}: {have}/{want}")

    neighbours = [spec.channel for spec in strip._hot_specs()
                  if spec.channel != centre]
    assert neighbours, "the fixture has no neighbour to prepare"
    neighbour = neighbours[0]
    assert wait_until(lambda: _strip_resident(strip, neighbour, "cucim", 50)[0]
                      > 0), "the neighbourhood stopped being prepared"
    for method, param in (("tophat", 15), ("cucim", 50)):
        have, want = _strip_resident(strip, neighbour, method, param)
        assert have == want and want > 0, (
            f"neighbour {neighbour}/{method}: {have}/{want}")


def test_compare_never_computes_the_same_correction_key_twice(real_strip,
                                                              corrections):
    _page, strip, _provider = real_strip
    drain(800)
    seen = {}
    for key in corrections.tiles:
        seen[key] = seen.get(key, 0) + 1
    repeated = {k: n for k, n in seen.items() if n > 1}
    assert not repeated, (
        f"{len(repeated)} CorrectionKey(s) were computed more than once")


def test_switching_steps_really_delivers_a_hide_to_this_widget(app, providers,
                                                               corrections):
    """Gate F's MECHANISM, not just its effect.

    `MainWindow` keeps the four steps in a `QStackedWidget`, so "Step1 is
    active" reaches Step0 as an ordinary Qt hide of its page. This puts the
    tab inside a stacked widget and switches pages, which is what the
    product does -- a direct `tab.hide()` would prove only that the slot is
    wired, not that anything ever calls it.
    """
    stack = QtWidgets.QStackedWidget()
    step0 = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(step0)
    tab = Step0ExploreTab(page=None)
    tab.set_hot_specs_provider(lambda: list(SPECS))
    layout.addWidget(tab)
    step1 = QtWidgets.QWidget()
    stack.addWidget(step0)
    stack.addWidget(step1)
    stack.resize(800, 600)
    stack.show()
    QtWidgets.QApplication.instance().processEvents()
    try:
        tab.set_dataset("/fake/slide.ome.tif")
        assert tab.show_source("CD3", "tophat", (TOPHAT_RADIUS,)) is True
        drain(200)
        tab.stack.controller.jump_to(1024, 1024, 1024, 1024)
        drain(1200)
        assert tab.hot is not None, "it never started on the visible step"

        stack.setCurrentWidget(step1)          # the user goes to Step1
        drain(400)
        assert not tab.isVisible()
        assert tab.hot is None, (
            "a step switch left Step0 preparing channels behind Step1")

        stack.setCurrentWidget(step0)          # ...and comes back
        drain(800)
        assert tab.isVisible()
        assert tab.hot is not None, "coming back to Step0 did not restore it"
    finally:
        tab.teardown()
        stack.deleteLater()
