"""v16_5 R7b B1.1 (docs/v16_5_r7b_corrected_read_plan.md): the decoded-chunk
cache changes no pixel and stays inside its budget.

On synthetic projects (`scripts/r2_synth_project.py`, corrected channels,
also a polygon region), the Rust backend draws the same stable pictures
with the cache on (`BLOCK01_STEP1_CHUNK_CACHE=1`) and off (default); with
it on, chunks are reused (hits), what it holds never exceeds its budget,
and the viewer's memory bound is the same as with it off (the budget is
carved out of the corrected tile cache). The pure cache rules (one decode
per chunk, eviction, bypass when all held chunks are in use, failures not
kept, close wakes waiters) are Rust unit tests (`chunk_cache.rs`).

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)
pytest.importorskip("fusion_viewer_core")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNTH = ROOT / "scripts" / "r2_synth_project.py"
COMPARE = ROOT / "scripts" / "r2_pixel_compare.py"
POLY = "67,101;2391,101;2391,2817;1229,1400;67,2817"


def _project(tmp_path, name, *extra):
    out = tmp_path / name
    proc = subprocess.run([sys.executable, str(SYNTH), str(out), *extra], capture_output=True,
                          text=True, timeout=600, env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return out / "project.json"


def _probe(tmp_path, project, tag, cache_on, *extra):
    env = dict(os.environ)
    env.pop("BLOCK01_STEP1_CHUNK_CACHE", None)
    if cache_on:
        env["BLOCK01_STEP1_CHUNK_CACHE"] = "1"
    out = tmp_path / f"{tag}.npz"
    proc = subprocess.run([sys.executable, str(COMPARE), "probe", "--backend", "rust", "--project",
                           str(project), "--out", str(out), *extra],
                          capture_output=True, text=True, timeout=1200, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result["binding"] == "Step1CoreBinding", result
    assert all(c["stable"] for c in result["cases"]), result["cases"]
    return np.load(out), result


@pytest.mark.parametrize("name,synth,extra", [
    ("f32", ["--dtype", "float32"], ["--channels", "a,b,c,f"]),
    ("poly", ["--roi", "101,2817,67,2391", "--polygon", POLY], ["--channels", "a,b,c,f"]),
], ids=["f32", "polygon"])
def test_cache_changes_no_pixel(tmp_path, name, synth, extra):
    project = _project(tmp_path, name, *synth)
    off_px, off = _probe(tmp_path, project, "off", False, *extra)
    on_px, on = _probe(tmp_path, project, "on", True, *extra)
    for case in off_px.files:
        assert np.array_equal(off_px[case], on_px[case]), case
    c_off, c_on = off["chunk"], on["chunk"]
    assert c_off.get("chunk_budget_bytes") == 0 and c_off.get("chunk_hits", 0) == 0
    assert c_on["chunk_budget_bytes"] > 0
    assert c_on["chunk_hits"] > 0, c_on                       # chunks were reused
    assert c_on["chunk_held_bytes"] + c_on["chunk_decoding_bytes"] <= c_on["chunk_budget_bytes"]
    # carved out of the corrected tile cache: the viewer's bound does not grow
    assert on["cpu_bound_bytes"] == off["cpu_bound_bytes"], (on["cpu_bound_bytes"], off["cpu_bound_bytes"])
    assert on["cpu_bytes"] <= on["cpu_bound_bytes"]


def test_reader_thread_switch_reaches_config(monkeypatch):
    from block01.ui.step1_core_binding import core_config
    from block01.ui.step1_viewer_mount import demo_budgets
    monkeypatch.delenv("BLOCK01_A9_READER_THREADS", raising=False)
    monkeypatch.delenv("BLOCK01_STEP1_CHUNK_CACHE", raising=False)
    cfg = core_config(demo_budgets())
    assert cfg["threads"] == 6 and cfg["chunk_cache_bytes"] == 0          # off by default
    monkeypatch.setenv("BLOCK01_A9_READER_THREADS", "12")
    monkeypatch.setenv("BLOCK01_STEP1_CHUNK_CACHE", "1")
    cfg = core_config(demo_budgets())
    assert cfg["threads"] == 12 and cfg["chunk_cache_bytes"] == 256 * 1024 * 1024
