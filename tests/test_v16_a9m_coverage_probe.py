"""Block A9-M: the coverage probe judges the frame by the planes that were
submitted and where their pixels are valid -- never by colour."""

import time
from types import SimpleNamespace

import numpy as np

from block01.viewer import coverage_probe as cp


def _plane(ident, rect, valid=None, shape=(32, 32)):
    values = np.ones(shape, np.float32)
    if valid is not None:
        values = np.where(valid, values, np.nan).astype(np.float32)
    return SimpleNamespace(identity=ident, world_rect=rect, values=values, valid=valid)


def _source(coarse=(), fine=(), level="coarse"):
    return SimpleNamespace(coarse=tuple(coarse), fine=tuple(fine), selected_level=level)


VIEW = (0.0, 100.0, 0.0, 100.0)


def test_a_fully_covered_frame_has_no_gap():
    s = {"A": _source([_plane("c", (0, 100, 0, 100))])}
    r = cp.gpu_frame(s, VIEW)
    # zoomed out the coarse level IS the target (codex A9-M)
    assert r["gap_cells"] == 0 and r["coarse_complete"] and r["target_fraction"] == 1.0


def test_a_hole_in_the_coarse_plane_is_a_gap_and_black_pixels_are_not():
    valid = np.ones((32, 32), bool)
    valid[:, 16:] = False                              # right half: no data
    s = {"A": _source([_plane("half", (0, 100, 0, 100), valid=valid)])}
    r = cp.gpu_frame(s, VIEW)
    assert r["gap_cells"] > 0 and not r["coarse_complete"]
    dark = _plane("dark", (0, 100, 0, 100))
    dark.values[:] = 0.0                               # black is real data
    assert cp.gpu_frame({"A": _source([dark])}, VIEW)["gap_cells"] == 0


def test_the_target_level_is_the_selected_one():
    coarse = [_plane("c2", (0, 100, 0, 100))]
    fine = [_plane("f1", (0, 50, 0, 100)), _plane("f2", (50, 100, 0, 100))]
    assert cp.gpu_frame({"A": _source(coarse, fine, "fine")}, VIEW)["target_fraction"] == 1.0
    assert cp.gpu_frame({"A": _source(coarse, fine[:1], "fine")}, VIEW)["target_fraction"] == 0.5
    assert cp.gpu_frame({"A": _source(coarse, fine, "coarse")}, VIEW)["target_fraction"] == 1.0
    # fine selected but none arrived yet: covered by coarse, target not reached
    r = cp.gpu_frame({"A": _source(coarse, (), "fine")}, VIEW)
    assert r["gap_cells"] == 0 and r["target_fraction"] == 0.0


def test_a_channel_the_frame_should_compose_but_has_no_source_is_a_gap():
    s = {"A": _source([_plane("a2", (0, 100, 0, 100))])}
    r = cp.gpu_frame(s, VIEW, expected=("A", "B"))
    assert r["channels"] == 2 and r["gap_cells"] == cp.GRID * cp.GRID


def test_the_region_polygon_excludes_what_is_outside_it():
    tri = [(0, 0), (100, 0), (0, 100)]                 # lower-left half
    s = {"A": _source([_plane("lower", (0, 50, 0, 50))])}
    assert cp.gpu_frame(s, VIEW)["gap_cells"] > 0
    r = cp.gpu_frame({"A": _source([_plane("tri", (0, 100, 0, 100))])}, VIEW, polygon=tri)
    assert r["gap_cells"] == 0
    hole = np.ones((32, 32), bool)
    hole[24:, 24:] = False                             # outside the triangle only
    r = cp.gpu_frame({"A": _source([_plane("h", (0, 100, 0, 100), valid=hole)])}, VIEW,
                     polygon=tri)
    assert r["gap_cells"] == 0


def test_the_viewport_is_clipped_to_the_region():
    s = {"A": _source([_plane("roi", (20, 60, 20, 60))])}
    assert cp.gpu_frame(s, VIEW)["gap_cells"] > 0
    assert cp.gpu_frame(s, VIEW, roi_world=(20, 60, 20, 60))["gap_cells"] == 0


def test_every_channel_must_cover():
    s = {"A": _source([_plane("a", (0, 100, 0, 100))]), "B": _source([])}
    assert cp.gpu_frame(s, VIEW)["gap_cells"] == cp.GRID * cp.GRID


def test_coverage_is_reported_as_sampled():
    assert cp.gpu_frame({"A": _source([_plane("s", (0, 100, 0, 100))])}, VIEW)["sampled"] == cp.SAMPLES == 128


def test_cpu_frames_use_visible_tiles_and_the_floor():
    r = cp.rect_frame({2: [(0, 50, 0, 100)]}, 2, VIEW)
    assert r["gap_cells"] > 0 and r["target_fraction"] == 0.5
    r = cp.rect_frame({2: [(0, 50, 0, 100)]}, 2, VIEW, floor_rects=[(0, 100, 0, 100)])
    assert r["gap_cells"] == 0 and r["coarse_complete"]


def test_the_cost_per_frame_is_bounded():
    planes = [_plane(("p", i), (i % 20 * 5, i % 20 * 5 + 5, i // 20 * 5, i // 20 * 5 + 5))
              for i in range(400)]
    s = {"A": _source(planes), "B": _source(planes)}
    cp.gpu_frame(s, VIEW)                               # summaries once
    t = time.perf_counter()
    for _ in range(5):
        cp.gpu_frame(s, VIEW)
    assert (time.perf_counter() - t) / 5 < 0.05        # 800 planes, well under a frame budget


def test_a_sixteen_channel_frame_costs_a_few_milliseconds_at_most():
    """Neutrality (the first baseline measured 27 ms per frame inside
    `gpu.submit` with 16 channels -- the probe was the stall)."""
    def plane(i, rect):
        return _plane(i, rect, shape=(512, 512))
    srcs = {}
    for c in range(16):
        coarse = [plane((c, "c"), (0, 30000, 0, 30000))]
        fine = [plane((c, k), (k % 4 * 2000, k % 4 * 2000 + 2000, k // 4 * 2000,
                               k // 4 * 2000 + 2000)) for k in range(12)]
        srcs[c] = _source(coarse, fine, "fine")
    cp.gpu_frame(srcs, (0, 8000, 0, 6000))
    t = time.perf_counter()
    for _ in range(20):
        cp.gpu_frame(srcs, (0, 8000, 0, 6000))
    assert (time.perf_counter() - t) / 20 < 0.005
