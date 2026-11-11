"""Step0's background correction on the Mac's GPU
(docs/v17_step0_mac_gpu_correction_plan.md §5, items 1-5).

Synthetic data only. The device tests skip where PyTorch's MPS is absent;
the backend-choice and memory tests run everywhere.
"""

import os

import numpy as np
import pytest

from block01.core import bg_correction as bc
from block01.core import bg_parallel as bp
from block01.core import mps_kernels as M


def _mps_present():
    try:
        import torch
        return torch.backends.mps.is_available()
    except Exception:                                   # noqa: BLE001
        return False


needs_mps = pytest.mark.skipif(not _mps_present(), reason="no Apple MPS here")


@pytest.fixture
def mps(monkeypatch):
    monkeypatch.delenv(M.ENV_SWITCH, raising=False)
    # the device state goes back to what it was, so no other test in this
    # process meets a GPU it did not ask for
    monkeypatch.setattr(M, "_state", M._state)
    if not M.ensure_ready():
        # MPS is here (the tests are marked), so a failing smoke test is a
        # failure, not a skip
        pytest.fail(f"MPS smoke test did not pass: {M.error()}")
    monkeypatch.setattr(bc, "GPU_MORPH_AVAILABLE", False)
    return M


def _bits(a):
    return np.ascontiguousarray(a, np.float32).view(np.uint32)


def _data(shape, kind, seed=0):
    rng = np.random.default_rng(seed)
    if kind == "int":
        return (rng.integers(0, 4096, shape)).astype(np.float32)
    return (rng.random(shape) * 255).astype(np.float32)


def _device_tophat(a, r, morph):
    t = M._torch.from_numpy(np.ascontiguousarray(a)).to(M._device)
    return (t - morph(-morph(-t, r), r)).cpu().numpy()


# ── 1. TopHat: the CPU's bits ─────────────────────────────────────────────

@needs_mps
@pytest.mark.parametrize("r", [1, 2, 3, 15, 28, 29, 50, 150])
@pytest.mark.parametrize("kind", ["int", "float"])
def test_tophat_is_the_cpus_result_bit_for_bit(mps, r, kind):
    shape = (max(311, 2 * r + 7), max(517, 2 * r + 31))          # non-square
    a = _data(shape, kind, seed=r)
    want = bc._tophat_cpu(a, r)
    assert np.array_equal(_bits(M.tophat(a, r)), _bits(want))
    # both exact decompositions, whichever the radius picks
    if r <= 50:
        assert np.array_equal(_bits(_device_tophat(a, r, M._max_disk_rect)), _bits(want))
    assert np.array_equal(_bits(_device_tophat(a, r, M._max_disk_rows)), _bits(want))
    # and through the per-call entry the preview uses
    assert np.array_equal(_bits(bc._apply_tophat_gpu_or_cpu(a, r)), _bits(want))


@needs_mps
def test_tophat_at_the_kernels_size_and_below_it(mps):
    r = 15
    at = _data((2 * r + 1, 40), "int")
    assert M.tophat_takes(at, r)
    assert np.array_equal(_bits(M.tophat(at, r)), _bits(bc._tophat_cpu(at, r)))
    below = _data((2 * r, 40), "int")
    assert not M.tophat_takes(below, r)                   # the CPU's (skimage)
    assert np.array_equal(_bits(bc._apply_tophat_gpu_or_cpu(below, r)),
                          _bits(bc._tophat_cpu(below, r)))


@needs_mps
def test_tophat_with_non_finite_pixels_goes_to_the_cpu(mps):
    a = _data((200, 200), "float")
    a[5, 7], a[100, 100] = np.nan, np.inf
    assert not M.tophat_takes(a, 3)
    got = bc._apply_tophat_gpu_or_cpu(a, 3)
    want = bc._tophat_cpu(a, 3)
    assert np.array_equal(np.isnan(got), np.isnan(want))
    assert np.array_equal(got[np.isfinite(want)], want[np.isfinite(want)])


@needs_mps
def test_tophat_equals_skimage_when_opencv_is_missing(mps, monkeypatch):
    a = _data((180, 260), "float")
    monkeypatch.setattr(bc, "_cv2", None)
    assert np.array_equal(_bits(M.tophat(a, 7)), _bits(bc._tophat_cpu(a, 7)))


# ── 2. the Gaussian: within float rounding of OpenCV ─────────────────────

@needs_mps
@pytest.mark.parametrize("sigma,shape", [(1, (300, 400)), (20, (600, 500)), (50, (800, 900)),
                                         (200, (1100, 1000)), (50, (100, 5000)), (50, (7, 7))])
def test_gaussian_is_within_rounding_of_opencv(mps, sigma, shape):
    a = _data(shape, "float", seed=sigma)
    got = M.gaussian_bg_subtract(a, sigma)
    assert got.shape == a.shape
    assert np.abs(got - bc._cucim_cpu_opencv(a, sigma)).max() <= 1e-3
    assert np.abs(got - bc._cucim_cpu_scipy(a, sigma)).max() <= 1e-3


@needs_mps
def test_gaussian_tiled_equals_untiled_on_the_device(mps):
    a = _data((900, 700), "float")
    whole = M.gaussian_bg_subtract(a, 20)
    tiled = bc._apply_background_method_tiled(a, "cucim", sigma=20, tile_size=256)
    assert np.abs(tiled - whole).max() <= 1e-4


def test_the_reflect_index_is_scipys_for_any_size():
    """scipy's 'reflect' is numpy's 'symmetric' (a b c | c b a), repeated
    when the pad is wider than the array."""
    for n, pad in ((5, 2), (5, 13), (1, 4), (7, 50), (3, 3)):
        x = np.arange(n, dtype=np.float64)
        assert np.array_equal(x[M.reflect_index(n, pad)], np.pad(x, pad, mode="symmetric"))


# ── 3. Save on "mps" ───────────────────────────────────────────────────────

class _Loader:
    def __init__(self, nan_at=None):
        self.shape = (5000, 300)
        self.ch_map = {"DAPI": 0, "CD3": 1}
        self.filepath = "/fake/slide.ome.tif"
        rng = np.random.default_rng(3)
        self.img = (rng.random(self.shape) * 255).astype(np.float32)
        if nan_at is not None:
            self.img[nan_at] = np.nan

    def _read_roi_zarr(self, page_idx, y0, y1, x0, x1):
        return self.img[y0:y1, x0:x1].copy()


def _config(method):
    return {"channel_decisions": {"CD3": method},
            "method_params": {"tophat_radius": 9, "cucim_sigma": 12},
            "channel_params": {}}


def _save(tmp_path, loader, method):
    from block01.core import product_swap
    from block01.ui.step0 import search_ctrl as sc
    zarr = pytest.importorskip("zarr")
    roi = {"name": "ROI_1", "bbox_fullres": [0, 5000, 0, 300],
           "polygon_fullres": None, "shape": [5000, 300]}
    worker = sc.WsiCorrectionWorker(loader, str(tmp_path), _config(method), rois=[roi])
    got = {"done": [], "error": []}
    worker.job_finished.connect(lambda p, d: got["done"].append(p))
    worker.error.connect(lambda m: got["error"].append(m))
    worker.run()
    assert got["done"] and not got["error"], got
    product_swap.apply_plan(worker.output_dir, worker.swap_plan)
    product_swap.commit(worker.output_dir)
    ds = zarr.open(os.path.join(str(tmp_path), "corrected_channels.zarr"), mode="r")["ROI_1"]["CD3"]
    return np.asarray(ds[:]), dict(ds.attrs)


@needs_mps
def test_a_tophat_save_on_the_mac_gpu_is_the_cpus_and_says_mps(mps, tmp_path):
    from block01.ui.step0 import search_ctrl as sc
    assert sc.correction_tile_size("tophat", None, "mps") == 2048       # 3 tiles here
    loader = _Loader()
    pixels, attrs = _save(tmp_path, loader, "tophat")
    assert (attrs["bg_compute_path"], attrs["tophat_footprint"], attrs["bg_compute_impl"]) == \
        ("mps", "disk", None)
    assert np.array_equal(_bits(pixels), _bits(bc._tophat_cpu(loader.img, 9)))   # seams too


@needs_mps
def test_a_gaussian_save_on_the_mac_gpu_records_its_implementation(mps, tmp_path):
    loader = _Loader()
    pixels, attrs = _save(tmp_path, loader, "cucim")
    assert (attrs["bg_compute_path"], attrs["bg_compute_impl"]) == ("mps", bc.GAUSSIAN_IMPL_MPS)
    assert np.abs(pixels - bc._cucim_cpu_scipy(loader.img, 12)).max() <= 1e-3


@needs_mps
def test_a_non_finite_gaussian_tile_restarts_the_channel_on_the_cpu_and_keeps_mps(mps, tmp_path):
    loader = _Loader(nan_at=(4000, 10))                  # in the last tile
    _pixels, attrs = _save(tmp_path, loader, "cucim")
    assert attrs["bg_compute_path"] == "cpu"
    assert attrs["bg_compute_impl"] in (bc.GAUSSIAN_IMPL_OPENCV, bc.GAUSSIAN_IMPL_SCIPY)
    assert M.ready()                                      # not switched off


@needs_mps
def test_a_device_failure_mid_channel_recomputes_on_the_cpu(mps, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = M.tophat

    def flaky(a, r):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("MPS out of memory (test)")
        return real(a, r)
    monkeypatch.setattr(M, "tophat", flaky)
    monkeypatch.setattr(M, "_state", "ready")             # restored after the test
    loader = _Loader()
    pixels, attrs = _save(tmp_path, loader, "tophat")
    assert attrs["bg_compute_path"] == "cpu" and attrs["tophat_footprint"] == "disk"
    assert np.array_equal(_bits(pixels), _bits(bc._tophat_cpu(loader.img, 9)))
    assert M.state() == "off"                             # for the rest of the session


# ── 4. which backend ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cuda,mps_state,cucim,want_t,want_c", [
    (True, "ready", True, "gpu", "gpu"),
    (True, "ready", False, "gpu", "cpu"),
    (False, "ready", False, "mps", "mps"),
    (False, "unknown", False, "cpu", "cpu"),
    (False, "off", False, "cpu", "cpu"),
])
def test_the_backend_rule(monkeypatch, cuda, mps_state, cucim, want_t, want_c):
    monkeypatch.delenv(M.ENV_SWITCH, raising=False)
    monkeypatch.setattr(bc, "GPU_MORPH_AVAILABLE", cuda)
    monkeypatch.setattr(bc, "CUCIM_AVAILABLE", cucim)
    monkeypatch.setattr(M, "_state", mps_state)
    assert bc.compute_path("tophat") == want_t
    assert bc.compute_path("cucim") == want_c
    assert bc.tophat_footprint("tophat", want_t) == ("square" if want_t == "gpu" else "disk")
    if want_c == "mps":
        assert bc.compute_impl("cucim", "mps") == bc.GAUSSIAN_IMPL_MPS


@needs_mps
def test_a_call_waiting_while_the_device_is_switched_off_is_refused(mps):
    """A failure elsewhere switches the device off; calls queued behind it
    get the CPU (preview) or restart the channel (Save), not the device."""
    import threading
    a = _data((64, 64), "int")
    entered, release = threading.Event(), threading.Event()
    got = {}

    def waiter():
        entered.set()
        try:
            M.tophat(a, 2)
        except M.MpsOff as exc:
            got["refused"] = exc
    with M._LOCK:                                        # a call in progress
        t = threading.Thread(target=waiter)
        t.start()
        entered.wait(2)
        M.disable(RuntimeError("device lost (test)"), "tophat")
    t.join(5)
    assert isinstance(got.get("refused"), M.MpsOff)
    # the preview's entry answers on the CPU, the Save's raises for its retry
    assert np.array_equal(_bits(bc._apply_tophat_gpu_or_cpu(a, 2)), _bits(bc._tophat_cpu(a, 2)))
    with pytest.raises(bc.GpuBackendFailed):
        bc.correct_tile(a, "tophat", 2, "mps")


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setattr(bc, "GPU_MORPH_AVAILABLE", False)
    monkeypatch.setattr(M, "_state", "ready")
    monkeypatch.setenv(M.ENV_SWITCH, "0")
    assert M.state() == "off" and bc.compute_path("tophat") == "cpu"


def test_asking_for_the_backend_never_waits_or_imports(monkeypatch):
    """`compute_path` reads a flag: it does not run the smoke test, nor wait
    for the device while a call holds it."""
    monkeypatch.setattr(bc, "GPU_MORPH_AVAILABLE", False)
    monkeypatch.setattr(M, "_state", "unknown")
    monkeypatch.setattr(M, "ensure_ready", lambda: pytest.fail("ran the smoke test"))
    with M._LOCK:                                        # a device call in progress
        assert bc.compute_path("tophat") == "cpu"
        monkeypatch.setattr(M, "_state", "ready")
        assert bc.compute_path("tophat") == "mps"


def test_tile_sizes_follow_the_path():
    from block01.ui.step0 import search_ctrl as sc
    assert sc.correction_tile_size("tophat", None, "mps") == 2048
    assert sc.correction_tile_size("cucim", bc.GAUSSIAN_IMPL_MPS, "mps") == 2048
    assert sc.correction_tile_size("tophat", None, "cpu") == 4096
    assert sc.correction_tile_size("cucim", bc.GAUSSIAN_IMPL_OPENCV, "cpu") == 1024
    assert sc.correction_tile_size("tophat", None, "gpu") == 4096


def test_no_gpu_wanted_stays_on_the_cpu(monkeypatch):
    """`prefer_gpu=False` (callers passing the CUDA-only flag) never reaches
    the Mac's GPU."""
    monkeypatch.setattr(bc, "GPU_MORPH_AVAILABLE", False)
    monkeypatch.setattr(M, "_state", "ready")
    monkeypatch.setattr(M, "gaussian_bg_subtract", lambda *a: pytest.fail("used the GPU"))
    a = _data((50, 60), "float")
    assert np.array_equal(bc._apply_cucim_or_cpu(a, 3, prefer_gpu=False), bc._cucim_cpu(a, 3))


# ── 5. free memory on a Mac ────────────────────────────────────────────────

def test_free_memory_falls_back_to_psutil(monkeypatch):
    import builtins
    psutil = pytest.importorskip("psutil")
    real_open = builtins.open

    def no_meminfo(path, *a, **k):
        if path == "/proc/meminfo":
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", no_meminfo)
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: type("VM", (), {"available": 11 << 30})())
    assert bp.mem_available_bytes() == 11 << 30
    # the CPU Save on a 12-core Mac: cpu // 2, not the unknown-memory cap of 4
    assert bp.choose_workers(12, 16, 11 << 30, bp.MEM_PER_TILE_FAST_BYTES) == 6
    monkeypatch.setattr(psutil, "virtual_memory", lambda: (_ for _ in ()).throw(OSError()))
    assert bp.mem_available_bytes() is None


def test_the_fusion_workers_follow_the_memory_rule_after_their_reserve(monkeypatch):
    """The second user of `mem_available_bytes` (Step0's whole-slide fusion):
    on a Mac it now follows the memory rule after its reserve, as on Linux."""
    from block01.ui.step0.overview_panel import FullFusionWorker
    w = FullFusionWorker.__new__(FullFusionWorker)
    w.zarr_chunk = 1024
    reserve = w._writer_bytes(8)
    monkeypatch.setattr(bp, "mem_available_bytes", lambda: 11 << 30)
    n = w._unit_workers(100, 6, 50 << 20, reserve_bytes=reserve)
    want = bp.choose_workers(os.cpu_count(), 100, (11 << 30) - reserve, per_tile_bytes=50 << 20)
    assert n == want
    monkeypatch.setattr(bp, "mem_available_bytes", lambda: None)
    assert w._unit_workers(100, 6, 50 << 20, reserve_bytes=reserve) == bp.choose_workers(
        os.cpu_count(), 100, None, per_tile_bytes=50 << 20)
