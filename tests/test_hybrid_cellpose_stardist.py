"""Hybrid: Cellpose whole cells + StarDist nuclei
(docs/v17_hybrid_cellpose_stardist_plan.md; user rulings 2026-11-10).

  * the engine (real, in-process): cells equal whole-cell's, nuclei equal
    StarDist nuclei-DAPI's on the same input and thresholds (default and
    non-default); repeated tasks give the same masks; one device in one form;
  * identity and provenance carry both parts; `cpu_only` reaches both;
  * parameters: flat and nested files load the same StarDist values; another
    StarDist model is warned about; the page shows both rows, applies the
    StarDist read-area cap and keeps its own values over a loaded file's;
  * Step2 identity: another hybrid behaviour version is asked, Cancel stops;
  * Step2 on a stand-in engine: every cell has a nucleus, the events recorded.

Synthetic data in the test's temporary directory only.
"""
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.seg_runner import engines  # noqa: E402

METHOD = "hybrid_cellpose_stardist"


def _need_models():
    pytest.importorskip("cellpose")
    pytest.importorskip("stardist")
    try:
        engines.model_path(engines.STARDIST_TORCH_ENTRY)
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def hybrid():
    _need_models()
    return engines.HybridEngine()


def _input():
    import test_step2_runner_path as rp
    from block01.core import preseg_input
    return preseg_input.model_input(preseg_input.INPUT_KIND[METHOD], rp._image())


# ── the engine ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("thresholds", [{}, {"prob_thresh": 0.6, "nms_thresh": 0.3}])
def test_the_hybrid_is_whole_cell_plus_stardist_nuclei(hybrid, thresholds):
    from block01.core import preseg_input
    import test_step2_runner_path as rp
    image = _input()
    params = dict({"diameter": 16.0, "flow_threshold": 0.4, "cellprob_threshold": 0.0,
                   "min_size": 15}, **thresholds)
    got = engines.run(hybrid, METHOD, image, params)
    cells = engines.run(hybrid.cellpose, "cellpose_wholecell_fusion", image, params)["cell"]
    nuclei = engines.run(hybrid.stardist, "stardist_nuclei_dapi",
                         preseg_input.model_input("nucleus", rp._image()), params)["nucleus"]
    np.testing.assert_array_equal(got["cell"], cells)
    np.testing.assert_array_equal(got["nucleus"], nuclei)
    assert got["cell"].max() > 0 and got["nucleus"].max() > 0


def test_repeated_tasks_give_the_same_masks(hybrid):
    image = _input()
    params = {"diameter": 16.0, "prob_thresh": 0.5}
    a = engines.run(hybrid, METHOD, image, params)
    b = engines.run(hybrid, METHOD, image, params)
    for k in ("cell", "nucleus"):
        np.testing.assert_array_equal(a[k], b[k])


def test_one_device_in_one_form(hybrid):
    assert hybrid.device in ("gpu", "mps", "cpu")
    assert engines._device_form(hybrid.cellpose.device) == hybrid.device
    assert engines._device_form(hybrid.stardist.device) == hybrid.device


def test_parts_on_different_devices_refuse_to_start(monkeypatch):
    class Part:
        def __init__(self, device):
            self.device = device
    monkeypatch.setattr(engines, "StardistEngine", lambda: Part("mps"))
    monkeypatch.setattr(engines, "CellposeEngine", lambda: Part("cpu"))
    with pytest.raises(RuntimeError, match="different devices"):
        engines.HybridEngine()


def test_identity_and_provenance_carry_both_parts(hybrid):
    from block01.seg_runner import runner
    ident = engines.behavior_identity("hybrid")
    assert ident["engine"] == "hybrid" and ident["behavior_version"] == 1
    assert ident["model_id"] == "cellpose_cpsam+stardist_2D_versatile_fluo"
    prov = runner.engine_provenance("hybrid", hybrid)
    assert prov["device"] == hybrid.device
    assert "cellpose" in prov["lib_versions"] and prov["lib_versions"]["stardist_backend"] == "torch"
    assert prov["model_artifact"]["entry"] == engines.STARDIST_TORCH_ENTRY
    assert "cellpose:" in prov["model_resolved_path"] and "stardist:" in prov["model_resolved_path"]


def test_cpu_only_reaches_both_parts(tmp_path):
    """The engine process with `cpu_only` (as Step2's unticked GPU box)."""
    _need_models()
    from block01.seg_runner.client import EngineProcess
    ep = EngineProcess("hybrid", log_path=str(tmp_path / "e.log"), cpu_only=True)
    try:
        hello = ep.start()
    finally:
        ep.close()
    assert hello["device"] == "cpu"
    assert hello["identity"]["engine"] == "hybrid"
    assert hello["provenance"]["model_artifact"]["entry"] == engines.STARDIST_TORCH_ENTRY


# ── parameters ───────────────────────────────────────────────────────────

def test_flat_and_nested_files_load_the_same_stardist_values():
    from block01.utils.segmentation_config import normalize_segmentation_config as norm
    flat = norm({"method": METHOD, "prob_thresh": 0.55, "nms_thresh": 0.25})
    nested = norm({"method": METHOD, "params": {"prob_thresh": 0.55, "nms_thresh": 0.25}})
    for cfg in (flat, nested):
        assert cfg["params"]["prob_thresh"] == 0.55 and cfg["params"]["nms_thresh"] == 0.25
        assert cfg["prob_thresh"] == 0.55


def test_another_stardist_model_is_said():
    from block01.core import preseg_contract
    said = preseg_contract.ignored_settings({"method": METHOD, "model_name": "2D_paper_dsb2018"})
    assert [k for k, _v, _why in said] == ["model_name"]


def test_the_page_shows_both_parts_and_keeps_its_own_values():
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from block01.ui.step2_page import Step2Page
    from block01.utils.segmentation_config import normalize_segmentation_config
    page = Step2Page()
    try:
        page.resize(500, 1400)
        page.show()
        cfg = normalize_segmentation_config({"method": METHOD, "diameter": 30.0,
                                             "prob_thresh": 0.5, "nms_thresh": None})
        page._loading_index_selection = True
        page._apply_seg_config_to_ui(cfg)
        page._loading_index_selection = False
        page._seg_config = dict(cfg)                      # as a loaded file
        QtWidgets.QApplication.processEvents()
        assert page._cp_diam.isVisible() and page._sd_prob.isVisible()
        assert not page._nuc_diam.isVisible()             # the Cellpose nucleus pass is not this
        assert page._is_stardist()                        # the 60 MP read-area cap applies
        back = page.get_seg_config()
        assert back["prob_thresh"] == 0.5 and back["nms_thresh"] is None
        page._sd_prob.setValue(0.7)                       # the user changes it
        back = page.get_seg_config()
        assert back["prob_thresh"] == 0.7 and back["params"]["prob_thresh"] == 0.7
    finally:
        page.close()


# ── Step2 ────────────────────────────────────────────────────────────────

def test_another_hybrid_behaviour_is_asked_and_cancel_stops(tmp_path, monkeypatch):
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import test_preseg_contract as tpc
    _, out, _path = tpc._use_and_save(
        tmp_path, METHOD, tpc.COMBOS[METHOD],
        identity=dict(engines.behavior_identity("hybrid"), behavior_version=0))
    page = tpc._page_config(app, out)
    shown = []

    def exec(box):
        shown.append(box.text())
        [b for b in box.buttons() if b.text() == "Cancel"][0].click()
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", exec)
    assert page._confirm_engine_identity(page.get_seg_config()) == (False, None)
    assert len(shown) == 1 and "hybrid" in shown[0] and "0 -> 1" in shown[0]


def test_step2_runs_the_hybrid_and_keeps_no_cell_without_a_nucleus(tmp_path, monkeypatch):
    import zarr
    from block01.core import seam_merge as sm
    from test_cellpose_cell_nucleus import _step2
    worker, got, _truth = _step2(tmp_path, monkeypatch, method=METHOD)
    worker.run()
    out = worker.output_dir
    assert got["error"] == []
    assert os.path.isfile(os.path.join(out, "segmentation_meta.json"))
    cells = np.asarray(zarr.open(os.path.join(out, "global_mask.zarr"), "r"))
    nuclei = np.asarray(zarr.open(os.path.join(out, "global_nuclei_mask.zarr"), "r"))
    table = np.asarray(zarr.open(os.path.join(out, "global_nuclei_cell.zarr"), "r"))
    n = int(cells.max())
    assert n > 0 and set(table[1:].tolist()) == set(range(1, n + 1))
    sm.validate(cells, nuclei, table[1:], n)
    with open(os.path.join(out, "segmentation_meta.json"), encoding="utf-8") as f:
        store = json.load(f)["label_store"]
    assert "pairing" in store["nuclei"]


# ── plan §15.4: StarDist on a near-blank tile ────────────────────────────

def _near_blank():
    """A 600 x 600 window, ~0.1 % tissue: two nuclei (brighter at their
    centre) in one corner, every other pixel 0 -- percentile 1 ==
    percentile 99.8 == 0."""
    img = np.zeros((600, 600), np.float32)
    yy, xx = np.mgrid[:600, :600]
    for cy, cx in ((12, 12), (12, 30)):
        r2 = (yy - cy) ** 2 + (xx - cx) ** 2
        inside = r2 <= 36
        img[inside] = (0.05 + 0.05 * np.exp(-r2 / 30.0))[inside]
    assert np.percentile(img, 99.8) == np.percentile(img, 1) == 0
    return img


def test_a_normal_tile_is_normalised_exactly_as_csbdeep_does():
    pytest.importorskip("csbdeep")
    from csbdeep.utils import normalize
    rng = np.random.default_rng(0)
    img = rng.random((64, 64), np.float32) * 0.2
    np.testing.assert_array_equal(engines.stardist_input(img),
                                  normalize(img, 1, 99.8, axis=(0, 1)))


def test_a_near_blank_tile_is_normalised_over_its_tissue():
    pytest.importorskip("csbdeep")
    x = engines.stardist_input(_near_blank())
    assert np.isfinite(x).all() and float(x.max()) < 10           # not ~1e19
    assert engines.stardist_input(np.zeros((50, 50), np.float32)) is None
    assert engines.stardist_input(np.full((50, 50), 0.3, np.float32)) is None


@pytest.mark.parametrize("which", ["stardist", "hybrid"])
def test_stardist_survives_a_near_blank_tile(hybrid, which):
    """Before the fix the engine process aborted in StarDist's polygon NMS
    ("Coordinate outside allowed range")."""
    from block01.core import preseg_input
    img = _near_blank()
    if which == "stardist":
        got = engines.run(hybrid.stardist, "stardist_nuclei_dapi", img, {})["nucleus"]
    else:
        fused = np.round(np.stack([img, img, img], -1) * 65535).astype(np.uint16)  # as Step1's
        got = engines.run(hybrid, METHOD, preseg_input.model_input(
            preseg_input.INPUT_KIND[METHOD], fused), {"diameter": 16.0})["nucleus"]
    assert got.max() >= 1                                    # the nuclei are found


# ── plan §15: the split switch ───────────────────────────────────────────

@pytest.mark.parametrize("method", ["cellpose_wholecell_nuclei", METHOD])
def test_the_switch_is_on_by_default_and_both_can_be_compared(method):
    from block01.utils import segmentation_param_schema as ps
    spec = next(s for s in ps.specs(method) if s.key == "split_cells_by_nuclei")
    assert spec.default is True and ps.format_values(spec, [True, False]) == "on, off"
    vals, _ = ps.parse_values(spec, "on, off")
    assert vals == [True, False]
    with pytest.raises(ps.ParamError):
        ps.parse_values(spec, "maybe")
    values = ps.default_values(method)
    values["split_cells_by_nuclei"] = vals
    assert ps.combo_count(method, values) == 2


@pytest.mark.parametrize("method", ["cellpose_wholecell_nuclei", METHOD])
def test_switch_off_survives_flat_and_nested_files(method):
    from block01.utils.segmentation_config import normalize_segmentation_config as norm
    assert norm({"method": method})["split_cells_by_nuclei"] is True
    for cfg in ({"method": method, "split_cells_by_nuclei": False},
                {"method": method, "params": {"split_cells_by_nuclei": False}}):
        got = norm(cfg)
        assert got["split_cells_by_nuclei"] is False and got["params"]["split_cells_by_nuclei"] is False


@pytest.mark.parametrize("method", ["cellpose_wholecell_nuclei", METHOD])
def test_the_page_carries_the_switch(method):
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from block01.ui.step2_page import Step2Page
    from block01.utils.segmentation_config import normalize_segmentation_config
    page = Step2Page()
    try:
        page.resize(500, 1400)
        page.show()
        page._loading_index_selection = True            # as a loaded file
        page._apply_seg_config_to_ui(normalize_segmentation_config(
            {"method": method, "split_cells_by_nuclei": False}))
        page._loading_index_selection = False
        QtWidgets.QApplication.processEvents()
        assert page._split_cells.isVisible() and not page._split_cells.isChecked()
        back = page.get_seg_config()
        assert back["split_cells_by_nuclei"] is False and back["params"]["pairing_rules"] == 2
        page._apply_seg_config_to_ui(normalize_segmentation_config(
            {"method": "cellpose_wholecell_fusion"}))
        QtWidgets.QApplication.processEvents()
        assert not page._split_cells.isVisible()
        assert "split_cells_by_nuclei" not in page.get_seg_config()["params"]
    finally:
        page.close()


@pytest.mark.parametrize("split", [True, False])
def test_step2_pairs_with_the_switch(tmp_path, monkeypatch, split):
    """The worker hands the file's switch to the pairing (codex advisory)."""
    from block01.core import nuclei_pairing
    from test_cellpose_cell_nucleus import _step2
    seen = []
    real = nuclei_pairing.combine_cells_and_nuclei

    def spy(cells, nuclei, split=True):
        seen.append(split)
        return real(cells, nuclei, split=split)
    monkeypatch.setattr(nuclei_pairing, "combine_cells_and_nuclei", spy)
    worker, got, _truth = _step2(tmp_path, monkeypatch, method=METHOD)
    worker.seg_config["split_cells_by_nuclei"] = split
    worker.run()
    assert got["error"] == [] and seen and set(seen) == {split}
