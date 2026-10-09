"""Block A9 §35: tiles stored in texture arrays and drawn instanced give the
SAME BYTES as one texture per plane, on the same GPU -- every format,
validity, edge tiles, coarse under fine, Overlay and Fusion, ROI clips,
fractional cameras, slot reuse after eviction and a camera-only frame.

Real GL only: set BLOCK01_REQUIRE_STEP1_GPU=1 (as test_step1_gpu_layer).
"""

import os

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

import test_step1_gpu_layer as g  # noqa: E402  (its import shim and helpers)
from block01.ui import step1_gpu_layer as layer_module  # noqa: E402
from block01.ui.step1_gpu_layer import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, DisplaySnapshot, RawPlane, ViewportSnapshot)

SIZE = (97, 61)
BUDGET = 512 * 1024 * 1024
T = 512


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _layer(app, arrays, budget=BUDGET):
    layer = g._layer(app, budget=budget)
    layer.use_tile_arrays = arrays
    return layer


def _grid(level_ds, shape, dtype, seed, name, nan=False, valid_rect=False):
    """A pyramid level `shape` (h, w) cut into <=512 tiles, world = px * ds."""
    rng = np.random.default_rng(seed)
    h, w = shape
    planes = []
    for ty in range(-(-h // T)):
        for tx in range(-(-w // T)):
            th, tw = min(T, h - ty * T), min(T, w - tx * T)
            top = np.iinfo(dtype).max if np.dtype(dtype).kind == "u" else 1000
            values = rng.integers(0, top, (th, tw)).astype(dtype)
            kw = {}
            if nan:
                values = values.astype(np.float32)
                values[rng.random((th, tw)) < 0.05] = np.nan
            if valid_rect and np.dtype(dtype).kind == "u":
                kw["valid_rect"] = (3, max(4, th - 5), 2, max(3, tw - 7))
            rect = (tx * T * level_ds, (tx * T + tw) * level_ds,
                    ty * T * level_ds, (ty * T + th) * level_ds)
            planes.append(RawPlane((name, level_ds, tx, ty), rect, values, **kw))
    return tuple(planes)


def _scene(big=True):
    # level-0 world 1300 x 900; coarse at ds 4, fine at ds 1 over a corner
    a_coarse = _grid(4.0, (225, 325), np.uint8, 1, "A")
    a_fine = tuple(p for p in _grid(1.0, (900, 1300), np.uint8, 2, "A", valid_rect=True)
                   if p.identity[2] < 2 and p.identity[3] < 1)
    b_coarse = _grid(4.0, (225, 325), np.uint16, 3, "B", valid_rect=True)
    c_coarse = _grid(2.0, (450, 650), np.float32, 4, "C", nan=True)
    big_plane = RawPlane(("D", "big"), (0.0, 1300.0, 0.0, 900.0),
                   np.random.default_rng(5).integers(0, 255, (600, 700)).astype(np.uint8))
    channels = [g._channel("A", coarse=a_coarse, fine=a_fine, selected="fine"),
                g._channel("B", coarse=b_coarse),
                g._channel("C", coarse=c_coarse),
                g._channel("D", coarse=(big_plane,))]
    return g._source(*(channels if big else channels[:3]))


MAPPINGS = {"A": (10.0, 240.0, 1.0), "B": (0.0, 65535.0, 0.6),
            "C": (100.0, 900.0, 1.3), "D": (0.0, 255.0, 1.0)}


def _overlay():
    return DisplaySnapshot(MODE_OVERLAY, MAPPINGS, {n: 1.0 for n in MAPPINGS},
                           {"A": (1, 0, 0), "B": (0, 1, 0), "C": (0, 0, 1), "D": (1, 1, 0)})


def _fusion():
    return DisplaySnapshot(MODE_FUSION, MAPPINGS,
                           groups={"g1": {"A": 1.0, "C": 0.5}, "g2": {"B": 0.8, "D": 0.0}},
                           group_weights={"g1": 1.0, "g2": 0.7}, nucleus=("D", 1.0))


VIEWS = [(0.0, 1300.0, 0.0, 900.0), (0.3, 640.7, 0.45, 450.2), (511.5, 516.5, 0.0, 3.3),
         (-40.0, 900.0, 300.0, 1000.0), (1023.99, 1300.01, 511.99, 900.0)]


def _both(app, display, viewport, big=True, single_pass=None):
    out = []
    for arrays in (False, True):
        layer = _layer(app, arrays)
        try:
            layer.submit(_scene(big), display, viewport)
            out.append(layer.readback_rgba_for_test())
            if arrays:
                assert layer._arrays is not None and layer._arrays.slots, "arrays were used"
                if single_pass is not None:
                    frames = layer._vt.frames if layer._vt is not None else 0
                    assert (frames > 0) is single_pass, f"single pass used: {frames}"
        finally:
            layer.deleteLater()
    return out


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("mode", ["overlay", "fusion"])
@pytest.mark.parametrize("big", [True, False], ids=["per-channel", "single-pass"])
def test_arrays_draw_the_same_bytes_as_one_texture_per_plane(app, view, mode, big):
    """A9 §35.7: the single pass (every plane in the arrays) and the
    per-channel fallback (an oversized plane present) both give the bytes
    of one texture per plane."""
    display = _overlay() if mode == "overlay" else _fusion()
    old, new = _both(app, display, ViewportSnapshot(view, SIZE, 1.0), big=big,
                     single_pass=not big)
    assert np.count_nonzero(old[..., 3]) > 0
    assert np.array_equal(old, new)


def test_the_single_pass_can_be_switched_off(app, monkeypatch):
    monkeypatch.setenv("BLOCK01_VT", "0")
    old, new = _both(app, _overlay(), ViewportSnapshot(VIEWS[1], SIZE, 1.0), big=False,
                     single_pass=False)
    assert np.array_equal(old, new)


@pytest.mark.parametrize("polygon", [None, ((100.0, 50.0), (900.0, 120.0), (500.0, 800.0), (60.0, 600.0))])
def test_the_region_clip_is_the_same_on_both_paths(app, polygon):
    viewport = ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0,
                                roi_world_rect=(50.0, 1000.0, 40.0, 850.0),
                                roi_polygon_world=polygon)
    old, new = _both(app, _overlay(), viewport, big=False, single_pass=True)
    assert np.array_equal(old, new)


def test_a_reused_slot_never_shows_the_tile_it_held_before(app):
    """A budget of a few slots: later tiles take earlier tiles' layers; an
    edge tile in a reused layer must not show the old texels around it."""
    budget = 64 * T * T * 4                      # the smallest array store
    layer = _layer(app, True, budget=budget)
    ref = _layer(app, False, budget=budget)
    try:
        viewport = ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0)
        for seed in range(14):                   # 14 x 6 tiles > 64 slots
            full = _grid(1.0, (900, 1300), np.float32, 10 + seed, f"S{seed}", nan=True)
            source = g._source(g._channel("A", coarse=full))
            layer.submit(source, _overlay(), viewport)
            ref.submit(source, _overlay(), viewport)
            assert np.array_equal(layer.readback_rgba_for_test(), ref.readback_rgba_for_test())
        assert layer._arrays.evictions > 0
    finally:
        layer.deleteLater()
        ref.deleteLater()


def test_a_camera_frame_from_the_arrays_matches_a_submission(app):
    layer = _layer(app, True)
    try:
        layer.resize(*SIZE)
        g._events(app)
        start = ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0)
        moved = (0.3, 640.7, 0.45, 450.2)
        layer.submit(_scene(), _overlay(), start)
        uploads = layer._arrays.uploads
        layer._attached_range = moved
        layer.makeCurrent()
        try:
            layer._recompose_for_camera()
        finally:
            layer.doneCurrent()
        followed = layer.readback_rgba_for_test()
        assert layer._arrays.uploads == uploads
        layer.submit(_scene(), _overlay(), ViewportSnapshot(moved, SIZE, 1.0))
        assert np.array_equal(followed, layer.readback_rgba_for_test())
    finally:
        layer.deleteLater()


def test_any_number_of_channels_is_one_composition_pass(app):
    """A9 §35.7: one pass composes every channel (plus `_finalize`), where
    one texture per plane needs a pass per plane and per channel."""
    viewport = ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0)
    counts = []
    for n in (1, 3):
        names = ["A", "B", "C"][:n]
        source = g._source(*[g._channel(name, coarse=_grid(1.0, (900, 1300), np.uint8, 7 + i, name))
                             for i, name in enumerate(names)])
        layer = _layer(app, True)
        try:
            counts.append(layer.submit(source, _overlay(), viewport)["pass_count"])
            assert layer._vt.frames == 1
        finally:
            layer.deleteLater()
    assert counts == [2, 2]


def test_a_small_budget_keeps_one_texture_per_plane(app):
    layer = _layer(app, True, budget=8 * 1024 * 1024)
    try:
        assert layer.tile_arrays_active is False
        layer.submit(_scene(), _overlay(), ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0))
        assert layer._arrays is None
    finally:
        layer.deleteLater()


def test_whole_blocks_are_counted_and_empty_ones_released(app):
    layer = _layer(app, True)
    try:
        layer.submit(_scene(), _overlay(), ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0))
        store = layer._arrays
        stats = layer.cache_stats()
        assert stats["array_bytes"] == sum(b.nbytes for b in store.blocks)
        assert stats["bytes"] >= stats["array_bytes"]
        layer.makeCurrent()
        try:
            dropped = store.release_unused(layer._gl, keep=set())
        finally:
            layer.doneCurrent()
        assert dropped == len(stats and store.blocks) or not store.blocks
        assert store.allocated_bytes == 0 and not store.slots
    finally:
        layer.deleteLater()


def test_a_hidden_layer_gives_back_everything_but_its_coarse(app):
    """A9 §36: release on need keeps the complete coarse and frees the rest
    (tiles, empty blocks, composition targets); the next submission draws
    the same picture again."""
    from block01.ui import gpu_memory
    layer = _layer(app, True)
    try:
        viewport = ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0)
        layer.submit(_scene(False), _overlay(), viewport)
        before = layer.readback_rgba_for_test()
        held = layer.gpu_bytes()
        layer.hidden = True
        released = gpu_memory.release_hidden("test")
        assert released > 0 and layer.gpu_bytes() < held
        coarse = {p.identity for c in _scene(False).channels for p in c.coarse}
        assert set(layer._arrays.slots) <= coarse
        layer.hidden = False
        layer.submit(_scene(False), _overlay(), viewport)
        assert np.array_equal(before, layer.readback_rgba_for_test())
    finally:
        layer.deleteLater()


def test_a_visible_layer_is_never_released(app):
    from block01.ui import gpu_memory
    layer = _layer(app, True)
    try:
        layer.submit(_scene(False), _overlay(), ViewportSnapshot((0.0, 1300.0, 0.0, 900.0), SIZE, 1.0))
        held = layer.gpu_bytes()
        layer.hidden = False
        gpu_memory.release_hidden("test")
        assert layer.gpu_bytes() == held
    finally:
        layer.deleteLater()
