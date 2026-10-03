"""Block S4-1: Step4's quantification sources (`core/quant_sources.py`).

  * the job: labels from the LabelStore only (old runs and incomplete stores
    refused), the region's bbox, the run's own slide (another slide refused),
    a nuclei-only run quantified on its nuclei;
  * fail-closed channel sources: `original` / unlisted -> raw; tophat /
    cucim -> Step0's corrected product, every check refusing on its own
    (missing product, missing array, another ROI's group only, shape, dtype,
    method, parameter, channel index, source slide, disagreeing decisions);
    no fallback to raw pixels and no correction at run time;
  * the readers: tiled TIFF decode == tifffile's zarr store on odd sizes;
    a strip TIFF falls back to the store with the same pixels; labels carry
    a background ring outside the region; reads are counted by source.

Synthetic projects in the test's temporary directory only.
`build_project` is shared with the other S4-1 test modules.
"""

import json
import os
import shutil

import numpy as np
import pytest
import tifffile
import zarr

from block01.core import quant_sources as qs

NAMES = ["DAPI", "CD3", "CD8", "PanCK"]
SLIDE_SHAPE = (150, 170)
ROI_BBOX = [10, 140, 20, 150]          # y0, y1, x0, x1


def _blobs(shape, n, seed):
    """Random rectangles / discs of labels 1..n, some touching."""
    rng = np.random.default_rng(seed)
    lab = np.zeros(shape, np.uint32)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for k in range(1, n + 1):
        cy, cx = rng.integers(0, shape[0]), rng.integers(0, shape[1])
        if k % 2:
            r = rng.integers(2, 9)
            lab[(yy - cy) ** 2 + (xx - cx) ** 2 <= r * r] = k
        else:
            h, w = rng.integers(1, 12, size=2)
            lab[cy:cy + h, cx:cx + w] = k
    return lab


def write_slide(path, data, tiled=True):
    kw = dict(tile=(32, 32)) if tiled else dict(rowsperstrip=7)
    tifffile.imwrite(path, data, ome=True, photometric="minisblack", compression="lzw",
                     metadata={"axes": "CYX", "Channel": {"Name": list(NAMES[:data.shape[0]])}},
                     **kw)


def _nuclei_for(lab, n_cells, corrupt):
    """One nucleus per cell (a 3 x 3 core where the cell has one), a second
    one in every 5th cell; ids of their own. `corrupt` adds the two defects
    of runs made before Step2's seam fix: a nucleus reaching outside its
    cell, and a nucleus whose cell has no pixels left."""
    from scipy import ndimage as ndi
    nuc = np.zeros(lab.shape, np.uint32)
    table = [0]
    for k in range(1, n_cells + 1):
        core = ndi.binary_erosion(lab == k, iterations=1)
        yy, xx = np.nonzero(core)
        if yy.size == 0:
            continue
        cy, cx = int(np.median(yy)), int(np.median(xx))
        m = np.zeros(lab.shape, bool)
        m[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] = True
        m &= core
        if not m.any():
            continue
        nuc[m] = len(table)
        table.append(k)
        if k % 5 == 0 and yy.size > 20:
            m2 = np.zeros(lab.shape, bool)
            m2[yy[0], xx[0]] = True
            m2 &= core & (nuc == 0)
            if m2.any():
                nuc[m2] = len(table)
                table.append(k)
    if corrupt:
        # a nucleus reaching into background / another cell
        ids = [n for n in range(1, len(table)) if (nuc == n).sum() >= 4]
        n = ids[0]
        ys, xs = np.nonzero(nuc == n)
        y, x = ys.max() + 1, xs.max() + 1
        while y < lab.shape[0] and lab[y, xs.max()] == table[n]:
            y += 1
        if y < lab.shape[0]:
            nuc[y, xs.max()] = n
        # an orphan: its cell loses every pixel
        orphan_cell = table[ids[1]]
        lab[lab == orphan_cell] = 0
        nuc[(nuc == ids[1])] = ids[1]
        nuc[(lab == 0) & (nuc == ids[1])] = ids[1]
    return nuc, np.asarray(table, np.uint32)


def build_project(root, *, decisions=None, n_cells=40, seed=0, rois=None, nuclei_only=False,
                  label_store=True, tiled=True, corrected_value=None, nuclei=False,
                  corrupt_nuclei=False, seam_merge=True):
    """A project with one ROI workspace and one Step2 run. Returns a dict of
    paths and the arrays behind them."""
    root = str(root)
    rng = np.random.default_rng(seed)
    slide_data = rng.integers(0, 256, size=(len(NAMES),) + SLIDE_SHAPE, dtype=np.uint8)
    slide = os.path.join(root, "slide.ome.tif")
    write_slide(slide, slide_data, tiled=tiled)
    # Block RM: a project whose run chain is correct -> fuse -> segment.
    from block01.utils import run_store
    ws = os.path.join(root, "proj", "rois", "ws1")
    os.makedirs(ws)
    json.dump({}, open(os.path.join(root, "proj", "project_manifest.json"), "w"))
    step0 = os.path.join(ws, "runs", "correct_20260927_115900")
    fuse = os.path.join(ws, "runs", "fuse_20260927_115930")
    run_dir = os.path.join(ws, "runs", "segment_20260927_120000_test")
    os.makedirs(step0)
    os.makedirs(fuse)
    os.makedirs(run_dir)
    json.dump({"source_ome": slide, "bbox_fullres": [0, SLIDE_SHAPE[0], 0, SLIDE_SHAPE[1]],
               "display_name": "Full WSI"}, open(os.path.join(ws, "roi_manifest.json"), "w"))
    decisions = dict(decisions if decisions is not None
                     else {"CD3": "tophat", "CD8": "original", "PanCK": "original"})
    cfg = {"channel_decisions": decisions, "method_params": {"tophat_radius": 15,
                                                            "cucim_sigma": 50},
           "channel_params": {"CD3": {"tophat_radius": 10}}}
    cfg_path = os.path.join(step0, run_store.PARAMS)
    json.dump({"kind": "correct", "correction_config": cfg, "channel_decisions": decisions},
              open(cfg_path, "w"))
    corrected = {k: v for k, v in decisions.items() if v in ("tophat", "cucim")}
    zpath = os.path.join(step0, "corrected_channels.zarr")
    rois = rois or [("Full WSI", ROI_BBOX)]
    corrected_data = {}
    if corrected:
        root_g = zarr.open_group(zpath, mode="w")
        root_g.attrs.update({"mode": "roi_only", "source_ome": slide, "correction_config": cfg})
        for roi_name, bbox in rois:
            g = root_g.create_group(qs.region_folder(roi_name))
            g.attrs.update({"roi_name": roi_name, "bbox_fullres": list(bbox),
                            "shape": [bbox[1] - bbox[0], bbox[3] - bbox[2]]})
            for ch, method in corrected.items():
                shape = (bbox[1] - bbox[0], bbox[3] - bbox[2])
                arr = (rng.random(shape, dtype=np.float32) * 50 if corrected_value is None
                       else np.full(shape, corrected_value, np.float32))
                a = g.create_dataset(ch, data=arr, chunks=(64, 64))
                radius, sigma = (10 if ch == "CD3" else 15), 50
                a.attrs.update({"correction_method": method,
                                "correction_param_value": radius if method == "tophat" else sigma,
                                "channel_index": NAMES.index(ch), "source_identity": "abc",
                                "roi_name": roi_name})
                corrected_data[(roi_name, ch)] = arr
    run_store.write_inputs(step0, None, slide_id="s")
    run_store.publish(step0)
    run_store.write_params(fuse, {"kind": "fuse"})
    run_store.write_inputs(fuse, step0)
    run_store.publish(fuse)
    labels = {}
    nuclei_arrays = {}
    roi_records = []
    for i, (roi_name, bbox) in enumerate(rois):
        shape = (bbox[1] - bbox[0], bbox[3] - bbox[2])
        lab = _blobs(shape, n_cells, seed + 1 + i)
        nuc = table = None
        if nuclei:
            nuc, table = _nuclei_for(lab, n_cells, corrupt_nuclei)
            nuclei_arrays[roi_name] = (nuc, table)
        labels[roi_name] = lab
        path = os.path.join(run_dir, f"global_mask_{roi_name}.zarr")
        z = zarr.open(path, mode="w", shape=shape, chunks=(64, 64), dtype="uint32")
        z[:] = lab
        entry = {"path": path, "dtype": "uint32", "shape": list(shape),
                 "chunks": [64, 64], "n_objects": n_cells}
        store = {"version": 1, "complete": True, "cell": None if nuclei_only else entry,
                 "nucleus": entry if nuclei_only else None}
        if nuclei:
            npath = os.path.join(run_dir, f"global_nuclei_mask_{roi_name}.zarr")
            tpath = os.path.join(run_dir, f"global_nuclei_cell_{roi_name}.zarr")
            nz = zarr.open(npath, mode="w", shape=shape, chunks=(64, 64), dtype="uint32")
            nz[:] = nuc
            tz = zarr.open(tpath, mode="w", shape=table.shape, chunks=(1024,), dtype="uint32")
            tz[:] = table
            store["nucleus"] = {"path": npath, "dtype": "uint32", "shape": list(shape),
                                "chunks": [64, 64], "n_objects": int(table.size - 1)}
            store["nucleus_to_cell"] = {"path": tpath, "dtype": "uint32",
                                        "length": int(table.size)}
            if seam_merge:
                store["seam_merge"] = {"version": 1, "duplicate_overlap_threshold": 0.5,
                                       "minimum_writable_fraction": 0.5}
        rec = {"roi_name": roi_name, "bbox_fullres": list(bbox)}
        if label_store:
            rec["label_store"] = store
        roi_records.append(rec)
    meta = {"mode": "roi", "run_id": os.path.basename(run_dir), "method": "stardist_nuclei_expansion",
            "created_at": "2026-09-27T12:00:00", "rois": roi_records,
            "paths": {"raw_ome": slide}}
    json.dump(meta, open(os.path.join(run_dir, "segmentation_meta.json"), "w"))
    run_store.write_params(run_dir, {"kind": "segment"})
    run_store.write_inputs(run_dir, fuse)
    run_store.publish(run_dir)
    return {"root": root, "slide": slide, "slide_data": slide_data, "ws": ws, "step0": step0,
            "run_dir": run_dir, "labels": labels, "corrected": corrected_data,
            "nuclei": nuclei_arrays,
            "zarr": zpath, "cfg": cfg_path, "rois": rois}


def _edit_json(path, fn):
    d = json.load(open(path))
    fn(d)
    json.dump(d, open(path, "w"))


# ── the job ──────────────────────────────────────────────────────────────

def test_the_job_names_every_channels_source(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])
    assert job.bbox == tuple(ROI_BBOX) and job.shape == (130, 130)
    assert job.compartment == qs.CELL and job.n_objects == 40
    kinds = {c.name: (c.kind, c.decision) for c in job.channels}
    assert kinds == {"DAPI": ("raw", "original"), "CD3": ("corrected", "tophat"),
                     "CD8": ("raw", "original"), "PanCK": ("raw", "original")}
    cd3 = next(c for c in job.channels if c.name == "CD3")
    assert cd3.offset == (0, 0) and cd3.array.endswith("Full_WSI/CD3")
    assert cd3.identity["correction_param_value"] == 10
    assert job.region == "Full_WSI"


def test_a_file_inside_the_run_folder_names_the_run(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(os.path.join(p["run_dir"], "segmentation_meta.json"))
    assert job.run_dir == os.path.realpath(p["run_dir"])


def test_a_run_without_a_label_store_is_refused(tmp_path):
    p = build_project(tmp_path, label_store=False)
    with pytest.raises(qs.QuantSourceError, match="re-run Step2"):
        qs.resolve_quant_job(p["run_dir"])


def test_an_incomplete_label_store_is_refused(tmp_path):
    p = build_project(tmp_path)
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d["rois"][0]["label_store"].update(complete=False))
    with pytest.raises(qs.QuantSourceError, match="incomplete"):
        qs.resolve_quant_job(p["run_dir"])


def test_a_run_of_another_slide_is_refused(tmp_path):
    p = build_project(tmp_path)
    other = os.path.join(p["root"], "other.ome.tif")
    shutil.copy(p["slide"], other)
    with pytest.raises(qs.QuantSourceError, match="another slide"):
        qs.resolve_quant_job(p["run_dir"], open_slide=other)


def test_a_nuclei_only_run_is_quantified_on_its_nuclei(tmp_path):
    p = build_project(tmp_path, nuclei_only=True)
    job = qs.resolve_quant_job(p["run_dir"])
    assert job.compartment == qs.NUCLEUS


def test_label_array_shape_must_match_the_region(tmp_path):
    p = build_project(tmp_path)
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d["rois"][0].update(bbox_fullres=[10, 139, 20, 150]))
    with pytest.raises(qs.QuantSourceError, match="the region is uint32"):
        qs.resolve_quant_job(p["run_dir"])


# ── fail-closed channel sources ──────────────────────────────────────────

def _refused(p, match):
    with pytest.raises(qs.QuantSourceError, match=match):
        qs.resolve_quant_job(p["run_dir"])


def test_a_missing_corrected_product_is_refused_not_read_raw(tmp_path):
    p = build_project(tmp_path)
    shutil.rmtree(p["zarr"])
    _refused(p, "cannot be opened")


def test_a_missing_corrected_array_is_refused(tmp_path):
    p = build_project(tmp_path)
    del zarr.open_group(p["zarr"], mode="a")["Full_WSI"]["CD3"]
    _refused(p, "no fallback to raw pixels")


def test_only_this_rois_group_counts(tmp_path):
    p = build_project(tmp_path, rois=[("Full WSI", ROI_BBOX)])
    g = zarr.open_group(p["zarr"], mode="a")
    g.move("Full_WSI", "Other_ROI")
    g["Other_ROI"].attrs["roi_name"] = "Other ROI"
    _refused(p, "no group")


def test_a_group_that_does_not_cover_the_region_is_refused(tmp_path):
    p = build_project(tmp_path)
    zarr.open_group(p["zarr"], mode="a")["Full_WSI"].attrs["bbox_fullres"] = [12, 140, 20, 150]
    _refused(p, "not region")


@pytest.mark.parametrize("attr,value,match", [
    ("correction_method", "cucim", "made with 'cucim'"),
    ("correction_param_value", 15, "parameter 15"),
    ("channel_index", 3, "slide channel 3"),
])
def test_each_array_identity_check_refuses(tmp_path, attr, value, match):
    p = build_project(tmp_path)
    zarr.open_group(p["zarr"], mode="a")["Full_WSI"]["CD3"].attrs[attr] = value
    _refused(p, match)


def test_a_float64_corrected_array_is_refused(tmp_path):
    p = build_project(tmp_path)
    g = zarr.open_group(p["zarr"], mode="a")["Full_WSI"]
    attrs = dict(g["CD3"].attrs)
    data = g["CD3"][:].astype(np.float64)
    del g["CD3"]
    g.create_dataset("CD3", data=data).attrs.update(attrs)
    _refused(p, "not float32")


def test_a_wrong_shape_corrected_array_is_refused(tmp_path):
    p = build_project(tmp_path)
    g = zarr.open_group(p["zarr"], mode="a")["Full_WSI"]
    attrs = dict(g["CD3"].attrs)
    del g["CD3"]
    g.create_dataset("CD3", data=np.zeros((130, 129), np.float32)).attrs.update(attrs)
    _refused(p, "its region")


def test_a_product_of_another_slide_is_refused(tmp_path):
    p = build_project(tmp_path)
    zarr.open_group(p["zarr"], mode="a").attrs["source_ome"] = "/elsewhere/slide.ome.tif"
    _refused(p, "made from")


def test_a_chain_without_a_published_correct_run_is_refused(tmp_path):
    # block RM (§5): Step4 reads the run's own chain; the handoff is not consulted
    p = build_project(tmp_path)
    os.remove(os.path.join(p["step0"], ".done"))
    _refused(p, "no published Step0 result")


def test_the_product_and_the_config_must_agree(tmp_path):
    p = build_project(tmp_path)
    g = zarr.open_group(p["zarr"], mode="a")
    cfg = dict(g.attrs["correction_config"])
    cfg["channel_decisions"] = dict(cfg["channel_decisions"], CD3="original")
    g.attrs["correction_config"] = cfg
    _refused(p, "was made for")


def test_no_step0_decisions_is_refused(tmp_path):
    p = build_project(tmp_path)
    os.remove(p["cfg"])
    _refused(p, "correction decisions are missing")


def test_a_project_without_corrected_channels_needs_no_product(tmp_path):
    p = build_project(tmp_path, decisions={"CD3": "original"})
    job = qs.resolve_quant_job(p["run_dir"])
    assert {c.kind for c in job.channels} == {"raw"}


def test_the_new_path_has_no_runtime_correction_and_no_legacy_loader():
    src = open(qs.__file__, encoding="utf-8").read()
    for name in ("OMETIFFLoader", "_apply_background_method", "_apply_configured_correction",
                 "io_loader"):
        assert name not in src.split('"""', 2)[2], name
    assert not hasattr(qs, "OMETIFFLoader")


# ── readers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tiled", [True, False])
def test_the_raw_reader_equals_tifffiles_store(tmp_path, tiled):
    rng = np.random.default_rng(3)
    data = rng.integers(0, 256, size=(3, 97, 131), dtype=np.uint8)
    path = str(tmp_path / "s.ome.tif")
    write_slide(path, data, tiled=tiled)
    r = qs.TiffTileReader(path, threads=4)
    try:
        assert r.mode == ("tiff_tiles" if tiled else "tifffile_zarr")
        for y0, y1, x0, x1 in [(0, 97, 0, 131), (5, 70, 33, 64), (31, 33, 95, 131), (96, 97, 0, 1)]:
            got = r.read([2, 0], y0, y1, x0, x1)
            assert got.dtype == np.uint8
            np.testing.assert_array_equal(got, data[[2, 0], y0:y1, x0:x1])
    finally:
        r.close()


def test_the_job_reader_reads_each_channel_from_its_source(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        raw = [c for c in job.channels if c.kind == "raw"]
        cor = [c for c in job.channels if c.kind == "corrected"]
        y0, x0 = ROI_BBOX[0], ROI_BBOX[2]
        got = r.channels(raw, 3, 50, 7, 90)
        np.testing.assert_array_equal(got, p["slide_data"][[c.index for c in raw],
                                                           y0 + 3:y0 + 50, x0 + 7:x0 + 90])
        np.testing.assert_array_equal(r.channels(cor, 3, 50, 7, 90)[0],
                                      p["corrected"][("Full WSI", "CD3")][3:50, 7:90])
        assert r.reads == {"raw": 3, "corrected": 1}
        halo = r.labels(0, 40, 120, 130)
        assert halo.shape == (42, 12)
        assert not halo[0].any() and not halo[:, -1].any()     # outside the region
        np.testing.assert_array_equal(halo[1:-1, :-1], p["labels"]["Full WSI"][0:40, 119:130])
    finally:
        r.close()


def test_a_corrected_region_inside_a_larger_group_is_read_at_its_offset(tmp_path):
    p = build_project(tmp_path)
    # the run's region is a sub-rectangle of the corrected group
    lab = p["labels"]["Full WSI"][5:105, 8:118]
    z = zarr.open(os.path.join(p["run_dir"], "global_mask_Full WSI.zarr"), mode="w",
                  shape=lab.shape, chunks=(64, 64), dtype="uint32")
    z[:] = lab
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"), lambda d: (
        d["rois"][0].update(bbox_fullres=[15, 115, 28, 138]),
        d["rois"][0]["label_store"]["cell"].update(shape=list(lab.shape))))
    job = qs.resolve_quant_job(p["run_dir"])
    cd3 = next(c for c in job.channels if c.name == "CD3")
    assert cd3.offset == (5, 8)
    r = qs.JobReader(job, read_threads=2)
    try:
        np.testing.assert_array_equal(r.channels([cd3], 0, 100, 0, 110)[0],
                                      p["corrected"][("Full WSI", "CD3")][5:105, 8:118])
    finally:
        r.close()


def test_a_run_with_nuclei_names_them_in_the_job(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    job = qs.resolve_quant_job(p["run_dir"])
    nuc, table = p["nuclei"]["Full WSI"]
    assert job.has_nuclei and job.n_nuclei == table.size - 1
    assert job.seam_merge["version"] == 1
    r = qs.JobReader(job, read_threads=2)
    try:
        np.testing.assert_array_equal(r.nucleus_table(), table)
        np.testing.assert_array_equal(r.nuclei(5, 40, 7, 60), nuc[5:40, 7:60])
    finally:
        r.close()


def test_a_run_made_before_the_seam_fix_says_so(tmp_path):
    p = build_project(tmp_path, nuclei=True, seam_merge=False)
    assert qs.resolve_quant_job(p["run_dir"]).seam_merge is None


def test_a_nucleus_table_of_the_wrong_length_is_refused(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d["rois"][0]["label_store"]["nucleus"].update(n_objects=3))
    with pytest.raises(qs.QuantSourceError, match="nucleus -> cell table"):
        qs.resolve_quant_job(p["run_dir"])
