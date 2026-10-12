"""v16_5 R3 plan §2.4: the Rust-drawn whole-slide viewer and the montage's
Python GPU layer side by side in one process (`scripts/r3_montage_probe.py`):
both draw, `gpu_memory` counts both, Smooth reaches both and comes back,
the tabs' hide / show leaves the whole-slide picture identical, and a
release takes only the hidden viewer's blocks. The montage keeps its own
program (it is not compared pixel for pixel with the whole-slide picture:
in the Python path they already differ at tile seams with Smooth; user
ruling 2026-10-12); its own pixel tests run unchanged with the Rust
backend chosen (`test_step1_montage_view` in a process with
BLOCK01_STEP1_BACKEND=rust).
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3_montage_probe.py"

pytestmark = pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                                reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3_mt") / "synth3.ome.tif"
    rng = np.random.default_rng(7)
    data = rng.integers(0, 255, (3, 2100, 1700), dtype=np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3", "CD8"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def test_a_whole_slide_rust_and_montage_python_side_by_side(slide):
    pytest.importorskip("fusion_viewer_core")
    proc = subprocess.run([sys.executable, str(PROBE), "--slide", slide], capture_output=True,
                          text=True, timeout=300, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result


def test_b_the_montages_own_tests_pass_with_the_rust_backend_chosen():
    pytest.importorskip("fusion_viewer_core")
    env = dict(os.environ, BLOCK01_STEP1_BACKEND="rust")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"--rootdir={ROOT}", f"--confcutdir={ROOT}",
                           str(ROOT / "tests" / "test_step1_montage_view.py")],
                          capture_output=True, text=True, timeout=900, env=env, cwd="/tmp")
    assert proc.returncode == 0, proc.stdout[-3000:]
