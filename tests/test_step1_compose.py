"""Step1's whole-slide composition, against the formulae it must not rewrite.

Block C1 of `docs/step1_rework_plan.md`, gate C.3 #1 and #8: with the window
the shared state holds, the composed tile is what
`core.preview_compose.overlay_rgb_u8` and `core.fusion_engine.fuse_channels`
produce from the same inputs -- pixel for pixel.

No Qt here: this is the arithmetic.
"""

import numpy as np
import pytest

from block01.core.fusion_engine import FusionEngine, fuse_channels
from block01.core.preview_compose import PreviewCache, overlay_rgb_u8
from block01.viewer.step1_compose import (
    MODE_FUSION, MODE_OVERLAY, compose, compose_fusion, compose_overlay,
    channel_signal, composition_key, fusion_channels, overlay_channels,
)

WINDOW = (0.0, 100.0, 1.0)


def _pixels(seed, shape=(8, 8), scale=100.0):
    rng = np.random.default_rng(seed)
    return (rng.random(shape, dtype=np.float32) * scale).astype(np.float32)


def _tiles(**channels):
    return {name: (values, np.ones(values.shape, bool))
            for name, values in channels.items()}


def _remap(mapping):
    lo, hi, gamma = mapping
    return {"min": lo, "max": hi, "gamma": gamma,
            "brightness": 0.0, "contrast": 1.0}


# ── 1. Overlay: the same picture as the patch preview's ───────────────

def test_the_overlay_is_what_overlay_rgb_u8_makes_of_the_same_inputs():
    tiles = _tiles(CD3=_pixels(1), CD8=_pixels(2))
    weights = {"CD3": 1.0, "CD8": 0.4}
    colors = {"CD3": (0.0, 1.0, 0.0), "CD8": (1.0, 0.8, 0.0)}
    mappings = {"CD3": WINDOW, "CD8": WINDOW}

    rgba, valid, missing = compose_overlay(tiles, weights, colors, mappings)

    expected = overlay_rgb_u8(
        "patch", {ch: values for ch, (values, _v) in tiles.items()},
        {ch: _remap(WINDOW) for ch in tiles}, colors, weights, PreviewCache())
    assert missing == []
    assert valid.all()
    assert np.array_equal(rgba[..., :3], expected)
    assert (rgba[..., 3] == 255).all()


def test_two_channels_of_one_colour_add_up_the_same_way():
    tiles = _tiles(CD3=_pixels(3), CD8=_pixels(4))
    colors = {"CD3": (1.0, 0.0, 0.0), "CD8": (1.0, 0.0, 0.0)}
    weights = {"CD3": 1.0, "CD8": 1.0}
    mappings = {ch: WINDOW for ch in tiles}

    rgba, _valid, _missing = compose_overlay(tiles, weights, colors, mappings)
    expected = overlay_rgb_u8(
        "patch", {ch: v for ch, (v, _m) in tiles.items()},
        {ch: _remap(WINDOW) for ch in tiles}, colors, weights, PreviewCache())

    assert np.array_equal(rgba[..., :3], expected)


def test_a_weight_of_one_and_a_weight_of_zero_point_five_differ():
    tiles = _tiles(CD3=_pixels(5))
    colors = {"CD3": (1.0, 1.0, 1.0)}
    mappings = {"CD3": WINDOW}

    full, _v, _m = compose_overlay(tiles, {"CD3": 1.0}, colors, mappings)
    half, _v, _m = compose_overlay(tiles, {"CD3": 0.5}, colors, mappings)

    assert not np.array_equal(full, half)


# ── 2. a zero-weight channel is DROPPED, not multiplied by zero ───────

def test_a_zero_weight_channel_is_never_mapped_at_all():
    """C.3 gate 8, the call half: it does not enter the composition."""
    mapped = []

    class _Watched(np.ndarray):
        pass

    tiles = _tiles(CD3=_pixels(6), CD8=_pixels(7))

    import block01.viewer.step1_compose as step1_compose
    real = step1_compose.apply_channel_remap

    def _spy(image, params=None):
        mapped.append(id(image))
        return real(image, params)

    step1_compose.apply_channel_remap = _spy
    try:
        compose_overlay(tiles, {"CD3": 1.0, "CD8": 0.0},
                        {"CD3": (1.0, 1.0, 1.0), "CD8": (1.0, 0.0, 0.0)},
                        {ch: WINDOW for ch in tiles})
    finally:
        step1_compose.apply_channel_remap = real

    assert id(tiles["CD3"][0]) in mapped
    assert id(tiles["CD8"][0]) not in mapped, (
        "a zero-weight channel was mapped before being ignored")


def test_a_zero_weight_channel_contributes_no_pixel():
    """C.3 gate 8, the pixel half."""
    tiles = _tiles(CD3=_pixels(8), CD8=_pixels(9))
    colors = {"CD3": (0.0, 1.0, 0.0), "CD8": (1.0, 0.0, 0.0)}
    mappings = {ch: WINDOW for ch in tiles}

    with_zero, _v, _m = compose_overlay(tiles, {"CD3": 1.0, "CD8": 0.0},
                                        colors, mappings)
    without, _v, _m = compose_overlay({"CD3": tiles["CD3"]}, {"CD3": 1.0},
                                      colors, mappings)

    assert np.array_equal(with_zero, without)


# ── 3. Fusion: `fuse_channels`, not a second implementation ───────────

def _groups():
    return {"markers": {"CD3": 0.8, "CD8": 0.5},
            "other": {"CD20": 1.0}}


def test_the_fusion_is_what_fuse_channels_makes_of_the_same_signals():
    tiles = _tiles(DAPI=_pixels(10), CD3=_pixels(11), CD8=_pixels(12),
                   CD20=_pixels(13))
    mappings = {ch: WINDOW for ch in tiles}
    groups = _groups()
    group_weights = {"markers": 1.0, "other": 0.6}

    rgba, valid, missing = compose_fusion(tiles, groups, group_weights,
                                          ("DAPI", 1.0), mappings)

    from block01.core.channel_remap import apply_channel_remap
    signals = {ch: np.asarray(apply_channel_remap(values, _remap(WINDOW)),
                              np.float32)
               for ch, (values, _v) in tiles.items()}
    cyto, nuc = fuse_channels(signals, groups, group_weights, "DAPI", 1.0)
    expected = FusionEngine.to_rgb(cyto, nuc)

    assert missing == [] and valid.all()
    assert np.array_equal(rgba[..., :3], expected)


def test_the_groups_are_taken_at_their_maximum_not_summed():
    """A case built so max and sum disagree."""
    ones = np.full((4, 4), 100.0, np.float32)
    tiles = _tiles(CD3=ones, CD20=ones, DAPI=np.zeros((4, 4), np.float32))
    groups = {"a": {"CD3": 1.0}, "b": {"CD20": 1.0}}
    mappings = {ch: WINDOW for ch in tiles}

    rgba, _v, _m = compose_fusion(tiles, groups, {"a": 1.0, "b": 1.0},
                                  ("DAPI", 1.0), mappings)

    red = rgba[..., 0].astype(np.float32)
    assert np.allclose(red, 255.0), red
    # A sum would saturate the same way here, so the discriminating case is
    # two HALF-strength groups: max says 0.5, sum says 1.0.
    half = np.full((4, 4), 50.0, np.float32)
    tiles = _tiles(CD3=half, CD20=half, DAPI=np.zeros((4, 4), np.float32))
    rgba, _v, _m = compose_fusion(tiles, groups, {"a": 1.0, "b": 1.0},
                                  ("DAPI", 1.0), mappings)
    red = rgba[..., 0].astype(np.float32)
    # `to_rgb` truncates rather than rounds: 0.5 * 255 -> 127.
    assert np.allclose(red, int(0.5 * 255)), red
    summed = int(min(1.0, 0.5 + 0.5) * 255)
    assert not np.allclose(red, summed), "the groups were summed"


def test_a_channel_the_tiles_do_not_carry_is_skipped_not_zeroed():
    present = _tiles(CD3=np.full((4, 4), 100.0, np.float32),
                     DAPI=np.zeros((4, 4), np.float32))
    groups = {"markers": {"CD3": 1.0, "CD8": 1.0}}
    mappings = {"CD3": WINDOW, "DAPI": WINDOW, "CD8": WINDOW}

    rgba, _v, _m = compose_fusion(present, groups, {"markers": 1.0},
                                  ("DAPI", 1.0), mappings)

    # CD8 absent: the group is CD3 alone, not CD3 plus a dark channel.
    from block01.core.channel_remap import apply_channel_remap
    signals = {"CD3": np.asarray(
        apply_channel_remap(present["CD3"][0], _remap(WINDOW)), np.float32),
        "DAPI": np.zeros((4, 4), np.float32)}
    cyto, nuc = fuse_channels(signals, groups, {"markers": 1.0}, "DAPI", 1.0)
    assert np.array_equal(rgba[..., :3], FusionEngine.to_rgb(cyto, nuc))


def test_a_heterogeneous_group_weight_reaches_the_picture():
    tiles = _tiles(CD3=np.full((4, 4), 100.0, np.float32),
                   DAPI=np.zeros((4, 4), np.float32))
    groups = {"markers": {"CD3": 1.0}}
    mappings = {ch: WINDOW for ch in tiles}

    full, _v, _m = compose_fusion(tiles, groups, {"markers": 1.0},
                                  ("DAPI", 0.0), mappings)
    quarter, _v, _m = compose_fusion(tiles, groups, {"markers": 0.25},
                                     ("DAPI", 0.0), mappings)

    assert full[..., 0].max() == 255
    assert quarter[..., 0].max() == int(0.25 * 255)


# ── 3b. the fusion's zero weights (C.1.1) ────────────────────────────

def _fusion_spy():
    """Which arrays `apply_channel_remap` was handed, by identity."""
    import block01.viewer.step1_compose as step1_compose
    mapped = []
    real = step1_compose.apply_channel_remap

    def _spy(image, params=None):
        mapped.append(id(image))
        return real(image, params)

    step1_compose.apply_channel_remap = _spy
    return mapped, (lambda: setattr(step1_compose, "apply_channel_remap", real))


def test_the_input_set_names_only_the_channels_that_can_contribute():
    """C.3 gate 8 for the fusion: three places a zero weight lives."""
    groups = {"markers": {"CD3": 1.0, "CD8": 0.0},
              "muted": {"CD20": 1.0}}
    group_weights = {"markers": 1.0, "muted": 0.0}

    assert fusion_channels(groups, group_weights, ("DAPI", 0.6)) == {
        "CD3", "DAPI"}
    assert fusion_channels(groups, group_weights, ("DAPI", 0.0)) == {"CD3"}
    assert overlay_channels({"CD3": 1.0, "CD8": 0.0}) == {"CD3"}


def test_a_zero_weight_fusion_channel_is_never_mapped():
    tiles = _tiles(CD3=_pixels(21), CD8=_pixels(22), DAPI=_pixels(23))
    mapped, restore = _fusion_spy()
    try:
        compose_fusion(tiles, {"markers": {"CD3": 1.0, "CD8": 0.0}},
                       {"markers": 1.0}, ("DAPI", 0.5),
                       {ch: WINDOW for ch in tiles})
    finally:
        restore()

    assert id(tiles["CD3"][0]) in mapped
    assert id(tiles["DAPI"][0]) in mapped
    assert id(tiles["CD8"][0]) not in mapped, (
        "a zero-weight fusion channel was mapped")


def test_a_zero_weight_group_is_never_mapped():
    tiles = _tiles(CD3=_pixels(24), CD20=_pixels(25), DAPI=_pixels(26))
    mapped, restore = _fusion_spy()
    try:
        compose_fusion(tiles,
                       {"markers": {"CD3": 1.0}, "muted": {"CD20": 1.0}},
                       {"markers": 1.0, "muted": 0.0}, ("DAPI", 0.5),
                       {ch: WINDOW for ch in tiles})
    finally:
        restore()

    assert id(tiles["CD3"][0]) in mapped
    assert id(tiles["CD20"][0]) not in mapped, (
        "a channel of a zero-weight group was mapped")


def test_a_zero_weight_nucleus_is_never_mapped():
    tiles = _tiles(CD3=_pixels(27), DAPI=_pixels(28))
    mapped, restore = _fusion_spy()
    try:
        rgba, _valid, _missing = compose_fusion(
            tiles, {"markers": {"CD3": 1.0}}, {"markers": 1.0}, ("DAPI", 0.0),
            {ch: WINDOW for ch in tiles})
    finally:
        restore()

    assert id(tiles["DAPI"][0]) not in mapped, "a zero-weight nucleus was mapped"
    assert (rgba[..., 2] == 0).all(), "a zero-weight nucleus reached the blue"


def test_dropping_the_zero_weights_changes_no_pixel():
    """Not reading them is an OPTIMISATION of `fuse_channels`, not a new
    formula: the engine already refuses `w <= 0`, scales a group by `gw` and
    a nucleus by `nuc_w`."""
    tiles = _tiles(CD3=_pixels(29), CD8=_pixels(30), CD20=_pixels(31),
                   DAPI=_pixels(32))
    groups = {"markers": {"CD3": 0.8, "CD8": 0.0}, "muted": {"CD20": 1.0}}
    group_weights = {"markers": 1.0, "muted": 0.0}
    mappings = {ch: WINDOW for ch in tiles}

    rgba, _valid, _missing = compose_fusion(tiles, groups, group_weights,
                                            ("DAPI", 0.6), mappings)

    signals = {ch: channel_signal(values, valid, WINDOW)
               for ch, (values, valid) in tiles.items()}
    cyto, nuc = fuse_channels(signals, groups, group_weights, "DAPI", 0.6)
    assert np.array_equal(rgba[..., :3], FusionEngine.to_rgb(cyto, nuc))


def test_an_explicit_zero_keeps_its_place_in_the_draft():
    """The drop is the composition's, not the domain's: the draft it was
    given still says CD8 takes part at 0.0."""
    groups = {"markers": {"CD3": 1.0, "CD8": 0.0}}
    group_weights = {"markers": 1.0}
    before = ({k: dict(v) for k, v in groups.items()}, dict(group_weights))

    fusion_channels(groups, group_weights, ("DAPI", 0.0))
    compose_fusion(_tiles(CD3=_pixels(33), CD8=_pixels(34)), groups,
                   group_weights, ("DAPI", 0.0), {"CD3": WINDOW, "CD8": WINDOW})

    assert ({k: dict(v) for k, v in groups.items()}, dict(group_weights)) == before
    assert groups["markers"]["CD8"] == 0.0


# ── 4. absence survives composition ───────────────────────────────────

def test_a_pixel_no_channel_can_answer_for_is_transparent():
    values = _pixels(14)
    valid = np.ones(values.shape, bool)
    valid[:4, :] = False
    tiles = {"CD3": (np.where(valid, values, np.nan), valid)}

    rgba, union, _m = compose_overlay(tiles, {"CD3": 1.0},
                                      {"CD3": (1.0, 1.0, 1.0)},
                                      {"CD3": WINDOW})

    assert (rgba[:4, :, 3] == 0).all(), "absent pixels were painted"
    assert (rgba[4:, :, 3] == 255).all()
    assert not union[:4, :].any()


def test_a_pixel_one_channel_can_answer_for_is_opaque():
    shape = (8, 8)
    left = np.zeros(shape, bool)
    left[:, :4] = True
    right = ~left
    tiles = {"CD3": (np.full(shape, 80.0, np.float32), left),
             "CD8": (np.full(shape, 80.0, np.float32), right)}

    rgba, union, _m = compose_overlay(
        tiles, {"CD3": 1.0, "CD8": 1.0},
        {"CD3": (1.0, 0.0, 0.0), "CD8": (0.0, 0.0, 1.0)},
        {ch: WINDOW for ch in tiles})

    assert (rgba[..., 3] == 255).all(), "a pixel with data was left absent"
    assert union.all()
    assert (rgba[:, :4, 0] > 0).all() and (rgba[:, :4, 2] == 0).all()
    assert (rgba[:, 4:, 2] > 0).all() and (rgba[:, 4:, 0] == 0).all()


# ── 5. no window means no composition, and a name to ask about ────────

def test_a_channel_with_no_window_is_named_rather_than_guessed():
    tiles = _tiles(CD3=_pixels(15), CD8=_pixels(16))

    rgba, _valid, missing = compose_overlay(
        tiles, {"CD3": 1.0, "CD8": 1.0},
        {ch: (1.0, 1.0, 1.0) for ch in tiles}, {"CD3": WINDOW})

    assert missing == ["CD8"]
    only_cd3, _v, _m = compose_overlay({"CD3": tiles["CD3"]}, {"CD3": 1.0},
                                       {"CD3": (1.0, 1.0, 1.0)},
                                       {"CD3": WINDOW})
    assert np.array_equal(rgba, only_cd3), (
        "a channel with no window still reached the picture")


def test_nothing_here_computes_an_automatic_window():
    """C.2: the composition layer may not re-derive a window."""
    import inspect

    import block01.viewer.step1_compose as step1_compose

    # THE CODE, not the prose: the module's own docstring says it computes
    # no automatic window, and a scan that reads the docstring would find
    # every forbidden word there.
    import ast

    tree = ast.parse(inspect.getsource(step1_compose))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            node.value.value = ""
    code = ast.unparse(tree)
    for forbidden in ("compute_qupath_auto_minmax", "percentile", "_norm("):
        assert forbidden not in code, forbidden


# ── 6. the composition key ────────────────────────────────────────────

def test_the_key_moves_with_everything_the_composition_adds():
    base = dict(tile_keys=[("CD3", 0, 1, 1)], weights={"CD3": 1.0},
                colors={"CD3": (1.0, 1.0, 1.0)}, mappings={"CD3": WINDOW},
                groups={"markers": {"CD3": 1.0}}, group_weights={"markers": 1.0},
                nucleus=("DAPI", 1.0))
    key = composition_key(MODE_OVERLAY, **base)

    assert composition_key(MODE_FUSION, **base) != key
    assert composition_key(MODE_OVERLAY, **dict(base, weights={"CD3": 0.5})) != key
    assert composition_key(MODE_OVERLAY,
                           **dict(base, colors={"CD3": (1.0, 0.0, 0.0)})) != key
    assert composition_key(MODE_OVERLAY,
                           **dict(base, mappings={"CD3": (0.0, 50.0, 1.0)})) != key
    assert composition_key(MODE_OVERLAY,
                           **dict(base, group_weights={"markers": 0.5})) != key
    assert composition_key(MODE_OVERLAY,
                           **dict(base, nucleus=("DAPI", 0.5))) != key
    assert composition_key(MODE_OVERLAY,
                           **dict(base, tile_keys=[("CD3", 0, 1, 2)])) != key
    assert composition_key(MODE_OVERLAY, **base) == key


def test_the_key_says_nothing_a_tile_key_already_says():
    """A weight in a TILE key would re-read pixels for a colour change."""
    from block01.viewer.step1_source import Step1SourceTable  # noqa: F401
    import inspect

    import block01.ui.step1_viewer_host as host_module

    source = inspect.getsource(host_module.Step1TileProvider)
    for forbidden in ("weight", "colour", "color", "overlay", "fusion"):
        assert forbidden not in source.lower(), forbidden
