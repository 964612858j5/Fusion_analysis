"""The drawn ROI reaches the GPU, and the picture stops at it.

G3.2c follow-up. The first round's gates all drove `Step1GpuLayer`
directly, so they could not have caught a region that never left Step0 --
and the user's real machine still showed pixels outside the rectangle.
These gates walk the PRODUCT path instead:

    Step0 writes a handoff with a drawn ROI
        -> MainWindow._load_step0_roi_result (the one authoritative reader)
        -> MainWindow._active_roi
        -> Step1ViewerBinding.roi_bbox()
        -> the live stack's Step1SourceTable.roi_bbox()
        -> ViewportSnapshot.roi_world_rect on every GPU submission
        -> the framebuffer, AND the composited GL widget

Nothing here is asserted on an empty set: the region is checked to be a
strict sub-rectangle of the slide, and the picture is checked to have
opaque pixels inside it before anything is claimed about the outside.

BLOCK01_REQUIRE_STEP1_GPU=1; a software renderer is a failure, not a skip.
"""

import importlib.util
import json
import os
import pathlib
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("G1 GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import OpenGL  # noqa: F401
    import tifffile
    import zarr
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

from block01.ui.step1_draft_spec import STEP1_SCOPE            # noqa: E402
from block01.ui.step1_gpu_layer import Step1GpuLayer           # noqa: E402
from block01.ui.step1_viewer_mount import BACKEND_GPU          # noqa: E402

SLIDE_H, SLIDE_W = 2048, 1536
#: A strict sub-rectangle on all four sides, and on no block boundary.
ROI_BBOX = [300, 1500, 250, 1100]          # y0, y1, x0, x1
CHANNELS = ("DAPI", "CD3")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _slide(path):
    rng = np.random.default_rng(5)
    data = (rng.random((len(CHANNELS), SLIDE_H, SLIDE_W)) * 500 + 200).astype(np.uint16)
    with tifffile.TiffWriter(str(path)) as writer:
        writer.write(data, subifds=2, tile=(256, 256), photometric="minisblack")
        reduced = data
        for _ in range(2):
            reduced = reduced[:, ::4, ::4].copy()
            writer.write(reduced, subfiletype=1, tile=(256, 256),
                         photometric="minisblack")
    return data


def _corrected(store_path, data):
    """A corrected product for the ROI, as Step0 would leave it."""
    y0, y1, x0, x1 = ROI_BBOX
    root = zarr.open_group(str(store_path), mode="w")
    group = root.create_group("ROI_1")
    group.attrs["roi_name"] = "ROI_1"
    group.attrs["bbox_fullres"] = list(ROI_BBOX)
    for index, channel in enumerate(CHANNELS):
        values = data[index, y0:y1, x0:x1].astype(np.float32)
        array = group.create_dataset(channel, shape=values.shape,
                                     chunks=(512, 512), dtype=np.float32,
                                     overwrite=True)
        array[:, :] = values
        array.attrs["roi_bbox_fullres"] = list(ROI_BBOX)
        array.attrs["channel_name"] = channel
        array.attrs["correction_method"] = "cucim"
        array.attrs["source_identity"] = f"test-write-{index}"
    return root


class _Loader:
    """What the window and the mount read from a loader."""

    def __init__(self, path, data):
        self.filepath = str(path)
        self.shape = (SLIDE_H, SLIDE_W)
        self._data = data
        self.ch_map = {name: i for i, name in enumerate(CHANNELS)}

    def channel_names(self):
        return list(CHANNELS)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1):
        index = self.ch_map[channel]
        step = max(1, int(downsample))
        return self._data[index, y0:y1:step, x0:x1:step].astype(np.float32)

    def set_correction_config(self, *_a, **_kw):
        return None

    def set_corrected_zarr_store(self, *_a, **_kw):
        return None


@pytest.fixture
def product(app, tmp_path):
    """A real MainWindow that has accepted a real handoff with a drawn ROI."""
    from block01.ui.main_window import MainWindow

    slide = tmp_path / "slide.ome.tif"
    data = _slide(slide)
    step0 = tmp_path / "roi1" / "step0"
    step0.mkdir(parents=True)
    _corrected(step0 / "corrected_channels.zarr", data)

    # A CONCAVE drawn shape whose bounding box is exactly ROI_BBOX, the way
    # Step0 writes the pair. `(x, y)` points, as `polygon_fullres` stores them.
    y0, y1, x0, x1 = ROI_BBOX
    mid_y = (y0 + y1) // 2
    roi = {"name": "ROI_1", "display_name": "ROI_1",
           "bbox_fullres": list(ROI_BBOX),
           "polygon_fullres": [[x0, y0], [x1, y0], [x1, mid_y - 120],
                               [x0 + 180, mid_y], [x1, mid_y + 120],
                               [x1, y1], [x0, y1]],
           "shape": [y1 - y0, x1 - x0]}
    (step0 / "roi_config.json").write_text(json.dumps([roi]))
    (step0 / "patch_config.json").write_text("[]")
    (step0 / "correction_config.json").write_text(json.dumps(
        {"channel_decisions": {c: "cucim" for c in CHANNELS},
         "method_params": {"tophat_radius": 25, "cucim_sigma": 30}}))
    manifest = {
        "handoff_schema_version": 2, "mode": "roi_only",
        "active_roi": "ROI_1", "display_name": "ROI_1",
        "bbox_fullres": list(ROI_BBOX), "analysis_region_type": "roi",
        "raw_ome_path": str(slide), "output_dir": str(step0),
        "corrected_zarr_path": str(step0 / "corrected_channels.zarr"),
        "corrected_zarr_valid": True, "corrected_zarr_n_channel_arrays": len(CHANNELS),
        "correction_config_path": str(step0 / "correction_config.json"),
        "roi_config_path": str(step0 / "roi_config.json"),
        "patch_config_path": str(step0 / "patch_config.json"),
        "corrected_decisions": {c: "cucim" for c in CHANNELS},
        "nucleus_channel": "DAPI", "n_rois": 1, "n_patches": 0,
        "created_from_step": "step0", "geometry_revision": 1,
    }
    # The reader also insists on an authoritative remap declaration: both a
    # readable file and its semantic hash, written with the product's own
    # helpers so the hash is the one it will recompute.
    from block01.utils.channel_remap_config import (
        channel_remap_config_hash, default_channel_remap_config,
        save_channel_remap_config)
    remap = default_channel_remap_config(list(CHANNELS))
    remap_path = step0 / "step0_channel_remap.json"
    save_channel_remap_config(remap, str(remap_path))
    manifest["channel_remap_config_path"] = str(remap_path)
    manifest["channel_remap_config_hash"] = channel_remap_config_hash(remap)
    # A v2 handoff is refused without this: the reader checks that the
    # manifest's raw path and the identity's path agree and that the file's
    # size:mtime still match. Written the way Step0 writes it.
    stat = slide.stat()
    manifest["source_identity"] = {
        "dataset_path": str(slide),
        "dataset_fingerprint": f"{stat.st_size}:{stat.st_mtime_ns}",
        "stage": "raw", "corrected_artifact": None,
    }
    (step0 / "step0_roi_result.json").write_text(json.dumps(manifest))

    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    window.loader = _Loader(slide, data)
    window.step0_output = {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(step0 / "step0_roi_result.json"),
        "output_dir": str(step0), "step1_dir": str(tmp_path / "roi1" / "step1"),
    }
    accepted = window._load_step0_roi_result(auto=True)
    assert accepted is True, "the synthetic handoff was refused"
    window.step0_done = True
    window._step1_context_ready = True

    submissions = []
    real_submit = Step1GpuLayer.submit

    def submit(self, source, display, viewport):
        stats = real_submit(self, source, display, viewport)
        submissions.append({"roi_world_rect": viewport.roi_world_rect,
                            "world_rect": viewport.world_rect,
                            "roi_scissor": stats.get("roi_scissor"),
                            "channels": [c.channel for c in source.channels]})
        return stats

    Step1GpuLayer.submit = submit
    try:
        window._stack.setCurrentWidget(window._step1_page_widget)
        app.processEvents()
        window._set_step_active(1)          # THIS is what builds the mount
        app.processEvents()
        window.right_tabs.setCurrentWidget(window.viewer_tab)
        for _ in range(30):
            app.processEvents()
        yield window, submissions
    finally:
        Step1GpuLayer.submit = real_submit
        window.close()


def _tick(window, channel, app, mode=None):
    """The product's tick: show the channel AND put it in the fusion.

    The draft is bound to THIS slide first, through the window's own
    `_bind_fusion_dataset`. A MainWindow starts bound to the configured
    demo slide, and a weight written while the draft still points there
    lands under another identity: the display snapshot then carries no
    weight at all, the GPU's active set is empty and the framebuffer is
    black -- measured, and mistaken for "clipped" until the snapshot was
    printed.
    """
    window._bind_fusion_dataset(str(window.loader.filepath),
                                reason="g32c-test")
    # ...and give THIS dataset its groups, the way the handoff does for a
    # slide it has never seen (`config.load_panel` inside
    # `_load_step0_roi_result`). Without them the draft has nowhere to store
    # a weight: `stored_weights` stays empty, the snapshot carries no
    # weights, the GPU's active set is empty and the framebuffer is black.
    if not window._display.fusion.is_initialized():
        window.config.set_channels(list(CHANNELS))
        window.config.load_panel({"markers": list(CHANNELS)}, "DAPI")
        window.config.set_nucleus("DAPI", 1.0)
    domain = window._display.fusion
    domain.set_fusion_enabled(channel, True, origin="g32c-test")
    domain.edit_channel_weight(channel, 1.0, origin="g32c-test")
    with window._display.state.using_scope(STEP1_SCOPE):
        window._display.state.set_display_visible(channel, True)
        window._display.state.set_mapping(channel, 0.0, 700.0, 1.0)
    if mode is not None:
        window._step1_mount.set_mode(mode)
    _settle_until_drawn(window, channel, app)


def _settle_until_drawn(window, channel, app, timeout=30.0):
    """Wait for the channel to be COMPLETE, not for a fixed event count.

    Tiles are read on worker threads: spinning `processEvents()` a few
    hundred times takes milliseconds and proves nothing (an earlier version
    of these gates did exactly that and measured an empty framebuffer).
    """
    import time

    binding = window._step1_mount.gpu_binding
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        stats = binding.stats()
        if (channel in stats["coarse_channels"]
                and binding._viewport_fine_ready(channel)):
            break
        time.sleep(0.01)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)


#: OFFSCREEN, `grabFramebuffer()` IS NOT AN ORACLE. Inside a full
#: MainWindow on the offscreen platform it sometimes returns uninitialised
#: GPU memory: measured here at 100 % of 496 958 pixels non-black on one
#: run and 60 % on the next, while the framebuffer itself held exactly the
#: 64 032 pixels of the region both times. A threshold that tried to tell
#: garbage from a frame was fragile in exactly the same way, so these gates
#: assert on the FRAMEBUFFER, which is the layer's actual output and is
#: deterministic. The composited, what-the-user-sees evidence is taken on
#: the REAL X display by
#: `scripts/diagnose_step1_roi_clip_onscreen.py`, where `grabWindow()` is
#: reliable -- and that is what the report cites for the screen.
COMPOSITED_GRAB_IS_UNRELIABLE_OFFSCREEN = True


def _frames(mount):
    """`(fbo, shown, inside)` -- the layer's FBO, what Qt composites, and
    which pixels have their centre inside the region."""
    fbo = mount.gpu_layer.readback_rgba_for_test()
    image = mount.gpu_layer.grabFramebuffer()
    image = image.convertToFormat(image.Format_RGBA8888)
    pointer = image.constBits()
    pointer.setsize(image.byteCount())
    shown = np.frombuffer(pointer, np.uint8).reshape(
        (image.height(), image.bytesPerLine() // 4, 4))[:, :image.width(), :]
    viewport = mount._gpu_viewport_snapshot()
    height, width = fbo.shape[:2]
    wx0, wx1, wy0, wy1 = viewport.world_rect
    bx0, bx1, by0, by1 = viewport.roi_world_rect
    xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
    ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
    inside = ((ys[:, None] >= by0) & (ys[:, None] < by1)
              & (xs[None, :] >= bx0) & (xs[None, :] < bx1))
    return fbo, shown, inside, viewport


# ── the region reaches the GPU at all ────────────────────────────────

def test_the_drawn_polygon_travels_from_the_handoff_to_the_gpu(product, app):
    """G3.2c.1: the SHAPE, not just its box, reaches the layer and clips."""
    window, submissions = product
    _tick(window, "DAPI", app)
    mount = window._step1_mount

    polygon = mount._gpu_roi_polygon(mount._gpu_roi_world_rect(
        mount.host.stack))
    assert polygon is not None and len(polygon) == 7
    drawn = window._active_roi["polygon_fullres"]
    assert [list(p) for p in polygon] == [[float(x), float(y)] for x, y in drawn]

    snapshot = mount._gpu_viewport_snapshot()
    assert snapshot.roi_polygon_world == polygon
    # The tick already submitted with this polygon; the gate reads THAT
    # frame. (An earlier version re-submitted a descriptor taken from
    # history to "make sure", which measured a frame the product never
    # drew.) The submission record is where the layer's answer is read.
    assert submissions, "no submission to inspect"
    fbo, shown, _inside_box, viewport = _frames(mount)
    assert mount.gpu_layer.roi_polygon_error() == ""
    inside = _inside_polygon(fbo.shape, viewport.world_rect,
                             viewport.roi_polygon_world)
    from scipy.ndimage import binary_dilation, binary_erosion
    strict_out = ~binary_dilation(inside, np.ones((3, 3), bool))
    strict_in = binary_erosion(inside, np.ones((3, 3), bool))
    assert int(np.count_nonzero((fbo[..., 3] > 0) & strict_in)) > 0
    assert int(np.count_nonzero((fbo[..., 3] > 0) & strict_out)) == 0
    del shown          # see COMPOSITED_GRAB_IS_UNRELIABLE_OFFSCREEN


def _inside_polygon(shape, world, polygon):
    from matplotlib.path import Path
    height, width = shape[:2]
    wx0, wx1, wy0, wy1 = world
    xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
    ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
    grid_x, grid_y = np.meshgrid(xs, ys)
    inside = Path(np.asarray(polygon, float)).contains_points(
        np.column_stack([grid_x.ravel(), grid_y.ravel()]))
    return inside.reshape(height, width)


def test_the_drawn_roi_travels_from_the_handoff_to_every_submission(product, app):
    window, submissions = product
    _tick(window, "DAPI", app)

    assert window._active_roi["bbox_fullres"] == ROI_BBOX
    assert window._step1_mount.viewer.roi_bbox() == tuple(ROI_BBOX)
    table = window._step1_mount.host.stack.provider.source_table
    assert table.roi_bbox() == tuple(ROI_BBOX)

    assert submissions, "the GPU was never asked to draw anything"
    expect = (float(ROI_BBOX[2]), float(ROI_BBOX[3]),
              float(ROI_BBOX[0]), float(ROI_BBOX[1]))
    assert all(s["roi_world_rect"] == expect for s in submissions), (
        f"a submission carried another region: "
        f"{ {s['roi_world_rect'] for s in submissions} }")

    # the region is a STRICT sub-rectangle of the slide, and its edges are
    # inside the camera -- otherwise there would be nothing to clip and the
    # gate would pass on an empty set
    assert 0 < ROI_BBOX[0] and ROI_BBOX[1] < SLIDE_H
    assert 0 < ROI_BBOX[2] and ROI_BBOX[3] < SLIDE_W
    world = submissions[-1]["world_rect"]
    assert world[0] < expect[0] and expect[1] < world[1]
    assert world[2] < expect[2] and expect[3] < world[3]
    scissor = submissions[-1]["roi_scissor"]
    assert scissor is not None and scissor[2] > 0 and scissor[3] > 0


def test_the_backend_under_test_is_the_gpu(product, app):
    window, _ = product
    assert window._step1_mount.backend == BACKEND_GPU, (
        f"not the GPU path: {window._step1_mount.gpu_status()}")
    report = window._step1_mount.gpu_layer.environment_report()
    assert report["software_renderer"] is False
    assert "NVIDIA" in report["gl_renderer"]


# ── and the picture stops there, in the FBO and on the widget ────────

@pytest.mark.parametrize("channel", ["DAPI", "CD3"])
def test_a_channel_is_drawn_inside_the_region_and_nowhere_else(product, app,
                                                               channel):
    window, _ = product
    _tick(window, channel, app)
    fbo, shown, inside, _viewport = _frames(window._step1_mount)

    assert int(np.count_nonzero((fbo[..., 3] > 0) & inside)) > 0, (
        f"{channel} drew nothing inside the region, so nothing is proven")
    assert int(np.count_nonzero((fbo[..., 3] > 0) & ~inside)) == 0
    # (the composited grab is not asserted offscreen -- see above)


def test_overlay_and_fusion_both_stop_at_the_region(product, app):
    from block01.ui.step1_draft_spec import MODE_FUSION, MODE_OVERLAY

    window, _ = product
    for channel in CHANNELS:
        _tick(window, channel, app)
    for mode in (MODE_OVERLAY, MODE_FUSION):
        window._step1_mount.set_mode(mode)
        _settle_until_drawn(window, CHANNELS[0], app)
        fbo, shown, inside, _v = _frames(window._step1_mount)
        assert int(np.count_nonzero((fbo[..., 3] > 0) & inside)) > 0, mode
        assert int(np.count_nonzero((fbo[..., 3] > 0) & ~inside)) == 0, mode
        # (the composited grab is not asserted offscreen -- see above)


@pytest.mark.parametrize("factor,what", [(0.25, "zoomed out"),
                                         (2.0, "zoomed in")])
def test_the_region_holds_after_a_pan_and_a_zoom(product, app, factor, what):
    window, _ = product
    _tick(window, "DAPI", app)
    mount = window._step1_mount
    camera = mount.current_camera()
    assert camera is not None
    cx, cy, scale = camera
    mount.apply_camera(cx + 120.0, cy - 80.0, scale * factor)
    _settle_until_drawn(window, "DAPI", app)
    fbo, shown, inside, _v = _frames(mount)
    assert int(np.count_nonzero((fbo[..., 3] > 0) & inside)) > 0, what
    assert int(np.count_nonzero((fbo[..., 3] > 0) & ~inside)) == 0, what
    # (the composited grab is not asserted offscreen -- see above), what
