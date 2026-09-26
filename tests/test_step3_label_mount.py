"""Block 4b: the mount's mask switch (`Step1WholeSlideMount(labels=...)`).

The rig of `test_step1_gpu_overview_skip.py` -- the REAL mount over a real
ExploreView / ExploreController / scheduler on a synthetic pyramid:

  * no GPU (CPU picture): no label binding, `mask_status()` says masks need
    the GPU display, setting masks draws nothing;
  * with the GPU: `labels=True` builds the layer WITH labels and a label
    binding on the stack's own controller and levels; a mask set on it is
    read and drawn; pause / resume reach it; a source change drops the
    masks (their owner sets them again); close ends its thread;
  * `labels=False` (Step1): the layer has no label targets, no binding.

GPU gates skip without a GPU; `BLOCK01_REQUIRE_STEP1_GPU=1` makes them fail.
Synthetic data in the test's temporary directory only.
"""

import importlib.util
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core import step3_masks as sm  # noqa: E402
from block01.core import label_pyramid as lp  # noqa: E402
from block01.ui.step1_gpu_layer import Step1GpuLayer  # noqa: E402
from block01.ui.step1_viewer_host import Step1ViewerHost  # noqa: E402
from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_CPU_FALLBACK, BACKEND_GPU, DEMO_GPU_RAW_TEXTURE_BYTES, Step1WholeSlideMount)

_spec = importlib.util.spec_from_file_location(
    "_overview_rig", os.path.join(os.path.dirname(__file__), "test_step1_gpu_overview_skip.py"))
rig_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rig_mod)

REQUIRE_GPU = os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") == "1"
ROI = rig_mod.ROI                       # (256, 1280, 256, 1280): y0, y1, x0, x1


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _mount(app, *, labels, gpu=True):
    reads, stacks = [], []
    window = rig_mod._Window(rig_mod._state(), rig_mod._domain())
    host = Step1ViewerHost(stack_factory=rig_mod._stack_factory(reads, stacks))
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(512, 512)
    host.show()
    app.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=gpu, labels=labels,
                                 camera_reason="step3")
    mount.open("CD3")
    rig_mod._settle(app)
    return mount


def _require_gpu(mount):
    if mount.backend == BACKEND_GPU:
        return
    reason = mount.gpu_status()["reason"]
    mount.close()
    if REQUIRE_GPU:
        pytest.fail(f"required GPU backend unavailable: {reason}")
    pytest.skip(f"GPU backend unavailable: {reason}")


def _wait(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _source(tmp_path, mount):
    """A cell mask over the rig's ROI, no pyramid yet (the binding builds it)."""
    import zarr
    y0, y1, x0, x1 = ROI
    ids = np.zeros((y1 - y0, x1 - x0), np.uint32)
    ids[100:300, 100:400] = 2 ** 24 + 5
    ids[500:700, 200:260] = 7
    path = str(tmp_path / "global_mask_ROI_1.zarr")
    z = zarr.open(path, mode="w", shape=ids.shape, chunks=(256, 256), dtype="<u4")
    z[:] = ids
    return {"cell": sm.MaskSource(kind="cell", mask_path=os.path.realpath(path), bbox=ROI,
                                  pyramid_path=lp.pyramid_path_for(os.path.realpath(path)),
                                  pyramid_kind="cell", pyramid=None,
                                  pyramid_reason="no label pyramid"),
            "nucleus": None}


def test_without_a_gpu_masks_are_not_drawn_and_the_status_says_why(app, tmp_path, capsys):
    mount = _mount(app, labels=True, gpu=False)
    try:
        assert mount.backend == BACKEND_CPU_FALLBACK
        assert mount.label_binding is None
        status = mount.mask_status()
        assert status["available"] is False and "Masks need the GPU display" in status["reason"]
        assert mount.set_mask_sources(_source(tmp_path, mount)) is False
        assert "masks not drawn" in capsys.readouterr().out
    finally:
        mount.close()


def test_step1s_viewer_draws_no_masks(app):
    mount = _mount(app, labels=False)
    try:
        assert mount.label_binding is None
        assert mount.mask_status()["available"] is False
        if mount.backend == BACKEND_GPU:
            assert mount.gpu_layer.labels_enabled is False
    finally:
        mount.close()


def test_step3s_viewer_draws_masks_and_follows_its_lifecycle(app, tmp_path):
    mount = _mount(app, labels=True)
    _require_gpu(mount)
    try:
        layer, binding = mount.gpu_layer, mount.label_binding
        assert layer.labels_enabled and binding is not None
        provider = mount.host.stack.provider
        assert binding.level_shapes == [tuple(provider.level_shape(l))
                                        for l in range(provider.num_levels)]
        assert binding.tile_size == mount.host.stack.controller.grid.tile_size
        mount.set_mask_sources(_source(tmp_path, mount))
        assert _wait(app, lambda: layer.label_stats()["shown"])
        assert mount.mask_status()["available"] is True
        # pause / resume reach the label binding
        mount.pause_requests()
        assert binding.paused
        mount.resume_requests()
        assert not binding.paused
        # a source change drops the masks; a new binding starts without them
        mount.source_changed("test")
        rig_mod._settle(app)
        assert mount.label_binding is not binding
        assert binding.stats()["thread_alive"] is False
        if mount.label_binding is not None:
            assert mount.label_binding.mask_status()["cell"]["source"] is False
        # close ends the thread
        last = mount.label_binding
        mount.close()
        assert mount.label_binding is None
        if last is not None:
            assert last.stats()["thread_alive"] is False
    finally:
        mount.close()
