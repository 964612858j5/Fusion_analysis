"""v16_5 R3 plan §2.5: the Rust-drawn Step1 viewer falls back at run time.

The core's test-only fault injection (`BLOCK01_RUST_TEST_FAULT`) makes
reads, a paint or a GL re-initialisation fail, and the REAL counting
decides (`viewer/failures.rs`: a tile the picture needs is retried, given
up after 3 failures; 8 failed reads in a row give up too; a success clears
both). Each case runs `scripts/r3_fallback_probe.py` on the real mount in a
process of its own, with a still view: just under a limit the picture
stays Rust and completes; at a limit the Python GPU path takes over on the
same camera, its scheduler starts, a failed paint is never drawn, and the
Rust readers end within the exit's one second.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3_fallback_probe.py"

pytestmark = pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                                reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3_fb") / "synth3.ome.tif"
    rng = np.random.default_rng(9)
    data = rng.integers(0, 255, (3, 2100, 1700), dtype=np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3", "CD8"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _run(scenario, fault, slide):
    pytest.importorskip("fusion_viewer_core")
    env = dict(os.environ, BLOCK01_RUST_TEST_FAULT=fault)
    proc = subprocess.run([sys.executable, str(PROBE), scenario, "--slide", slide],
                          capture_output=True, text=True, timeout=300, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{scenario} {fault}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
    return result


@pytest.mark.parametrize("fault", ["tile:2", "all:7"])
def test_a_just_under_a_limit_the_picture_stays_rust(fault, slide):
    _run("stays", fault, slide)


@pytest.mark.parametrize("fault, why", [("tile:3", "failed 3 times"), ("all:8", "8 reads in a row"),
                                        ("paint:5", "paint failed")])
def test_b_at_a_limit_python_takes_over(fault, why, slide):
    result = _run("falls_back", fault, slide)
    assert why in result["rust_refused"], result


def test_c_a_failed_gl_reinitialisation_falls_back(slide):
    _run("reinit", "reinit", slide)


def test_d_a_failed_first_gl_initialisation_starts_on_python(slide):
    """The mount never draws with the core: it starts on the Python GPU
    path and says why (`_start_rust_backend`'s refusal, R3 plan §2.6)."""
    _run("init", "init", slide)


def test_e_a_channel_the_slide_lacks_hands_over_to_python(slide):
    """A guard: the product's Fusion model drops channels that are not
    enabled before they reach the binding, so the stray channel is put
    into the display at the binding (see the probe)."""
    _run("not_in_slide", "", slide)
