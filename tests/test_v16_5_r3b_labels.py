"""v16_5 R3b: Step3's masks planned and read by the Rust core, on the REAL
mount (`scripts/r3b_label_probe.py`, one process per case; each checks the
viewer's masks really are the core's, `CoreLabelBinding`) -- the behaviour
of `Step3LabelBinding`'s tests, R3b plan §4 -- and the presented picture
through each backend's own label path against Python's (§3.3:
`scripts/r3b_pixel_compare.py`, synthetic, the concave region, and the
biopsy with a real cell mask when the bench data is there).
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3b_label_probe.py"
PIXELS = ROOT / "scripts" / "r3b_pixel_compare.py"
BIOPSY = ROOT / "scripts" / "r3_biopsy_labels.py"
BIOPSY_PROJECT = "/sda1/Fusion/a9_bench/r2/biopsy.json"
REAL_MASK = "/sda1/Fusion/analysis_pipline/outcome/global_mask_ROI_1.zarr"

pytestmark = pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                                reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3b") / "synth3.ome.tif"
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


def _run(script, args, env=None, timeout=600):
    pytest.importorskip("fusion_viewer_core")
    pytest.importorskip("zarr")
    proc = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ, **(env or {})))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
    return result


SLOW = {"BLOCK01_RUST_TEST_LABEL_DELAY_MS": "150"}
CASES = {
    "follow": SLOW,                     # reads follow the camera, stale ones dropped
    "pyramid": {},                      # nothing read while building; then zoomed-out masks
    "level0_only": {},                  # a foreign pyramid kept: level 0 only, status says so
    "pause": {},                        # paused asks nothing, build cancelled; resume catches up
    "rapid_pause": {},                  # pause/resume at once: one pyramid writer at a time
    "switch_build": {},                 # new sources during a build
    "close_reading": SLOW,              # closing during a read: exit wait <= 1 s
    "close_build": {},                  # closing during a build: exit wait <= 1 s
    "errors": {"BLOCK01_RUST_TEST_FAULT": "label:2"},    # reported at once, cleared by new sources
    "late_error": {"BLOCK01_RUST_TEST_FAULT": "label:50", "BLOCK01_RUST_TEST_LABEL_DELAY_MS": "300"},
    "hide_show": {},                    # shown again reads; a colour change reads nothing
    "l1": {},                           # memory pyramid / zstd -> Python reads; back to the core
    "ratio": {},                        # a new device pixel ratio -> a new outline radius
    "gl_state": {},                     # GL state unchanged around every paint
}


@pytest.mark.parametrize("scenario", sorted(CASES))
def test_a_behaviour(scenario, slide, tmp_path):
    _run(PROBE, [scenario, "--slide", slide, "--work", tmp_path], CASES[scenario])


@pytest.mark.parametrize("clip", ["", "--clip"])
def test_b_pixels_equal_python_through_each_label_path(clip, slide, tmp_path):
    for backend in ("rust", "python"):
        (tmp_path / backend).mkdir()
        _run(PIXELS, ["--backend", backend, "--slide", slide, "--work", tmp_path / backend,
                      "--out", tmp_path / f"{backend}.npz"] + ([clip] if clip else []))
    _run(PIXELS, ["--compare", tmp_path / "rust.npz", tmp_path / "python.npz"])


@pytest.mark.skipif(not (os.path.exists(BIOPSY_PROJECT) and os.path.exists(REAL_MASK)),
                    reason="the biopsy bench data is not on this machine")
def test_c_biopsy_with_a_real_cell_mask(tmp_path):
    import shutil
    for backend in ("rust", "python"):
        shutil.copytree(REAL_MASK, tmp_path / backend / "global_mask_ROI_1.zarr")   # its pyramid is built there
        _run(BIOPSY, ["--backend", backend, "--project", BIOPSY_PROJECT,
                      "--mask", tmp_path / backend / "global_mask_ROI_1.zarr", "--out", tmp_path / f"{backend}.npz"],
             timeout=900)
    _run(BIOPSY, ["--compare", tmp_path / "rust.npz", tmp_path / "python.npz"])
