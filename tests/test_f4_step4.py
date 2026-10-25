"""F4-4 (docs/f4_regions_downstream_plan.md §F.9): Step4's h5ad and CSV carry
the Regions result a Step2 run froze -- typed per-object core and sample
columns (TMA), the area of every bound core, a per-position table, the
measurement-only exclusions as NaN in every expression matrix with a uint8
`measurement_qc` layer, the record in uns and provenance; plain-region runs
carry no core content; old formats are refused; a run without Regions is
the old output. Synthetic projects (`test_quant_sources.build_project`), the
facts written by Step2's own writer; one chain from a real Step2 run.
"""

import csv
import json
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_quant_sources import NAMES, ROI_BBOX, SLIDE_SHAPE, build_project  # noqa: E402

from PyQt5.QtWidgets import QApplication  # noqa: E402

from block01.core import quant_sources as qs  # noqa: E402
from block01.core.regions import analysis as RA  # noqa: E402
from block01.core.regions import geometry as G  # noqa: E402
from block01.core.regions import quant_apply as qa  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

_app = QApplication.instance() or QApplication([])
ad = pytest.importorskip("anndata")

Y0, Y1, X0, X1 = ROI_BBOX
XM = (X0 + X1) // 2


def _rect(y0, y1, x0, x1):
    return [[x0, y0], [x1 - 1, y0], [x1 - 1, y1 - 1], [x0, y1 - 1]]


def _snapshot(mode="tma", donors=("D1", "D2"), samples=("S1", "S2"), exclusions=None):
    """Two run cores (left / right half of the ROI), one bound core that did
    not run, one empty position; measurement-only exclusions as given."""
    left, right, other = _rect(Y0, Y1, X0, XM), _rect(Y0, Y1, XM, X1), _rect(0, 8, 0, 8)
    cores, positions, display = [], [], []
    if mode == "tma":
        for i, (poly, donor, sample) in enumerate(zip((left, right), donors, samples)):
            g = G.region_outline([poly], SLIDE_SHAPE)
            cores.append({"uid": f"c{i}", "position_key": f"k{i}", "position_id": i + 1,
                          "polygon": g["polygon_fullres"], "bbox": g["bbox_fullres"],
                          "region_id": f"reg{i}", "label": f"X1-Y{i + 1:02d}", "donor_block_id": donor,
                          "tma_unique_sample_id": sample, "annotation": f"TMA {i + 1}",
                          "core_height": "1.5", "qc": "unreviewed" if i else "ok"})
            display.append({"uid": f"c{i}", "position_key": f"k{i}", "polygons": [poly],
                            "name": "", "run": True, "reason": ""})
        display.append({"uid": "c9", "position_key": "k2", "polygons": [other], "name": "",
                        "run": False, "reason": "not_ticked"})
        for i, (sel, reason) in enumerate(((True, ""), (True, ""), (False, "not_ticked"), (False, "empty"))):
            positions.append({"position_key": f"k{i}", "position_id": i + 1, "label": f"X1-Y{i + 1:02d}",
                              "occupied": i < 3, "donor_block_id": ("D1", "D2", "D3", "")[i],
                              "tma_unique_sample_id": ("S1", "S2", "S3", "")[i], "annotation": f"TMA {i + 1}",
                              "core_height": "1.5", "spot_slide": "", "selected": sel, "reason": reason})
    snap = {"snapshot_version": RA.SNAPSHOT_VERSION, "mode": mode, "slide_id": "slide_00aa",
            "slide_shape": list(SLIDE_SHAPE),
            "regions": {"save_count": 3, "content_sha256": "save-sha", "saved_at": "t"},
            "parent": {"region_id": None, "bbox_fullres": list(ROI_BBOX), "polygon_fullres": None},
            "map_sheet": ({"sha256": "map-sha", "block_id": "RB_1", "block_name": "TMA138.1",
                           "project_name": "TMA138", "path": ""} if mode == "tma" else None),
            "cores": cores, "omitted": [], "positions": positions,
            "exclusions": list(exclusions or []), "display_cores": display,
            "selection_mode": "ticked", "seg_channels": ["DAPI"], "policies": dict(RA.POLICIES)}
    snap["identity"] = RA.identity(snap)
    return snap


def _excl(uid, channels):
    return {"uid": uid, "type": "measurement", "reasons": ["fold"], "channels": channels,
            "polygon": _rect(0, 4, 0, 4), "bbox": [0, 4, 0, 4], "seg_invalid": False}


def _add_regions(p, snap, facts, roi="Full WSI", tamper=None, bit_order=None):
    """Write the snapshot and the facts the way Step2 does (its own writer)
    and put the record into the run's LabelStore."""
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    run_dir = p["run_dir"]
    RA.save_snapshot(os.path.join(run_dir, W.REGIONS_SNAPSHOT_FILE), snap)
    meas = list(bit_order) if bit_order is not None else \
        [e["uid"] for e in snap["exclusions"] if not e["seg_invalid"]]
    fake = types.SimpleNamespace(
        _regions={"identity": snap["identity"], "measurement_exclusions": meas},
        output_dir=run_dir, _nuclei_partials=[], _regions_skipped={"": [3]},
        REGIONS_SNAPSHOT_FILE=W.REGIONS_SNAPSHOT_FILE, REGIONS_FACTS_VERSION=W.REGIONS_FACTS_VERSION,
        _abs=lambda path: os.path.abspath(path))
    merger = types.SimpleNamespace(facts=facts, judge=object(),
                                   stats={"regions_dropped_no_core": 4, "regions_dropped_exclusion": 1,
                                          "nuclei_dropped_regions": 2})
    rec = W._regions_write(fake, "", merger)
    if tamper:
        tamper(rec)
    meta_path = os.path.join(run_dir, "segmentation_meta.json")
    meta = json.load(open(meta_path))
    for r in meta["rois"]:
        if r["roi_name"] == roi:
            r["label_store"]["regions"] = rec
    json.dump(meta, open(meta_path, "w"))
    return rec


def _facts(lab, n, bits=None, mode="tma"):
    """Row i = object i + 1: core by which half its centroid lies in."""
    f = np.zeros((n, 6))
    for i in range(1, n + 1):
        ys, xs = np.nonzero(lab == i)
        f[i - 1, 2] = (0 if (xs.mean() + X0 < XM) else 1) if (mode == "tma" and xs.size) else -1
        if mode == "tma" and not xs.size:
            f[i - 1, 2] = 0
        f[i - 1, 3] = 0.75
        f[i - 1, 4] = (2 if i % 3 else 1) | (4 if i % 5 == 0 else 0)
    if bits:
        for i, b in bits.items():
            f[i - 1, 5] = b
    return f


def _read(path):
    return ad.read_h5ad(path)


def _proj(tmp_path, name="p", **kw):
    (tmp_path / name).mkdir()
    return build_project(tmp_path / name, **kw)


# ── TMA: obs, areas, per-position table, uns, provenance ─────────────────

def test_a_tma_run_carries_typed_core_and_sample_columns(tmp_path):
    p = _proj(tmp_path)
    lab = p["labels"]["Full WSI"]
    snap = _snapshot()
    _add_regions(p, snap, _facts(lab, 40))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = _read(res["h5ad"])
    o = a.obs
    assert list(o.columns[:3]) == ["cell_id", "slide_id", "segmentation_run_id"]
    for c in ("core_position_key", "donor_block_id", "tma_annotation_name", "tma_block_name",
              "tma_project_name", "core_qc", "core_label", "tma_unique_sample_id"):
        assert str(o[c].dtype) == "category", c
    assert o["core_position_id"].dtype == np.int64 and o["cell_core_fraction"].dtype == np.float32
    for c in ("cell_touches_core_edge", "analysis_include", "touches_region_edge"):
        assert o[c].dtype == bool, c
    ids = o["cell_id"].to_numpy()
    for i, cid in enumerate(ids):
        ys, xs = np.nonzero(lab == cid)
        k = 0 if xs.mean() + X0 < XM else 1
        assert o["core_position_key"].iloc[i] == f"k{k}"
        assert o["donor_block_id"].iloc[i] == ("D1", "D2")[k]
        assert o["tma_annotation_name"].iloc[i] == f"TMA {k + 1}"
        assert bool(o["analysis_include"].iloc[i]) == bool(cid % 3)
        assert bool(o["touches_region_edge"].iloc[i]) == (cid % 5 == 0)
    assert set(o["tma_block_name"]) == {"TMA138.1"} and set(o["tma_project_name"]) == {"TMA138"}
    t = a.uns["tma_cores"]
    assert list(t["position_key"]) == ["k0", "k1", "k2", "k3"]
    assert list(t["selected"]) == [True, True, False, False]
    assert list(t["reason"]) == ["", "", "not_ticked", "empty"]
    n_left = int((o["core_position_key"] == "k0").sum())
    assert list(t["n_obs"]) == [n_left, len(ids) - n_left, 0, 0]
    want_left = int(G.fill(snap["cores"][0]["polygon"], snap["cores"][0]["bbox"]).sum())
    assert t["area_px"][0] == want_left and t["area_px"][2] == 64          # the bound core that did not run
    assert np.isnan(t["area_px"][3])                                         # an empty position
    assert np.isnan(t["area_um2"]).all()                                     # this slide records no pixel size
    r = a.uns["regions"]
    assert r["identity"] == snap["identity"] and r["selection_mode"] == "ticked"
    assert "source" in r
    assert "display_cores" not in json.loads(r["snapshot_json"])
    assert dict(a.uns["regions_dropped"]) == {"no_core": 4, "outside_region": 0, "exclusion": 1,
                                              "nuclei": 2}
    prov = json.load(open(res["provenance"]))
    assert prov["regions"]["identity"] == snap["identity"]
    assert json.loads(a.uns["provenance_json"])["regions"]["position_keys"] == ["k0", "k1"]


def test_the_core_area_converts_with_the_slides_own_pixel_size(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    _add_regions(p, _snapshot(), _facts(p["labels"]["Full WSI"], 40))
    monkeypatch.setattr(qa, "_pixel_size", lambda job: (0.5, 0.25))           # not square
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"))
    a = _read(res["h5ad"])
    t = a.uns["tma_cores"]
    assert t["area_um2"][0] == pytest.approx(t["area_px"][0] * 0.125)
    assert t["area_mm2"][2] == pytest.approx(64 * 0.125 / 1e6)
    o = a.obs
    row = o.index[0]
    assert o.loc[row, "core_area_um2"] == pytest.approx(o.loc[row, "core_area_px"] * 0.125)


def test_a_multi_piece_core_is_measured_as_its_pieces_joined():
    shape = (60, 60)
    pieces = [_rect(0, 10, 0, 10), _rect(30, 40, 30, 40)]
    assert qa.outline_area_px(pieces, shape) == 200


# ── measurement-only exclusions ─────────────────────────────────────────

def test_measurement_exclusions_empty_every_expression_matrix_and_nothing_else(tmp_path):
    kw = dict(write_csv=True, statistics=["mean", "max"], regions=["cell", "nucleus", "cytoplasm"],
              features=("morphology", "nuclear_summary"), distribution=["median"], markers=["PanCK", "CD8"])
    p0 = _proj(tmp_path, "plain", nuclei=True)
    plain = few.run_extraction(p0["run_dir"], str(tmp_path / "plain_o"), **kw)
    p = _proj(tmp_path, "regions", nuclei=True)
    lab = p["labels"]["Full WSI"]
    excl = [_excl("eA", ["CD8"]), _excl("eB", ["PanCK", "CD3"])]
    bits = {3: 2, 4: 1, 6: 3}             # the facts' bit order is eB, eA (not the snapshot's)
    _add_regions(p, _snapshot(exclusions=excl), _facts(lab, 40, bits), bit_order=["eB", "eA"])
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), **kw)
    a, b = _read(res["h5ad"]), _read(plain["h5ad"])
    ids = a.obs["cell_id"].to_numpy()
    assert (ids == b.obs["cell_id"].to_numpy()).all()
    removed = {3: {"CD8"}, 4: {"PanCK", "CD3"}, 6: {"CD8", "PanCK", "CD3"}}
    qc = np.asarray(a.layers["measurement_qc"])
    assert qc.dtype == np.uint8
    layers = [k for k in a.layers if k != "measurement_qc"]
    assert any(k.startswith("nucleus_") for k in layers) and any(k.startswith("cytoplasm_") for k in layers)
    natural_nan = 0
    for layer in layers:
        x, y = np.asarray(a.layers[layer]), np.asarray(b.layers[layer])
        for r, cid in enumerate(ids):
            for c, ch in enumerate(NAMES):
                if ch in removed.get(int(cid), ()):
                    assert np.isnan(x[r, c]) and qc[r, c] == 1, (layer, cid, ch)
                else:
                    assert qc[r, c] == 0
                    if np.isnan(y[r, c]):
                        natural_nan += 1                  # an existing NaN keeps QC 0
                    assert (np.isnan(x[r, c]) and np.isnan(y[r, c])) or x[r, c] == y[r, c]
    assert natural_nan > 0
    assert np.array_equal(np.asarray(a.X), np.asarray(a.layers["cell_mean"]), equal_nan=True)
    assert len(a.obsm) > 0
    for key in a.obsm:                                          # distribution statistics, by marker name
        da, db = a.obsm[key], b.obsm[key]
        for r, cid in enumerate(ids):
            for m in da.columns:
                if m in removed.get(int(cid), ()):
                    assert np.isnan(da[m].iloc[r])
                else:
                    assert (np.isnan(da[m].iloc[r]) and np.isnan(db[m].iloc[r])) or da[m].iloc[r] == db[m].iloc[r]
    for c in b.obs.columns:                                     # morphology and nuclear summary untouched
        assert np.array_equal(a.obs[c].to_numpy(), b.obs[c].to_numpy(), equal_nan=True), c
    hdr, rows = _csv(res["csv"])
    checked = set()
    for row in rows:                       # every column of a removed channel: compartments, distribution
        for ch in removed.get(int(row[0]), ()):
            cols = [j for j, h in enumerate(hdr) if h.startswith(f"{ch}_")]
            assert cols
            for j in cols:
                assert row[j] == "nan", (row[0], hdr[j])
                checked.add(hdr[j])
    assert any("nucleus" in h for h in checked) and any("cytoplasm" in h for h in checked)
    assert any("median" in h for h in checked)
    prov = json.load(open(res["provenance"]))
    assert prov["regions"]["measurement_qc"]["objects_with_removed_values"] == \
        sum(1 for c in (3, 4, 6) if c in ids)


def test_a_stop_during_the_measurement_step_publishes_nothing(tmp_path):
    from block01.core import quant_engine as qe
    p = _proj(tmp_path)
    _add_regions(p, _snapshot(exclusions=[_excl("eA", ["CD8"])]),
                 _facts(p["labels"]["Full WSI"], 40, {3: 1}))
    calls = {"n": 0}

    def stop_late():                      # quantification runs; the Regions step is stopped
        calls["n"] += 1
        return calls["n"] > 1 and getattr(stop_late, "armed", False)
    real = qa.apply_measurement_qc

    def arm(result, rin, **kw):
        stop_late.armed = True
        return real(result, rin, **kw)
    import unittest.mock as um
    out = tmp_path / "o"
    with um.patch.object(qa, "apply_measurement_qc", arm):
        with pytest.raises(qe.QuantStopped):
            few.run_extraction(p["run_dir"], str(out), should_stop=stop_late)
    assert not out.exists() or not os.listdir(out)


def test_a_failure_while_adding_regions_leaves_nothing(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    _add_regions(p, _snapshot(), _facts(p["labels"]["Full WSI"], 40))
    monkeypatch.setattr(qa, "obs_frame", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    out = tmp_path / "o"
    with pytest.raises(OSError):
        few.run_extraction(p["run_dir"], str(out), write_csv=True)
    assert not out.exists() or not os.listdir(out)


def test_bit_51_is_the_51st_exclusion_in_the_facts_order(tmp_path):
    p = _proj(tmp_path)
    excl = [_excl(f"e{k:02d}", [NAMES[1 + k % 3]]) for k in range(52)]
    _add_regions(p, _snapshot(exclusions=excl), _facts(p["labels"]["Full WSI"], 40, {7: 1 << 51}))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"))
    a = _read(res["h5ad"])
    r = list(a.obs["cell_id"]).index(7)
    ch = NAMES[1 + 51 % 3]
    assert np.asarray(a.layers["measurement_qc"])[r].tolist() == [int(n == ch) for n in NAMES]
    assert a.uns["regions"]["measurement_exclusion_bits"][51] == "e51"


# ── the CSV carries every obs column, text as text ───────────────────────

def _csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        hdr = next(r)
        return hdr, [row for row in r]


def test_the_csv_has_every_obs_column_with_text_as_text(tmp_path):
    p = _proj(tmp_path)
    tricky = ('K2024, "A"\nB', "患者二")
    _add_regions(p, _snapshot(donors=tricky, samples=("007", "0012")), _facts(p["labels"]["Full WSI"], 40))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = _read(res["h5ad"])
    hdr, rows = _csv(res["csv"])
    assert hdr[:len(a.obs.columns)] == list(a.obs.columns)
    first_morph = next(c for c in a.obs.columns if c == "area")
    regions_cols = set(list(a.obs.columns)[1:list(a.obs.columns).index(first_morph)])
    assert {"core_area_um2", "cell_core_fraction", "slide_id"} <= regions_cols
    for r, row in enumerate(rows):
        for j, c in enumerate(a.obs.columns):
            v = a.obs[c].iloc[r]
            if isinstance(v, (bool, np.bool_)):
                assert row[j] == ("True" if v else "False")
            elif isinstance(v, (int, np.integer)):
                assert row[j] == str(int(v))
            elif isinstance(v, (float, np.floating)):
                if c in regions_cols:          # the new Regions columns: lossless
                    assert (row[j] == "nan" and np.isnan(v)) or float(row[j]) == float(v)
                else:                          # the existing columns keep their %.6g
                    assert (row[j] == "nan" and np.isnan(v)) or float(row[j]) == pytest.approx(float(v), rel=1e-5)
            else:
                assert row[j] == str(v), (c, row[j], v)
    assert any(row[hdr.index("donor_block_id")] == tricky[0] for row in rows)
    assert {row[hdr.index("tma_unique_sample_id")] for row in rows} <= {"007", "0012"}


# ── plain regions, old formats, checks, provenance, empty, no Regions ────

def test_a_plain_region_run_carries_no_core_content(tmp_path):
    p = _proj(tmp_path)
    _add_regions(p, _snapshot(mode="roi", exclusions=[_excl("eA", ["CD8"])]),
                 _facts(p["labels"]["Full WSI"], 40, {2: 1}, mode="roi"))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = _read(res["h5ad"])
    assert [c for c in a.obs.columns if c.startswith(("core_", "tma_", "donor")) or
            c in ("analysis_include", "cell_core_fraction", "cell_touches_core_edge")] == []
    assert {"slide_id", "segmentation_run_id", "touches_region_edge"} <= set(a.obs.columns)
    assert "tma_cores" not in a.uns and "measurement_qc" in a.layers
    hdr, _rows = _csv(res["csv"])
    assert not any(h.startswith(("core_", "tma_")) for h in hdr)


@pytest.mark.parametrize("case", ["no_version", "object_array", "no_display_cores", "identity",
                                  "rows", "stray_bit", "uid", "core_index", "parent"])
def test_unusable_regions_facts_are_refused_before_any_output(tmp_path, case):
    p = _proj(tmp_path)
    lab = p["labels"]["Full WSI"]
    snap = _snapshot(exclusions=[_excl("eA", ["CD8"])])
    facts = _facts(lab, 40)
    tamper = None
    if case == "no_version":
        tamper = lambda rec: rec.pop("facts_version")                    # noqa: E731
    elif case == "rows":
        facts = facts[:39]
    elif case == "stray_bit":
        facts[0, 5] = 2
    elif case == "core_index":
        facts[0, 2] = 5
    elif case == "identity":
        tamper = lambda rec: rec.update(identity="other")                # noqa: E731
    elif case == "parent":
        snap["parent"]["bbox_fullres"] = [0, 10, 0, 10]
        snap["identity"] = RA.identity(snap)
    elif case == "no_display_cores":
        snap.pop("display_cores")
    rec = _add_regions(p, snap, facts, tamper=tamper)
    if case == "object_array":
        path = rec["cells"]["path"]
        z = dict(np.load(path, allow_pickle=False))
        z["measurement_exclusions"] = np.array(["eA"], dtype=object)
        with open(path, "wb") as f:
            np.savez(f, **z)
    if case == "uid":
        path = rec["cells"]["path"]
        z = dict(np.load(path, allow_pickle=False))
        z["measurement_exclusions"] = np.array(["eZ"])
        with open(path, "wb") as f:
            np.savez(f, **z)
    out = tmp_path / "o"
    with pytest.raises(qs.QuantSourceError):
        few.run_extraction(p["run_dir"], str(out))
    assert not out.exists() or not os.listdir(out)


def test_a_later_regions_save_cannot_change_what_a_run_exports(tmp_path, monkeypatch):
    from block01.core.regions import lock as RK
    p = _proj(tmp_path)
    _add_regions(p, _snapshot(), _facts(p["labels"]["Full WSI"], 40))
    monkeypatch.setattr(RK, "load_saved", lambda *a, **k: pytest.fail("Step4 read the current Save"))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"))
    assert _read(res["h5ad"]).uns["regions"]["save_content_sha256"] == "save-sha"


def test_provenance_registers_the_frozen_save_or_lists_it_unresolved(tmp_path, monkeypatch):
    from block01.core import provenance as prov
    seen = {}
    monkeypatch.setattr(prov, "find_workspace", lambda d: (str(tmp_path), str(tmp_path / "ws")))
    monkeypatch.setattr(prov, "ensure_raw_slide", lambda pd, s: "slide_1")
    monkeypatch.setattr(prov, "workspace_regions", lambda ws, sid: {})
    monkeypatch.setattr(prov, "lookup", lambda *a: None)
    monkeypatch.setattr(prov, "safe_register", lambda *a, **k: seen.update(k) or "id")
    job = types.SimpleNamespace(slide="s", bbox=(0, 1, 0, 1), roi_name="Full WSI", run_dir="r",
                                run_id="seg_1", channels=())
    rec = {"created_at": "t", "outputs": {"h5ad": "x.h5ad"},
           "regions": {"identity": "I", "selection_mode": "all_eligible", "position_keys": ["k0"],
                       "save_content_sha256": "C"}}
    prov.register_step4(str(tmp_path / "x.h5ad"), job, rec)
    assert {"role": "regions_layout", "content_sha256": "C"} in seen["unresolved_inputs"]
    monkeypatch.setattr(prov, "lookup", lambda pd, kind, loc, token: "art_regions" if kind == "regions_layout" else None)
    prov.register_step4(str(tmp_path / "x.h5ad"), job, rec)
    assert "art_regions" in seen["depends_on"]
    assert not [u for u in seen["unresolved_inputs"] if u.get("role") == "regions_layout"]
    assert seen["parameters"]["regions"] == {"identity": "I", "selection_mode": "all_eligible",
                                             "position_keys": ["k0"]}


def test_a_run_without_regions_is_the_old_output(tmp_path):
    p = _proj(tmp_path)
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = _read(res["h5ad"])
    assert "regions" not in a.uns and "measurement_qc" not in a.layers
    assert not [c for c in a.obs.columns if c in ("slide_id", "touches_region_edge")]
    assert "regions" not in json.load(open(res["provenance"]))


def test_a_nuclei_only_run_matches_its_nuclei_to_the_facts(tmp_path):
    p = _proj(tmp_path, nuclei_only=True)
    _add_regions(p, _snapshot(), _facts(p["labels"]["Full WSI"], 40))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"))
    a = _read(res["h5ad"])
    assert a.obs.columns[0] == "nucleus_id" and "core_position_key" in a.obs


def test_a_run_with_no_objects_still_writes_its_outputs(tmp_path):
    p = _proj(tmp_path, n_cells=0)
    _add_regions(p, _snapshot(), np.zeros((0, 6)))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = _read(res["h5ad"])
    assert a.n_obs == 0 and a.layers["measurement_qc"].shape == (0, len(NAMES))
    assert list(a.uns["tma_cores"]["n_obs"]) == [0, 0, 0, 0]
    hdr, rows = _csv(res["csv"])
    assert rows == [] and "core_position_key" in hdr


# ── the chain: a real Step2 run with Regions, then Step4 ─────────────────

def test_a_real_step2_run_with_regions_quantifies_with_its_cores(tmp_path):
    """Step2 (real engine, cores frozen at Run) writes the facts; Step4 reads
    them back: every exported cell carries its core, the per-position counts
    add up, the facts format is the pickle-free one."""
    pytest.importorskip("stardist")
    import zarr
    import test_step2_runner_path as t2
    import test_v16_artifact_graph as tg
    from block01.core.project_identity import describe_slide
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    slide = tg._nuclei_slide(tmp_path)
    _project, ctx = tg._step0_run(tmp_path, slide)
    fused = tg._fuse_run(ctx, slide)
    y0, y1, x0, x1 = tg.ROI_BBOX
    xm = (x0 + x1) // 2
    shape = list(tg.SLIDE_SHAPE)
    cores = []
    for i, (a, b) in enumerate(((x0, xm), (xm, x1))):
        poly = _rect(y0, y1, a, b)
        g = G.region_outline([poly], tuple(shape))
        cores.append({"uid": f"c{i}", "position_key": f"k{i}", "position_id": i + 1,
                      "polygon": g["polygon_fullres"], "bbox": g["bbox_fullres"], "region_id": f"r{i}",
                      "label": f"X1-Y{i + 1:02d}", "donor_block_id": f"D{i}", "tma_unique_sample_id": f"S{i}",
                      "annotation": f"TMA {i + 1}", "core_height": "", "qc": "ok"})
    snap = {"snapshot_version": RA.SNAPSHOT_VERSION, "mode": "tma", "slide_id": describe_slide(slide)[0],
            "slide_shape": shape, "regions": {"save_count": 1, "content_sha256": "c", "saved_at": "t"},
            "parent": {"region_id": None, "bbox_fullres": list(tg.ROI_BBOX), "polygon_fullres": None},
            "map_sheet": {"sha256": "m", "block_id": "B", "block_name": "BN", "project_name": "P", "path": ""},
            "cores": cores, "omitted": [],
            "positions": [{"position_key": f"k{i}", "position_id": i + 1, "label": f"X1-Y{i + 1:02d}",
                           "occupied": True, "donor_block_id": f"D{i}", "tma_unique_sample_id": f"S{i}",
                           "annotation": "", "core_height": "", "spot_slide": "", "selected": True,
                           "reason": ""} for i in range(2)],
            "exclusions": [],
            "display_cores": [{"uid": c["uid"], "position_key": c["position_key"],
                               "polygons": [_rect(y0, y1, *((x0, xm), (xm, x1))[i])], "name": "",
                               "run": True, "reason": ""} for i, c in enumerate(cores)],
            "selection_mode": "ticked", "seg_channels": ["DAPI"], "policies": dict(RA.POLICIES)}
    snap["identity"] = RA.identity(snap)
    cfg = dict(t2._contract_config(tmp_path / "cfg", "stardist_nuclei_expansion"))
    cfg.get("preseg_contract", {}).pop("regions", None)
    w = SegmentMergeWorker(fused, seg_config=cfg, n_rows=2, n_cols=2, overlap_px=t2.HALO,
                           output_dir=ctx["step_dirs"]["step2"], rois=[dict(tg.ROI, roi_id=ctx["roi_id"])],
                           regions_request={"snapshot": snap, "identity": snap["identity"],
                                            "input_kind": "nucleus", "selection_mode": "ticked"})
    got = t2._collect(w)
    w.run()
    assert got["error"] == [] and got["finished"], got["error"]
    facts = np.load(os.path.join(w.output_dir, "global_cell_regions_Full WSI.npz"), allow_pickle=False)
    assert int(facts["facts_version"]) == 2
    res = few.run_extraction(w.output_dir, str(tmp_path / "q"), roi_name="Full WSI", write_csv=True)
    a = _read(res["h5ad"])
    assert a.n_obs > 0 and set(a.obs["core_position_key"]) <= {"k0", "k1"}
    lab = np.asarray(zarr.open(os.path.join(w.output_dir, "global_mask_Full WSI.zarr"), mode="r"))
    for r, cid in enumerate(a.obs["cell_id"]):
        ys, xs = np.nonzero(lab == cid)
        inside_left = (xs + x0 < xm).mean()
        k = a.obs["core_position_key"].iloc[r]
        assert (k == "k0") == (inside_left > 0.5), (cid, k, inside_left)
    t = a.uns["tma_cores"]
    assert int(t["n_obs"].sum()) == a.n_obs
