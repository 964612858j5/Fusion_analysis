"""The preview's pixels as functions, and the cache they share.

`core/preview_compose.py` exists because the arrays had to leave the GUI
thread: a Step1 frame cost 42-85 ms on the real desk and every published frame
was a stall of that length. Copying the arithmetic into a worker is how "the
screen and the saved file drifted apart" happened once already, so there is
one implementation with its cache passed in, and two callers -- the window's
immediate redraw and the background frame worker.

Own module, and no Qt: these are functions of their arguments, which is the
property that makes them safe to call from a thread.
"""

import gc

import numpy as np
import pytest

from block01.core import preview_compose as pc
from block01.core.channel_remap import compose_multichannel_overlay


def _arr(seed=0, shape=(16, 12), scale=1000.0):
    rng = np.random.default_rng(seed)
    return (rng.random(shape, dtype=np.float32) * scale).astype(np.float32)


WINDOW = {"min": 0.0, "max": 1000.0, "gamma": 1.0}


# ── the cache's rules ────────────────────────────────────────────────────

def test_a_hit_needs_the_same_window_and_the_same_array():
    cache = pc.PreviewCache()
    a, b = _arr(1), _arr(2)
    key = (0, "CD3")

    cache.put(key, a, ("w1",), np.zeros((4, 4), np.float32))

    assert cache.get(key, a, ("w1",)) is not None
    assert cache.get(key, a, ("w2",)) is None, "a different window hit"
    assert cache.get(key, b, ("w1",)) is None, "a different array hit"


def test_the_source_is_re_verified_not_taken_on_trust():
    """`id()` is not an identity: CPython may give a released array's address
    to the next one, and a channel, shape and window that all match would then
    serve the previous patch's pixels. The entry holds a weak reference."""
    cache = pc.PreviewCache()
    key = (0, "CD3")
    a = _arr(1)
    cache.put(key, a, ("w",), np.zeros((4, 4), np.float32))
    entry = cache.entry(key)
    assert entry["ref"]() is a

    b = _arr(2)
    assert cache.get(key, b, ("w",)) is None


def test_a_released_source_takes_its_entry_with_it():
    cache = pc.PreviewCache()
    doomed = _arr(3)
    cache.put((0, "CD3"), doomed, ("w",), np.zeros((4, 4), np.float32))
    assert len(cache) == 1

    del doomed
    gc.collect()
    cache.put((0, "CD8"), _arr(4), ("w",), np.zeros((4, 4), np.float32))

    assert cache.keys() == [(0, "CD8")], \
        "an entry whose array is gone can never be hit and must not be kept"


def test_one_version_per_channel_not_one_per_slider_step():
    """A window the user dragged past has no reader, and on a real slide each
    one is 4 MiB."""
    cache = pc.PreviewCache()
    arr = _arr(5)
    for step in range(300):
        cache.put((0, "CD3"), arr, (("min", float(step)),),
                  np.zeros((4, 4), np.float32))

    assert cache.keys() == [(0, "CD3")]
    assert cache.entry((0, "CD3"))["window"] == (("min", 299.0),)


def test_a_hit_moves_the_entry_to_the_back():
    """Eviction order must be least-recently-USED, not least-recently-
    recomputed: after a patch switch the current patch's channels are the ones
    every frame hits, and nothing has recomputed them."""
    cache = pc.PreviewCache(max_entries=2)
    a, b, c = _arr(1), _arr(2), _arr(3)
    small = np.zeros((4, 4), np.float32)
    cache.put((0, "A"), a, ("w",), small)
    cache.put((0, "B"), b, ("w",), small)

    assert cache.get((0, "A"), a, ("w",)) is not None      # a HIT on A
    cache.put((0, "C"), c, ("w",), small)

    assert set(cache.keys()) == {(0, "A"), (0, "C")}, cache.keys()


def test_the_cache_obeys_both_bounds():
    """Entry count alone is not a bound: 64 signals of a 1024x1024 patch are
    256 MiB. Measured with real-size arrays, not a toy patch."""
    big = np.zeros((1024, 1024), np.float32)
    assert big.nbytes == 4 * 1024 * 1024
    sources = [_arr(i, shape=(4, 4)) for i in range(80)]
    cache = pc.PreviewCache(max_entries=64,
                            max_bytes=40 * 1024 * 1024)     # 10 arrays

    for i, source in enumerate(sources):
        cache.put((0, f"CH{i}"), source, ("w",), big)

    assert cache.nbytes() <= 40 * 1024 * 1024
    assert len(cache) <= 10


def test_dropping_a_patch_or_a_channel_keeps_the_rest():
    cache = pc.PreviewCache()
    keep = _arr(1)
    small = np.zeros((4, 4), np.float32)
    for key in ((0, "CD3"), (0, "CD8"), (1, "CD3"), (1, "CD8")):
        cache.put(key, keep, ("w",), small)

    cache.drop_patch(0)
    assert set(cache.keys()) == {(1, "CD3"), (1, "CD8")}

    cache.drop_channel("CD3")
    assert cache.keys() == [(1, "CD8")]


def test_an_unreferenceable_source_is_computed_and_not_cached():
    cache = pc.PreviewCache()
    value = np.zeros((4, 4), np.float32)

    assert cache.put((0, "CD3"), object.__new__(_NoWeakref), ("w",),
                     value) is value
    assert len(cache) == 0


class _NoWeakref:
    __slots__ = ()          # no __weakref__ slot: weakref.ref() raises


# ── the window's values, not its provenance ──────────────────────────────

def test_the_window_key_is_made_of_numbers():
    """A slider moved in the Intensity window changes the numbers while the
    source stays "step0", so a key made of labels would keep handing back
    yesterday's pixels."""
    a = pc.window_key("CD3", {"CD3": {"min": 0.0, "max": 900.0, "gamma": 1.0}})
    b = pc.window_key("CD3", {"CD3": {"min": 0.0, "max": 900.0, "gamma": 1.0}})
    c = pc.window_key("CD3", {"CD3": {"min": 0.0, "max": 901.0, "gamma": 1.0}})

    assert a == b and a != c
    assert pc.window_key("CD3", {}) == ("auto",)
    assert pc.window_key("CD3", None) == ("auto",)


# ── the pixels ───────────────────────────────────────────────────────────

def test_a_gray_is_computed_once_and_reused():
    cache = pc.PreviewCache()
    arr = _arr(7)
    remap = {"CD3": WINDOW}

    first = pc.channel_gray(0, "CD3", arr, remap, cache)
    again = pc.channel_gray(0, "CD3", arr, remap, cache)

    assert again is first
    assert first.dtype == np.float32
    assert 0.0 <= float(first.min()) and float(first.max()) <= 1.0


def test_a_channel_with_no_window_gets_an_automatic_one():
    cache = pc.PreviewCache()
    arr = _arr(8)

    gray = pc.channel_gray(0, "CD3", arr, {}, cache)

    assert gray.shape == arr.shape
    assert float(gray.max()) > 0.0


def test_the_overlay_equals_the_general_compositor_with_an_identity_window():
    """The fast path may not change a pixel. The identity window is where the
    general compositor does nothing but clip, which is also the only place the
    two can differ."""
    grays = {"DAPI": _arr(1, scale=1.0), "CD3": _arr(2, scale=1.0) * 0.35,
             "OUT": _arr(3, scale=2.0) - 0.5}
    colors = {"DAPI": (0.1, 0.4, 1.0), "CD3": (1.0, 0.2, 0.2),
              "OUT": (0.9, 0.9, 0.1)}
    identity = {ch: {"min": 0.0, "max": 1.0, "brightness": 0.0,
                     "contrast": 1.0, "gamma": 1.0} for ch in grays}

    fast = pc.tint_and_sum_grays(grays, colors)
    old = compose_multichannel_overlay(grays, colors, identity)

    assert np.abs(fast - old).max() < 1e-6


def test_the_overlay_skips_a_channel_at_weight_zero():
    cache = pc.PreviewCache()
    arrays = {"CD3": _arr(1), "CD8": _arr(2)}
    remap = {"CD3": WINDOW, "CD8": WINDOW}
    colors = {"CD3": (1.0, 0.0, 0.0), "CD8": (0.0, 1.0, 0.0)}

    both = pc.overlay_rgb_u8(0, arrays, remap, colors,
                             {"CD3": 1.0, "CD8": 1.0}, cache)
    one = pc.overlay_rgb_u8(0, arrays, remap, colors,
                            {"CD3": 1.0, "CD8": 0.0}, cache)

    assert both.dtype == np.uint8 and both.shape == (16, 12, 3)
    assert not np.array_equal(both, one)
    assert int(one[:, :, 1].max()) == 0, "the zero-weight channel contributed"


def test_the_overlay_is_none_when_nothing_contributes():
    cache = pc.PreviewCache()
    assert pc.overlay_rgb_u8(0, {"CD3": _arr(1)}, {"CD3": WINDOW},
                             {"CD3": (1.0, 1.0, 1.0)}, {"CD3": 0.0},
                             cache) is None


def test_the_fusion_preview_uses_the_engines_own_rgb_step():
    """Passed in rather than reimplemented: the screen and the file only agree
    while there is one implementation of each stage."""
    cache = pc.PreviewCache()
    arrays = {"DAPI": _arr(1), "CD3": _arr(2)}
    remap = {"DAPI": WINDOW, "CD3": WINDOW}
    calls = []

    def to_rgb(cyto, nuc):
        calls.append((cyto.shape, nuc.shape))
        return np.stack([cyto, np.zeros_like(cyto), nuc], axis=-1)

    rgb, nothing = pc.fusion_rgb_u8(
        0, arrays, remap, {"g": {"CD3": 1.0}}, {"g": 1.0}, ("DAPI", 1.0),
        cache, lambda a: a, to_rgb)

    assert calls, "the engine's RGB step was not used"
    assert rgb.dtype == np.uint8 and rgb.shape == (16, 12, 3)
    assert nothing is False


def test_an_all_zero_configuration_is_reported_not_substituted():
    cache = pc.PreviewCache()
    arrays = {"DAPI": _arr(1), "CD3": _arr(2)}
    remap = {"DAPI": WINDOW, "CD3": WINDOW}

    rgb, nothing = pc.fusion_rgb_u8(
        0, arrays, remap, {"g": {"CD3": 0.0}}, {"g": 1.0}, ("DAPI", 0.0),
        cache, lambda a: a,
        lambda cyto, nuc: np.stack([cyto, cyto, nuc], axis=-1))

    assert nothing is True, \
        "an all-zero configuration must be named, not drawn as something else"
    assert int(np.asarray(rgb).max()) == 0


def test_the_fusion_preview_recomputes_only_the_changed_channel():
    cache = pc.PreviewCache()
    arrays = {"DAPI": _arr(1), "CD3": _arr(2)}
    remap = {"DAPI": dict(WINDOW), "CD3": dict(WINDOW)}
    to_rgb = lambda cyto, nuc: np.stack([cyto, cyto, nuc], axis=-1)

    pc.fusion_rgb_u8(0, arrays, remap, {"g": {"CD3": 1.0}}, {"g": 1.0},
                     ("DAPI", 1.0), cache, lambda a: a, to_rgb)
    before = {key: cache.entry(key)["value"] for key in cache.keys()}
    assert set(before) == {(0, "DAPI", "signal"), (0, "CD3", "signal")}

    remap["CD3"] = {"min": 5.0, "max": 900.0, "gamma": 1.0}
    pc.fusion_rgb_u8(0, arrays, remap, {"g": {"CD3": 1.0}}, {"g": 1.0},
                     ("DAPI", 1.0), cache, lambda a: a, to_rgb)

    assert cache.entry((0, "DAPI", "signal"))["value"] is before[
        (0, "DAPI", "signal")], \
        "an unchanged channel's signal was recomputed"
    assert cache.entry((0, "CD3", "signal"))["value"] is not before[
        (0, "CD3", "signal")], \
        "the changed channel was not recomputed"


def test_a_missing_cache_still_computes():
    """`cache=None` is a legitimate caller (a one-off render); it just pays
    every time."""
    arr = _arr(9)
    first = pc.channel_gray(0, "CD3", arr, {"CD3": WINDOW}, None)
    again = pc.channel_gray(0, "CD3", arr, {"CD3": WINDOW}, None)

    assert first is not again
    assert np.array_equal(first, again)


def test_the_module_needs_no_qt():
    """The property that makes these safe to call from a worker."""
    import sys

    mod = sys.modules[pc.__name__]
    source = open(mod.__file__, encoding="utf-8").read()
    assert "PyQt5" not in source
    assert "QtCore" not in source


# ── the overlay's gray and the fusion's signal are not the same array ────

def test_a_channel_with_no_window_means_two_different_arrays():
    """The cross-use this key guards against.

    With no explicit window, the overlay's fallback is a patch percentile and
    the fusion's is the loader's own normalisation -- two different pictures
    of the same channel. Both used to key on (patch, channel) with the window
    `("auto",)`, so switching preview modes could hand back the other one's
    array: a cache hit that is not the same picture.
    """
    cache = pc.PreviewCache()
    arr = _arr(11, scale=5000.0)

    def loud_norm(a):
        return np.full_like(np.asarray(a, np.float32), 0.25)

    gray = pc.channel_gray(0, "CD3", arr, {}, cache)
    signal = pc.channel_signal(0, "CD3", arr, {}, cache, loud_norm)

    assert not np.allclose(gray, signal), \
        "the two fallbacks produce the same array; this test proves nothing"
    assert float(signal.max()) == pytest.approx(0.25)
    assert pc.channel_gray(0, "CD3", arr, {}, cache) is gray
    assert pc.channel_signal(0, "CD3", arr, {}, cache, loud_norm) is signal
    assert set(cache.keys()) == {(0, "CD3", "gray"), (0, "CD3", "signal")}


def test_switching_modes_back_and_forth_never_crosses(app=None):
    """Both directions, repeatedly: overlay, fusion, overlay, fusion."""
    cache = pc.PreviewCache()
    arrays = {"DAPI": _arr(1, scale=4000.0), "CD3": _arr(2, scale=4000.0)}
    colors = {"DAPI": (1.0, 1.0, 1.0), "CD3": (1.0, 0.0, 0.0)}
    weights = {"DAPI": 1.0, "CD3": 1.0}
    to_rgb = lambda cyto, nuc: np.stack([cyto, cyto, nuc], axis=-1)

    def flat_norm(a):
        return np.full_like(np.asarray(a, np.float32), 0.5)

    first_overlay = pc.overlay_rgb_u8(0, arrays, {}, colors, weights, cache)
    first_fusion, _ = pc.fusion_rgb_u8(
        0, arrays, {}, {"g": {"CD3": 1.0}}, {"g": 1.0}, ("DAPI", 1.0),
        cache, flat_norm, to_rgb)
    again_overlay = pc.overlay_rgb_u8(0, arrays, {}, colors, weights, cache)
    again_fusion, _ = pc.fusion_rgb_u8(
        0, arrays, {}, {"g": {"CD3": 1.0}}, {"g": 1.0}, ("DAPI", 1.0),
        cache, flat_norm, to_rgb)

    assert np.array_equal(first_overlay, again_overlay), \
        "the overlay changed after a fusion frame: it was served the fusion's"
    assert np.array_equal(first_fusion, again_fusion), \
        "the fusion changed after an overlay frame"
    assert not np.array_equal(first_overlay, first_fusion), \
        "the two modes produced identical pixels; the test cannot see a cross"
