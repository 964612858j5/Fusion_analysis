"""The tick says what is SHOWN -- in Fusion exactly as in Overlay.

G3.2d.1, user ruling of 2026-09-23. Before it, unticking DAPI in Fusion
mode left the nucleus in the display snapshot and the GPU went on drawing
it: measured on the real machine, the frame was byte-identical before and
after the tick, with 20 591 blue-dominant pixels both times.

What the ruling does NOT change: the fusion model. The nucleus channel, its
weight and every group/channel weight stay exactly where they were while a
channel is unticked, and re-ticking restores the picture from them -- there
is no second copy of that answer anywhere.

Forced GPU (`BLOCK01_REQUIRE_STEP1_GPU=1`): a software renderer or a
missing PyOpenGL is a failure, not a skip.
"""

import importlib.util
import os
import pathlib
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("Step1 fusion visibility gates require BLOCK01_REQUIRE_STEP1_GPU=1",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(_ROOT.parent))
sys.path.insert(0, str(_ROOT / "tests"))

# The real MainWindow + real v2 handoff + real GPU mount fixture, reused so
# these gates run on the product path rather than on a hand-built snapshot.
from test_step1_gpu_roi_clip_product_path import (  # noqa: E402,F401
    CHANNELS, app, product, _frames, _settle_until_drawn, _tick)

from block01.ui.step1_draft_spec import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, STEP1_SCOPE, build_spec)
from block01.ui.main_window import STEP1_PREVIEW_FUSION  # noqa: E402

NUCLEUS = "DAPI"
MARKER = "CD3"


def set_visible(window, channel, visible):
    """The product's tick, and nothing else."""
    with window._display.state.using_scope(STEP1_SCOPE):
        window._display.state.set_display_visible(channel, visible)


def fusion_ready(window, app):
    """Both channels ticked and weighted, Fusion mode, one complete frame."""
    _tick(window, MARKER, app)
    _tick(window, NUCLEUS, app)
    domain = window._display.fusion
    domain.set_nucleus(NUCLEUS, 1.0) if hasattr(domain, "set_nucleus") else None
    window._step1_mount.set_mode(MODE_FUSION)
    # The WINDOW's own preview mode too, through the product's switch. The
    # first version of this helper set only the mount, so Tissue Preview and
    # the legacy patch preview stayed in overlay and three mutations of their
    # Fusion branches walked straight through the surface gate.
    window.set_preview_mode(STEP1_PREVIEW_FUSION)
    assert window.tissue_render_mode() == "fusion", (
        "the rig never reached Fusion mode: "
        f"{window.tissue_render_mode()!r}")
    _settle_until_drawn(window, MARKER, app)
    return window._step1_mount


def snapshot(window):
    return window._step1_mount._gpu_display_snapshot()


def blue_pixels(frame):
    """Nucleus contribution: the fusion pass writes it into blue."""
    opaque = frame[..., 3] > 0
    return int(np.count_nonzero(
        opaque & (frame[..., 2].astype(int) > frame[..., 0].astype(int) + 8)))


def red_pixels(frame):
    opaque = frame[..., 3] > 0
    return int(np.count_nonzero(
        opaque & (frame[..., 0].astype(int) > frame[..., 2].astype(int) + 8)))


def counters(mount):
    stats = mount.gpu_binding.stats()
    return {"uploads": mount.gpu_layer.cache_stats().get("uploads", 0),
            "coarse": tuple(stats["coarse_channels"]),
            "requests": stats.get("requests", stats.get("request_count", 0))}


# ── ticked / unticked / re-ticked, on real pixels ────────────────────

def test_the_nucleus_shows_while_it_is_ticked(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    assert snapshot(window).nucleus == (NUCLEUS, 1.0)
    frame, _shown, _inside, _vp = _frames(mount)
    assert blue_pixels(frame) > 0, "the nucleus never drew, so nothing is proven"


def test_unticking_the_nucleus_removes_it_from_the_frame(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    before, _s, _i, _v = _frames(mount)
    assert blue_pixels(before) > 0

    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)

    assert snapshot(window).nucleus == ("", 0.0), "the snapshot kept the nucleus"
    after, _s, _i, _v = _frames(mount)
    assert blue_pixels(after) == 0, "DAPI is still on screen"
    assert red_pixels(after) > 0, "the ticked marker disappeared too"
    assert not np.array_equal(before, after), "the frame did not change at all"


def test_the_model_keeps_the_nucleus_while_it_is_unticked(product, app):
    window, _subs = product
    fusion_ready(window, app)
    domain = window._display.fusion
    before_nucleus = tuple(domain.nucleus())
    before_config = domain.effective_config()

    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)

    assert tuple(domain.nucleus()) == before_nucleus
    assert domain.effective_config() == before_config, (
        "unticking rewrote the saved fusion configuration")
    assert before_nucleus[0] == NUCLEUS and before_nucleus[1] > 0


def test_re_ticking_restores_the_same_frame_without_reading_again(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    before, _s, _i, _v = _frames(mount)
    marks = counters(mount)

    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)
    set_visible(window, NUCLEUS, True)
    _settle_until_drawn(window, NUCLEUS, app)

    again, _s, _i, _v = _frames(mount)
    assert snapshot(window).nucleus == (NUCLEUS, 1.0)
    assert np.array_equal(before, again), "the restored frame is not the old one"
    after = counters(mount)
    assert after["uploads"] == marks["uploads"], (
        f"re-ticking re-uploaded: {marks['uploads']} -> {after['uploads']}")
    assert after["coarse"] == marks["coarse"], "a resident channel was re-read"


def test_with_every_contributor_unticked_the_frame_is_empty(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    set_visible(window, NUCLEUS, False)
    set_visible(window, MARKER, False)
    _settle_until_drawn(window, MARKER, app)

    spec = snapshot(window)
    assert spec.nucleus == ("", 0.0)
    assert all(not channels for channels in spec.groups.values())
    frame, _s, _i, _v = _frames(mount)
    assert int(np.count_nonzero(frame[..., 3] > 0)) == 0, (
        "the last frame stayed on screen")


# ── a contributor that is not the nucleus ────────────────────────────

def test_a_group_contributor_follows_its_tick_and_keeps_its_weight(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    domain = window._display.fusion
    before_groups = domain.effective_config()["groups"]
    before, _s, _i, _v = _frames(mount)
    assert red_pixels(before) > 0

    set_visible(window, MARKER, False)
    _settle_until_drawn(window, NUCLEUS, app)
    spec = snapshot(window)
    assert MARKER not in {ch for chans in spec.groups.values() for ch in chans}
    middle, _s, _i, _v = _frames(mount)
    assert red_pixels(middle) == 0, "the unticked marker is still drawn"
    assert blue_pixels(middle) > 0, "the ticked nucleus vanished with it"
    assert domain.effective_config()["groups"] == before_groups, (
        "unticking rewrote the group weights")

    set_visible(window, MARKER, True)
    _settle_until_drawn(window, MARKER, app)
    assert np.array_equal(_frames(mount)[0], before)


# ── every display surface answers the same ───────────────────────────

def test_all_display_surfaces_agree_on_the_contributor_set(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)

    # 1. whole-slide GPU viewer, 2. the CPU compose binding: both are
    # `build_spec`, so ask it directly with the same owners
    gpu = snapshot(window)
    compose_spec = build_spec(window._display.fusion, window._display.state,
                              MODE_FUSION, scope=STEP1_SCOPE)
    assert gpu.nucleus == ("", 0.0)
    assert compose_spec["nucleus"] == ("", 0.0)
    assert compose_spec["groups"] == {g: dict(ch) for g, ch in gpu.groups.items()}

    # 3. Tissue Preview and the published render spec. The thumbnail needs
    # low-resolution arrays to get as far as composing; in this rig they
    # are not loaded, so they are supplied here -- WITHOUT touching the
    # contributor decision, which is what the gate is about. (The first
    # version of this gate skipped the check when the snapshot came back
    # None, and three mutations walked straight through it.)
    lowres = {ch: np.full((16, 16), 500.0, np.float32) for ch in CHANNELS}
    real_array = window._display.lowres_array
    real_ensure = window._display.ensure_lowres
    window._display.lowres_array = lambda ch: lowres.get(ch)
    window._display.ensure_lowres = lambda names: []
    # The fusion thumbnail also asks the loader for its normaliser, which the
    # test double does not carry. A stand-in is supplied so the contributor
    # decision -- the only thing this gate reads -- can be reached; no pixel
    # claim is made from it.
    loader = window.loader
    had_norm = hasattr(loader, "_norm")
    if not had_norm:
        loader._norm = staticmethod(lambda a: np.asarray(a, np.float32))
    try:
        tissue = window.tissue_render_snapshot()
        assert isinstance(tissue, dict), "the thumbnail still composed nothing"
        assert tissue.get("mode") == "fusion", (
            f"the thumbnail composed in {tissue.get('mode')!r}, so its Fusion "
            "branch was never asked anything")
        assert "groups" in tissue, (
            "the fusion thumbnail spec is missing its contributor fields")
        # `arrays` IS the read set: the snapshot only fetches the channels it
        # decided this frame is made of.
        assert MARKER in (tissue.get("arrays") or {})
        assert NUCLEUS not in (tissue.get("arrays") or {}), (
            "the Tissue Preview still asks for the unticked nucleus")
        nucleus = tissue.get("nucleus")
        assert not nucleus or not nucleus[0], (
            f"the Tissue Preview kept the nucleus: {nucleus}")
        groups = tissue.get("groups") or {}
        assert NUCLEUS not in {ch for chans in groups.values() for ch in chans}
        published = window.tissue_render_spec()
        assert isinstance(published, dict)
        published_nucleus = published.get("nucleus")
        assert not published_nucleus or not published_nucleus[0], (
            "the PUBLISHED render spec kept the nucleus")
    finally:
        window._display.lowres_array = real_array
        window._display.ensure_lowres = real_ensure
        if not had_norm:
            del loader._norm

    # 4. the legacy patch preview's contributor set, AT ITS CALL SITE.
    # `_needed_channels()` also keeps the CURRENT channel resident, which is
    # what makes re-ticking free -- so the current channel is moved to the
    # marker first, otherwise an unticked nucleus would legitimately appear
    # there and the gate would prove nothing.
    window.config.set_current_channel(MARKER)
    assert window.config.current_channel() == MARKER, (
        "the current channel never moved, so this gate would have excused "
        "an unticked nucleus as 'kept resident because it is current'")
    needed = window._needed_channels()
    assert MARKER in needed
    assert NUCLEUS not in needed, (
        "the legacy patch preview still loads the unticked nucleus as a "
        "contributor")
    assert NUCLEUS not in window._fusion_display_contributors()
    assert MARKER in window._fusion_display_contributors()
    # ...while the MODEL's own list -- what a Save commits -- is untouched
    assert NUCLEUS in window._fusion_weighted_channels()


    # 5. the other direction: a GROUP MEMBER unticked, the nucleus back on.
    # Unticking the nucleus alone never exercises the group filter -- the
    # nucleus belongs to no group -- so "leave the groups unfiltered" was
    # invisible to the checks above. Here the frame stays non-empty, so the
    # thumbnail still composes and its group membership can be read.
    set_visible(window, NUCLEUS, True)
    set_visible(window, MARKER, False)
    _settle_until_drawn(window, NUCLEUS, app)
    window.config.set_current_channel(NUCLEUS)
    assert window.config.current_channel() == NUCLEUS
    window._display.lowres_array = lambda ch: lowres.get(ch)
    window._display.ensure_lowres = lambda names: []
    if not hasattr(loader, "_norm"):
        loader._norm = staticmethod(lambda a: np.asarray(a, np.float32))
    try:
        tissue = window.tissue_render_snapshot()
        assert isinstance(tissue, dict) and tissue.get("mode") == "fusion"
        tissue_groups = tissue.get("groups") or {}
        assert MARKER not in {ch for chans in tissue_groups.values()
                              for ch in chans}, (
            "the Tissue Preview still fuses the unticked group member")
        assert MARKER not in (tissue.get("arrays") or {}), (
            "the Tissue Preview still reads the unticked group member")
        assert NUCLEUS in (tissue.get("arrays") or {}), (
            "the ticked nucleus fell out of the thumbnail")
    finally:
        window._display.lowres_array = real_array
        window._display.ensure_lowres = real_ensure
        if not had_norm and hasattr(loader, "_norm"):
            del loader._norm
    assert MARKER not in window._needed_channels(), (
        "the legacy patch preview still loads the unticked group member")
    assert MARKER not in window._fusion_display_contributors()
    assert MARKER in window._fusion_weighted_channels(), (
        "unticking a group member rewrote the model's own list")


# ── Overlay keeps its meaning, and the round trip revives nothing ────

def test_overlay_still_means_what_it_meant(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    mount.set_mode(MODE_OVERLAY)
    _settle_until_drawn(window, MARKER, app)
    spec = snapshot(window)
    assert set(spec.weights) == {MARKER, NUCLEUS}

    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)
    assert set(snapshot(window).weights) == {MARKER}


def test_a_mode_round_trip_does_not_revive_an_unticked_nucleus(product, app):
    window, _subs = product
    mount = fusion_ready(window, app)
    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)
    hidden, _s, _i, _v = _frames(mount)

    mount.set_mode(MODE_OVERLAY)
    _settle_until_drawn(window, MARKER, app)
    assert NUCLEUS not in snapshot(window).weights

    mount.set_mode(MODE_FUSION)
    _settle_until_drawn(window, MARKER, app)
    assert snapshot(window).nucleus == ("", 0.0), "the round trip revived DAPI"
    back, _s, _i, _v = _frames(mount)
    assert blue_pixels(back) == 0
    assert np.array_equal(back, hidden), "a stale frame came back with the mode"

    # the camera and the clip are untouched by any of this
    viewport = mount._gpu_viewport_snapshot()
    assert viewport.roi_world_rect is not None
    assert viewport.roi_polygon_world is not None


# ── save / restore keeps the two authorities apart ───────────────────

def test_visibility_and_fusion_config_restore_from_their_own_authorities(product,
                                                                         app):
    window, _subs = product
    fusion_ready(window, app)
    domain = window._display.fusion
    set_visible(window, NUCLEUS, False)
    _settle_until_drawn(window, MARKER, app)

    saved_config = domain.effective_config()
    saved_visibility = sorted(window.config.visible_channels())
    assert NUCLEUS not in saved_visibility
    assert (saved_config.get("nucleus") or {}).get("channel") == NUCLEUS
    assert float((saved_config.get("nucleus") or {}).get("weight") or 0.0) > 0

    # the display answer moves on its own...
    set_visible(window, NUCLEUS, True)
    _settle_until_drawn(window, NUCLEUS, app)
    assert NUCLEUS in window.config.visible_channels()
    # ...and the science did not move with it
    assert domain.effective_config() == saved_config
