"""v16_5 R2 (plan §5.6): the GL state around the PRODUCTION Rust paint is
unchanged -- a full glGet snapshot before and after every paint, across a
polygon region (scissor + stencil), Fusion, the camera wholly outside the
region, a resize, a reparent and another painter drawing after the core
(`scripts/r2_gl_state_probe.py`, one process).

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)
pytest.importorskip("fusion_viewer_core")
pytest.importorskip("OpenGL")

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_a_gl_state_holds_around_every_paint(tmp_path):
    made = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_synth_project.py"), str(tmp_path / "p"),
                           "--roi", "101,2817,67,2391",
                           "--polygon", "67,101;2391,101;2391,2817;1229,1400;67,2817"],
                          capture_output=True, text=True, timeout=600, env=dict(os.environ))
    assert made.returncode == 0, made.stderr[-2000:]
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_gl_state_probe.py"),
                           "--project", str(tmp_path / "p" / "project.json")],
                          capture_output=True, text=True, timeout=600, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    for step in result["steps"]:
        assert step["paints"] > 0 and step["stable"], step
        assert step["binding"] == "Step1CoreBinding", step
        assert step["diff_count"] == 0, step
    assert result["other_green_centre"] >= 100, result
    assert result["ok"], result
