"""v16_5 R7c step 3 (docs/v16_5_r7c3_order_plan.md): request order, helper
levels and first appearance, `BLOCK01_STEP1_ORDER=cur|odon|hybrid` (unset:
hybrid, the default the user chose 2026-10-16).

A core with no GL on a synthetic 4-level slide (tmp_path), one reader so the
queue's order is the read order:

* cur reads the coarsest level first (unchanged);
* odon reads the target, then target+1 around the view, then the coarsest;
* hybrid withdraws its target+1 tiles once the target is complete.

On a real GPU (BLOCK01_REQUIRE_STEP1_GPU=1), the real Step1 mount: the
stable picture is the same under all three orders, also under GPU budget
pressure (uploads deferred).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_raw_manifest  # noqa: E402
from block01.ui.step1_core_binding import core_config  # noqa: E402
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIDE = 4096


@pytest.fixture
def slide(tmp_path):
    data = np.random.default_rng(3).integers(0, 255, (2, SIDE, SIDE), dtype=np.uint8)
    path = tmp_path / "s.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=3, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b"]}})
        cur = data
        for _ in range(3):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _starts(slide, monkeypatch, order, delay_ms=20.0):
    """The keys in the order the one reader started them, and every record."""
    if order is None:
        monkeypatch.delenv("BLOCK01_STEP1_ORDER", raising=False)
    else:
        monkeypatch.setenv("BLOCK01_STEP1_ORDER", order)
    monkeypatch.setenv("BLOCK01_STEP1_PREFETCH", "off")
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=1,
                                                      test_read_delay_ms=delay_ms), trace=True)
    recs = []
    try:
        # the view first, then the channel ticked on (2 x 2 level-0 tiles)
        c.set_view(0, [1024, 1024, 2048, 2048], 1, False)
        c.set_display([(0, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0)], False)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            c.paint(0, 1, 1, [1024.0, 2048.0, 1024.0, 2048.0])
            recs += c.drain_trace()
            if c.idle():
                break
            time.sleep(0.005)
        recs += c.drain_trace()
    finally:
        c.close()
        raw.close()
    starts = [r[2] for r in sorted(recs, key=lambda r: r[0]) if r[1] == "start"]
    return starts, recs


def test_cur_reads_the_coarsest_level_first(slide, monkeypatch):
    starts, recs = _starts(slide, monkeypatch, "cur")
    assert starts[0][1] == 3                                  # the coarsest level (one tile)
    assert {k[1] for k in starts} == {0, 3}, "cur asks for no helper level"
    assert not [r for r in recs if r[1] == "order"], "cur's queue is the old one"


def test_odon_reads_the_target_then_target_plus_one_then_the_coarsest(slide, monkeypatch):
    starts, recs = _starts(slide, monkeypatch, "odon")
    levels = [k[1] for k in starts]
    assert levels[:4] == [0, 0, 0, 0], levels
    assert 1 in levels and levels.index(3) > max(i for i, l in enumerate(levels) if l == 1), levels
    # target+1 around the view: level 1 covers it with tile (1, 1); grown by one: 3 x 3
    ones = {(k[2], k[3]) for k in starts if k[1] == 1}
    assert ones == {(x, y) for x in range(3) for y in range(3)}, ones
    assert [r for r in recs if r[1] == "order"][0][3]["order"] == 1.0


@pytest.mark.parametrize("order", ["hybrid", None])
def test_hybrid_withdraws_target_plus_one_once_the_target_is_complete(slide, monkeypatch, order):
    """hybrid, and the default (the variable unset) is hybrid."""
    starts, recs = _starts(slide, monkeypatch, order, delay_ms=60.0)
    asked = {r[2] for r in recs if r[1] == "req" and r[2][1] == 1}
    started = [k for k in starts if k[1] == 1]
    assert len(asked) == 9, asked                              # asked for while the target was missing
    assert len(started) <= 2, started                          # withdrawn once it was complete
    assert any(r[1] == "recv" and r[2][1] == 3 and r[3].get("ok") == 1.0 for r in recs)


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    out = tmp_path_factory.mktemp("r7c3") / "synth"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_synth_project.py"), str(out)],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return out


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
@pytest.mark.parametrize("mode,env", [
    ("overlay", {}),
    ("fusion", {}),
    ("overlay", {"BLOCK01_A9_RIG_GPU_BYTES": str(160 * 1024 * 1024)}),   # budget pressure
], ids=["overlay", "fusion", "budget"])
def test_the_three_orders_settle_on_the_same_picture(tmp_path, synth, mode, env):
    pics = {}
    for order in ("cur", "odon", "hybrid"):
        env_ = dict(os.environ, BLOCK01_STEP1_ORDER=order, **env)
        npz = tmp_path / f"{order}.npz"
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_pixel_compare.py"), "probe",
                              "--backend", "rust", "--project", str(synth / "project.json"), "--out", str(npz),
                              "--channels", "a,b,c,e", "--mode", mode],
                             capture_output=True, text=True, timeout=1200, env=env_)
        lines = [l for l in run.stdout.splitlines() if l.startswith("{")]
        assert lines, run.stdout[-2000:] + run.stderr[-3000:]
        assert all(c["stable"] for c in json.loads(lines[-1])["cases"]), lines[-1]
        pics[order] = np.load(npz)
    for case in pics["cur"].files:
        for order in ("odon", "hybrid"):
            assert np.array_equal(pics["cur"][case], pics[order][case]), (order, case)


def test_two_passes_of_one_issue_share_odons_new_read_ahead_cap(slide, monkeypatch):
    """hybrid: coming back to a view whose target is cached withdraws its
    helpers inside the issue (a second pass); odon's read-ahead cap still
    counts the whole issue."""
    monkeypatch.setenv("BLOCK01_STEP1_ORDER", "hybrid")
    monkeypatch.setenv("BLOCK01_STEP1_PREFETCH", "odon")
    monkeypatch.setenv("BLOCK01_R7C3_TUNE", "cap=3")
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=4), trace=True)

    def settle(box):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            c.paint(0, 1, 1, [float(box[1]), float(box[3]), float(box[0]), float(box[2])])
            if c.idle():
                return
            time.sleep(0.005)

    try:
        a, b = [1024, 1024, 2048, 2048], [3072, 3072, 4096, 4096]
        c.set_view(0, a, 1, False)
        c.set_display([(0, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0)], False)
        settle(a)
        c.set_view(0, b, 2, False)                 # far: a's tiles are not kept
        settle(b)
        c.drain_trace()
        c.set_view(0, a, 3, False)                 # a's target: CPU cache hits
        recs = c.drain_trace()
    finally:
        c.close()
        raw.close()
    orders = [r for r in recs if r[1] == "order"]
    new_ahead = [r for r in recs if r[1] == "req" and r[3].get("ahead") == 1.0]
    assert len(orders) == 2, "the hits withdrew the helpers: a second pass"
    assert len(new_ahead) <= 3, len(new_ahead)
