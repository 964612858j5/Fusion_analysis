"""v16_5 R3 plan §2.2-2.3: the Rust-drawn Step1 viewer and `gpu_memory`.

Each scenario of `scripts/r3_memory_probe.py` runs on the REAL mount in a
process of its own and first checks the viewer really is the Rust one:
release while hidden keeps only the coarse of the channels drawn last and
the picture comes back identical; Smooth changed at run time draws what a
process started with it draws; a new GPU block asks for the pressure check
after the paint; the core's CPU memory stays under its bound; dispose
leaves nothing on the GPU.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3_memory_probe.py"
SYNTH = ROOT / "scripts" / "r2_synth_project.py"

pytestmark = pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                                reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3_mem") / "synth3.ome.tif"
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


def _probe(*args):
    pytest.importorskip("fusion_viewer_core")
    proc = subprocess.run([sys.executable, str(PROBE), *map(str, args)], capture_output=True, text=True,
                          timeout=300, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{args[0]}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
    assert result["binding"] == "Step1CoreBinding"
    return result


@pytest.mark.parametrize("scenario", ["release_hidden", "pressure", "dispose"])
def test_a_gpu_memory(scenario, slide):
    _probe(scenario, "--slide", slide)


def test_b_smooth_at_run_time_draws_what_a_start_with_it_draws(slide, tmp_path):
    _probe("smooth", "--slide", slide, "--shot", tmp_path / "toggled.npy")
    _probe("still", "--slide", slide, "--smooth", 0, "--shot", tmp_path / "started.npy")
    toggled, started = np.load(tmp_path / "toggled.npy"), np.load(tmp_path / "started.npy")
    assert toggled.shape == started.shape and np.array_equal(toggled, started)


@pytest.mark.parametrize("extra", ["--extra-raw", "--extra"])
def test_c_cpu_memory_stays_under_its_bound(extra, tmp_path):
    """Raw channels (one allowance) and corrected ones (the other)."""
    made = subprocess.run([sys.executable, str(SYNTH), str(tmp_path / "p"), extra, "6"],
                          capture_output=True, text=True, timeout=300, env=dict(os.environ))
    assert made.returncode == 0, made.stderr[-2000:]
    result = _probe("memory", "--project", tmp_path / "p" / "project.json")
    key = "cache_corrected_bytes" if extra == "--extra" else "cache_raw_bytes"
    assert result[key] > 0, result


@pytest.mark.parametrize("entry", ["step2", "preseg"])
def test_d_a_segmentation_start_releases_the_hidden_rust_viewer(entry, slide, tmp_path):
    """Step2's real `_run()` and the pre-segmentation job's real `start()`
    (their work replaced by nothing) give the hidden Rust viewer's GPU
    blocks back down to its coarse (`scripts/r3_release_entry_probe.py`)."""
    pytest.importorskip("fusion_viewer_core")
    pytest.importorskip("zarr")
    probe = ROOT / "scripts" / "r3_release_entry_probe.py"
    proc = subprocess.run([sys.executable, str(probe), entry, "--slide", slide, "--work", str(tmp_path)],
                          capture_output=True, text=True, timeout=300, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{entry}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
