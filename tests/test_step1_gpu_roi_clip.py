"""The GPU picture stops at the ROI bbox, and nothing inside it moves.

G3.2c. A coarse plane is drawn over the whole world area of every texel it
holds, and a texel whose block only PARTLY meets the analysis region is a
real sample -- so before this, the region's edge leaked up to a whole block
of DAPI (or any channel) outside the rectangle. Measured on this GPU
before the fix: 22 640 opaque pixels outside a 600x600 region at a
64-pixel stride; after it: 0.

The 18 GPU display cases read REAL FRAMEBUFFER PIXELS back
(`readback_rgba_for_test`). Where the ROI intersects displayed data, they
check both sides of the boundary:

  * outside the bbox, alpha is 0 -- by the pixel-centre rule, the same rule
    the rest of the pipeline uses to decide which sample a pixel is;
  * inside the bbox, the image is IDENTICAL to the same submission with no
    region at all -- clipping may not change one pixel of what it keeps.

Set BLOCK01_REQUIRE_STEP1_GPU=1; in that mode a software renderer, a
missing PyOpenGL or a shader failure is a FAILURE, never a skip.
The other seven cases check the pixel-centre rule and mount conversion
without constructing a GL widget.
"""

import importlib.util
import math
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
    SourceDescriptor, Step1GpuLayer, ViewportSnapshot, roi_scissor_box)

SIZE = 256
WORLD = (0.0, 1024.0, 0.0, 1024.0)          # (x0, x1, y0, y1)
STRIDE = 64                                  # one coarse texel, in world px
#: Deliberately NOT on a block boundary, so partial coarse cells exist on
#: all four sides.
ROI = (200.0, 800.0, 300.0, 900.0)           # (y0, y1, x0, x1), source order


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


def roi_rect(roi=ROI):
    """`(y0, y1, x0, x1)` -> the layer's `(x0, x1, y0, y1)`."""
    y0, y1, x0, x1 = roi
    return (float(x0), float(x1), float(y0), float(y1))


def coarse_plane(roi=ROI, world=WORLD, stride=STRIDE, value=700.0, tag="coarse"):
    """A block plane whose texels are valid wherever the block MEETS the ROI.

    This is the geometry the binding produces: the plane's world rect is the
    whole tile, and a partially covered block is a real sample.
    """
    y0, y1, x0, x1 = roi
    wx0, wx1, wy0, wy1 = world
    cols = int(round((wx1 - wx0) / stride))
    rows = int(round((wy1 - wy0) / stride))
    values = np.full((rows, cols), value, np.float32)
    for row in range(rows):
        for col in range(cols):
            by0, by1 = wy0 + row * stride, wy0 + (row + 1) * stride
            bx0, bx1 = wx0 + col * stride, wx0 + (col + 1) * stride
            if by1 <= y0 or by0 >= y1 or bx1 <= x0 or bx0 >= x1:
                values[row, col] = np.nan       # no sample at all
    return RawPlane(identity=(tag, stride), world_rect=world, values=values)


def fine_plane(rect, value=500.0, tag="fine", pixels=64):
    x0, x1, y0, y1 = rect
    return RawPlane(identity=(tag, rect), world_rect=rect,
                    values=np.full((pixels, pixels), value, np.float32))


def overlay(channels, colors=None):
    colors = colors or {name: (0.2, 0.6, 1.0) for name in channels}
    return DisplaySnapshot(mode=MODE_OVERLAY,
                           mappings={n: (0.0, 1000.0, 1.0) for n in channels},
                           weights={n: 1.0 for n in channels}, colors=colors)


def fusion(markers, nucleus):
    names = list(markers) + [nucleus]
    return DisplaySnapshot(
        mode=MODE_FUSION,
        mappings={n: (0.0, 1000.0, 1.0) for n in names},
        groups={"markers": {n: 1.0 for n in markers}},
        group_weights={"markers": 1.0}, nucleus=(nucleus, 1.0))


def render(layer, descriptor, display, *, roi=ROI, world=WORLD, size=SIZE,
           dpr=1.0):
    """One submission, and the framebuffer it produced (C1 top-left rows)."""
    logical = int(round(size / dpr))
    layer.resize(logical, logical)
    viewport = ViewportSnapshot(world, (logical, logical), dpr,
                                roi_world_rect=None if roi is None
                                else roi_rect(roi))
    stats = layer.submit(descriptor, display, viewport)
    return layer.readback_rgba_for_test(), stats


def inside_mask(frame, roi=ROI, world=WORLD):
    """Which output pixels have their CENTRE inside the region."""
    height, width = frame.shape[:2]
    wx0, wx1, wy0, wy1 = world
    y0, y1, x0, x1 = roi
    xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
    # `readback_rgba_for_test` already flipped to top-left rows, so row 0 is
    # the smallest world y.
    ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
    return ((ys[:, None] >= y0) & (ys[:, None] < y1)
            & (xs[None, :] >= x0) & (xs[None, :] < x1))


def assert_clipped(clipped, unclipped, roi=ROI, world=WORLD, what=""):
    inside = inside_mask(clipped, roi, world)
    outside_opaque = int(np.count_nonzero((clipped[..., 3] > 0) & ~inside))
    assert outside_opaque == 0, (
        f"{what}: {outside_opaque} opaque pixels outside the bbox")
    assert np.array_equal(clipped[inside], unclipped[inside]), (
        f"{what}: clipping changed pixels INSIDE the bbox")
    return inside


# ── the pixel rule itself ────────────────────────────────────────────

def test_the_scissor_box_is_the_pixel_centre_rule():
    """Checked against a per-pixel evaluation, not against itself."""
    for width, height in ((7, 5), (64, 64), (1, 1), (33, 17)):
        world = (-12.5, 310.25, 4.0, 199.5)
        region = (30.3, 288.8, 11.2, 150.9)
        box = roi_scissor_box(world, region, (width, height))
        wx0, wx1, wy0, wy1 = world
        bx0, bx1, by0, by1 = region
        expected = np.zeros((height, width), bool)
        for j in range(height):
            cy = wy1 - (j + 0.5) * (wy1 - wy0) / height
            for i in range(width):
                cx = wx0 + (i + 0.5) * (wx1 - wx0) / width
                expected[j, i] = (bx0 <= cx < bx1) and (by0 <= cy < by1)
        got = np.zeros((height, width), bool)
        x, y, w, h = box
        if w > 0 and h > 0:
            got[y:y + h, x:x + w] = True
        assert np.array_equal(expected, got), (width, height, box)


def test_no_region_clips_nothing():
    assert roi_scissor_box(WORLD, None, (SIZE, SIZE)) is None


# ── coarse, fine, mixed ──────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["coarse_only", "fine_only", "mixed"])
def test_every_tier_stops_at_the_bbox(layer, kind):
    coarse = coarse_plane()
    fine = fine_plane((256.0, 768.0, 256.0, 768.0))
    if kind == "coarse_only":
        source = ChannelSource("DAPI", coarse=(coarse,), selected_level="coarse")
    elif kind == "fine_only":
        source = ChannelSource("DAPI", fine=(fine,), selected_level="fine")
    else:
        source = ChannelSource("DAPI", coarse=(coarse,), fine=(fine,),
                               selected_level="fine")
    descriptor = SourceDescriptor((source,))
    display = overlay(["DAPI"])
    unclipped, _ = render(layer, descriptor, display, roi=None)
    clipped, stats = render(layer, descriptor, display)
    inside = assert_clipped(clipped, unclipped, what=kind)
    assert int(np.count_nonzero(clipped[..., 3] > 0)) > 0, "nothing drawn"
    assert stats["roi_scissor"] is not None
    # the fix has something to do: the unclipped frame DID leak
    assert int(np.count_nonzero((unclipped[..., 3] > 0) & ~inside)) > 0, (
        "this case never leaked, so it cannot prove the clip")


def test_overlay_and_fusion_and_dapi_are_all_clipped(layer):
    coarse = coarse_plane()
    other = coarse_plane(value=400.0, tag="cd3")
    channels = (ChannelSource("DAPI", coarse=(coarse,), selected_level="coarse"),
                ChannelSource("CD3", coarse=(other,), selected_level="coarse"))
    descriptor = SourceDescriptor(channels)
    for display, what in ((overlay(["DAPI", "CD3"]), "overlay"),
                          (fusion(["CD3"], "DAPI"), "fusion")):
        unclipped, _ = render(layer, descriptor, display, roi=None)
        clipped, _ = render(layer, descriptor, display)
        assert_clipped(clipped, unclipped, what=what)
        assert int(np.count_nonzero(clipped[..., 3] > 0)) > 0


def test_a_zero_weight_channel_changes_nothing_about_the_boundary(layer):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),
        ChannelSource("CD3", coarse=(coarse_plane(value=900.0, tag="cd3"),),
                      selected_level="coarse")))
    display = DisplaySnapshot(
        mode=MODE_OVERLAY,
        mappings={"DAPI": (0.0, 1000.0, 1.0), "CD3": (0.0, 1000.0, 1.0)},
        weights={"DAPI": 1.0, "CD3": 0.0},
        colors={"DAPI": (0.2, 0.6, 1.0), "CD3": (1.0, 0.0, 0.0)})
    unclipped, _ = render(layer, descriptor, display, roi=None)
    clipped, _ = render(layer, descriptor, display)
    assert_clipped(clipped, unclipped, what="zero weight")


# ── the four edges, and the cells that straddle them ─────────────────

def test_each_edge_is_where_the_bbox_says(layer):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),))
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]))
    opaque = clipped[..., 3] > 0
    rows = np.where(opaque.any(axis=1))[0]
    cols = np.where(opaque.any(axis=0))[0]
    inside = inside_mask(clipped)
    want_rows = np.where(inside.any(axis=1))[0]
    want_cols = np.where(inside.any(axis=0))[0]
    assert (int(rows.min()), int(rows.max())) == (int(want_rows.min()),
                                                  int(want_rows.max()))
    assert (int(cols.min()), int(cols.max())) == (int(want_cols.min()),
                                                  int(want_cols.max()))


def test_a_partial_coarse_cell_is_cut_not_kept_whole(layer):
    """The ROI edge falls INSIDE a block; the block may not paint past it."""
    roi = (200.0, 803.0, 300.0, 907.0)          # 3 and 7 px into a block
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(roi=roi),),
                      selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=roi)
    assert_clipped(clipped, unclipped, roi=roi, what="partial cell")


def test_adjacent_tiles_do_not_open_a_seam(layer):
    """Two neighbouring fine planes, clipped as one picture."""
    left = fine_plane((256.0, 512.0, 256.0, 768.0), tag="left")
    right = fine_plane((512.0, 768.0, 256.0, 768.0), value=650.0, tag="right")
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", fine=(left, right), selected_level="fine"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]))
    inside = assert_clipped(clipped, unclipped, what="adjacent tiles")
    # The seam column is still drawn wherever there was data to draw. Rows
    # inside the region but OUTSIDE both planes are legitimately empty --
    # the planes cover y in [256, 768) while the region is [200, 800) --
    # so the comparison is against the unclipped frame, not against "opaque
    # everywhere", which is what an earlier version of this gate demanded.
    seam = int(round((512.0 - WORLD[0]) / (WORLD[1] - WORLD[0]) * clipped.shape[1]))
    rows = inside[:, seam] & (unclipped[:, seam, 3] > 0)
    assert int(np.count_nonzero(rows)) > 0, "no seam pixels to check"
    assert np.array_equal(clipped[rows, seam], unclipped[rows, seam])


# ── camera: pan, zoom-out, non-integer scale, DPR ────────────────────

@pytest.mark.parametrize("world,what", [
    ((100.0, 612.0, 150.0, 662.0), "panned"),
    ((-500.0, 2000.0, -400.0, 2100.0), "zoomed out past the region"),
    ((13.7, 987.3, 41.9, 1015.5), "non-integer scale"),
    ((250.0, 450.0, 350.0, 550.0), "entirely inside the region"),
])
def test_the_boundary_follows_the_camera(layer, world, what):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(world=(0.0, 1024.0, 0.0, 1024.0)),),
                      selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None,
                          world=world)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), world=world)
    assert_clipped(clipped, unclipped, world=world, what=what)


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_device_pixel_ratio_does_not_move_the_boundary(layer, dpr):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None, dpr=dpr)
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]), dpr=dpr)
    assert_clipped(clipped, unclipped, what=f"dpr={dpr}")
    assert stats["physical_size"][0] == clipped.shape[1]


def test_a_region_off_screen_leaves_nothing_on_screen(layer):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),))
    clipped, stats = render(layer, descriptor, overlay(["DAPI"]),
                            world=(2000.0, 3000.0, 2000.0, 3000.0))
    assert int(np.count_nonzero(clipped[..., 3] > 0)) == 0
    assert stats["roi_scissor"][2] == 0 or stats["roi_scissor"][3] == 0


# ── the owner's own conversion, which is where an axis swap would hide ──

def test_the_mount_converts_the_source_tables_rect_order(app):
    """`roi_bbox()` answers `(y0, y1, x0, x1)`; the layer wants
    `(x0, x1, y0, y1)`. Four DISTINCT numbers, so a swap cannot pass.

    This gate exists because a mutation that returned the source order
    unchanged left every pixel gate in this file green: they all drive the
    layer directly and never go through the owner.
    """
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount

    class _Table:
        @staticmethod
        def roi_bbox():
            return (11, 22, 33, 44)          # y0, y1, x0, x1

    class _Provider:
        source_table = _Table()

    class _Stack:
        provider = _Provider()

    assert Step1WholeSlideMount._gpu_roi_world_rect(_Stack()) == (
        33.0, 44.0, 11.0, 22.0)


@pytest.mark.parametrize("table", ["none", "no_bbox", "raises", "short"])
def test_a_project_without_a_region_clips_nothing(app, table):
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount

    class _Raises:
        @staticmethod
        def roi_bbox():
            raise RuntimeError("no table today")

    class _Short:
        @staticmethod
        def roi_bbox():
            return (1, 2)

    class _NoBbox:
        @staticmethod
        def roi_bbox():
            return None

    chosen = {"none": None, "no_bbox": _NoBbox(), "raises": _Raises(),
              "short": _Short()}[table]

    class _Provider:
        source_table = chosen

    class _Stack:
        provider = _Provider()

    assert Step1WholeSlideMount._gpu_roi_world_rect(_Stack()) is None


# ── the no-region case, and the stale-region case ────────────────────

def test_a_whole_slide_project_is_byte_for_byte_what_it_was(layer):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),))
    first, stats = render(layer, descriptor, overlay(["DAPI"]), roi=None)
    second, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None)
    assert np.array_equal(first, second)
    assert stats["roi_scissor"] is None
    # and it still shows the leak, i.e. nothing was clipped behind our back
    assert int(np.count_nonzero((first[..., 3] > 0) & ~inside_mask(first))) > 0


def test_a_new_region_replaces_the_old_one_in_the_next_submission(layer):
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(coarse_plane(),), selected_level="coarse"),))
    render(layer, descriptor, overlay(["DAPI"]), roi=ROI)
    moved = (500.0, 900.0, 100.0, 400.0)
    plane = coarse_plane(roi=moved)
    descriptor = SourceDescriptor((
        ChannelSource("DAPI", coarse=(plane,), selected_level="coarse"),))
    unclipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=None)
    clipped, _ = render(layer, descriptor, overlay(["DAPI"]), roi=moved)
    assert_clipped(clipped, unclipped, roi=moved, what="moved region")
    # nothing from the FIRST region survives outside the new one
    stale = inside_mask(clipped, ROI) & ~inside_mask(clipped, moved)
    assert int(np.count_nonzero((clipped[..., 3] > 0) & stale)) == 0
