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


def test_a_handed_over_run_is_ready(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], "Full WSI", open_slide=p["slide"])
    assert page.refusal() == ""
    assert page._btn_run.isEnabled()
    assert page._roi_combo.count() == 1 and not page._roi_combo.isEnabled()
    assert page._slide_lbl.text() == os.path.realpath(p["slide"])
    assert "3 raw, 1 from Step 0's correction" in page._source_lbl.text()
    assert page._out_edit.text() == os.path.join(
        p["ws"], "step4", "quantification_runs", "seg_20260927_120000_test", "Full_WSI")
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
    assert page._out_edit.text().endswith(os.path.join("seg_20260927_120000_test", "ROI_2"))
    page._roi_combo.setCurrentIndex(0)
    assert page._job.roi_name == "ROI 1"
    assert page._out_edit.text().endswith("ROI_1")


def test_the_job_runs_from_the_page_into_the_users_folder(tmp_path):
    p = build_project(tmp_path)
    page = _page()
    page.set_run(p["run_dir"], open_slide=p["slide"])
    mine = str(tmp_path / "my_output")
    page._out_edit.setText(mine)
    page._run()
    page._worker.wait(60000)
    for _ in range(50):
        _app.processEvents()
    assert page._errors == []
    assert page._announced and os.path.isfile(os.path.join(mine, "cell_features.csv"))
    assert os.path.isfile(os.path.join(mine, "cell_features_provenance.json"))
    assert page._btn_run.isEnabled()


def test_mainwindow_hands_step3s_choice_to_step4():
    from block01.ui.main_window import MainWindow
    choice = MainWindow._step4_choice
    mw = types.SimpleNamespace(step2_output={"output_dir": "/runs/latest"}, step3_output=None)
    assert choice(mw) == ("/runs/latest", None)
    mw._step3_mask_key = "/runs/chosen\x1fROI 2"
    assert choice(mw) == ("/runs/chosen", "ROI 2")
    assert choice(mw, "/runs/named") == ("/runs/named", None)
    empty = types.SimpleNamespace(step2_output=None, step3_output=None)
    assert choice(empty) == ("", None)
