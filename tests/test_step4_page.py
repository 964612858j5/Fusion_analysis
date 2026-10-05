"""Block S4-1: the Step4 page (`ui/step4_page.py`) and MainWindow's hand-off.

  * a run handed over: its region, its slide (read-only), the source summary,
    the default output folder, `Extract Features` available;
  * an old run (no label store), a run of another slide, a failed source
    check: the reason on the page, `Extract Features` unavailable;
  * a run with two regions: both listed; choosing one moves the output;
  * the five fast statistics, all checked; no median / p90;
  * a user-edited output folder is kept when the job runs; the job runs to
    the end from the page;
  * MainWindow hands Step3's chosen (run, region) to Step4.

Offscreen; synthetic projects in the test's temporary directory only.
"""

import os

from block01.workers.feature_extract_worker import NEW_QUANT_RUN  # noqa: E402
import shutil
import sys
import types

from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from test_quant_sources import build_project  # noqa: E402

from block01.ui.step4_page import Step4Page  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _page():
    page = Step4Page()
    page._announced = []
    page._errors = []
    page._announce = lambda title, text: page._announced.append(text)
    page._report_error = lambda msg: page._errors.append(msg)
    return page


def _produced(p, region="Full_WSI"):
    """Block RM (§5): where the page's last run wrote -- the newest quant run
    of the workspace."""
    from block01.utils import run_store
    return os.path.join(run_store.latest_run(p["ws"], "quant"), region)


def test_a_handed_over_run_is_ready(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], "Full WSI", open_slide=p["slide"])
    assert page.refusal() == ""
    assert page._btn_run.isEnabled()
    assert page._roi_combo.count() == 1 and not page._roi_combo.isEnabled()
    assert page._slide_lbl.text() == os.path.realpath(p["slide"])
    assert "3 raw, 1 from Step 0's correction" in page._source_lbl.text()
    # block RM (§5): a result of a project goes into a new quant run
    assert page._out_edit.text() == os.path.join(p["ws"], "runs", NEW_QUANT_RUN, "Full_WSI")
    assert page._out_edit.isReadOnly()
    assert not page._reason_lbl.isVisible()


def test_statistics_are_the_five_fast_ones_all_checked():
    page = _page()
    assert list(page._stat_checks) == ["mean", "sum", "std", "min", "max"]
    assert all(cb.isChecked() for cb in page._stat_checks.values())


def test_an_old_run_says_why(tmp_path):
    p = build_project(tmp_path, label_store=False)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert "re-run Step2" in page.refusal()
    assert not page._btn_run.isEnabled()


def test_a_run_of_another_slide_is_refused(tmp_path):
    p = build_project(tmp_path)
    other = os.path.join(p["root"], "other.ome.tif")
    shutil.copy(p["slide"], other)
    page = _page()
    page.set_run(p["run_dir"], open_slide=other)
    assert "another slide" in page.refusal()
    assert not page._btn_run.isEnabled()


def test_a_failed_source_check_says_why(tmp_path):
    p = build_project(tmp_path)
    shutil.rmtree(p["zarr"])
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert "corrected product" in page.refusal()
    assert not page._btn_run.isEnabled()


def test_two_regions_are_listed_and_the_output_follows_the_choice(tmp_path):
    p = build_project(tmp_path, rois=[("ROI 1", [0, 60, 0, 70]), ("ROI 2", [70, 150, 80, 170])])
    page = _page()
    page.set_run(p["run_dir"], "ROI 2", open_slide=p["slide"])
    assert [page._roi_combo.itemText(i) for i in range(2)] == ["ROI 1", "ROI 2"]
    assert page._roi_combo.isEnabled()
    assert page._out_edit.text().endswith(os.path.join(NEW_QUANT_RUN, "ROI_2"))
    page._roi_combo.setCurrentIndex(0)
    assert page._job.roi_name == "ROI 1"
    assert page._out_edit.text().endswith("ROI_1")


def test_the_job_runs_from_the_page_into_a_new_quant_run(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    page._run()
    page._worker.wait(60000)
    for _ in range(50):
        _app.processEvents()
    assert page._errors == []
    assert page._announced
    mine = _produced(p)
    assert sorted(os.listdir(mine)) == ["cell_features.h5ad", "cell_features_provenance.json"]
    assert page._btn_run.isEnabled()


def test_mainwindow_hands_step3s_choice_to_step4():
    """Block A8 (C-a): Step4 reads the ONE current segmentation run of the
    project state -- the latest Step2 result until Step3 chooses one -- and
    no longer guesses through the mask key / step3_output / step2_output."""
    from block01.core.project_state import ProjectState
    from block01.ui.main_window import MainWindow
    choice = MainWindow._step4_choice
    mw = types.SimpleNamespace(_project_state=ProjectState())
    mw._project_state.choose_segmentation_run("/runs/latest", origin="step2")
    assert choice(mw) == ("/runs/latest", None)
    mw._project_state.choose_segmentation_run("/runs/chosen", "ROI 2", origin="step3")
    assert choice(mw) == ("/runs/chosen", "ROI 2")
    assert choice(mw, "/runs/named") == ("/runs/named", None)
    mw._step3_mask_key = "/runs/elsewhere\x1fROI 9"       # display cache: not read
    assert choice(mw) == ("/runs/chosen", "ROI 2")
    empty = types.SimpleNamespace(_project_state=ProjectState())
    assert choice(empty) == ("", None)



# ── block S4-2: the output scope questionnaire ───────────────────────────

def test_the_groups_are_open_and_fold():
    page = _page()
    page.show()
    try:
        for key, (arrow, body) in page._groups.items():
            assert arrow.isChecked() and body.isVisible(), key
            arrow.setChecked(False)
            assert not body.isVisible(), key
            arrow.setChecked(True)
            assert body.isVisible(), key
    finally:
        page.close()


def test_a_run_without_nuclei_greys_out_what_needs_them(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    for cb in (page._region_checks["nucleus"], page._region_checks["cytoplasm"],
               page._feature_checks["nuclear_summary"]):
        assert not cb.isEnabled() and not cb.isChecked()
        assert "no nuclei" in cb.toolTip()
    assert page.scope() == (["mean", "sum", "std", "min", "max"], [], ["morphology"], False)


def test_a_run_with_nuclei_checks_them_by_default(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    stats, regions, features, csv = page.scope()
    assert regions == ["nucleus", "cytoplasm"]
    assert features == ["morphology", "nuclear_summary"] and csv is False
    assert not page._risk_lbl.isVisibleTo(page) and page._risk_lbl.text() == ""


def test_the_always_there_items_cannot_be_unchecked(tmp_path):
    page = _page()
    for cb in (page._region_checks["cell"], page._feature_checks["expression"],
               page._output_checks["h5ad"]):
        assert cb.isChecked() and not cb.isEnabled()


def test_a_nuclei_only_run_names_its_primary_region(tmp_path):
    p = build_project(tmp_path, nuclei_only=True)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert page._region_checks["cell"].text() == "Nucleus"
    assert not page._region_checks["cytoplasm"].isEnabled()
    assert "X = nucleus mean" in page._prefix_info.text()


def test_the_outputs_line_follows_the_choice(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert "cell_features.h5ad" in page._prefix_info.text()
    assert "cell_features.csv" not in page._prefix_info.text()
    assert "X = cell mean" in page._prefix_info.text()
    page._output_checks["csv"].setChecked(True)
    page._stat_checks["mean"].setChecked(False)
    assert "cell_features.csv" in page._prefix_info.text()
    assert "X = cell sum" in page._prefix_info.text()
    page._prefix_edit.setText("s1")
    assert "s1_cell_features.h5ad" in page._prefix_info.text()
    for cb in page._stat_checks.values():
        cb.setChecked(False)
    assert "choose at least one statistic" in page._prefix_info.text()


def test_an_old_run_shows_the_risk(tmp_path):
    p = build_project(tmp_path, nuclei=True, seam_merge=False)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert page._risk_lbl.text().startswith("Risk:") and page._btn_run.isEnabled()
    page._region_checks["nucleus"].setChecked(False)
    page._region_checks["cytoplasm"].setChecked(False)
    page._feature_checks["nuclear_summary"].setChecked(False)
    assert page._risk_lbl.text() == ""


def test_the_page_runs_the_chosen_scope(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    page._output_checks["csv"].setChecked(True)
    page._region_checks["cytoplasm"].setChecked(False)
    page._run()
    page._worker.wait(60000)
    for _ in range(50):
        _app.processEvents()
    assert page._errors == []
    import anndata
    mine = _produced(p)
    ad = anndata.read_h5ad(os.path.join(mine, "cell_features.h5ad"))
    assert "nucleus_mean" in ad.layers and "cytoplasm_mean" not in ad.layers
    assert os.path.isfile(os.path.join(mine, "cell_features.csv"))


# ── block S4-3: distribution statistics on the page ──────────────────────

def _check_markers(page, names):
    from PyQt5.QtCore import Qt
    for i in range(page._marker_list.count()):
        item = page._marker_list.item(i)
        if item.text() in names:
            item.setCheckState(Qt.Checked)


def test_distribution_is_off_and_its_marker_list_hidden_by_default(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert all(not cb.isChecked() for cb in page._dist_checks.values())
    assert list(page._dist_checks) == ["median", "p90", "p95", "gini"]
    assert not page._marker_list.isVisibleTo(page)
    assert [page._marker_list.item(i).text() for i in range(page._marker_list.count())] == \
        ["DAPI", "CD3", "CD8", "PanCK"]
    assert page.distribution_scope() == ([], [])


def test_distribution_without_markers_cannot_run(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    page._dist_checks["median"].setChecked(True)
    assert page._marker_list.isVisibleTo(page)
    assert not page._btn_run.isEnabled()
    assert "choose the markers" in page._prefix_info.text()
    _check_markers(page, ["CD8"])
    assert page._btn_run.isEnabled()
    assert "distribution: 1 marker" in page._prefix_info.text()
    assert page.distribution_scope() == (["median"], ["CD8"])


def test_folding_statistics_hides_the_distribution_row():
    page = _page()
    page.show()
    try:
        arrow, _body = page._groups["statistics"]
        assert page._dist_widget.isVisible()
        arrow.setChecked(False)
        assert not page._dist_widget.isVisible()
        arrow.setChecked(True)
        assert page._dist_widget.isVisible()
    finally:
        page.close()


def test_the_page_runs_the_distribution_statistics(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    page._dist_checks["p90"].setChecked(True)
    _check_markers(page, ["CD3", "PanCK"])
    page._run()
    page._worker.wait(60000)
    for _ in range(50):
        _app.processEvents()
    assert page._errors == []
    import anndata
    mine = _produced(p)
    ad = anndata.read_h5ad(os.path.join(mine, "cell_features.h5ad"))
    assert set(ad.obsm.keys()) == {"cell_p90", "nucleus_p90", "cytoplasm_p90"}
    assert list(ad.obsm["cell_p90"].columns) == ["CD3", "PanCK"]
