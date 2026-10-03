"""Block 4a: Step3's mask data layer (`core/step3_masks.py`).

  * runs: index and scanned directories de-duplicated; unfinished / failed
    runs, directories outside the workspace and runs of another ROI left
    out; newest first; the active mark and a stale active; the choice rule;
  * classification by METHOD: a nuclei-only method's primary output is the
    nucleus mask; an expansion has cells only; nuclear-guided has both;
    an unknown method shows nothing;
  * the full pyramid check: each defect is refused with its own reason;
  * coordinate sources: ROI mode (bbox from the ROI record, else the ROI's
    own meta), whole-image mode (shape = the workspace bbox), TIFF-only and
    mismatches refused with a reason;
  * `read_label_tile` against an independent per-pixel reference on odd
    sizes and non-integer ratios, the world rect against
    `Step1GpuBinding._world_rect`, labels above 2^24, `Unavailable` on a
    coarse level without a pyramid;
  * `ensure_pyramid`: disk, memory, level 0 only, cancel;
  * `pyramid_path_for` is Step2's old naming, letter for letter;
  * `outline_reference` / `fill_colour` against hand-worked examples.

Synthetic projects in the test's temporary directory only.
"""

import json
import math
import os
import shutil
import stat
import subprocess
import sys
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.core import label_pyramid as lp  # noqa: E402
from block01.core import step3_masks as sm  # noqa: E402

SHAPES = [(401, 523), (101, 131), (26, 33)]      # ratios 3.970 / 3.992, 15.42 / 15.85
BBOX = (37, 37 + 176, 101, 101 + 208)            # origin not a multiple of a ratio
BIG = 2 ** 24                                    # float32 loses ids above this


# ── synthetic projects ───────────────────────────────────────────────────

def _slide(tmp_path, shapes=SHAPES):
    import tifffile
    path = str(tmp_path / "slide.ome.tif")
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(np.zeros((2,) + shapes[0], np.uint8), subifds=len(shapes) - 1,
                 tile=(64, 64), metadata={"axes": "CYX"})
        for s in shapes[1:]:
            tw.write(np.zeros((2,) + s, np.uint8), subfiletype=1, tile=(64, 64))
    return path


def _zarr(path, arr, dtype="<u4"):
    import zarr
    z = zarr.open(str(path), mode="w", shape=arr.shape, chunks=(64, 64), dtype=dtype)
    z[:] = arr
    return str(path)


def _labels(shape, seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 50, size=shape, dtype=np.uint32)
    arr[arr > 0] += BIG + 12345                  # every id above 2^24
    return arr


def _workspace(tmp_path, roi_id="roi_a", bbox=BBOX, name="ROI_1"):
    rdir = tmp_path / "rois" / roi_id
    (rdir / "step2" / "segmentation_runs").mkdir(parents=True)
    (rdir / "roi_manifest.json").write_text(json.dumps(
        {"roi_id": roi_id, "display_name": name, "bbox_fullres": list(bbox)}))
    (rdir / "roi_index.json").write_text(json.dumps({"version": 1, "roi_id": roi_id}))
    return str(rdir)


def _index(rdir, **fields):
    path = os.path.join(rdir, "roi_index.json")
    idx = json.load(open(path))
    for key, value in fields.items():
        if key == "runs":
            idx.setdefault("segmentation_runs", {}).update(value)
        else:
            idx[key] = value
    open(path, "w").write(json.dumps(idx))


def _run(rdir, run_id, method="cellpose_wholecell_fusion", *, mode="roi", roi_name="ROI_1",
         bbox=BBOX, created_at="2026-09-26T10:00:00", nuclei=False, pyramid=True,
         shapes=SHAPES, seed=0, roi_id="roi_a", index=True, status="done",
         alias="link", rec_bbox=True):
    """One finished Step2 run as Step2 writes it (block N's layout; block RM:
    a segment run in the workspace's runs/, published when `status` is
    done)."""
    folder = run_id if run_id.startswith("segment_") else f"segment_{run_id}"
    run_dir = os.path.join(rdir, "runs", folder)
    os.makedirs(run_dir)
    shape = (bbox[1] - bbox[0], bbox[3] - bbox[2])
    arr = _labels(shape, seed)
    suffix = f"_{roi_name}" if mode == "roi" else ""
    mask = _zarr(os.path.join(run_dir, f"global_mask{suffix}.zarr"), arr)
    nuc = nuc_arr = None
    if nuclei:
        nuc_arr = _labels(shape, seed + 1)
        nuc = _zarr(os.path.join(run_dir, f"global_nuclei_mask{suffix}.zarr"), nuc_arr)
    pyrs = {"cell": None, "nucleus": None}
    if pyramid:
        for kind, src in (("cell", mask), ("nucleus", nuc)):
            if src:
                pyrs[kind] = lp.build(src, lp.pyramid_path_for(src), shapes, bbox, kind)
    if mode == "roi":
        alias_path = os.path.join(run_dir, "global_mask.zarr")
        if alias == "link":
            os.symlink(os.path.basename(mask), alias_path)
        elif alias == "copy":
            shutil.copytree(mask, alias_path)
        entry = {"roi_name": roi_name, "roi_id": roi_id,
                 "zarr_path": alias_path if alias else mask,
                 "paths": {"mask_zarr": mask}, "label_pyramid": pyrs}
        if rec_bbox:
            entry["bbox_fullres"] = list(bbox)
        meta = {"mode": "roi", "run_id": run_id, "roi_id": roi_id, "method": method,
                "created_at": created_at, "rois": [entry],
                "label_pyramid": {roi_name: pyrs}}
        with open(os.path.join(run_dir, f"segmentation_meta_{roi_name}.json"), "w") as f:
            json.dump({"bbox": list(bbox), "roi_bbox_fullres": list(bbox)}, f)
    else:
        meta = {"mode": "full_wsi", "result_id": run_id, "method": method,
                "created_at": created_at, "zarr_path": mask,
                "paths": {"mask_zarr": mask}, "label_pyramid": pyrs}
    with open(os.path.join(run_dir, "segmentation_meta.json"), "w") as f:
        json.dump(meta, f)
    if status == "done":
        with open(os.path.join(run_dir, ".done"), "w") as f:
            f.write("{}")
    if index:
        _index(rdir, runs={run_id: {"run_id": run_id, "method": method,
                                    "created_at": created_at, "status": status,
                                    "path": os.path.relpath(run_dir, rdir)}},
               active_segmentation_run=run_id)
    return types.SimpleNamespace(dir=run_dir, mask=mask, arr=arr, nuc=nuc, nuc_arr=nuc_arr)


def _only(rdir):
    runs = sm.list_runs(rdir)
    assert len(runs) == 1
    return runs[0]


# ── runs ─────────────────────────────────────────────────────────────────

def test_runs_are_deduplicated_filtered_and_sorted(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_old", created_at="2026-09-26T08:00:00")
    _run(rdir, "seg_new", created_at="2026-09-26T12:00:00", index=False)   # scanned only
    _run(rdir, "seg_mid", created_at="2026-09-26T10:00:00")                # index + scan
    _run(rdir, "seg_failed", created_at="2026-09-26T13:00:00", status="failed")
    os.makedirs(os.path.join(rdir, "step2", "segmentation_runs", "seg_nometa"))
    _run(rdir, "seg_other_roi", created_at="2026-09-26T14:00:00", roi_id="roi_b",
         index=False)
    res_dir = os.path.join(rdir, "step2", "segmentation_results", "old_run")
    os.makedirs(res_dir)
    json.dump({"method": "cellpose_nuclei_dapi", "created_at": "2026-09-25T09:00:00"},
              open(os.path.join(res_dir, "run_metadata.json"), "w"))
    _index(rdir, active_segmentation_run="seg_mid")

    runs = sm.list_runs(rdir)
    ids = [r.run_id for r in runs]
    # block RM (§4, §11): published segment runs under runs/ only -- not a
    # failed one, not one naming another ROI, not the old step2/ folders
    assert ids == ["seg_new", "seg_mid", "seg_old"]
    assert [r.active for r in runs] == [False, True, False]
    assert all(r.run_dir == os.path.realpath(r.run_dir) for r in runs)


def test_a_stale_active_run_is_not_marked(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_a")
    _index(rdir, active_segmentation_run="seg_gone")
    assert [r.active for r in sm.list_runs(rdir)] == [False]
    assert sm.choose_run(sm.list_runs(rdir)).run_id == "seg_a"          # newest


def test_an_index_entry_outside_the_workspace_is_not_listed(tmp_path):
    rdir = _workspace(tmp_path)
    other = _workspace(tmp_path, roi_id="roi_b")
    far = _run(other, "seg_far", roi_id="roi_a", index=False)          # claims roi_a
    _index(rdir, runs={"seg_far": {"run_id": "seg_far", "status": "done",
                                   "path": far.dir}})
    assert sm.list_runs(rdir) == []


def test_the_choice_rule(tmp_path):
    rdir = _workspace(tmp_path)
    a = _run(rdir, "seg_a", created_at="2026-09-26T08:00:00")
    _run(rdir, "seg_b", created_at="2026-09-26T09:00:00")
    _run(rdir, "seg_c", created_at="2026-09-26T10:00:00")
    _index(rdir, active_segmentation_run="seg_b")
    runs = sm.list_runs(rdir)
    assert sm.choose_run([]) is None
    assert sm.choose_run(runs, requested_dir=a.dir).run_id == "seg_a"
    assert sm.choose_run(runs, requested_dir=str(tmp_path), current="seg_c").run_id == "seg_c"
    assert sm.choose_run(runs, current="seg_gone").run_id == "seg_b"          # active
    other = _workspace(tmp_path, roi_id="roi_b")
    far = _run(other, "seg_far", roi_id="roi_b")
    assert sm.choose_run(runs, requested_dir=far.dir).run_id == "seg_b"
    no_active = [r for r in runs if not r.active]
    assert sm.choose_run(no_active).run_id == "seg_c"                        # newest


# ── classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method,cell,nucleus", [
    ("cellpose_wholecell_fusion", "primary", None),
    ("mesmer_whole_cell", "primary", None),
    ("cellpose_nuclei_dapi", None, "primary"),
    ("stardist_nuclei_dapi", None, "primary"),
    ("mesmer_nuclei", None, "primary"),
    ("cellpose_nuclei_expansion", "primary", None),
    ("stardist_nuclei_expansion", "primary", None),
    ("mesmer_nuclear_guided", "primary", "nuclei"),
    ("cellpose_nuclei_hq", "primary", "nuclei"),
])
def test_masks_are_classified_by_method(tmp_path, method, cell, nucleus):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", method, nuclei=(nucleus == "nuclei"))
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    files = {"primary": r.mask, "nuclei": r.nuc}
    for kind, want in (("cell", cell), ("nucleus", nucleus)):
        if want is None:
            assert got[kind] is None and got["reasons"][kind]
        else:
            src = got[kind]
            assert src.kind == kind
            assert src.mask_path == os.path.realpath(files[want])
            assert src.pyramid is not None, src.pyramid_reason
            assert src.pyramid_kind == ("cell" if want == "primary" else "nucleus")


def test_an_unknown_method_shows_nothing(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x", "some_new_method")
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is None and got["nucleus"] is None
    assert "unknown method" in got["reasons"]["cell"]


def test_a_nuclear_guided_run_without_its_nuclei_file_keeps_the_cells(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x", "mesmer_nuclear_guided", nuclei=False)
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is not None and got["nucleus"] is None
    assert "missing" in got["reasons"]["nucleus"]


# ── the full pyramid check ───────────────────────────────────────────────

def _pyramid_reason(rdir, shapes=SHAPES):
    return sm.resolve_masks(_only(rdir), "ROI_1", BBOX, shapes)["cell"].pyramid_reason


def _pyr_group(r):
    import zarr
    return zarr.open_group(lp.pyramid_path_for(r.mask), mode="r+")


def test_a_whole_pyramid_passes(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x")
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    assert src.pyramid is not None and src.pyramid.where == "disk"
    assert src.pyramid_reason is None


def test_every_pyramid_defect_is_refused_with_its_own_reason(tmp_path):
    reasons = []

    def case(name, spoil, shapes=SHAPES):
        rdir = _workspace(tmp_path / name)
        r = _run(rdir, "seg_x")
        spoil(r)
        why = _pyramid_reason(rdir, shapes)
        assert why, name
        reasons.append(why)

    def missing_level(r):
        g = _pyr_group(r)
        del g["2"]

    def wrong_shape(r):
        g = _pyr_group(r)
        g.create_dataset("1", shape=(3, 3), dtype="<u4", overwrite=True)

    def wrong_dtype(r):
        g = _pyr_group(r)
        shape = g["2"].shape
        g.create_dataset("2", shape=shape, dtype="<u2", overwrite=True)

    def other_mask(r):
        other = _zarr(os.path.join(r.dir, "global_mask_other.zarr"), r.arr)
        dest = lp.pyramid_path_for(r.mask)
        shutil.rmtree(dest)
        lp.build(other, dest, SHAPES, BBOX, "cell")

    def other_bbox(r):
        _pyr_group(r).attrs["roi_bbox"] = [0, 176, 0, 208]

    case("missing_level", missing_level)
    case("wrong_shape", wrong_shape)
    case("wrong_dtype", wrong_dtype)
    case("other_mask", other_mask)
    case("other_bbox", other_bbox)
    case("other_levels", lambda r: None, shapes=[(401, 523), (101, 131)])
    assert len(set(reasons)) == len(reasons), reasons


def test_an_incomplete_pyramid_is_not_used(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x")
    _pyr_group(r).attrs["complete"] = False
    assert "incomplete" in _pyramid_reason(rdir)


# ── coordinate sources ───────────────────────────────────────────────────

def test_the_roi_bbox_comes_from_the_roi_record_or_its_own_meta(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x", rec_bbox=False)
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    assert src.bbox == BBOX and src.pyramid is not None


def test_a_bbox_that_is_not_the_current_rois_is_refused(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x")
    got = sm.resolve_masks(_only(rdir), "ROI_1", (0, 176, 0, 208), SHAPES)
    assert got["cell"] is None and "current ROI" in got["reasons"]["cell"]


def test_a_run_without_the_current_roi_is_refused(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x")
    got = sm.resolve_masks(_only(rdir), "ROI_2", BBOX, SHAPES)
    assert got["cell"] is None and "ROI_2" in got["reasons"]["cell"]


def test_the_alias_copy_is_not_mistaken_for_the_mask(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", alias="copy")
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    assert src.mask_path == os.path.realpath(r.mask) and src.pyramid is not None


def test_a_run_from_before_block_n_has_its_mask_without_a_pyramid(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x", pyramid=False)
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    assert src is not None and src.pyramid is None and src.pyramid_reason == "no label pyramid"
    assert sm.read_label_tile(src, 0, 0, 0, 64, SHAPES).labels.shape == (64, 64)


def test_a_whole_image_run(tmp_path):
    whole = (0, 401, 0, 523)
    rdir = _workspace(tmp_path, bbox=whole, name="Full WSI")
    _run(rdir, "seg_x", mode="full_wsi", bbox=whole)
    src = sm.resolve_masks(_only(rdir), "Full WSI", whole, SHAPES)["cell"]
    assert src.bbox == whole and src.pyramid is not None
    got = sm.resolve_masks(_only(rdir), "Full WSI", (0, 400, 0, 523), SHAPES)
    assert got["cell"] is None and "region" in got["reasons"]["cell"]


def test_a_tiff_only_run_is_refused(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", alias=None)
    shutil.rmtree(r.mask)
    open(os.path.join(r.dir, "global_mask_ROI_1.ome.tiff"), "wb").close()
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is None and "OME-TIFF" in got["reasons"]["cell"]


# ── tiles ────────────────────────────────────────────────────────────────

def _reference_tile(arr, bbox, level, tx, ty, T, shapes):
    """Per pixel from the viewer's own ratios: level-L pixel (a, b) shows the
    mask at slide pixel (floor((a + .5) * ds_y), floor((b + .5) * ds_x)),
    level 0 the pixel itself; 0 outside the ROI."""
    h, w = shapes[level]
    ds_y, ds_x = shapes[0][0] / h, shapes[0][1] / w
    rows = range(ty * T, min(ty * T + T, h))
    cols = range(tx * T, min(tx * T + T, w))
    out = np.zeros((len(rows), len(cols)), np.uint32)
    y0, y1, x0, x1 = bbox
    for i, a in enumerate(rows):
        y = a if level == 0 else int(math.floor((a + 0.5) * ds_y))
        if not y0 <= y < y1:
            continue
        for j, b in enumerate(cols):
            x = b if level == 0 else int(math.floor((b + 0.5) * ds_x))
            if x0 <= x < x1:
                out[i, j] = arr[y - y0, x - x0]
    return out


def _binding_world_rect(slide, level, tx, ty, T, shape):
    from block01.ui.step1_gpu_binding import Step1GpuBinding
    from block01.viewer.raw_tile_provider import RawTileProvider
    fake = types.SimpleNamespace(provider=RawTileProvider(slide))
    key = types.SimpleNamespace(tile=types.SimpleNamespace(
        level=level, tx=tx, ty=ty, grid=types.SimpleNamespace(tile_size=T)))
    return Step1GpuBinding._world_rect(fake, key, shape)


@pytest.mark.parametrize("bbox", [BBOX, (250, 401, 390, 523), (0, 401, 0, 523)])
def test_every_tile_of_every_level_matches_the_reference(tmp_path, bbox):
    slide = _slide(tmp_path)
    from block01.viewer.raw_tile_provider import RawTileProvider
    rp = RawTileProvider(slide)
    shapes = [tuple(rp.level_shape(lvl)) for lvl in range(len(SHAPES))]
    assert shapes == SHAPES
    rdir = _workspace(tmp_path, bbox=bbox)
    r = _run(rdir, "seg_x", bbox=bbox)
    src = sm.resolve_masks(_only(rdir), "ROI_1", bbox, shapes)["cell"]
    T = 48
    for level, (h, w) in enumerate(shapes):
        for ty in range(math.ceil(h / T)):
            for tx in range(math.ceil(w / T)):
                tile = sm.read_label_tile(src, level, tx, ty, T, shapes)
                want = _reference_tile(r.arr, bbox, level, tx, ty, T, shapes)
                assert tile.labels.dtype == np.uint32
                np.testing.assert_array_equal(tile.labels, want)
                assert tile.world_rect == pytest.approx(
                    _binding_world_rect(slide, level, tx, ty, T, tile.labels.shape), abs=0)
    assert r.arr.max() > BIG


def test_a_coarse_level_without_a_pyramid_is_unavailable(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", pyramid=False)
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    for level in (1, 2):
        got = sm.read_label_tile(src, level, 0, 0, 64, SHAPES)
        assert isinstance(got, sm.Unavailable) and got.reason
    tile = sm.read_label_tile(src, 0, 2, 1, 64, SHAPES)
    np.testing.assert_array_equal(tile.labels, _reference_tile(r.arr, BBOX, 0, 2, 1, 64, SHAPES))
    assert isinstance(sm.read_label_tile(src, 0, 99, 0, 64, SHAPES), sm.Unavailable)


# ── ensure_pyramid ───────────────────────────────────────────────────────

def _bare(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", pyramid=False)
    return r, sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]


def test_ensure_writes_into_the_run_directory(tmp_path):
    r, src = _bare(tmp_path)
    res = sm.ensure_pyramid(src, SHAPES)
    assert res.status == "ready" and res.source.pyramid.where == "disk"
    assert os.path.isdir(lp.pyramid_path_for(r.mask))
    assert sm.ensure_pyramid(res.source, SHAPES).source is res.source        # nothing to do
    tile = sm.read_label_tile(res.source, 1, 1, 0, 64, SHAPES)
    np.testing.assert_array_equal(tile.labels, _reference_tile(r.arr, BBOX, 1, 1, 0, 64, SHAPES))


@pytest.fixture
def read_only_dir():
    made = []

    def lock(path):
        os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
        made.append(path)
    yield lock
    for path in made:
        os.chmod(path, stat.S_IRWXU)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anyway")
def test_ensure_falls_back_to_memory_when_the_directory_is_read_only(tmp_path, read_only_dir):
    r, src = _bare(tmp_path)
    before = sorted(os.listdir(r.dir))
    read_only_dir(r.dir)
    res = sm.ensure_pyramid(src, SHAPES)
    assert res.status == "ready" and res.source.pyramid.where == "memory"
    assert "cannot be written" in res.source.pyramid_reason
    assert sorted(os.listdir(r.dir)) == before
    for level in (1, 2):
        tile = sm.read_label_tile(res.source, level, 0, 0, 64, SHAPES)
        np.testing.assert_array_equal(tile.labels,
                                      _reference_tile(r.arr, BBOX, level, 0, 0, 64, SHAPES))


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anyway")
def test_ensure_leaves_level_0_only_when_memory_fails_too(tmp_path, read_only_dir, monkeypatch):
    r, src = _bare(tmp_path)
    read_only_dir(r.dir)

    def no_memory(*_a, **_k):
        raise MemoryError("simulated")
    monkeypatch.setattr(lp, "build_in_memory", no_memory)
    res = sm.ensure_pyramid(src, SHAPES)
    assert res.status == "level0_only" and res.source.pyramid is None
    assert "cannot be written" in res.reason and "MemoryError" in res.reason
    assert isinstance(sm.read_label_tile(res.source, 1, 0, 0, 64, SHAPES), sm.Unavailable)


def test_a_cancel_ends_it_without_the_memory_fallback(tmp_path, monkeypatch):
    r, src = _bare(tmp_path)
    tried = []
    monkeypatch.setattr(lp, "build_in_memory", lambda *a, **k: tried.append(1))
    res = sm.ensure_pyramid(src, SHAPES, cancel_check=lambda: True)
    dest = lp.pyramid_path_for(r.mask)
    assert res.status == "cancelled" and not tried
    assert not os.path.exists(dest) and not os.path.exists(dest + ".partial")


def test_a_pyramid_that_read_accepts_is_never_replaced(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x")                          # built for SHAPES
    dest = lp.pyramid_path_for(r.mask)
    before = json.load(open(os.path.join(dest, ".zattrs")))
    other = [(401, 523), (101, 131)]                 # a viewer with other levels
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, other)["cell"]
    assert src.pyramid is None
    for cancel in (None, lambda: True):
        res = sm.ensure_pyramid(src, other, cancel_check=cancel)
        assert res.status == "level0_only" and "kept" in res.reason
    assert json.load(open(os.path.join(dest, ".zattrs"))) == before


def test_a_stale_pyramid_is_rebuilt(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x")
    dest = lp.pyramid_path_for(r.mask)
    _pyr_group(r).attrs["complete"] = False
    src = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)["cell"]
    res = sm.ensure_pyramid(src, SHAPES)
    assert res.status == "ready" and lp.read(dest) is not None


# ── naming ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "global_mask_ROI_1.zarr", "global_nuclei_mask_ROI_1.zarr",
    "global_mask.zarr", "global_nuclei_mask.zarr", "global_mask_Full WSI.zarr",
])
def test_the_pyramid_name_is_step2s_old_rule(name):
    src = os.path.join("/runs/seg_1", name)
    stem = os.path.basename(src)[:-len(".zarr")]
    old = os.path.join(os.path.dirname(src),
                       stem.replace("global_nuclei_mask", "label_pyramid_nuclei")
                           .replace("global_mask", "label_pyramid") + ".zarr")
    assert lp.pyramid_path_for(src) == old


def test_the_in_memory_build_equals_the_disk_build(tmp_path):
    import zarr
    arr = _labels((176, 208))
    src = _zarr(tmp_path / "m.zarr", arr)
    disk = zarr.open_group(lp.build(src, str(tmp_path / "p.zarr"), SHAPES, BBOX, "cell"), mode="r")
    mem = lp.build_in_memory(src, SHAPES, BBOX, "cell")
    for key in ("1", "2"):
        np.testing.assert_array_equal(np.asarray(mem[key]), np.asarray(disk[key]))
    da, ma = dict(disk.attrs), dict(mem.attrs)
    assert ma["level0"]["path"] == os.path.abspath(src)
    da.pop("level0"), ma.pop("level0")
    assert da == ma
    assert sorted(os.listdir(tmp_path)) == ["m.zarr", "p.zarr"]


def test_the_module_is_qt_free():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import importlib.util as u, sys; "
            "s = u.spec_from_file_location('block01', '__init__.py', "
            "submodule_search_locations=['.']); p = u.module_from_spec(s); "
            "sys.modules['block01'] = p; s.loader.exec_module(p); "
            "import block01.core.step3_masks as m; "
            "print(m.__file__, any(k.startswith('PyQt') for k in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root)
    assert out.returncode == 0, out.stderr
    path, qt = out.stdout.split()[-2:]
    assert os.path.dirname(os.path.dirname(os.path.realpath(path))) == os.path.realpath(root)
    assert qt == "False"


# ── display reference ────────────────────────────────────────────────────

def test_outline_one_cell_width_0_and_1():
    ids = np.zeros((5, 5), np.uint32)
    ids[1:4, 1:4] = 7
    assert not sm.outline_reference(ids, 0).any()
    want = np.zeros((5, 5), bool)
    want[1:4, 1:4] = True
    want[2, 2] = False
    np.testing.assert_array_equal(sm.outline_reference(ids, 1), want)


def test_outline_two_neighbours_and_the_image_edge():
    ids = np.ones((5, 8), np.uint32)
    ids[:, 4:] = 2
    want = np.ones((5, 8), bool)
    want[1:4, [1, 2, 5, 6]] = False
    np.testing.assert_array_equal(sm.outline_reference(ids, 1), want)


def test_outline_neighbourhood_is_square_not_diamond():
    ids = np.ones((7, 7), np.uint32)
    ids[3, 3] = 2                                # (2, 2) sees it only diagonally
    want = np.zeros((7, 7), bool)
    want[[0, -1], :] = want[:, [0, -1]] = True   # the image edge
    want[2:5, 2:5] = True
    np.testing.assert_array_equal(sm.outline_reference(ids, 1), want)


def test_outline_width_4_and_zero_is_never_drawn():
    ids = np.ones((11, 11), np.uint32)
    want = np.ones((11, 11), bool)
    want[4:7, 4:7] = False                        # window of 9 x 9 inside the image
    np.testing.assert_array_equal(sm.outline_reference(ids, 4), want)
    ids = np.zeros((6, 6), np.uint32)
    ids[0, 0] = 3
    got = sm.outline_reference(ids, 2)
    assert got[0, 0] and got.sum() == 1
    with pytest.raises(ValueError):
        sm.outline_reference(ids, 9)


def test_outline_radius_8_for_a_high_dpi_screen():
    ids = np.ones((19, 19), np.uint32)
    want = np.ones((19, 19), bool)
    want[8:11, 8:11] = False                      # window of 17 x 17 inside the image
    np.testing.assert_array_equal(sm.outline_reference(ids, 8), want)


def _lowbias32(x):
    x &= 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x7FEB352D) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x846CA68B) & 0xFFFFFFFF
    x ^= x >> 16
    return x


def test_fill_colours_are_fixed_per_id():
    ids = np.array([[0, 1, BIG + 5], [1, 2 ** 32 - 1, 0]], np.uint32)
    rgba = sm.fill_colour(ids)
    assert rgba.shape == (2, 3, 4) and rgba.dtype == np.uint8
    assert (rgba[ids == 0] == 0).all()
    np.testing.assert_array_equal(rgba[0, 1], rgba[1, 0])
    for v in (1, BIG + 5, 2 ** 32 - 1):
        h = _lowbias32(v)
        want = [55 + (((h >> s) & 0xFF) * 200) // 255 for s in (0, 8, 16)] + [255]
        got = rgba[ids == v][0]
        assert list(got) == want
    assert not (rgba[0, 1] == rgba[0, 2]).all()


# ── the label store (block N2) ───────────────────────────────────────────

def _with_store(rdir, r, store):
    meta_path = os.path.join(r.dir, "segmentation_meta.json")
    meta = json.load(open(meta_path))
    meta["rois"][0]["label_store"] = store
    json.dump(meta, open(meta_path, "w"))


def _array(path):
    return {"path": path, "dtype": "uint32", "shape": [176, 208], "chunks": [1024, 1024],
            "n_objects": 1}


def test_the_label_store_decides_what_each_array_is(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", "cellpose_wholecell_fusion", nuclei=True)   # method: no nuclei
    _with_store(rdir, r, {"version": 1, "complete": True,
                          "cell": _array(r.mask), "nucleus": _array(r.nuc),
                          "nucleus_to_cell": {"path": "t", "length": 2}, "relation": {}})
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"].mask_path == os.path.realpath(r.mask)
    assert got["nucleus"].mask_path == os.path.realpath(r.nuc)
    assert got["nucleus"].pyramid is not None and got["nucleus"].pyramid_kind == "nucleus"
    # a nuclei-only store: the primary file IS the nucleus mask
    _with_store(rdir, r, {"version": 1, "complete": True, "cell": None,
                          "nucleus": _array(r.mask)})
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is None and "no cell mask" in got["reasons"]["cell"]
    assert got["nucleus"].mask_path == os.path.realpath(r.mask)
    assert got["nucleus"].pyramid is not None and got["nucleus"].pyramid_kind == "cell"


def test_an_incomplete_label_store_shows_nothing(tmp_path):
    rdir = _workspace(tmp_path)
    r = _run(rdir, "seg_x", "mesmer_nuclear_guided", nuclei=True)
    _with_store(rdir, r, {"version": 1, "complete": False, "cell": _array(r.mask),
                          "nucleus": _array(r.nuc)})
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is None and got["nucleus"] is None
    assert "incomplete" in got["reasons"]["cell"] and "re-run Step2" in got["reasons"]["nucleus"]


def test_an_expansion_run_from_before_n2_says_re_run(tmp_path):
    rdir = _workspace(tmp_path)
    _run(rdir, "seg_x", "cellpose_nuclei_expansion")
    got = sm.resolve_masks(_only(rdir), "ROI_1", BBOX, SHAPES)
    assert got["cell"] is not None and got["nucleus"] is None
    assert got["reasons"]["nucleus"] == "this run was made before nuclei were kept — re-run Step2"
