"""v16_5 R2 (plan §3, §5.1): the Rust-drawn stable Step1 picture equals the
Python GPU path's, pixel for pixel (alpha included), on synthetic projects
(`scripts/r2_synth_project.py`: raw u8 / u16 / float32, corrected channels
of whole-number values -- NaN, +-inf and negative patches included -- a
valid and a stale sidecar, a missing product) in Overlay and Fusion (a
channel in two groups, a nucleus), whole-slide / rectangle / concave
polygon / refused-polygon regions, smoothing on and off -- five camera
cases each (`scripts/r2_pixel_compare.py`, one backend per process).

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

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNTH = ROOT / "scripts" / "r2_synth_project.py"
COMPARE = ROOT / "scripts" / "r2_pixel_compare.py"
POLY = "67,101;2391,101;2391,2817;1229,1400;67,2817"            # concave, spans the region
BAD_POLY = "67,101;2000,101;2000,2000"                           # not the region's rectangle
GROUPS = json.dumps({"g1": {"b": 1.0, "c": 0.8}, "g2": {"c": 0.5, "f": 1.0}})
PROJECTS = {
    "u8": [],
    "u16": ["--dtype", "uint16"],
    "f32": ["--dtype", "float32"],
    "poly": ["--roi", "101,2817,67,2391", "--polygon", POLY],
    "badpoly": ["--roi", "101,2817,67,2391", "--polygon", BAD_POLY],
}


@pytest.fixture(scope="module")
def projects(tmp_path_factory):
    root = tmp_path_factory.mktemp("r2px")
    made = {}

    def get(name):
        if name not in made:
            out = root / name
            proc = subprocess.run([sys.executable, str(SYNTH), str(out), *PROJECTS[name]],
                                  capture_output=True, text=True, timeout=600, env=dict(os.environ))
            assert proc.returncode == 0, proc.stderr[-2000:]
            made[name] = out / "project.json"
        return made[name]
    return get


@pytest.mark.parametrize("project,extra", [
    ("u8", ["--channels", "a,b,c,f"]),
    ("u16", ["--channels", "a,b,c,f"]),
    ("f32", ["--channels", "a,b,c,f"]),
    ("u8", ["--mode", "fusion", "--groups", GROUPS, "--nucleus", "a", "--channels", "a,b,c,f"]),
    ("f32", ["--mode", "fusion", "--groups", GROUPS, "--nucleus", "a", "--channels", "a,b,c,f"]),
    ("poly", ["--channels", "a,b,c,f"]),
    ("poly", ["--mode", "fusion", "--groups", GROUPS, "--nucleus", "a", "--channels", "a,b,c,f"]),
    ("poly", ["--channels", "a,b,c,f", "--no-polygon"]),
    ("badpoly", ["--channels", "a,b,c,f"]),
    ("u8", ["--channels", "a,b,c,f", "--smooth", "0"]),
    ("u8", ["--channels", "a,d,e,f"]),
], ids=["u8", "u16", "f32", "u8_fusion", "f32_fusion", "polygon", "polygon_fusion", "rectangle",
        "refused_polygon", "smooth_off", "missing_and_stale"])
def test_a_pictures_are_identical(tmp_path, projects, project, extra):
    proc = subprocess.run([sys.executable, str(COMPARE), "compare", "--project", str(projects(project)),
                           "--out", str(tmp_path), *extra],
                          capture_output=True, text=True, timeout=1800, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result["rust"]["binding"] == "Step1CoreBinding", result["rust"]
    assert result["python"]["binding"] == "Step1GpuBinding", result["python"]
    for side in ("python", "rust"):
        assert all(c["stable"] for c in result[side]["cases"]), result[side]["cases"]
    assert all(c["differ"] == 0 for c in result["cases"]), result["cases"]
    if project == "badpoly":
        assert result["rust"]["roi_polygon_error"], result["rust"]
    assert result["ok"], result
