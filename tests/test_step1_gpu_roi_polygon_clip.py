"""The GPU picture stops at the DRAWN ROI, not at its bounding box.

G3.2c.1. G3.2c clipped to `roi_bbox_fullres`, and the 2026-09-22 machine
proved that works -- and that it is not enough: the user's nine-point ROI
covers 79.4 % of its own bbox, and a raw channel (which Step0 never
polygon-masks) filled the other 20.6 % right out to the rectangle's four
straight edges.

Every gate reads REAL FRAMEBUFFER PIXELS and asserts three things:

  * outside the polygon, alpha is 0;
  * inside the polygon, the image is IDENTICAL to the same submission with
    no polygon at all -- clipping may not change what it keeps;
  * the unclipped frame REALLY DID paint outside the polygon, so a gate
    cannot pass on an empty picture.

The even-odd rule is what makes a CONCAVE shape work, so the default
polygon here is concave on purpose.
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
sys.path.insert(0, str(_ROOT.parent))

from block01.ui.step1_gpu_layer import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, ChannelSource, DisplaySnapshot, RawPlane,
    SourceDescriptor, Step1GpuLayer, ViewportSnapshot, sanitize_roi_polygon)

SIZE = 256
WORLD = (0.0, 1024.0, 0.0, 1024.0)
STRIDE = 64

#: A deliberately CONCAVE polygon: a square with a deep notch cut into its
#: right edge. A triangle fan with GL_REPLACE would fill the notch; even-odd
#: does not, and `test_the_notch_of_a_concave_polygon_stays_empty` is the
#: gate that tells the two apart.
CONCAVE = ((200.0, 200.0), (800.0, 200.0), (800.0, 400.0), (450.0, 512.0),
           (800.0, 624.0), (800.0, 824.0), (200.0, 824.0))
#: Its bounding box, in the layer's `(x0, x1, y0, y1)` order.
CONCAVE_RECT = (200.0, 800.0, 200.0, 824.0)

#: The nine-point region the user actually drew (2026-09-22 20:20 machine
#: capture), scaled into this rig's world. Points are `(x, y)`, clockwise.
REAL_ROI = ((7552, 1632), (4320, 4384), (5312, 8608), (7552, 11104),
            (14752, 10336), (16992, 9344), (16992, 5376), (16992, 2144),
            (9024, 640))
REAL_RECT = (4320.0, 16992.0, 640.0, 11104.0)
REAL_WORLD = (0.0, 21000.0, 0.0, 12000.0)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def layer(app):
    made = Step1GpuLayer(max_raw_texture_bytes=16 * 1024 * 1024,
                         require_hardware=True)
    made.resize(SIZE, SIZE)
    made.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    made.show()
    for _ in range(8):
        app.processEvents()
    if not made._initialized:
        pytest.fail(f"required G1 layer did not initialize: {made._init_error}")
    yield made
    made.dispose()
    made.deleteLater()


def plane(world=WORLD, value=700.0, tag="coarse", cells=None):
    """A plane that covers the whole world rect, so anything unclipped shows."""
    wx0, wx1, wy0, wy1 = world
    cells = cells or int(round((wx1 - wx0) / STRIDE))
    rows = int(round((wy1 - wy0) / STRIDE)) if cells is None else cells
    values = np.full((rows, cells), value, np.float32)
    return RawPlane(identity=(tag, world), world_rect=world, values=values)


def overlay(channels):
    return DisplaySnapshot(mode=MODE_OVERLAY,
                           mappings={n: (0.0, 1000.0, 1.0) for n in channels},
                           weights={n: 1.0 for n in channels},
                           colors={n: (0.2, 0.6, 1.0) for n in channels})


def fusion(markers, nucleus):
    names = list(markers) + [nucleus]
    return DisplaySnapshot(mode=MODE_FUSION,
                           mappings={n: (0.0, 1000.0, 1.0) for n in names},
                           groups={"markers": {n: 1.0 for n in markers}},
                           group_weights={"markers": 1.0},
                           nucleus=(nucleus, 1.0))


def render(layer, descriptor, display, *, polygon, rect, world=WORLD,
           size=SIZE, dpr=1.0):
    logical = int(round(size / dpr))
    layer.resize(logical, logical)
    viewport = ViewportSnapshot(world, (logical, logical), dpr,
                                roi_world_rect=rect,
                                roi_polygon_world=polygon)
    stats = layer.submit(descriptor, display, viewport)
    return layer.readback_rgba_for_test(), stats


def inside_polygon(shape, world, polygon):
    """Which output pixels have their CENTRE inside the polygon (even-odd)."""
    from matplotlib.path import Path          # only for the ORACLE, not the code
    height, width = shape[:2]
    wx0, wx1, wy0, wy1 = world
    xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
    ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
    grid_x, grid_y = np.meshgrid(xs, ys)
    points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    mask = Path(np.asarray(polygon, float)).contains_points(points)
    return mask.reshape(height, width)


def assert_polygon_clipped(clipped, unclipped, polygon, world=WORLD, what=""):
    inside = inside_polygon(clipped.shape, world, polygon)
    # the edge is a half-pixel question; judge strictly inside/outside by
    # eroding one pixel on each side of the boundary
    from scipy.ndimage import binary_erosion, binary_dilation
    strict_in = binary_erosion(inside, np.ones((3, 3), bool))
    strict_out = ~binary_dilation(inside, np.ones((3, 3), bool))
    leaked = int(np.count_nonzero((clipped[..., 3] > 0) & strict_out))
    assert leaked == 0, f"{what}: {leaked} opaque pixels outside the polygon"
    kept = (clipped[..., 3] > 0) & strict_in
    assert int(np.count_nonzero(kept)) > 0, f"{what}: nothing kept inside"
    assert np.array_equal(clipped[strict_in], unclipped[strict_in]), (
        f"{what}: clipping changed pixels INSIDE the polygon")
    would_leak = int(np.count_nonzero((unclipped[..., 3] > 0) & strict_out))
    assert would_leak > 0, (
        f"{what}: the unclipped frame did not paint outside the polygon, "
        f"so this case cannot prove anything")
    return inside


# ── the validator ────────────────────────────────────────────────────

def test_a_polygon_must_belong_to_its_rectangle():
    good, error = sanitize_roi_polygon(CONCAVE, CONCAVE_RECT)
    assert good is not None and error == ""
    for points, why in (
            (CONCAVE, (0.0, 100.0, 0.0, 100.0)),      # another ROI's rect
            (((1.0, 2.0), (3.0, 4.0)), CONCAVE_RECT),  # too few points
            ((("x", 1), (2, 3), (4, 5)), CONCAVE_RECT),  # not numbers
            (((float("nan"), 1.0), (2.0, 3.0), (4.0, 5.0)), CONCAVE_RECT)):
        polygon, error = sanitize_roi_polygon(points, why)
        assert polygon is None and error, why
    assert sanitize_roi_polygon(None, CONCAVE_RECT) == (None, "")


# ── the shape itself ─────────────────────────────────────────────────

def test_the_notch_of_a_concave_polygon_stays_empty(layer):
    """The gate that separates even-odd from a solid triangle fan."""
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]),
                          polygon=None, rect=None)
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]),
                            polygon=CONCAVE, rect=CONCAVE_RECT)
    assert stats["roi_polygon_points"] == len(CONCAVE)
    assert stats["roi_polygon_error"] == ""
    assert_polygon_clipped(clipped, unclipped, CONCAVE, what="concave")

    # the notch is INSIDE the bounding box and OUTSIDE the shape: a fan with
    # GL_REPLACE fills it, even-odd leaves it empty
    height, width = clipped.shape[:2]
    notch_x = int((700.0 - WORLD[0]) / (WORLD[1] - WORLD[0]) * width)
    notch_y = int((512.0 - WORLD[2]) / (WORLD[3] - WORLD[2]) * height)
    assert clipped[notch_y, notch_x, 3] == 0, "the concave notch was filled"
    assert unclipped[notch_y, notch_x, 3] > 0, "the notch had nothing to hide"


@pytest.mark.parametrize("winding", ["as_drawn", "reversed"])
def test_both_windings_clip_the_same(layer, winding):
    polygon = CONCAVE if winding == "as_drawn" else tuple(reversed(CONCAVE))
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]),
                          polygon=None, rect=None)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]),
                        polygon=polygon, rect=CONCAVE_RECT)
    assert_polygon_clipped(clipped, unclipped, polygon, what=winding)


def test_the_real_nine_point_roi_from_the_machine(layer):
    """The 2026-09-22 capture, counted against the POLYGON not the bbox."""
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(plane(world=REAL_WORLD, cells=None),),
                      selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None, world=REAL_WORLD)
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]),
                            polygon=REAL_ROI, rect=REAL_RECT, world=REAL_WORLD)
    inside = assert_polygon_clipped(clipped, unclipped, REAL_ROI,
                                    world=REAL_WORLD, what="real ROI")
    # and the bbox-only clip really would have leaked here
    from block01.ui.step1_gpu_layer import roi_scissor_box
    box = roi_scissor_box(REAL_WORLD, REAL_RECT, (clipped.shape[1], clipped.shape[0]))
    bbox_mask = np.zeros(clipped.shape[:2], bool)
    x, y, w, h = box
    bbox_mask[y:y + h, x:x + w] = True
    bbox_mask = np.flipud(bbox_mask)            # scissor is bottom-up
    extra = int(np.count_nonzero(bbox_mask & ~inside))
    assert extra > 0, "this ROI has no area between its polygon and its bbox"


# ── the tiers, the modes, and the camera ─────────────────────────────

@pytest.mark.parametrize("kind", ["coarse_only", "fine_only", "mixed"])
def test_every_tier_stops_at_the_polygon(layer, kind):
    coarse = plane()
    fine = RawPlane(identity=("fine", 1), world_rect=(256.0, 768.0, 256.0, 768.0),
                    values=np.full((64, 64), 500.0, np.float32))
    if kind == "coarse_only":
        source = ChannelSource("DAPI", coarse=(coarse,), selected_level="coarse")
    elif kind == "fine_only":
        source = ChannelSource("DAPI", fine=(fine,), selected_level="fine")
    else:
        source = ChannelSource("DAPI", coarse=(coarse,), fine=(fine,),
                               selected_level="fine")
    descriptor = SourceDescriptor((source,))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                        rect=CONCAVE_RECT)
    assert_polygon_clipped(clipped, unclipped, CONCAVE, what=kind)


def test_overlay_and_fusion_and_a_raw_dapi_alone(layer):
    marker = ChannelSource("CD3", coarse=(plane(value=400.0, tag="cd3"),),
                           selected_level="coarse")
    nucleus = ChannelSource("DAPI", coarse=(plane(),), selected_level="coarse")
    for descriptor, display, what in (
            (SourceDescriptor((nucleus,)), overlay(["DAPI"]), "raw DAPI alone"),
            (SourceDescriptor((nucleus, marker)), overlay(["DAPI", "CD3"]), "overlay"),
            (SourceDescriptor((nucleus, marker)), fusion(["CD3"], "DAPI"), "fusion")):
        unclipped, _ = render(layer, descriptor, display, polygon=None, rect=None)
        clipped, _ = render(layer, descriptor, display, polygon=CONCAVE,
                            rect=CONCAVE_RECT)
        assert_polygon_clipped(clipped, unclipped, CONCAVE, what=what)


@pytest.mark.parametrize("world,what", [
    ((100.0, 612.0, 150.0, 662.0), "panned"),
    ((-500.0, 2000.0, -400.0, 2100.0), "zoomed out"),
    ((13.7, 987.3, 41.9, 1015.5), "non-integer scale"),
])
def test_the_polygon_follows_the_camera(layer, world, what):
    descriptor = SourceDescriptor((ChannelSource(
        "DAPI", coarse=(plane(world=(0.0, 1024.0, 0.0, 1024.0)),),
        selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None, world=world)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                        rect=CONCAVE_RECT, world=world)
    assert_polygon_clipped(clipped, unclipped, CONCAVE, world=world, what=what)


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_device_pixel_ratio_does_not_move_the_shape(layer, dpr):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None, dpr=dpr)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                        rect=CONCAVE_RECT, dpr=dpr)
    assert_polygon_clipped(clipped, unclipped, CONCAVE, what=f"dpr={dpr}")


def test_a_resize_rebuilds_the_stencil(layer):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
           rect=CONCAVE_RECT, size=128)
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None, size=320)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                        rect=CONCAVE_RECT, size=320)
    assert clipped.shape[0] == 320
    assert_polygon_clipped(clipped, unclipped, CONCAVE, what="after resize")


def test_a_region_off_screen_leaves_nothing(layer):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                        rect=CONCAVE_RECT, world=(5000.0, 6000.0, 5000.0, 6000.0))
    assert int(np.count_nonzero(clipped[..., 3] > 0)) == 0


# ── one ROI after another, and no ROI at all ─────────────────────────

def test_roi_a_then_b_then_none(layer):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    other = ((100.0, 100.0), (400.0, 100.0), (400.0, 400.0), (100.0, 400.0))
    other_rect = (100.0, 400.0, 100.0, 400.0)
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                          rect=None)

    first, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                      rect=CONCAVE_RECT)
    assert_polygon_clipped(first, unclipped, CONCAVE, what="ROI A")

    second, _ = render(layer, descriptor, overlay(["DAPI"]), polygon=other,
                       rect=other_rect)
    assert_polygon_clipped(second, unclipped, other, what="ROI B")
    # nothing of A survives outside B
    inside_a = inside_polygon(second.shape, WORLD, CONCAVE)
    inside_b = inside_polygon(second.shape, WORLD, other)
    stale = inside_a & ~inside_b
    assert int(np.count_nonzero((second[..., 3] > 0) & stale)) == 0

    cleared, stats = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                            rect=None)
    assert stats["roi_polygon_points"] == 0
    assert np.array_equal(cleared, unclipped), "a stale mask survived"


def test_a_bbox_without_a_polygon_is_still_g32c(layer):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]), polygon=None,
                            rect=CONCAVE_RECT)
    assert stats["roi_polygon_points"] == 0 and stats["roi_polygon_error"] == ""
    height, width = clipped.shape[:2]
    notch_x = int(700.0 / 1024.0 * width)
    notch_y = int(512.0 / 1024.0 * height)
    assert clipped[notch_y, notch_x, 3] > 0, (
        "with no polygon the rectangle's interior must still be drawn")


# ── a polygon that cannot be used is REFUSED, not ignored ────────────

def test_a_broken_polygon_is_reported_and_the_rectangle_still_clips(layer):
    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    broken = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))     # not this ROI's bounds
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]),
                            polygon=broken, rect=CONCAVE_RECT)
    assert stats["roi_polygon_error"], "a refused polygon said nothing"
    assert stats["roi_polygon_points"] == 0
    assert "polygon" in layer.roi_polygon_error().lower()
    # the picture is still bounded by the rectangle -- it does not leak
    from block01.ui.step1_gpu_layer import roi_scissor_box
    box = roi_scissor_box(WORLD, CONCAVE_RECT, (clipped.shape[1], clipped.shape[0]))
    mask = np.zeros(clipped.shape[:2], bool)
    x, y, w, h = box
    mask[y:y + h, x:x + w] = True
    mask = np.flipud(mask)
    assert int(np.count_nonzero((clipped[..., 3] > 0) & ~mask)) == 0


# ── the limit this block does NOT cover, written down as a gate ──────

def test_the_cpu_fallback_has_no_polygon_clipping(app):
    """A LIMITATION, encoded so it cannot be forgotten.

    G3.2c.1 clips on the GPU. The CPU composition (`Step1ComposedLayer`,
    used when the GPU layer cannot start) takes no polygon and is not
    changed by this block: on that path the picture is still bounded by
    what the source rule gives -- the ROI rectangle for a raw channel.
    If a later block teaches the CPU path to clip, this gate is the one
    that should be rewritten deliberately.
    """
    import inspect

    from block01.ui.step1_composed_layer import Step1ComposedLayer
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount

    signature = inspect.signature(Step1ComposedLayer.__init__)
    assert not any("polygon" in name for name in signature.parameters), (
        "the CPU composed layer now takes a polygon; this limitation gate "
        "needs rewriting rather than deleting")
    source = inspect.getsource(Step1WholeSlideMount._start_cpu_backend)
    assert "polygon" not in source, (
        "the CPU backend now sees a polygon; see the note above")
    # ...and the GPU path is the one that does have it
    assert "roi_polygon_world" in inspect.getsource(
        Step1WholeSlideMount._gpu_viewport_snapshot)


# ── GL state is handed back as it was found ──────────────────────────

def test_gl_state_is_restored_after_a_masked_submission(layer, app):
    from OpenGL import GL

    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
           rect=CONCAVE_RECT)
    layer.makeCurrent()
    try:
        assert GL.glIsEnabled(GL.GL_SCISSOR_TEST) == GL.GL_FALSE
        assert GL.glIsEnabled(GL.GL_STENCIL_TEST) == GL.GL_FALSE
        assert int(GL.glGetIntegerv(GL.GL_STENCIL_WRITEMASK)) == 0xFF
        assert list(GL.glGetBooleanv(GL.GL_COLOR_WRITEMASK)) == [1, 1, 1, 1]
        assert int(GL.glGetIntegerv(GL.GL_CURRENT_PROGRAM)) == 0
        assert int(GL.glGetIntegerv(GL.GL_VERTEX_ARRAY_BINDING)) == 0
    finally:
        layer.doneCurrent()


def test_a_failure_while_clipping_still_hands_the_state_back(layer):
    """The error path is a path too.

    Found in the G3.2d review: the restore used to sit after the draw, so a
    shader or context failure between them would have left the scissor, the
    stencil test and a closed colour mask on for the next frame and for
    `paintGL`'s blit.
    """
    from OpenGL import GL

    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    real_draw = type(layer)._draw
    boom = RuntimeError("the draw failed")

    def failing(self):
        raise boom

    type(layer)._draw = failing
    try:
        with pytest.raises(RuntimeError):
            render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
                   rect=CONCAVE_RECT)
    finally:
        type(layer)._draw = real_draw

    layer.makeCurrent()
    try:
        assert GL.glIsEnabled(GL.GL_SCISSOR_TEST) == GL.GL_FALSE
        assert GL.glIsEnabled(GL.GL_STENCIL_TEST) == GL.GL_FALSE
        # "writes are allowed again" -- 0xFF is what the restore sets and
        # 0xFFFFFFFF (read back as -1) is the pristine default; the state
        # that must NOT survive is a suppressed mask (0x00) or the mask
        # fan (0x01).
        mask = int(GL.glGetIntegerv(GL.GL_STENCIL_WRITEMASK)) & 0xFFFFFFFF
        assert mask not in (0x00, 0x01), f"stencil writes left masked: {mask:#x}"
        assert list(GL.glGetBooleanv(GL.GL_COLOR_WRITEMASK)) == [1, 1, 1, 1]
    finally:
        layer.doneCurrent()


def test_the_off_screen_path_also_restores_state(layer):
    from OpenGL import GL

    descriptor = SourceDescriptor((ChannelSource("DAPI", coarse=(plane(),),
                                                 selected_level="coarse"),))
    render(layer, descriptor, overlay(["DAPI"]), polygon=CONCAVE,
           rect=CONCAVE_RECT, world=(5000.0, 6000.0, 5000.0, 6000.0))
    layer.makeCurrent()
    try:
        assert GL.glIsEnabled(GL.GL_SCISSOR_TEST) == GL.GL_FALSE
        assert GL.glIsEnabled(GL.GL_STENCIL_TEST) == GL.GL_FALSE
        assert int(GL.glGetIntegerv(GL.GL_STENCIL_WRITEMASK)) == 0xFF
    finally:
        layer.doneCurrent()
