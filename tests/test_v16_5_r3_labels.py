"""v16_5 R3 plan §2.1, §3: Step3's masks drawn by the Rust layer.

`scripts/r3_label_probe.py` opens a Step3-kind viewer (it draws masks) on
the REAL mount, once per backend, and draws the cases of
`test_step3_label_render` over the picture: the tiles and styles there, and
with `--clip` its concave region. Here:
  * the Rust results meet the same checks as the Python layer's tests --
    the id image equals the INDEPENDENT reference, a later plane covers an
    earlier one with its zeros, outlines equal `outline_reference`, fills
    `fill_colour` and blend, the nucleus layer lies on top, the region
    clips the masks like the picture, a label-only change composes no
    channel, no masks leaves the picture as it is, the budget holds;
  * Rust and Python give the very same id images and presented pictures.
Device pixel ratio 1 only: the real screen's (the Python layer's tests
cover 1.5 and 2 with a synthetic viewport).
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "r3_label_probe.py"
sys.path.insert(0, str(ROOT / "tests"))
from test_step3_label_render import _tiles, reference_ids  # noqa: E402
from block01.core.step3_masks import fill_colour, outline_reference  # noqa: E402


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path_factory.mktemp("r3_lab") / "synth3.ome.tif"
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


def _probe(backend, slide, out, *extra):
    pytest.importorskip("fusion_viewer_core")
    proc = subprocess.run([sys.executable, str(PROBE), "--backend", backend, "--slide", slide,
                           "--out", str(out), *extra], capture_output=True, text=True, timeout=300,
                          env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{backend}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
    return result, np.load(out)


@pytest.fixture(scope="module")
def runs(slide, tmp_path_factory):
    d = tmp_path_factory.mktemp("r3_lab_runs")
    got = {}
    for backend in ("rust", "python"):
        got[backend] = _probe(backend, slide, d / f"{backend}.npz")
        got[backend + "_clip"] = _probe(backend, slide, d / f"{backend}_clip.npz", "--clip")
    assert got["rust"][0]["binding"] == "Step1CoreBinding"
    assert got["python"][0]["binding"] == "Step1GpuBinding"
    return got


def _vp(result, shot):
    x0, x1, y0, y1 = result["view"]
    return type("VP", (), {"world_rect": (x0, x1, y0, y1)})(), (shot.shape[1], shot.shape[0])


def test_a_rust_id_image_equals_the_independent_reference(runs):
    result, saved = runs["rust"]
    vp, size = _vp(result, saved["picture"])
    planes = _tiles()
    want = reference_ids(planes, vp, size)
    np.testing.assert_array_equal(saved["ids_ids"], want)
    assert saved["ids_ids"].max() > 2 ** 24 and (saved["ids_ids"] == 0).any()


def test_b_rust_later_plane_covers_an_earlier_one(runs):
    from block01.ui.step1_gpu_layer import LabelPlane
    result, saved = runs["rust"]
    vp, size = _vp(result, saved["picture"])
    old = _tiles(seed=1)
    x0, y0 = old[0].world_rect[0], old[0].world_rect[2]
    new_ids = np.zeros((5, 7), np.uint32)
    new_ids[1:3, 1:4] = 2 ** 24 + 99
    new = LabelPlane(("new",), (x0 + 50.0, x0 + 400.0, y0 + 30.0, y0 + 300.0), new_ids)
    np.testing.assert_array_equal(saved["overwrite_ids"], reference_ids(old + (new,), vp, size))


@pytest.mark.parametrize("width", [0, 1, 2, 4])
def test_c_rust_outlines_equal_the_reference(runs, width):
    result, saved = runs["rust"]
    vp, size = _vp(result, saved["picture"])
    ids = reference_ids(_tiles(), vp, size)
    edge = outline_reference(ids, width)                       # radius at ratio 1
    shown, picture = saved[f"outline{width}_shown"], saved["picture"]
    np.testing.assert_array_equal(shown[edge][:, :3], np.tile([255, 0, 255], (edge.sum(), 1)))
    np.testing.assert_array_equal(shown[~edge], picture[~edge])
    if width:
        assert edge.any()


@pytest.mark.parametrize("backend", ["rust", "python"])
@pytest.mark.parametrize("width", [1, 2])
def test_c2_dashed_outlines_equal_the_reference(runs, backend, width):
    """Multi-result plan §4: the dashed outline on both backends is the CPU
    reference's (`outline_reference(..., dash=)`), up to the pixels a float
    rounding puts on the other side of a dash end."""
    from block01.core.step3_masks import DASH_DUTY, dash_pattern
    result, saved = runs[backend]
    vp, size = _vp(result, saved["picture"])
    ids = reference_ids(_tiles(), vp, size)
    period, phase = dash_pattern(width, vp.world_rect, size)
    want = outline_reference(ids, width, dash=(period, DASH_DUTY, phase))
    solid = outline_reference(ids, width)
    got = np.all(saved[f"dash{width}_shown"][..., :3] == [255, 0, 255], axis=-1) & solid
    assert want.sum() > 0.3 * solid.sum() and want.sum() < 0.9 * solid.sum(), "not dashed"
    assert (got != want).sum() <= max(3, 0.01 * solid.sum()), (got != want).sum()
    picture = saved["picture"]
    np.testing.assert_array_equal(saved[f"dash{width}_shown"][~solid], picture[~solid])


def test_d_rust_fills_blend_and_the_nucleus_lies_on_top(runs):
    result, saved = runs["rust"]
    vp, size = _vp(result, saved["picture"])
    ids = reference_ids(_tiles(), vp, size)
    inside, picture = ids != 0, saved["picture"]
    np.testing.assert_array_equal(saved["fill_shown"][inside][:, :3], fill_colour(ids)[inside][:, :3])
    np.testing.assert_array_equal(saved["fill_shown"][~inside], picture[~inside])
    want = 0.5 * fill_colour(ids)[..., :3].astype(float) + 0.5 * picture[..., :3].astype(float)
    assert np.abs(saved["fill_half_shown"].astype(int)[inside][:, :3] - want[inside]).max() <= 1
    edge = outline_reference(ids, 1)
    fill_only = inside & ~edge
    shown = saved["nucleus_shown"]
    np.testing.assert_array_equal(shown[edge][:, :3], np.tile([0, 0, 255], (edge.sum(), 1)))
    np.testing.assert_array_equal(shown[fill_only][:, :3], fill_colour(ids)[fill_only][:, :3])


def test_e_rust_region_clips_the_masks_like_the_picture(runs):
    _result, saved = runs["rust_clip"]
    picture = saved["picture"]
    outside = picture[..., 3] == 0
    assert outside.any() and (~outside).any()
    for name in ("clip_fill", "clip_outline", "clip_green"):
        np.testing.assert_array_equal(saved[f"{name}_shown"][outside], picture[outside])
    assert (saved["clip_fill_shown"][~outside] != picture[~outside]).any()


def test_f_rust_counters_no_masks_and_budget(runs):
    result, saved = runs["rust"]
    subs, comps = result["no_channel"]["image_submissions"], result["no_channel"]["label_compositions"]
    assert subs[1] == subs[0] and comps[1] == comps[0] + 3, result["no_channel"]
    np.testing.assert_array_equal(saved["none_shown"], saved["resized_picture"])   # drawn after the resize
    assert result["none_shown_flag"] is False
    assert "label budget" in result["budget_refused"]


@pytest.mark.parametrize("run", ["", "_clip"])
def test_g_rust_and_python_draw_the_same_pixels(runs, run):
    rust, python = runs["rust" + run][1], runs["python" + run][1]
    assert sorted(rust.files) == sorted(python.files)
    for key in python.files:
        np.testing.assert_array_equal(rust[key], python[key], err_msg=key)


def test_h_rust_resized_and_presented(runs):
    """A new size makes the targets again: the id image still equals the
    reference at the new size; what Qt presents (`grabFramebuffer`) is the
    presented target where the picture is opaque."""
    result, saved = runs["rust"]
    vp = type("VP", (), {"world_rect": tuple(result["resized_view"])})()
    shot = saved["resized_picture"]
    assert shot.shape != saved["picture"].shape
    np.testing.assert_array_equal(saved["resized_ids"], reference_ids(_tiles(), vp, (shot.shape[1], shot.shape[0])))
    for name in ("nucleus", "resized"):
        grab, shown = saved[f"{name}_grab"], saved[f"{name}_shown"]
        assert grab.shape == shown.shape
        opaque = shown[..., 3] == 255
        np.testing.assert_array_equal(grab[opaque][:, :3], shown[opaque][:, :3])


def test_i_gl_state_holds_around_every_paint_and_label_draw(slide):
    """Plan §5 gate 2: R0's full glGet snapshot before and after every
    `paintGL` AND every `set_labels` (masks drawn at once, alone) of a
    Step3-kind Rust viewer with a concave region: outline, fill, nucleus,
    across the region's edge, label-only changes, a resize, masks off --
    no state differs (`scripts/r3_gl_state_probe.py`)."""
    pytest.importorskip("fusion_viewer_core")
    probe = ROOT / "scripts" / "r3_gl_state_probe.py"
    proc = subprocess.run([sys.executable, str(probe), "--slide", slide], capture_output=True, text=True,
                          timeout=300, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1])
    assert result.get("ok") is True and proc.returncode == 0, result
