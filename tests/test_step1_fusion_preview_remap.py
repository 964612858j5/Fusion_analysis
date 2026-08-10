"""Step1 fusion PREVIEW reflects the Step0 manual remap without going black.

Root cause of the black preview: the preview cache used to hold percentile-
normalized [0,1] data, but apply_channel_remap's Min/Max are RAW intensity units
— so dividing [0,1] by e.g. 167 collapsed everything to ~0 (black). Fix: Step1
loads RAW (normalize=False); a remapped channel uses apply_channel_remap(raw),
others use the EXACT loader percentile norm (unchanged appearance).
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_preview_channel_signal_remap_and_percentile(app):
    from block01.ui.main_window import MainWindow
    from block01.core.io_loader import OMETIFFLoader
    from block01.core.channel_remap import apply_channel_remap

    w = MainWindow()
    try:
        class _L:
            _norm = staticmethod(OMETIFFLoader._norm)
        w.loader = _L()

        raw = (np.random.default_rng(0).random((64, 64)) * 255).astype(np.float32)
        params = {"min": 0.0, "max": 167.0, "gamma": 1.0}
        remap = {"CD11c": params}

        # remapped channel == apply_channel_remap(raw, params)
        got = w._preview_channel_signal("CD11c", raw, remap)
        assert np.allclose(got, apply_channel_remap(raw, params).astype(np.float32))
        assert float(got.max()) > 0.3          # has visible signal (not black)

        # non-remapped channel == the EXACT loader percentile norm (unchanged look)
        other = w._preview_channel_signal("X", raw, remap)
        assert np.allclose(other, OMETIFFLoader._norm(raw))

        # documents the bug the fix removes: feeding NORMALIZED [0,1] into the
        # raw-unit remap window collapses to ~0 (the black screen)
        buggy = apply_channel_remap(OMETIFFLoader._norm(raw), params)
        assert float(buggy.max()) < 0.05
    finally:
        w.close()


def test_fuse_fullres_applies_remap_for_patch_segmentation():
    """Patch-level Cellpose fusion input (FusionEngine.fuse_fullres) applies the
    Step0 manual remap on RAW channels; non-remapped channels use the loader
    percentile norm. This is the fix for Patch Results using un-adjusted channels."""
    from block01.core.fusion_engine import FusionEngine
    from block01.core.io_loader import OMETIFFLoader
    from block01.core.channel_remap import apply_channel_remap

    rng = np.random.default_rng(0)
    cd8 = (rng.random((64, 64)) * 255).astype(np.float32)
    dapi = (rng.random((64, 64)) * 255).astype(np.float32)

    class _L:
        ch_map = {"CD8": 0, "DAPI": 1}
        _norm = staticmethod(OMETIFFLoader._norm)
        def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
            return {"CD8": cd8, "DAPI": dapi}[ch]

    fe = FusionEngine()
    params = {"min": 0.0, "max": 167.0, "gamma": 1.0}
    fused = fe.fuse_fullres(
        _L(), 0, 64, 0, 64, {"g": {"CD8": 1.0}}, {"g": 1.0}, "DAPI", 1.0,
        channel_remap_params={"CD8": params})
    cyto = fused[:, :, 0].astype(np.float32) / 65535.0
    nuc = fused[:, :, 1].astype(np.float32) / 65535.0
    # cyto (single channel, weight 1) == the manual remap of raw CD8
    exp_cyto = np.clip(apply_channel_remap(cd8, params).astype(np.float32), 0, 1)
    assert np.allclose(cyto, exp_cyto, atol=2 / 65535)
    # nucleus (no remap) == the loader percentile norm of raw DAPI
    exp_nuc = np.clip(OMETIFFLoader._norm(dapi), 0, 1)
    assert np.allclose(nuc, exp_nuc, atol=2 / 65535)


def test_preview_loader_thread_normalize_flag():
    """PreviewLoaderThread keeps normalize=True by default (other callers), and
    forwards the flag to read_region."""
    from block01.workers.cellpose_worker import PreviewLoaderThread

    seen = {}

    class _Loader:
        ch_map = {"A": 0}
        def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
            seen["normalize"] = normalize
            return np.ones((y1 - y0, x1 - x0), dtype=np.float32)

    t = PreviewLoaderThread(0, _Loader(), ["A"], 0, 4, 0, 4, downsample=1,
                            normalize=False)
    assert t.normalize is False
    t.run()                                     # synchronous for the test
    assert seen.get("normalize") is False

    t2 = PreviewLoaderThread(0, _Loader(), ["A"], 0, 4, 0, 4, downsample=1)
    assert t2.normalize is True                 # default unchanged for other hosts
