"""Block 4b: the GPU label layer, on REAL framebuffer pixels.

  * the screen id image equals an INDEPENDENT reference computed here from
    the view rect, the output size and each tile's world rect (the image
    shader's own mapping, in float32) -- never derived from the GPU result;
  * on that id image, outlines equal `step3_masks.outline_reference` at
    device pixel ratios 1, 1.5 and 2, fills equal `fill_colour`, alpha
    blends, id 0 leaves the picture alone, the nucleus layer lies on top;
  * a later plane overwrites an earlier one, its id 0 included;
  * a concave ROI polygon clips the masks exactly like the picture;
  * a label-only change composes no channel; `labels=True` with no masks
    shows the very same picture as `labels=False`; the budget holds.

Real GL only: skipped unless BLOCK01_REQUIRE_STEP1_GPU=1 (as every G1 test).
On this WSL machine: QT_QPA_PLATFORM=xcb PYOPENGL_PLATFORM=glx
MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA.
"""

import importlib.util
import os
import pathlib
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("G1 GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1",
                allow_module_level=True)

try:
    import OpenGL  # noqa: F401
    from PyQt5 import QtCore, QtWidgets
except ImportError as exc:
    pytest.fail(f"required G1 GPU runtime dependency is unavailable: {exc}",
                pytrace=False)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)

from block01.core.step3_masks import fill_colour, outline_reference  # noqa: E402
from block01.ui.step1_gpu_layer import (  # noqa: E402
    LABEL_FILL, LABEL_OUTLINE, MODE_OVERLAY, ChannelSource, DisplaySnapshot,
    LabelLayer, LabelPlane, LabelSnapshot, RawPlane, SourceDescriptor,
    Step1GpuLayer, Step1GpuLayerError, ViewportSnapshot, label_radius)

WORLD = (3.3, 1003.3, 11.0, 811.0)             # 1000 x 800 level-0 pixels
PHYS = (200, 160)                               # physical output
BIG = 2 ** 24


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _drop(made):
    """Dispose and really delete: a GL widget left for interpreter exit
    crashes the WSL D3D12 driver on the way out."""
    made.dispose()
    made.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    QtWidgets.QApplication.processEvents()


def _make(app, labels=True, **kw):
    made = Step1GpuLayer(max_raw_texture_bytes=16 * 1024 * 1024,
                         require_hardware=True, labels=labels, **kw)
    made.resize(64, 64)
    made.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    made.show()
    for _ in range(8):
        app.processEvents()
    if not made.initialized:
        pytest.fail(f"required G1 layer did not initialize: {made.init_error}")
    return made


@pytest.fixture(scope="module")
def _shared(app):
    # ONE layer for the module: the WSL D3D12 driver aborts in glReadPixels
    # after about five GL contexts in one process (HEAD's own polygon-clip
    # module stops there too), so contexts are not made per test.
    made = _make(app)
    yield made
    _drop(made)


@pytest.fixture
def layer(_shared):
    _shared.set_labels(LabelSnapshot())
    return _shared


def _picture():
    values = np.full((8, 10), 700.0, np.float32)
    source = ChannelSource("c", coarse=(RawPlane(("img",), WORLD, values),))
    display = DisplaySnapshot(mode=MODE_OVERLAY, mappings={"c": (0.0, 1000.0, 1.0)},
                              weights={"c": 1.0}, colors={"c": (0.2, 0.4, 0.6)})
    return SourceDescriptor((source,)), display


def _viewport(dpr=1.0, polygon=None, rect=None, world=WORLD):
    logical = (int(round(PHYS[0] / dpr)), int(round(PHYS[1] / dpr)))
    return ViewportSnapshot(world, logical, dpr, roi_world_rect=rect,
                            roi_polygon_world=polygon)


def _submit(layer, viewport):
    descriptor, display = _picture()
    layer.resize(*viewport.logical_size)
    return layer.submit(descriptor, display, viewport)


def _tiles(seed=0):
    """Two side-by-side tiles of one label image: odd sizes, a non-integer
    world scale, cells crossing the seam, ids above 2^24."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 6, size=(9, 13)).astype(np.uint32)
    ids = np.kron(coarse, np.ones((5, 5), np.uint32))[:43, :61]   # blocks of 5
    ids[ids > 0] += BIG + 7
    # Off the pixel-centre lattice on purpose (centres are at 11 + 2.5 + 5k
    # etc.): an edge exactly ON a centre is decided by the last float bit,
    # which CPU float32 and the GPU may round differently.
    x0, y0, scale = 57.23, 43.61, 13.37                           # world px per texel
    left, right = ids[:, :30], ids[:, 30:]
    planes = (
        LabelPlane(("t", seed, 0), (x0, x0 + 30 * scale, y0, y0 + 43 * scale), left),
        LabelPlane(("t", seed, 1), (x0 + 30 * scale, x0 + 61 * scale, y0, y0 + 43 * scale), right),
    )
    return planes


def reference_ids(planes, viewport, size=PHYS):
    """Per output pixel, from its centre: the image shader's mapping in
    float32, then the texel under it; later planes over earlier ones."""
    f = np.float32
    width, height = size
    out = np.zeros((height, width), np.uint32)          # GL rows, bottom first
    vx0, vx1, vy0, vy1 = (f(v) for v in viewport.world_rect)
    i = np.arange(width, dtype=f)
    j = np.arange(height, dtype=f)
    ux = (i + f(0.5)) / f(width)
    uy = (j + f(0.5)) / f(height)
    wx = vx0 + ux * (vx1 - vx0)
    wy = vy1 - uy * (vy1 - vy0)
    for plane in planes:
        px0, px1, py0, py1 = (f(v) for v in plane.world_rect)
        th, tw = plane.ids.shape
        cols = (wx >= px0) & (wx < px1)
        rows = (wy >= py0) & (wy < py1)
        tx = np.clip(np.floor((wx - px0) / (px1 - px0) * f(tw)).astype(int), 0, tw - 1)
        ty = np.clip(np.floor((wy - py0) / (py1 - py0) * f(th)).astype(int), 0, th - 1)
        rr, cc = np.nonzero(rows[:, None] & cols[None, :])
        out[rr, cc] = plane.ids[ty[rr], tx[cc]]
    return np.flipud(out)                                # top-left rows, like readbacks


def _snapshot(planes, **style):
    return LabelSnapshot((LabelLayer("cell", planes, **style),))


# ── the id image ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_the_id_image_equals_the_independent_reference(layer, dpr):
    planes = _tiles()
    vp = _viewport(dpr)
    _submit(layer, vp)
    layer.set_labels(_snapshot(planes))
    got = layer.readback_label_ids_for_test()
    want = reference_ids(planes, vp)
    assert got.shape == want.shape
    np.testing.assert_array_equal(got, want)
    assert got.max() > BIG and (got == 0).any()


def test_a_later_plane_overwrites_an_earlier_one_its_zeros_included(layer):
    old = _tiles(seed=1)
    x0, x1, y0, y1 = old[0].world_rect[0], old[1].world_rect[1], old[0].world_rect[2], old[0].world_rect[3]
    new_ids = np.zeros((5, 7), np.uint32)
    new_ids[1:3, 1:4] = BIG + 99                        # mostly background
    new = LabelPlane(("new",), (x0 + 50.0, x0 + 400.0, y0 + 30.0, y0 + 300.0), new_ids)
    vp = _viewport()
    _submit(layer, vp)
    layer.set_labels(_snapshot(old + (new,)))
    got = layer.readback_label_ids_for_test()
    np.testing.assert_array_equal(got, reference_ids(old + (new,), vp))
    only_old = reference_ids(old, vp)
    under_new = reference_ids((new,), vp) != 0
    covered = reference_ids((LabelPlane(("cov",), new.world_rect, np.ones_like(new_ids)),), vp) == 1
    assert (covered & (only_old != 0) & ~under_new).any()           # zeros did cover labels
    assert not (got[covered & ~under_new]).any()


# ── outlines, fills, blending, order ─────────────────────────────────────

@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("width", [0, 1, 2, 4])
def test_outlines_equal_the_reference(layer, dpr, width):
    planes = _tiles()
    vp = _viewport(dpr)
    _submit(layer, vp)
    picture = layer.readback_rgba_for_test()
    layer.set_labels(_snapshot(planes, color=(1.0, 0.0, 1.0), alpha=1.0, width=width,
                               mode=LABEL_OUTLINE))
    shown = layer.readback_shown_for_test()
    ids = reference_ids(planes, vp)
    edge = outline_reference(ids, label_radius(width, dpr))
    np.testing.assert_array_equal(shown[edge][:, :3], np.tile([255, 0, 255], (edge.sum(), 1)))
    np.testing.assert_array_equal(shown[~edge], picture[~edge])
    if width:
        assert edge.any()


def test_fills_equal_the_reference_and_blend(layer):
    planes = _tiles()
    vp = _viewport()
    _submit(layer, vp)
    picture = layer.readback_rgba_for_test()
    ids = reference_ids(planes, vp)
    inside = ids != 0
    layer.set_labels(_snapshot(planes, alpha=1.0, mode=LABEL_FILL))
    shown = layer.readback_shown_for_test()
    np.testing.assert_array_equal(shown[inside][:, :3], fill_colour(ids)[inside][:, :3])
    np.testing.assert_array_equal(shown[~inside], picture[~inside])
    layer.set_labels(_snapshot(planes, alpha=0.5, mode=LABEL_FILL))
    shown = layer.readback_shown_for_test().astype(int)
    want = 0.5 * fill_colour(ids)[..., :3].astype(float) + 0.5 * picture[..., :3].astype(float)
    assert np.abs(shown[inside][:, :3] - want[inside]).max() <= 1


def test_the_nucleus_layer_lies_on_top(layer):
    planes = _tiles()
    vp = _viewport()
    _submit(layer, vp)
    ids = reference_ids(planes, vp)
    layer.set_labels(LabelSnapshot((
        LabelLayer("cell", planes, color=(1.0, 0.0, 0.0), alpha=1.0, mode=LABEL_FILL),
        LabelLayer("nucleus", planes, color=(0.0, 0.0, 1.0), alpha=1.0, width=1,
                   mode=LABEL_OUTLINE),
    )))
    shown = layer.readback_shown_for_test()
    edge = outline_reference(ids, 1)
    fill_only = (ids != 0) & ~edge
    np.testing.assert_array_equal(shown[edge][:, :3], np.tile([0, 0, 255], (edge.sum(), 1)))
    np.testing.assert_array_equal(shown[fill_only][:, :3], fill_colour(ids)[fill_only][:, :3])


# ── the ROI, the picture, the budget ─────────────────────────────────────

CONCAVE = ((100.0, 60.0), (900.0, 60.0), (900.0, 300.0), (420.0, 420.0),
           (900.0, 560.0), (900.0, 760.0), (100.0, 760.0))


def test_a_concave_roi_clips_the_masks_like_the_picture(layer):
    planes = _tiles()
    rect = (100.0, 900.0, 60.0, 760.0)
    vp = _viewport(polygon=CONCAVE, rect=rect)
    _submit(layer, vp)
    picture = layer.readback_rgba_for_test()
    outside = picture[..., 3] == 0
    assert outside.any() and (~outside).any()
    ids = reference_ids(planes, vp)
    assert (outside & (ids != 0)).any()          # there IS mask outside the ROI to clip
    for mode in (LABEL_FILL, LABEL_OUTLINE):
        layer.set_labels(_snapshot(planes, alpha=1.0, mode=mode, width=2))
        shown = layer.readback_shown_for_test()
        np.testing.assert_array_equal(shown[outside], picture[outside])
        assert (shown[~outside] != picture[~outside]).any()
    # a label-only redraw keeps the same boundary
    layer.set_labels(_snapshot(planes, alpha=1.0, mode=LABEL_FILL, color=(0, 1, 0)))
    np.testing.assert_array_equal(layer.readback_shown_for_test()[outside], picture[outside])


def test_a_label_change_composes_no_channel(layer):
    planes = _tiles()
    vp = _viewport()
    stats = _submit(layer, vp)
    before = layer.label_stats()
    for alpha in (0.3, 0.6, 0.9):
        layer.set_labels(_snapshot(planes, alpha=alpha))
    after = layer.label_stats()
    assert after["image_submissions"] == before["image_submissions"]
    assert after["label_compositions"] == before["label_compositions"] + 3
    assert stats["mode"] == MODE_OVERLAY


def test_labels_on_without_masks_shows_the_same_picture(app):
    plain, labelled = _make(app, labels=False), _make(app, labels=True)
    try:
        for dpr in (1.0, 2.0):
            vp = _viewport(dpr, polygon=CONCAVE, rect=(100.0, 900.0, 60.0, 760.0))
            _submit(plain, vp)
            _submit(labelled, vp)
            np.testing.assert_array_equal(labelled.readback_shown_for_test(),
                                          plain.readback_rgba_for_test())
            assert not labelled.label_stats()["shown"]
        labelled.set_labels(_snapshot(_tiles()))
        labelled.set_labels(LabelSnapshot())                 # masks off again
        np.testing.assert_array_equal(labelled.readback_shown_for_test(),
                                      plain.readback_rgba_for_test())
        assert not plain.labels_enabled
        assert "label_ids" not in plain._programs and "ids" not in plain._targets
        with pytest.raises(Step1GpuLayerError):
            plain.set_labels(LabelSnapshot())
    finally:
        for made in (plain, labelled):
            _drop(made)


def test_the_label_budget_holds(app):
    small = _make(app, max_label_texture_bytes=4 * 43 * 30)    # one tile's bytes
    try:
        _submit(small, _viewport())
        planes = _tiles()
        small.set_labels(_snapshot(planes[:1]))
        with pytest.raises(Step1GpuLayerError, match="label budget"):
            small.set_labels(_snapshot(planes))
    finally:
        _drop(small)
