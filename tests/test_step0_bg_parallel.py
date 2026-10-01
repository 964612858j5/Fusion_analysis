"""Block S0P: Step0's background correction in parallel on the CPU
(docs/v16_Step0_cpu_parallel_application.md v2, gates P1-P8).

Segment 1: the bounded in-order executor (`core/bg_parallel.py`) and the
worker-count rule, on their own.
"""

import os
import threading
import time

import numpy as np
import pytest

from block01.core import bg_parallel as bp  # noqa: E402


# ── the worker-count rule (ruling 4) ────────────────────────────────────

@pytest.mark.parametrize("cpu,tiles,avail,want", [
    (16, 16, None, 4),                      # this machine, memory unknown
    (16, 16, int(8e9), 4),
    (16, 2, None, 2),                       # never more than the tiles
    (16, 16, int(2.6e9), 2),                # (2.6 - 1.0) / 0.75 = 2
    (16, 16, int(1.5e9), 1),                # < 2 fit: serial
    (4, 16, None, 1),                       # 4 // 2 - 1 = 1: serial
    (6, 16, None, 2),
    (None, 16, None, 1),
])
def test_choose_workers(cpu, tiles, avail, want):
    assert bp.choose_workers(cpu, tiles, avail) == want


def test_mem_available_is_read_or_none():
    v = bp.mem_available_bytes()
    assert v is None or v > 0


# ── the executor ────────────────────────────────────────────────────────

def _task(delay_of=lambda i: 0.01, reads=None, started=None):
    def task(i, read_done):
        if started is not None:
            started.append(i)
        if reads is not None:
            reads.append(i)
        read_done()
        time.sleep(delay_of(i))
        return i * 10
    return task


def test_results_come_in_order_whatever_finishes_first():
    # later tiles finish first
    out = list(bp.ordered_results(_task(lambda i: 0.05 * (5 - i)), range(6), 4))
    assert out == [0, 10, 20, 30, 40, 50]


def test_at_most_n_tiles_are_in_flight():
    stats = {}
    list(bp.ordered_results(_task(lambda i: 0.02), range(12), 3, stats=stats))
    assert stats["max_in_flight"] == 3 and stats["submitted"] == 12


def test_the_corrections_overlap():
    t0 = time.perf_counter()
    list(bp.ordered_results(_task(lambda i: 0.2), range(8), 4))
    assert time.perf_counter() - t0 < 0.2 * 8 / 2          # serial would be 1.6 s


def test_a_stop_during_a_read_starts_nothing_new():
    """`test_wsi_cancel`'s semantics: a cancel raised while tile 0 is read
    -> tile 0 completes, no other tile is read."""
    flag = threading.Event()
    reads = []

    def task(i, read_done):
        reads.append(i)
        if i == 0:
            time.sleep(0.1)                 # the read takes a while ...
            flag.set()                      # ... and the cancel arrives during it
        read_done()
        time.sleep(0.05)
        return i
    out = list(bp.ordered_results(task, range(5), 4, should_stop=flag.is_set))
    assert reads == [0] and out == [0]


def test_leaving_early_cancels_the_unstarted_and_waits_for_the_running():
    started, done = [], []

    def task(i, read_done):
        started.append(i)
        read_done()
        time.sleep(0.1)
        done.append(i)
        return i
    gen = bp.ordered_results(task, range(20), 3)
    assert next(gen) == 0
    gen.close()                              # the consumer stops (cancel)
    assert sorted(done) == sorted(started)   # every started tile finished
    assert len(started) <= 1 + 3             # nothing beyond the window started
    assert not [t for t in threading.enumerate() if t.name.startswith("bg-tiles")]


def test_an_exception_in_a_tile_reaches_the_consumer_in_order():
    def task(i, read_done):
        read_done()
        if i == 2:
            raise RuntimeError("tile 2")
        return i
    got = []
    with pytest.raises(RuntimeError, match="tile 2"):
        for v in bp.ordered_results(task, range(6), 3):
            got.append(v)
    assert got == [0, 1]


def test_one_worker_is_todays_serial_loop():
    order = []

    def task(i, read_done):
        order.append(("start", i))
        read_done()
        order.append(("end", i))
        return i
    assert list(bp.ordered_results(task, range(3), 1)) == [0, 1, 2]
    assert order == [("start", 0), ("end", 0), ("start", 1), ("end", 1),
                     ("start", 2), ("end", 2)]


def test_a_task_that_never_reports_its_read_still_lets_the_next_start():
    out = list(bp.ordered_results(lambda i, rd: i, range(5), 3))
    assert out == list(range(5))


def test_array_results_are_delivered_untouched():
    rng = np.random.default_rng(0)
    data = [rng.random((8, 8), dtype=np.float32) for _ in range(6)]

    def task(i, read_done):
        read_done()
        return data[i].copy()
    for a, b in zip(bp.ordered_results(task, range(6), 4), data):
        assert np.array_equal(a, b)


# ── segment 2: one backend per channel (P7) and the reuse signature (P8) ─

zarr = pytest.importorskip("zarr")


class _Loader:
    """A fake slide: 3 tiles of 4096 rows, deterministic pixels per page."""

    def __init__(self, h=9000, w=96):
        self.shape = (h, w)
        self.ch_map = {"DAPI": 0, "CD3": 1, "CD20": 2}
        self.filepath = "/fake/slide.ome.tif"
        rng = np.random.default_rng(7)
        self._pages = {p: (rng.random((h, w), dtype=np.float32) * 200) for p in range(3)}
        self.reads = 0

    def _read_roi_zarr(self, page_idx, y0, y1, x0, x1):
        self.reads += 1
        return self._pages[page_idx][y0:y1, x0:x1].copy()


def _cfg(method="tophat"):
    return {"channel_decisions": {"CD3": method},
            "method_params": {"tophat_radius": 3, "cucim_sigma": 4}, "channel_params": {}}


def _roi(h=9000, w=96):
    return {"name": "R", "bbox_fullres": [0, h, 0, w], "polygon_fullres": None, "shape": [h, w]}


def _save(out_dir, loader=None, method="tophat", **kw):
    from block01.ui.step0 import search_ctrl as sc
    loader = loader or _Loader()
    w = sc.WsiCorrectionWorker(loader, str(out_dir), _cfg(method), rois=[_roi()], **kw)
    got = {"finished": [], "error": [], "canceled": []}
    w.finished.connect(lambda p, d: got["finished"].append(p))
    w.error.connect(got["error"].append)
    w.canceled.connect(got["canceled"].append)
    w.run()
    assert got["error"] == [], got["error"]
    return w, got, zarr.open_group(str(out_dir / "corrected_channels.zarr"), mode="r")["R"]["CD3"]


@pytest.mark.parametrize("method", ["tophat", "cucim"])
def test_the_backend_is_recorded_on_the_array(tmp_path, method):
    from block01.core.bg_correction import current_compute_signature
    _w, _g, arr = _save(tmp_path, method=method)
    assert (arr.attrs["bg_compute_path"], arr.attrs["tophat_footprint"]) == \
        current_compute_signature(method)


def test_a_gpu_failure_mid_channel_recomputes_the_whole_channel_on_the_cpu(tmp_path, monkeypatch):
    """P7: never half GPU, half CPU."""
    from block01.core import bg_correction as bg
    from block01.ui.step0 import search_ctrl as sc
    _w, _g, ref = _save(tmp_path / "cpu")
    reference = np.asarray(ref[...])
    calls = []

    def fake_correct(raw, method, param, path):
        calls.append(path)
        if path == "gpu":
            if sum(1 for c in calls if c == "gpu") == 2:       # the 2nd GPU tile fails
                raise bg.GpuBackendFailed("simulated")
            return bg.correct_tile(raw, method, param, "cpu") + 1000.0   # "GPU" pixels
        return bg.correct_tile(raw, method, param, "cpu")
    monkeypatch.setattr(sc, "compute_path", lambda method: "gpu")
    monkeypatch.setattr(sc, "correct_tile", fake_correct)
    _w, got, arr = _save(tmp_path / "gpu")
    assert got["finished"]
    assert calls[:2] == ["gpu", "gpu"] and set(calls[2:]) == {"cpu"}
    assert (arr.attrs["bg_compute_path"], arr.attrs["tophat_footprint"]) == ("cpu", "disk")
    assert np.array_equal(np.asarray(arr[...]), reference)        # no GPU tile left


def test_reuse_needs_the_same_backend(tmp_path):
    """P8: a product without the new attrs, or made on another backend, is
    not reused; the same backend is."""
    from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION, current_compute_signature
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    _save(tmp_path)
    zp = str(tmp_path / "corrected_channels.zarr")
    current = ("tophat", 3, BG_CORRECTION_ALGO_VERSION) + current_compute_signature("tophat")
    assert read_corrected_zarr_state(zp)[0]["CD3"] == current             # same -> skipped
    g = zarr.open_group(zp, mode="r+")["R"]["CD3"]
    del g.attrs["bg_compute_path"]
    del g.attrs["tophat_footprint"]
    assert read_corrected_zarr_state(zp)[0]["CD3"] != current             # pre-S0P -> recompute
    g.attrs["bg_compute_path"], g.attrs["tophat_footprint"] = "gpu", "square"
    other = read_corrected_zarr_state(zp)[0]["CD3"]
    assert other[3:] == ("gpu", "square")
    if current_compute_signature("tophat") == ("cpu", "disk"):
        assert other != current                                           # GPU product on a CPU machine


# ── segment 3: the Save loop on the executor (P1, P3, P4, P5) ───────────

tifffile = pytest.importorskip("tifffile")
SH, SW = 9000, 4500


@pytest.fixture(scope="module")
def pyramid_slide(tmp_path_factory):
    """A real 3-level OME-TIFF, so the coarse plane is written too."""
    rng = np.random.default_rng(11)
    data = rng.integers(0, 255, (3, SH, SW), dtype=np.uint8)
    path = tmp_path_factory.mktemp("s0p") / "slide.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3", "CD20"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::4, ::4])
            tw.write(cur, subfiletype=1, tile=(512, 512))
    return str(path)


# an ROI whose origin is not a multiple of the coarse stride, with a polygon
ROI = {"name": "R", "bbox_fullres": [37, 8937, 61, 4461], "shape": [8900, 4400],
       "polygon_fullres": [(61, 37), (4461, 37), (4461, 8937), (2000, 8000), (61, 8937)]}


def _real_save(out_dir, slide, method, monkeypatch, n=None, hook=None):
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0 import search_ctrl as sc
    if n is not None:
        monkeypatch.setattr(sc, "choose_workers", lambda cpu, tiles, avail: n)
    loader = OMETIFFLoader(slide)
    cfg = {"channel_decisions": {"CD3": method},
           "method_params": {"tophat_radius": 3, "cucim_sigma": 6}, "channel_params": {}}
    w = sc.WsiCorrectionWorker(loader, str(out_dir), cfg, rois=[dict(ROI)])
    got = {"finished": [], "error": [], "canceled": []}
    w.finished.connect(lambda p, d: got["finished"].append(p))
    w.error.connect(got["error"].append)
    w.canceled.connect(got["canceled"].append)
    if hook:
        hook(w)
    w.run()
    assert got["error"] == [], got["error"]
    return w, got


def _product(out_dir):
    root = zarr.open_group(str(out_dir / "corrected_channels.zarr"), mode="r")
    arr = root["R"]["CD3"]
    plane_root = str(out_dir / "corrected_coarse.zarr")
    planes = {}
    if os.path.isdir(plane_root):
        for base, _d, files in os.walk(plane_root):
            if ".zarray" in files:
                planes[os.path.relpath(base, plane_root)] = np.asarray(zarr.open(base, mode="r"))
    attrs = {k: v for k, v in arr.attrs.items() if k not in ("source_identity", "written_at")}
    return np.asarray(arr[...]), planes, attrs



def _independent_reference(slide, method, param):
    """What the pre-S0P worker computed, rebuilt here from scratch: each
    4096 tile's padded window read straight from the slide, corrected with
    the pre-S0P per-tile functions, cropped, zeroed outside the polygon."""
    from block01.core import bg_correction as bg
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    y0, y1, x0, x1 = ROI["bbox_fullres"]
    h, w = y1 - y0, x1 - x0
    with tifffile.TiffFile(slide) as tf:
        page = tf.series[0].levels[0].asarray()[1]                 # CD3
    poly = WsiCorrectionWorker._poly_mask(ROI["polygon_fullres"], y0, x0, h, w)
    out = np.zeros((h, w), np.float32)
    for core, padded, crop in bg._tile_slices(h, w, 4096, bg.method_overlap(method, param)):
        py0, py1, px0, px1 = padded
        raw = page[y0 + py0:y0 + py1, x0 + px0:x0 + px1].astype(np.float32)
        corr = (bg._apply_tophat_cpu(raw, param) if method == "tophat"
                else bg._apply_cucim_or_cpu(raw, param, prefer_gpu=False))
        cy0, cy1, cx0, cx1 = crop
        c0, c1, c2, c3 = core
        out[c0:c1, c2:c3] = corr[cy0:cy1, cx0:cx1]
    out[~poly] = 0
    return out


@pytest.mark.parametrize("method", ["tophat", "cucim"])
def test_parallel_save_is_bitwise_the_serial_one(tmp_path, pyramid_slide, monkeypatch, method):
    """P1: product, coarse plane and attrs (but the per-write token) equal."""
    _real_save(tmp_path / "serial", pyramid_slide, method, monkeypatch, n=1)
    ref = _product(tmp_path / "serial")
    assert ref[1], "the coarse plane must be written for this slide"
    # and the serial path itself is what the pre-S0P worker computed
    assert np.array_equal(ref[0], _independent_reference(
        pyramid_slide, method, 3 if method == "tophat" else 6))
    for n in (2, 4):
        w, _ = _real_save(tmp_path / f"n{n}", pyramid_slide, method, monkeypatch, n=n)
        got = _product(tmp_path / f"n{n}")
        assert np.array_equal(got[0], ref[0])
        assert sorted(got[1]) == sorted(ref[1])
        assert all(np.array_equal(got[1][k], ref[1][k]) for k in ref[1])
        assert got[2] == ref[2]
        assert w._tile_stats["workers"] == n
        assert 1 < w._tile_stats["max_in_flight"] <= n          # P4: bounded, and parallel


def test_a_cancel_while_tiles_compute_starts_nothing_and_writes_nothing_more(
        tmp_path, pyramid_slide, monkeypatch):
    """P3 at the worker: cancel after the 2nd result with n tiles in flight."""
    from block01.ui.step0 import search_ctrl as sc
    writes = []
    real_write = sc.WsiCorrectionWorker._write_tile

    def spy(ds, acc, out, core, poly):
        writes.append(core)
        return real_write(ds, acc, out, core, poly)
    monkeypatch.setattr(sc.WsiCorrectionWorker, "_write_tile", staticmethod(spy))

    def hook(w):
        w.progress.connect(lambda *a: w.stop_after_current_channel() if a[2] == 2 else None)
    t0 = __import__("time").perf_counter()
    w, got = _real_save(tmp_path, pyramid_slide, "tophat", monkeypatch, n=4, hook=hook)
    assert got["canceled"] and not got["finished"]
    assert len(writes) == 2                                      # nothing written after the cancel
    assert w._tile_stats["submitted"] <= 2 + 4                   # nothing started beyond the window
    assert not os.path.exists(tmp_path / "corrected_channels.zarr")     # a fresh save drops it
    assert not [t for t in __import__("threading").enumerate() if t.name.startswith("bg-tiles")]
    assert __import__("time").perf_counter() - t0 < 120


def test_the_gpu_backend_stays_one_tile_at_a_time(tmp_path, monkeypatch):
    """P5."""
    from block01.core import bg_correction as bg
    from block01.ui.step0 import search_ctrl as sc
    order = []

    def fake(raw, method, param, path):
        order.append(path)
        return bg.correct_tile(raw, method, param, "cpu")
    monkeypatch.setattr(sc, "compute_path", lambda method: "gpu")
    monkeypatch.setattr(sc, "correct_tile", fake)
    monkeypatch.setattr(sc, "choose_workers", lambda cpu, tiles, avail: 4)
    w, got, arr = _save(tmp_path)
    assert got["finished"] and set(order) == {"gpu"}
    assert w._tile_stats["workers"] == 1 and w._tile_stats["max_in_flight"] == 1
    assert arr.attrs["bg_compute_path"] == "gpu" and arr.attrs["tophat_footprint"] == "square"
