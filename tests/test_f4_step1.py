"""F4-2 (docs/f4_regions_downstream_plan.md v2.1 §C): Step1 with Regions --
the segmentation channels per method, the Regions rules applied to a
window's results before the patch crop, random patches inside the selected
cores, the frozen snapshot in the run and the contract, and the old flow
untouched when a project has no Regions. Synthetic data only.
"""

import json
import os
import types

import numpy as np
import pytest

from test_regions_r1 import SHAPE, SID, _finished, _key, _place, project  # noqa: F401

from block01.core import preseg_contract, preseg_input, random_patches
from block01.core.regions import analysis as A
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K
from block01.core.regions.raster import AnalysisRaster

FUSION = {"nucleus": {"channel": "DAPI", "weight": 1.0},
          "groups": {"membrane": {"group_weight": 1.0, "channels": {"PanCK": 1.0, "CD45": 0.5, "CD8": 0.0}},
                     "off": {"group_weight": 0.0, "channels": {"Ki67": 1.0}}}}


def test_the_segmentation_channels_follow_what_the_method_reads():
    assert preseg_input.seg_channels("cellpose_nuclei_dapi", FUSION) == ["DAPI"]
    assert preseg_input.seg_channels("mesmer_nuclei", FUSION) == ["DAPI"]
    assert preseg_input.seg_channels("cellpose_wholecell_fusion", FUSION) == ["CD45", "DAPI", "PanCK"]
    assert preseg_input.seg_channels("mesmer_whole_cell", FUSION) == ["CD45", "DAPI", "PanCK"]


def _raster(cores=((10, 10, 60, 60),), excl=(), seg_invalid=True, mode="tma"):
    snap = {"mode": mode, "slide_shape": [200, 200],
            "cores": [{"polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]} for y0, x0, y1, x1 in cores],
            "exclusions": [{"polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "channels": "all",
                            "seg_invalid": seg_invalid} for y0, x0, y1, x1 in excl]}
    return AnalysisRaster(snap)


def test_window_results_keep_only_cells_the_regions_rules_keep_before_the_crop():
    r = _raster(excl=((40, 40, 50, 50),))
    win = (0, 100, 0, 100)
    cell = np.zeros((100, 100), np.int32)
    cell[20:30, 20:30] = 1                       # in the core
    cell[70:80, 70:80] = 2                       # on glass
    cell[42:46, 42:46] = 3                       # on a fold
    nuc = (cell > 0).astype(np.int32) * cell     # expansion: same ids
    out, dropped = preseg_input.regions_filter("cellpose_nuclei_expansion", {"cell": cell, "nucleus": nuc}, win, r)
    assert set(np.unique(out["cell"])) == {0, 1} and set(np.unique(out["nucleus"])) == {0, 1}
    assert dropped == {"cell": 2}
    nuc2 = np.zeros_like(cell)
    nuc2[22:26, 22:26] = 7                       # in kept cell 1
    nuc2[72:76, 72:76] = 8                       # in dropped cell 2 (glass)
    nuc2[43:45, 43:45] = 9                       # in dropped cell 3 (fold): follows it
    nuc2[25:28, 50:53] = 6                       # in no cell, inside the core: judged on its own
    out, dropped = preseg_input.regions_filter("mesmer_nuclear_guided", {"cell": cell, "nucleus": nuc2}, win, r)
    assert set(np.unique(out["nucleus"])) == {0, 6, 7} and dropped == {"cell": 2, "nucleus": 2}
    out, _ = preseg_input.regions_filter("cellpose_nuclei_dapi", {"cell": None, "nucleus": nuc2}, win, r)
    assert set(np.unique(out["nucleus"])) == {0, 6, 7}                 # nuclei only: each judged itself


def test_random_patches_go_round_robin_inside_the_cores_and_avoid_exclusions():
    mask = np.ones((60, 60), bool)               # all tissue at ds 4 (240 x 240 level-0)
    cores = [[(10, 10), (110, 10), (110, 110), (10, 110)], [(130, 130), (230, 130), (230, 230), (130, 230)]]
    gen = random_patches.generate_in_cores(4, 30, 30, mask=mask, ds=4, cores=cores,
                                           avoid=[[(10, 10), (60, 10), (60, 60), (10, 60)]])
    assert len(gen.patches) == 4
    inside = [[random_patches.rect_inside_polygon(p, c) for c in cores] for p in gen.patches]
    assert all(any(row) for row in inside)
    assert [row.index(True) for row in inside] == [0, 1, 0, 1]          # each core in turn
    assert not any(p[0] < 60 and 10 < p[1] and p[2] < 60 and 10 < p[3] for p in gen.patches)
    tiny = random_patches.generate_in_cores(3, 200, 200, mask=mask, ds=4, cores=cores)
    assert tiny.patches == [] and tiny.shortfall == 3                  # never loosened
    thin = [[(10, 10), (14, 10), (110, 106), (110, 110), (106, 110), (10, 14)]]   # a thin diagonal fold
    g = random_patches.generate_in_cores(2, 30, 30, mask=mask, ds=4, cores=cores[:1], avoid=thin)
    assert len(g.patches) == 2                                          # its box does not block the core
    inside = random_patches.generate_in_cores(4, 30, 30, mask=mask, ds=4, cores=cores, region=(0, 120, 0, 120))
    assert inside.patches and all(p[1] <= 120 and p[3] <= 120 for p in inside.patches)   # the parent ROI


def test_the_contract_carries_the_frozen_snapshot_of_the_methods_input(project, tmp_path):
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    path = A.save_snapshot(str(tmp_path / "regions_nucleus.json"), snap)
    run = {"run_id": "preseg_1", "halo_px": 200, "source": {"pixel_key": "pk"}, "fusion": {"hash": "fh"},
           "combos": [{"combo_id": "c1", "method": "cellpose_nuclei_dapi", "params": {"diameter": 10}}],
           "engines": {"cellpose": {"engine": "cellpose", "v": 1}},
           "regions": {"nucleus": {"identity": snap["identity"], "path": path}}}
    recs = {"t": {"combo_id": "c1", "status": "ok", "params": {"diameter": 10},
                  "method": "cellpose_nuclei_dapi", "engine_identity": {"engine": "cellpose", "v": 1}}}
    block = preseg_contract.build(run, "c1", recs)
    assert block["regions"]["identity"] == snap["identity"] and block["regions"]["snapshot"] == snap
    os.remove(path)                                                     # the preseg run deleted later
    cfg = {preseg_contract.CONTRACT_KEY: block, "method": "cellpose_nuclei_dapi"}
    assert preseg_contract.validate(cfg)["regions"]["snapshot"]["cores"]   # the handoff keeps it whole
    bad = dict(block, regions=dict(block["regions"], identity="0" * 64))
    with pytest.raises(preseg_contract.ContractError):
        preseg_contract.validate({preseg_contract.CONTRACT_KEY: bad, "method": "cellpose_nuclei_dapi"})
    del run["regions"]
    assert "regions" not in preseg_contract.build(run, "c1", recs)    # no Regions: as before


def _window(project, study=None, present=True):
    from block01.ui.main_window import MainWindow
    fake = types.SimpleNamespace(_regions_files_present=lambda: present,
                                 _regions_context=lambda: {"project_dir": project, "slide_id": SID})
    fake.call = lambda name, *a: getattr(MainWindow, name)(fake, *a)
    return fake


def test_step1_freezes_one_snapshot_per_input_kind_and_refuses_a_tma_without_a_save(project, monkeypatch):
    from block01.core.tma import study as tstudy
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    cy, cx = _place(1, 1)
    D.add_exclusion(d, G.circle_polygon(cy, cx, 20, SHAPE), ["blur"], channels=["PanCK"])
    monkeypatch.setattr(tstudy, "load_study", lambda pd: s)
    w = _window(project)
    out, why = w.call("_preseg_regions", [{"method": "cellpose_nuclei_dapi"}], {"fusion_config": FUSION}, None)
    assert out is None and "Save in Regions first" in why            # a TMA must be saved first
    K.save(project, d, s)
    methods = [{"method": "cellpose_nuclei_dapi"}, {"method": "cellpose_nuclei_expansion"},
               {"method": "cellpose_wholecell_fusion"}]
    out, why = w.call("_preseg_regions", methods, {"fusion_config": FUSION}, None)
    assert why == "" and set(out) == {"nucleus", "fusion_fusion_nucleus"}
    assert not out["nucleus"]["exclusions"][0]["seg_invalid"]             # PanCK is not read for nuclei
    assert out["fusion_fusion_nucleus"]["exclusions"][0]["seg_invalid"]   # but is for the fusion input
    for key in d["positions"]:
        D.set_run(d, key, False)
    K.save(project, d, s)
    out, why = w.call("_preseg_regions", methods, {"fusion_config": FUSION}, None)
    assert out is None and "no core is selected" in why


def test_a_project_without_regions_is_left_exactly_as_it_was(project, monkeypatch):
    from block01.core import provenance as prov
    before = sorted(os.listdir(project))
    w = _window(project, present=False)
    w._regions_context = lambda: pytest.fail("the slide must not even be opened")
    assert w.call("_preseg_regions", [{"method": "cellpose_nuclei_dapi"}], {"fusion_config": FUSION}, None) == (None, "")
    assert w.call("_random_patch_cores") == (None, None, "")
    assert sorted(os.listdir(project)) == before and not prov.load_entries(project)


def test_the_run_job_judges_results_with_its_frozen_snapshot(project, tmp_path):
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    path = A.save_snapshot(str(tmp_path / "regions_nucleus.json"), snap)
    job = PresegRunJob.__new__(PresegRunJob)
    job.run = {"regions": {"nucleus": {"identity": snap["identity"], "path": path}}}
    r = job._regions_raster("cellpose_nuclei_dapi")
    assert r is job._regions_raster("stardist_nuclei_dapi")               # one raster per snapshot
    assert job._regions_raster("cellpose_wholecell_fusion") is None       # no snapshot for that kind
    cy, cx = _place(1, 1)
    assert r.core_index((int(cy), int(cy) + 1, int(cx), int(cx) + 1))[0, 0] == 1


def test_random_patches_in_a_tma_without_usable_cores_are_refused_not_spread_over_tissue(project, monkeypatch):
    from block01.core.tma import study as tstudy
    s, d = _finished()
    K.save(project, d, s)                                               # nothing ticked
    monkeypatch.setattr(tstudy, "load_study", lambda pd: s)         # the study the snapshot reads
    w = _window(project)
    cores, avoid, why = w.call("_random_patch_cores")
    assert cores is None and "no core is selected" in why


def test_the_summary_says_when_regions_was_saved_again(project, tmp_path):
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    path = A.save_snapshot(str(tmp_path / "regions_nucleus.json"), snap)
    w = _window(project)
    run = {"regions": {"nucleus": {"identity": snap["identity"], "path": path, "cores": 9, "omitted": 0,
                                   "project_dir": project}}}
    assert w.call("_preseg_regions_line", run) == "Regions: 9 cores"
    D.add_exclusion(d, G.circle_polygon(300, 300, 20, SHAPE), ["blur"])
    K.save(project, d, s)
    assert w.call("_preseg_regions_line", run).endswith("a newer Regions Save exists")


def test_a_core_without_room_does_not_use_up_the_other_cores_turns():
    mask = np.ones((60, 60), bool)
    cores = [[(10, 10), (110, 10), (110, 110), (10, 110)], [(130, 130), (230, 130), (230, 230), (130, 230)]]
    g = random_patches.generate_in_cores(1, 30, 30, mask=mask, ds=4, cores=cores, avoid=[cores[0]])
    assert len(g.patches) == 1 and random_patches.rect_inside_polygon(g.patches[0], cores[1])
