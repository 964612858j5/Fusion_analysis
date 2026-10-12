"""v16_5 R3 plan §2.5-2.6: Step3's viewer with the Rust backend, on the
REAL mount (`scripts/r3_label_mount_probe.py`, one process per case, each
first checking the viewer really is the Rust one):

  * lifecycle: the label binding reads a cell mask and the Rust layer shows
    it; pause / resume reach the binding; a source change makes a new
    binding; the old and the last label threads end (`dispose` no longer
    waits: the program's exit does, as `closing_threads.wait_all` here);
  * fallback: a failed Rust paint hands the picture to the Python GPU path,
    which shows the masks the mount carried over -- with the zoomed-out
    pyramid the Rust-time binding built (without the carry-over the new
    binding refuses that pyramid and shows nothing zoomed out).
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3_label_mount_probe.py"

pytestmark = pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                                reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3_lm") / "synth3.ome.tif"
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


@pytest.mark.parametrize("scenario, fault", [("lifecycle", ""), ("fallback", "paint:30")])
def test_step3_with_the_rust_backend(scenario, fault, slide, tmp_path):
    pytest.importorskip("fusion_viewer_core")
    pytest.importorskip("zarr")
    env = dict(os.environ, BLOCK01_RUST_TEST_FAULT=fault)
    proc = subprocess.run([sys.executable, str(PROBE), scenario, "--slide", slide,
                           "--mask-dir", str(tmp_path)], capture_output=True, text=True,
                          timeout=300, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{scenario}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
    assert result["binding_at_start"] == "Step1CoreBinding"
