"""Block A3+A4 acceptance helpers, run on COPIES only.

    python scripts/diagnose_v16_a3a4_acceptance.py copy SRC_COPY DEST
    python scripts/diagnose_v16_a3a4_acceptance.py adopt COPY
    python scripts/diagnose_v16_a3a4_acceptance.py tree COPY OUT.json
    python scripts/diagnose_v16_a3a4_acceptance.py tree-diff A.json B.json
    python scripts/diagnose_v16_a3a4_acceptance.py check-cells CELLS.parquet RUN_DIR H5AD

`copy` duplicates a path-rewritten test1 copy (it carries
`A0_COPY_INFO.json`) to DEST and rewrites SRC_COPY -> DEST in its text files;
it refuses anything under ~/fusion_data and anything that is not such a copy.
`adopt` does what the first Save of new code does to an existing project:
`ensure_project_manifest` (schema, P2 description, transforms, raw_slide)
and the Step0 corrected channels' entries for the copy's workspace. `tree`
records every file's size and sha256 (zarr chunks included); `tree-diff`
prints what was added, removed or changed between two records.

Step4 and fusion themselves run through `scripts/diagnose_v16_a2c_oracle.py`
(the same commands exist on the pre-A3 commit) and are compared with its
`same-step4` / `same-fused`.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

FUSION_DATA = os.path.realpath(os.path.expanduser("~/fusion_data"))
TEXT_SUFFIXES = (".json", ".csv", ".txt", ".log", ".zattrs", ".zarray", ".zgroup")


def _guard(path):
    if os.path.realpath(path).startswith(FUSION_DATA):
        raise SystemExit(f"never write under ~/fusion_data: {path}")


def _info(copy):
    path = os.path.join(copy, "A0_COPY_INFO.json")
    if not os.path.exists(path):
        raise SystemExit(f"{copy} is not a path-rewritten copy (no A0_COPY_INFO.json)")
    return json.load(open(path, encoding="utf-8"))


def cmd_copy(args):
    src, dest = os.path.abspath(args.src), os.path.abspath(args.dest)
    _guard(dest)
    info = _info(src)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    old, new = src.encode(), dest.encode()
    rewritten = 0
    for base, _dirs, files in os.walk(dest):
        for name in files:
            if not (name.endswith(TEXT_SUFFIXES) or name.startswith(".z")):
                continue
            p = os.path.join(base, name)
            data = open(p, "rb").read()
            if old in data:
                open(p, "wb").write(data.replace(old, new))
                rewritten += 1
    info = dict(info, dest=dest, copied_from=src)
    json.dump(info, open(os.path.join(dest, "A0_COPY_INFO.json"), "w"), indent=1)
    print(f"copied {src} -> {dest}; rewrote {rewritten} files")


def cmd_adopt(args):
    from block01.core import provenance as prov
    from block01.utils import roi_project as rp
    copy = os.path.abspath(args.copy)
    _guard(copy)
    info = _info(copy)
    manifest = json.load(open(os.path.join(copy, "project_manifest.json")))
    rp.ensure_project_manifest(copy, manifest["source_ome"])
    ws = os.path.join(copy, "rois", info["workspace"])
    zpath = os.path.join(ws, "step0", "corrected_channels.zarr")
    ids = prov.register_corrected_channels(copy, ws, zpath, manifest["source_ome"])
    print(json.dumps({"project_schema_version": json.load(open(os.path.join(
        copy, "project_manifest.json")))["project_schema_version"], "corrected_channels": ids},
        indent=1))


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cmd_tree(args):
    root = os.path.abspath(args.copy)
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            out[os.path.relpath(p, root)] = [os.path.getsize(p), _sha(p)]
    json.dump(out, open(args.out, "w"))
    print(f"{len(out)} files -> {args.out}")


def cmd_tree_diff(args):
    a, b = json.load(open(args.a)), json.load(open(args.b))
    report = {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
              "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    print(json.dumps(report, indent=1))


def cmd_check_cells(args):
    """A4-G1 ... G4 on a real result: the table against the LabelStore, the
    Step4 h5ad and Step3's label tile (sampled cells), read only."""
    import types
    import h5py
    import numpy as np
    import zarr
    from block01.core import object_tables as ot
    from block01.core import project_identity as pid
    from block01.core import quant_sources as qs
    from block01.core import step3_masks as sm
    df = ot.read_table(args.parquet).to_pandas()
    run = qs.open_run(args.run_dir)
    (roi_name, bbox), = qs.run_regions(run)
    store = qs._label_store(run, roi_name)
    labels = zarr.open(store["cell"]["path"], mode="r")
    n = int(store["cell"]["n_objects"])
    oy, ox = int(bbox[0]), int(bbox[2])
    report = {}
    # G1: keys = labels with pixels (an independent count: np.unique per band)
    present = np.zeros(n + 1, np.int64)
    for r0 in range(0, labels.shape[0], 1024):
        u, c = np.unique(np.asarray(labels[r0:r0 + 1024]), return_counts=True)
        present[u] += c
    ids = np.nonzero(present[1:])[0] + 1
    report["rows"] = len(df)
    report["G1_keys_equal_labelstore"] = bool(np.array_equal(df["cell_id"].to_numpy(), ids))
    report["G1_empty_labels"] = [int(v) for v in np.nonzero(present[1:] == 0)[0] + 1]
    report["G1_unique"] = not df.duplicated(["segmentation_run_id", "region_id", "cell_id"]).any()
    report["G1_area_equal_independent"] = bool(np.array_equal(df["cell_area"].to_numpy(),
                                                              present[ids]))
    # G2: bitwise against the Step4 h5ad
    with h5py.File(args.h5ad, "r") as f:
        obs = {k: f["obs"][k][()] for k in ("cell_id", "centroid_x", "centroid_y", "bbox_min_x",
                                            "bbox_min_y", "bbox_max_x", "bbox_max_y", "area")}
    order = np.argsort(obs["cell_id"])
    obs = {k: v[order] for k, v in obs.items()}
    report["G2_same_ids_as_step4"] = bool(np.array_equal(obs["cell_id"].astype(np.int64),
                                                         df["cell_id"].to_numpy()))
    for mine, theirs in (("x_global", "centroid_x"), ("y_global", "centroid_y"),
                         ("bbox_x0", "bbox_min_x"), ("bbox_y0", "bbox_min_y"),
                         ("bbox_x1", "bbox_max_x"), ("bbox_y1", "bbox_max_y"),
                         ("cell_area", "area")):
        report[f"G2_{mine}_bitwise"] = bool(np.array_equal(
            df[mine].to_numpy().astype(np.float64), obs[theirs].astype(np.float64)))
    # G2 raster + G3 viewer, on sampled cells
    rng = np.random.default_rng(0)
    sample = df.iloc[rng.choice(len(df), size=min(200, len(df)), replace=False)]
    frames = pid.SlideFrames([labels.shape])
    source = types.SimpleNamespace(mask_path=store["cell"]["path"], bbox=tuple(bbox),
                                   pyramid=None)
    bad_bbox, bad_pick = [], []
    T = 512
    for row in sample.itertuples():
        win = np.asarray(labels[row.bbox_y0 - oy:row.bbox_y1 - oy,
                                row.bbox_x0 - ox:row.bbox_x1 - ox]) == row.cell_id
        ys, xs = np.nonzero(win)
        if not (ys.min() == 0 and xs.min() == 0 and ys.max() + 1 == win.shape[0]
                and xs.max() + 1 == win.shape[1] and ys.size == row.cell_area):
            bad_bbox.append(int(row.cell_id))
        lx = int(xs[ys == 0].min()) + row.bbox_x0
        wx, wy = frames.global_to_viewer_world(lx, row.bbox_y0)
        tile = sm.read_label_tile(source, 0, int(wx // T), int(wy // T), T, [labels.shape])
        x0, _x1, y0, _y1 = tile.world_rect
        gx, gy = frames.viewer_world_to_global(wx - x0, wy - y0)
        if tile.labels[int(gy), int(gx)] != row.cell_id:
            bad_pick.append(int(row.cell_id))
    report["G2_raster_bbox_mismatches"] = bad_bbox
    report["G3_viewer_pick_mismatches"] = bad_pick
    # G4: nuclei, an independent per-nucleus count
    if "nucleus_to_cell" in store:
        table = np.asarray(zarr.open(store["nucleus_to_cell"]["path"], mode="r")[...])
        nuc = zarr.open(store["nucleus"]["path"], mode="r")
        npx = np.zeros(table.size, np.int64)
        for r0 in range(0, nuc.shape[0], 1024):
            u, c = np.unique(np.asarray(nuc[r0:r0 + 1024]), return_counts=True)
            npx[u] += c
        cnt, area = {}, {}
        for k in range(1, table.size):
            cell = int(table[k])
            if cell:
                cnt[cell] = cnt.get(cell, 0) + 1
                area[cell] = area.get(cell, 0) + int(npx[k])
        report["G4_nucleus_count_equal"] = all(
            int(r.nucleus_count) == cnt.get(int(r.cell_id), 0) for r in df.itertuples())
        report["G4_nucleus_area_equal"] = all(
            int(r.nucleus_area) == area.get(int(r.cell_id), 0) for r in df.itertuples())
    print(json.dumps(report, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("copy")
    p.add_argument("src")
    p.add_argument("dest")
    p = sub.add_parser("adopt")
    p.add_argument("copy")
    p = sub.add_parser("tree")
    p.add_argument("copy")
    p.add_argument("out")
    p = sub.add_parser("check-cells")
    p.add_argument("parquet")
    p.add_argument("run_dir")
    p.add_argument("h5ad")
    p = sub.add_parser("tree-diff")
    p.add_argument("a")
    p.add_argument("b")
    args = ap.parse_args(argv)
    return {"copy": cmd_copy, "adopt": cmd_adopt, "tree": cmd_tree,
            "tree-diff": cmd_tree_diff, "check-cells": cmd_check_cells}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
