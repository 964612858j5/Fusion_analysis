"""v16_5 R6 (docs/v16_5_r6_motion_clarity_plan.md §5): the core's draw log
is measurement only.

With the tile trace on, `BLOCK01_A9_DRAW_LOG=1` adds per-paint records of
what was drawn (`fview`, `draw`, `avail`); without it there are none. The
stable pictures are the same bytes either way (nothing drawn changes), and
the switch reaches the core config.

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

from block01.ui.step1_core_binding import core_config
from block01.ui.step1_viewer_mount import demo_budgets

PROBE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sraw_pixel_probe.py"
LOG_STAGES = {"fview", "draw", "avail"}


def test_switch_reaches_config_default_off(monkeypatch):
    monkeypatch.delenv("BLOCK01_A9_DRAW_LOG", raising=False)
    assert core_config(demo_budgets())["draw_log"] is False
    monkeypatch.setenv("BLOCK01_A9_DRAW_LOG", "1")
    assert core_config(demo_budgets())["draw_log"] is True


def _slide(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path / "r6.ome.tif"
    yy, xx = np.mgrid[0:2000, 0:1800]
    data = np.stack([((yy // 5 + xx // 3 + k * 50) % 256).astype(np.uint8) for k in range(2)])
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _run(tmp_path, slide, name, draw_log):
    out, trace = tmp_path / f"{name}.npz", tmp_path / f"{name}.jsonl"
    env = dict(os.environ, BLOCK01_A9_TILETRACE=str(trace))
    env.pop("BLOCK01_A9_DRAW_LOG", None)
    if draw_log:
        env["BLOCK01_A9_DRAW_LOG"] = "1"
    proc = subprocess.run([sys.executable, str(PROBE), "--backend", "rust", "--slide", slide,
                           "--out", str(out), "--smooth", "0"], capture_output=True, text=True,
                          timeout=600, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result["binding"] == "Step1CoreBinding", result
    assert all(c["stable"] for c in result["cases"]), result
    records = [json.loads(l) for l in open(trace)] if trace.exists() else []
    return np.load(out), records


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
def test_draw_log_records_only(tmp_path):
    pytest.importorskip("fusion_viewer_core")
    slide = _slide(tmp_path)
    off_px, off = _run(tmp_path, slide, "off", False)
    on_px, on = _run(tmp_path, slide, "on", True)
    assert off, "the tile trace itself recorded nothing"
    assert not LOG_STAGES & {r["s"] for r in off}
    stages = {r["s"] for r in on}
    assert {"fview", "draw"} <= stages, stages
    views = {int(r["frame"]): r for r in on if r["s"] == "fview"}
    draws = [r for r in on if r["s"] == "draw"]
    # every drawn tile is of a shown channel and belongs to a logged frame
    for r in draws:
        assert r["k"][0] in ("DAPI", "CD8")
    assert {int(r["frame"]) for r in draws} <= set(views)
    for name in off_px.files:
        assert np.array_equal(off_px[name], on_px[name]), name
