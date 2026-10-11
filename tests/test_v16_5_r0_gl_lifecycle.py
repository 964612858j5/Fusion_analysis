"""v16_5 R0: the Rust core inside Qt's GL context, on a real display.

Every scenario of scripts/r0_gl_probe.py runs in a process of its own (one
GL widget per process; the WSL D3D12 driver allows ~5 contexts per
process). Gated like the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1 and a
real X display.
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

PROBE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "r0_gl_probe.py"


def _run(scenario, **env):
    full = dict(os.environ, **env)
    proc = subprocess.run([sys.executable, str(PROBE), scenario], env=full,
                          capture_output=True, text=True, timeout=120)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{scenario}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    return json.loads(lines[-1]), proc.returncode


@pytest.mark.parametrize("scenario", ["first_show", "resize", "reparent", "hide_show",
                                      "close_order", "coexist"])
def test_a_lifecycle(scenario):
    result, code = _run(scenario)
    assert result.get("ok") is True and code == 0, result


def test_b_device_pixel_ratio_two():
    result, code = _run("dpi", QT_SCALE_FACTOR="2")
    assert result["ratio"] == 2.0
    assert result.get("ok") is True and code == 0, result


def test_c_moving_to_a_screen_with_another_ratio():
    """Each screen its own ratio (Qt's positional QT_SCREEN_SCALE_FACTORS);
    the window moves to the second screen and back at run time."""
    result, code = _run("screen_move", QT_SCREEN_SCALE_FACTORS="1;2")
    if result.get("ok") is None:
        pytest.skip(result.get("skipped", "needs two screens"))
    assert result.get("ok") is True and code == 0, result


def test_d_the_checks_catch_a_dirty_state_and_a_wrong_picture():
    result, code = _run("fault_control")
    assert result["caught_state"] and result["caught_picture"], result
