"""G3.2b.4B1.1: Full Image and Compare reuse ONE pair of tile caches.

Until this block the two modes each built their own raw and corrected LRU,
so every corrected tile the full image had already computed for the current
viewport, channel and parameters was computed again the moment the user
right-clicked into compare -- and again on the way back. A `CorrectionKey`
already carries the source identity, the channel, the method and the
effective parameter, so a tile computed in one mode is by construction the
right answer in the other.

WHAT IS SHARED: the full image's two `LRUByteCache` instances, and nothing
else. The scheduler, provider, `CorrectionCompute`, controllers and
ViewBoxes stay each mode's own -- sharing the scheduler would make
`suspend_for_production`'s "wait until idle" mean "wait for the other mode
too", a GUI-thread wait on somebody else's work.

WHO OWNS THEM: the full image. The strip BORROWS, exactly as it already
borrows the overview store, and therefore never empties them on the way out.
"""

import importlib.util
import os
import pathlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import compare_strip as cs  # noqa: E402
from block01.ui.step0.step0_explore_tab import ExploreStack  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402

_RIG_PATH = pathlib.Path(__file__).with_name("test_step0_compare_tiles.py")
_spec = importlib.util.spec_from_file_location("compare_rig", _RIG_PATH)
RIG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RIG)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def drain(ms=600, step=20):
    for _ in range(max(1, ms // step)):
        QtWidgets.QApplication.instance().processEvents()
        QtTest.QTest.qWait(step)


@pytest.fixture
def provider(monkeypatch):
    from block01.viewer import raw_tile_provider as rtp
    made = RIG._RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: made)
    return made


@pytest.fixture
def corrections(monkeypatch):
    """Every corrected TILE and FLOOR computation, counted apart."""
    from types import SimpleNamespace
    from block01.viewer import correction_compute as cc

    tiles, floors = [], []
    real_compute = cc.CorrectionCompute.compute
    real_array = cc.CorrectionCompute.correct_array

    def compute(inner, key):
        tiles.append(key)
        return real_compute(inner, key)

    def correct_array(inner, arr, method, param):
        floors.append((method, param))
        return real_array(inner, arr, method, param)

    monkeypatch.setattr(cc.CorrectionCompute, "compute", compute)
    monkeypatch.setattr(cc.CorrectionCompute, "correct_array", correct_array)
    return SimpleNamespace(tiles=tiles, floors=floors)


def _build_strip(page, *, caches, store=None):
    page._compare_strip_widget._stack_factory = cs.build_compare_stacks
    strip = page._compare_strip_widget
    strip.set_dataset(page.ome_path)
    stacks = strip.ensure_built(
        page.current_channel, params_for=page._compare_params_for,
        tint=page._full_image_tint(),
        nucleus=page._full_image_nucleus_args(),
        viewport_l0=None, overview_store=store, caches=caches)
    drain(800)
    return strip, stacks


# ── 1. what is shared, and what is emphatically not ──────────────────

def test_the_strip_borrows_the_caches_and_nothing_else(app, provider):
    page = RIG._page(app)
    raw_cache = LRUByteCache(1 << 20)
    corrected_cache = LRUByteCache(1 << 20)
    strip, stacks = _build_strip(page, caches=(raw_cache, corrected_cache))
    try:
        assert stacks is not None, strip._build_error
        assert stacks.caches[0] is raw_cache
        assert stacks.caches[1] is corrected_cache
        assert stacks.owns_caches is False, (
            "the strip claimed ownership of caches it was lent")
        # ...and these are emphatically its own.
        assert stacks.scheduler is not None
        assert stacks.provider is not None
        assert stacks.compute is not None
        assert len(stacks.controllers) == 3
    finally:
        strip.teardown(wait_for_floor=False)


def test_a_strip_with_no_caches_lent_still_makes_and_owns_its_own(app,
                                                                 provider):
    """A strip built with no full image behind it must keep working."""
    page = RIG._page(app)
    strip, stacks = _build_strip(page, caches=None)
    try:
        assert stacks is not None, strip._build_error
        assert stacks.owns_caches is True
        assert all(cache is not None for cache in stacks.caches)
    finally:
        strip.teardown(wait_for_floor=False)


@pytest.mark.parametrize("caches_given", [True, False])
def test_teardown_empties_only_the_caches_the_strip_owns(app, provider,
                                                         caches_given):
    page = RIG._page(app)
    raw_cache = LRUByteCache(1 << 20)
    corrected_cache = LRUByteCache(1 << 20)
    lent = (raw_cache, corrected_cache) if caches_given else None
    strip, stacks = _build_strip(page, caches=lent)
    assert stacks is not None, strip._build_error
    caches = list(stacks.caches)
    # Put something in, through the cache's own public interface.
    import numpy as np
    for cache in caches:
        cache.put(("sentinel", id(cache)), np.zeros(4, np.float32))
    before = [cache.stats()["items"] for cache in caches]
    assert all(count > 0 for count in before)

    strip.teardown(wait_for_floor=False)
    drain(300)

    after = [cache.stats()["items"] for cache in caches]
    if caches_given:
        assert after == before, (
            "the strip emptied caches it had only borrowed")
    else:
        assert all(count == 0 for count in after), (
            "the strip stopped clearing the caches it owns")


def test_the_full_image_stack_still_clears_the_caches_it_owns(app):
    """Ownership did not silently move: the owner still cleans up."""
    import numpy as np
    from types import SimpleNamespace

    raw_cache = LRUByteCache(1 << 20)
    corrected_cache = LRUByteCache(1 << 20)
    controller = SimpleNamespace(teardown=lambda **k: None)
    stack = ExploreStack(provider=None, scheduler=None, controller=controller,
                         view=None, caches=(raw_cache, corrected_cache))
    assert stack.owns_caches is True
    for cache in (raw_cache, corrected_cache):
        cache.put(("sentinel", id(cache)), np.zeros(4, np.float32))

    stack.teardown()

    assert raw_cache.stats()["items"] == 0
    assert corrected_cache.stats()["items"] == 0


def test_a_borrowing_full_image_stack_leaves_them_alone(app):
    import numpy as np
    from types import SimpleNamespace

    raw_cache = LRUByteCache(1 << 20)
    controller = SimpleNamespace(teardown=lambda **k: None)
    stack = ExploreStack(provider=None, scheduler=None, controller=controller,
                         view=None, caches=(raw_cache,), owns_caches=False)
    raw_cache.put(("sentinel", 1), np.zeros(4, np.float32))

    stack.teardown()

    assert raw_cache.stats()["items"] == 1


# ── 2. the reuse itself ──────────────────────────────────────────────

def test_a_tile_the_full_image_computed_is_not_computed_again(app, provider,
                                                              corrections):
    """The point of the sharing, on the strip's own public entry.

    The same `CorrectionKey` is put in the lent cache first; the strip must
    then answer from it rather than recompute. The key is built from the
    strip's own controller so it is exactly what the strip asks for.
    """
    page = RIG._page(app)
    raw_cache = LRUByteCache(64 << 20)
    corrected_cache = LRUByteCache(64 << 20)
    strip, stacks = _build_strip(page, caches=(raw_cache, corrected_cache))
    try:
        assert stacks is not None, strip._build_error
        drain(800)
        computed_once = [k for k in corrections.tiles]
        assert computed_once, "the fixture computed no corrected tile at all"

        # Everything it computed is now resident in the LENT cache...
        resident = [k for k in computed_once
                    if corrected_cache.get(k) is not None]
        assert resident, "nothing the strip computed reached the lent cache"

        # ...so a second strip over the same lent cache recomputes none of it.
        strip.teardown(wait_for_floor=False)
        drain(400)
        before = len(corrections.tiles)
        strip2, stacks2 = _build_strip(page, caches=(raw_cache,
                                                     corrected_cache))
        try:
            assert stacks2 is not None, strip2._build_error
            drain(800)
            again = [k for k in corrections.tiles[before:] if k in set(resident)]
            assert not again, (
                f"{len(again)} tiles the lent cache already held were "
                "computed a second time")
        finally:
            strip2.teardown(wait_for_floor=False)
    finally:
        try:
            strip.teardown(wait_for_floor=False)
        except Exception:                                   # noqa: BLE001
            pass


def test_another_dataset_never_hits_what_this_one_computed(app, provider,
                                                           corrections):
    """Source identity still decides, and it is inside the key."""
    page = RIG._page(app)
    raw_cache = LRUByteCache(64 << 20)
    corrected_cache = LRUByteCache(64 << 20)
    strip, stacks = _build_strip(page, caches=(raw_cache, corrected_cache))
    try:
        assert stacks is not None, strip._build_error
        drain(800)
        keys = list(corrections.tiles)
        assert keys
        sources = {k.source for k in keys}
        assert len(sources) == 1
        source = next(iter(sources))
        # A key that differs ONLY by source identity must miss.
        import dataclasses
        other = dataclasses.replace(keys[0], source=("fake", "another-slide"))
        assert other != keys[0]
        assert corrected_cache.get(other) is None, (
            "a key from another dataset hit this dataset's cache")
        assert corrected_cache.get(keys[0]) is not None
        assert source != ("fake", "another-slide")
    finally:
        strip.teardown(wait_for_floor=False)


# ── 3. the page really lends them, on its own entry ──────────────────

def test_the_page_lends_the_full_images_caches_when_compare_opens(app):
    """The wiring, driven by the real right-click that opens compare.

    `_page` gives the full image a stand-in stack; this gives that stand-in
    the two caches a real one has, and records what the page hands the strip.
    """
    page = RIG._page(app)
    raw_cache = LRUByteCache(1 << 20)
    corrected_cache = LRUByteCache(1 << 20)
    page._explore_tab.stack.caches = (raw_cache, corrected_cache)

    seen = {}
    real_ensure = page._compare_strip_widget.ensure_built

    def recording(channel, **kwargs):
        seen.update(kwargs)
        return real_ensure(channel, **kwargs)

    page._compare_strip_widget.ensure_built = recording
    RIG._enter(page)

    assert "caches" in seen, "the page did not offer its caches at all"
    assert seen["caches"] is not None
    assert list(seen["caches"]) == [raw_cache, corrected_cache], (
        "the page lent something other than the full image's own caches")
    # ...and the overview store it already lent is still lent.
    assert seen.get("overview_store") is not None


def test_a_full_image_with_no_caches_yet_lends_nothing(app):
    """A stand-in or half-built stack must not break the compare entry.

    The page reads the caches with `getattr(..., "caches", None)`, so a
    stack that has none simply lends none -- and the strip then makes and
    owns its own, which `test_a_strip_with_no_caches_lent_still_makes_and_owns_its_own`
    covers. What is asserted here is only that the entry still runs and
    offers `None` rather than raising.
    """
    page = RIG._page(app)
    assert not hasattr(page._explore_tab.stack, "caches"), (
        "this fixture's stand-in stack grew a `caches` attribute; the test "
        "no longer covers the missing-attribute case")

    seen = {}
    real_ensure = page._compare_strip_widget.ensure_built

    def recording(channel, **kwargs):
        seen.update(kwargs)
        return real_ensure(channel, **kwargs)

    page._compare_strip_widget.ensure_built = recording
    RIG._enter(page)

    assert "caches" in seen, "the page did not reach ensure_built at all"
    assert seen["caches"] is None


def test_a_builder_that_cannot_borrow_still_opens_compare(app):
    """`_stack_factory` is a seam, and older builders do not take `caches`.

    Several rigs -- and any builder written before caches were lent -- have
    the signature this function had before. Handing them an argument they do
    not accept raised inside `ensure_built`'s try and surfaced as "Compare
    could not be opened", which is a regression for a mode that had been
    working. The lending is therefore OFFERED, not forced.
    """
    page = RIG._page(app)
    calls = []

    def old_style_factory(path, channel, parent_widget=None, *,
                          sources=cs.COMPARE_SOURCES, params_for=None,
                          tint=None, nucleus_channel=None, nucleus_tint=None,
                          nucleus_enabled=False, viewport_l0=None,
                          overview_store=None):
        calls.append(True)
        return RIG._fake_compare_factory([])(
            path, channel, parent_widget, sources=sources,
            params_for=params_for, tint=tint, nucleus_channel=nucleus_channel,
            nucleus_tint=nucleus_tint, nucleus_enabled=nucleus_enabled,
            viewport_l0=viewport_l0, overview_store=overview_store)

    strip = page._compare_strip_widget
    strip._stack_factory = old_style_factory
    strip.set_dataset(page.ome_path)
    raw_cache = LRUByteCache(1 << 20)
    stacks = strip.ensure_built(
        page.current_channel, params_for=page._compare_params_for,
        tint=page._full_image_tint(), nucleus=page._full_image_nucleus_args(),
        viewport_l0=None, overview_store=None,
        caches=(raw_cache, LRUByteCache(1 << 20)))

    assert calls, "the old-style builder was never called"
    assert stacks is not None, (
        f"compare refused to open with an old-style builder: "
        f"{strip._build_error}")


def test_a_builder_with_kwargs_is_offered_the_caches(app):
    """`**kwargs` counts as accepting them -- do not starve a real builder."""
    seen = {}

    def kwargs_factory(path, channel, parent_widget=None, **kwargs):
        seen.update(kwargs)
        return RIG._fake_compare_factory([])(
            path, channel, parent_widget,
            **{k: v for k, v in kwargs.items() if k != "caches"})

    page = RIG._page(app)
    strip = page._compare_strip_widget
    strip._stack_factory = kwargs_factory
    strip.set_dataset(page.ome_path)
    lent = (LRUByteCache(1 << 20), LRUByteCache(1 << 20))
    strip.ensure_built(
        page.current_channel, params_for=page._compare_params_for,
        tint=page._full_image_tint(), nucleus=page._full_image_nucleus_args(),
        viewport_l0=None, overview_store=None, caches=lent)

    assert seen.get("caches") == lent
