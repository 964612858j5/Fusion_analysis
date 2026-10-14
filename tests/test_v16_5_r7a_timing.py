"""v16_5 R7a (docs/v16_5_r7a_timing_plan.md §2): segment timing and read
split are measurement only.

With the tile trace and `BLOCK01_A9_DRAW_LOG=1`, paints that compose write
`fphase` records (six CPU segments, the rest, and later `fphase_gpu`
results), and every read record carries its stages (`file_ms`, ...,
`rest_ms`). Without the switch there are none, and the stable pictures are
the same bytes either way.

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import os

import numpy as np
import pytest

import importlib.util as _iu
import pathlib as _pl

_spec = _iu.spec_from_file_location("r6_drawlog", _pl.Path(__file__).with_name("test_v16_5_r6_drawlog.py"))
_r6 = _iu.module_from_spec(_spec)
_spec.loader.exec_module(_r6)
_run, _slide = _r6._run, _r6._slide

SEGMENTS = ("tables", "targets", "upload", "compose", "labels", "blit")


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
def test_timing_records_only(tmp_path):
    pytest.importorskip("fusion_viewer_core")
    slide = _slide(tmp_path)
    off_px, off = _run(tmp_path, slide, "off", False)
    on_px, on = _run(tmp_path, slide, "on", True)
    stages = {r["s"] for r in off}
    assert not {"fphase", "fphase_gpu"} & stages
    assert not any("file_ms" in r for r in off if r["s"] == "read")
    phases = [r for r in on if r["s"] == "fphase"]
    assert phases, "no fphase record with the switch on"
    for r in phases:
        assert all(r[s] >= 0 for s in SEGMENTS), r
        assert r["composed"] in (0, 1) and r["draw_ms"] >= 0
        # every field is rounded to 3 decimals when merged (step1_core_layer)
        assert abs(r["draw_ms"] - sum(r[s] for s in SEGMENTS) - r["rest"]) < 0.01
    gpu = [r for r in on if r["s"] == "fphase_gpu"]
    assert gpu, "no GPU timer result came back"
    assert all(r["ms"] >= 0 and 1 <= r["seg"] <= 5 for r in gpu)
    reads = [r for r in on if r["s"] == "read" and not r.get("err")]
    assert reads and all("file_ms" in r and "rest_ms" in r for r in reads)
    assert all(r["bytes"] > 0 and r["chunks"] >= 1 and r["src"] in (1, 2) for r in reads)
    for name in off_px.files:
        assert np.array_equal(off_px[name], on_px[name]), name
