"""v16_5 S-Raw: the Rust-drawn Step1 viewer's lifecycle, and its shaders.

Each lifecycle scenario of `scripts/sraw_lifecycle_probe.py` runs on the
REAL mount in a process of its own (one GL widget per process): pause and
resume, the hand-over to the Python GPU path when Fusion is chosen (its
scheduler starting only then), a rebuilt source, and close. The shader
copies the Rust core compiles must stay byte-identical to `ui/shaders`.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "sraw_lifecycle_probe.py"


@pytest.mark.parametrize("name", ["step1_gpu.vert", "step1_gpu.frag", "step1_gpu_vt.frag"])
def test_a_shader_copies_are_identical(name):
    ours = (ROOT / "native" / "fusion_viewer_core" / "shaders" / name).read_bytes()
    assert ours == (ROOT / "ui" / "shaders" / name).read_bytes(), name


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("sraw_life") / "synth3.ome.tif"
    rng = np.random.default_rng(4)
    data = rng.integers(0, 255, (3, 2100, 1700), dtype=np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3", "CD8"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
@pytest.mark.parametrize("scenario", ["pause_resume", "fusion_fallback", "source_change",
                                      "partial_roi", "close"])
def test_b_lifecycle(scenario, slide):
    pytest.importorskip("fusion_viewer_core")
    proc = subprocess.run([sys.executable, str(PROBE), scenario, "--slide", slide],
                          capture_output=True, text=True, timeout=300, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{scenario}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
