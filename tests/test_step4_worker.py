"""Block S4-1: Step4's worker (`workers/feature_extract_worker.py`).

  * the outputs: the CSV (valid cells only, the morphology columns, the
    chosen statistics, integer columns written as integers) and the
    provenance (morphology_version 2, the boundary definition, empty labels,
    the source of every channel, what was read where);
  * a corrected channel is quantified from Step0's product, not the slide;
  * a refused job, a Stop and a failing write leave no output and no
    `*.partial`;
  * the QThread: a reason (not a traceback) for a refused job; a statistic
    Step4 does not compute is named;
  * the default output folder.

Synthetic projects in the test's temporary directory only.
"""

import csv
import json
import os
import sys

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from test_quant_sources import ROI_BBOX, build_project  # noqa: E402

from block01.core import quant_engine as qe  # noqa: E402
from block01.core import quant_sources as qs  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _rows(path):
    with open(path, newline="") as f:
        r = csv.reader(f)
        header = next(r)
        return header, [row for row in r]


def _leftovers(folder):
    return [f for f in os.listdir(folder) if f.endswith(".partial")] if os.path.isdir(folder) else []


def test_outputs_and_provenance(tmp_path):
    p = build_project(tmp_path)
    lab = p["labels"]["Full WSI"]
    lab_path = os.path.join(p["run_dir"], "global_mask_Full WSI.zarr")
    import zarr
    z = zarr.open(lab_path, mode="a")
    lab = np.where(lab == 5, 0, lab).astype(np.uint32)   # label 5 becomes empty
    z[:] = lab
    out = str(tmp_path / "out")
    res = few.run_extraction(p["run_dir"], out, statistics=["mean", "max"], file_prefix="s1")
    header, rows = _rows(res["csv"])
    assert os.path.basename(res["csv"]) == "s1_cell_features.csv"
    assert header[:16] == ["cell_id"] + list(qe.MORPHOLOGY_COLUMNS)
    assert "perimeter" not in header and "circularity" not in header
    assert header[16:] == ["DAPI_mean", "DAPI_max", "CD3_mean", "CD3_max", "CD8_mean",
                           "CD8_max", "PanCK_mean", "PanCK_max"]
    present = sorted(int(v) for v in np.unique(lab) if v)
    assert [int(r[0]) for r in rows] == present
    assert all("." not in r[1] and "e" not in r[1] for r in rows)   # area as an integer
    prov = json.load(open(res["provenance"]))
    assert prov["morphology_version"] == 2
    assert prov["perimeter_definition"] == "8-neighbor label-aware boundary pixel count"
    assert prov["primary_compartment"] == "cell"
    assert prov["max_label_id"] == 40 and prov["n_valid_cells"] == len(present)
    assert 5 in prov["empty_label_ids"]
    assert prov["n_empty_labels"] == 40 - len(present)
    src = {c["name"]: c["source"] for c in prov["channels"]}
    assert src == {"DAPI": "raw", "CD3": "corrected", "CD8": "raw", "PanCK": "raw"}
    assert prov["reads"]["corrected"] >= 1 and prov["reads"]["raw"] >= 3
    assert prov["region"]["bbox_fullres"] == ROI_BBOX
    assert prov["raw_reader"] == "tiff_tiles"
    assert not _leftovers(out)


def test_a_corrected_channel_is_quantified_from_step0s_product(tmp_path):
    p = build_project(tmp_path, corrected_value=7.25)
    out = str(tmp_path / "out")
    res = few.run_extraction(p["run_dir"], out, statistics=["mean", "min", "max"])
    header, rows = _rows(res["csv"])
    for name in ("CD3_mean", "CD3_min", "CD3_max"):
        vals = {float(r[header.index(name)]) for r in rows}
        assert vals == {7.25}, name
    # and the raw channels are the slide's pixels
    lab = p["labels"]["Full WSI"]
    y0, _y1, x0, _x1 = ROI_BBOX
    dapi = p["slide_data"][0, y0:y0 + lab.shape[0], x0:x0 + lab.shape[1]].astype(float)
    ids = [int(r[0]) for r in rows]
    from scipy import ndimage as ndi
    want = ndi.mean(dapi, lab, ids)
    got = np.array([float(r[header.index("DAPI_mean")]) for r in rows])
    np.testing.assert_allclose(got, want, rtol=1e-5)


def test_a_refused_job_writes_nothing(tmp_path):
    p = build_project(tmp_path)
    import shutil
    shutil.rmtree(p["zarr"])
    out = str(tmp_path / "out")
    with pytest.raises(qs.QuantSourceError):
        few.run_extraction(p["run_dir"], out)
    assert not os.path.exists(out)


def test_stop_writes_nothing(tmp_path):
    p = build_project(tmp_path)
    out = str(tmp_path / "out")
    with pytest.raises(qe.QuantStopped):
        few.run_extraction(p["run_dir"], out, settings=qe.QuantSettings(tile=16),
                           should_stop=lambda: True)
    assert not os.path.exists(out) or not os.listdir(out)


@pytest.mark.parametrize("fail", ["csv", "provenance"])
def test_a_failing_write_leaves_no_half_output(tmp_path, monkeypatch, fail):
    p = build_project(tmp_path)
    out = str(tmp_path / "out")

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    if fail == "csv":
        monkeypatch.setattr(few.np, "savetxt", boom)
    else:
        monkeypatch.setattr(few.json, "dump", boom)
    with pytest.raises(OSError):
        few.run_extraction(p["run_dir"], out)
    assert os.listdir(out) == []


def test_a_failing_rename_of_the_provenance_removes_the_table(tmp_path, monkeypatch):
    p = build_project(tmp_path)
    out = str(tmp_path / "out")
    real = os.replace

    def replace(a, b):
        if b.endswith("_provenance.json"):
            raise OSError(5, "I/O error")
        return real(a, b)
    monkeypatch.setattr(few.os, "replace", replace)
    with pytest.raises(OSError):
        few.run_extraction(p["run_dir"], out)
    assert os.listdir(out) == []


def _run_thread(worker):
    got = {"done": None, "error": None}
    worker.extraction_done.connect(lambda d, b: got.update(done=(d, b)))
    worker.error.connect(lambda m: got.update(error=m))
    worker.start()
    worker.wait(60000)
    _app.processEvents()
    return got


def test_the_thread_reports_a_reason_not_a_traceback(tmp_path):
    p = build_project(tmp_path, label_store=False)
    got = _run_thread(few.FeatureExtractWorker(p["run_dir"], str(tmp_path / "out")))
    assert got["done"] is None
    assert "re-run Step2" in got["error"] and "Traceback" not in got["error"]


def test_the_thread_names_a_statistic_step4_does_not_compute(tmp_path):
    p = build_project(tmp_path)
    got = _run_thread(few.FeatureExtractWorker(p["run_dir"], str(tmp_path / "out"),
                                               statistics=["mean", "median"]))
    assert "median" in got["error"] and "mean, sum, std, min and max" in got["error"]
    assert not os.path.exists(tmp_path / "out")


def test_the_thread_finishes(tmp_path):
    p = build_project(tmp_path)
    out = str(tmp_path / "out")
    got = _run_thread(few.FeatureExtractWorker(p["run_dir"], out))
    assert got["error"] is None and got["done"] == (out, "cell_features")
    assert os.path.isfile(os.path.join(out, "cell_features.csv"))
    assert os.path.isfile(os.path.join(out, "cell_features_provenance.json"))


def test_the_default_output_folder(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    assert few.default_output_dir(job) == os.path.join(
        p["ws"], "step4", "quantification_runs", "seg_20260927_120000_test", "Full_WSI")
