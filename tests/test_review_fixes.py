"""Review fixes of 2026-10-31 (docs/v17_review_fixes_plan.md): the small,
self-contained items.

  * Step4's `Batch...` is not shown (§2);
  * Step1's bootstrap warns about a missing raw OME path when asked by the
    user, stays quiet when automatic (§6a);
  * the CSV reads back every float64 exactly, centroids above 1e6 px too
    (§5);
  * an unknown method name is refused, no name still means the default
    (§6b);
  * project files rewritten in place survive a failed write whole, with no
    temporary left behind (§4);
  * the code version is marked `-dirty` when a code file differs -- not for
    other files -- in the app and in the engine runner alike (§6c).

Synthetic folders in the test's temporary directory only.
"""

import json
import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_step4_batch_is_not_shown(app):
    from block01.ui.step4_page import Step4Page
    page = Step4Page()
    try:
        page.show()
        app.processEvents()
        assert not page._batch_btn.isVisible()
    finally:
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("auto, warned", [(False, True), (True, False)])
def test_a_missing_raw_path_warns_only_when_asked(app, tmp_path, monkeypatch, auto, warned):
    from block01.ui import main_window as mw
    step0 = tmp_path / "step0"
    step0.mkdir()
    (step0 / "step0_roi_result.json").write_text(json.dumps({}))
    monkeypatch.setattr(mw.QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(step0)))
    monkeypatch.setattr(mw.step0_handoff, "read_manifest", lambda path: {
        "handoff_schema_version": 2, "raw_ome_path": str(tmp_path / "missing.ome.tiff")})
    monkeypatch.setattr(mw, "OUTPUT_DIR", str(step0))
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    fake = types.SimpleNamespace()
    assert mw.MainWindow._bootstrap_step1_context_from_disk(fake, auto=auto) is False
    assert shown == (["Raw OME-TIFF path missing."] if warned else [])


def test_the_csv_reads_back_every_float64_exactly(tmp_path):
    import numpy as np
    from block01.workers import feature_extract_worker as few
    values = np.array([[1, 1234567.891234, 0.1, 1e-300],
                       [2, 9876543.21, 2.0 / 3.0, 12345678901.5]])

    class Result:
        columns = ["cell_id", "centroid_x", "CD3_mean", "CD8_sum"]
        ids = values[:, 0].astype(np.int64)

        def block(self, r0, r1):
            return values[r0:r1]
    path = tmp_path / "t.csv"
    few._write_csv(str(path), Result())
    rows = [line.split(",") for line in path.read_text().splitlines()[1:]]
    assert [r[0] for r in rows] == ["1", "2"]
    assert np.array_equal(np.array([[float(x) for x in r[1:]] for r in rows]), values[:, 1:])


def test_an_unknown_method_name_is_refused():
    from block01.utils import segmentation_config as sc
    with pytest.raises(ValueError, match="Unknown segmentation method: cellpose_typo"):
        sc.normalize_segmentation_config({"method": "cellpose_typo"})
    for empty in ({}, {"method": ""}, {"method": None}, None):
        assert sc.normalize_segmentation_config(empty)["method"] == sc.CELLPOSE_WHOLECELL_FUSION


def _project_writers(tmp_path):
    """(name, path, write(payload_marker)) of the files rewritten in place."""
    from block01.utils import channel_remap_config as crc
    from block01.utils import roi_project, segmentation_params as sp
    from block01.utils import segmentation_config as sc

    def remap(mark):
        cfg = crc.default_channel_remap_config(["CD45"])
        cfg["channels"]["CD45"]["max"] = 1000.0 + mark
        crc.save_channel_remap_config(cfg, str(tmp_path / "remap.json"))

    def index(mark):
        sp.save_segmentation_params(str(tmp_path / "settings"), {
            "method": sc.STARDIST_NUCLEI_EXPANSION, "params": {"expand_distance": float(mark)}})

    return [
        ("roi index", tmp_path / "roi_index.json",
         lambda mark: roi_project.save_json(str(tmp_path / "roi_index.json"), {"mark": mark})),
        ("params index", None, index),
        ("remap", tmp_path / "remap.json", remap),
    ]


@pytest.mark.parametrize("failure", ["dump", "replace"])
def test_a_failed_write_leaves_the_previous_file_whole(tmp_path, monkeypatch, failure):
    from block01.core import provenance as prov
    from block01.utils import segmentation_params as sp
    for name, path, write in _project_writers(tmp_path):
        write(1)
        path = path or __import__("pathlib").Path(sp.params_index_path(str(tmp_path / "settings")))
        before = path.read_bytes()

        real_dump = prov.json.dump

        def boom(*a, **k):
            raise OSError("disk full")

        def dump(obj, *a, **k):
            # only the index's own write fails, not the parameter file
            # `save_segmentation_params` writes before it
            if name == "params index" and not (isinstance(obj, dict) and "methods" in obj):
                return real_dump(obj, *a, **k)
            raise OSError("disk full")
        with monkeypatch.context() as m:
            if failure == "dump":
                m.setattr(prov.json, "dump", dump)
            else:
                m.setattr(prov.os, "replace", boom)
            with pytest.raises(OSError):
                write(2)
        assert path.read_bytes() == before, name
        assert not [f for f in os.listdir(path.parent) if ".tmp." in f], name


def _repo(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.org")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("n\n")
    git("add", ".")
    git("commit", "-q", "-m", "c")
    return git


def test_the_code_version_is_marked_dirty_only_by_code(tmp_path, monkeypatch):
    import pathlib
    from block01.core import provenance as prov
    from block01.seg_runner import runner

    def both():
        monkeypatch.setattr(runner, "_REPO", pathlib.Path(tmp_path))
        return prov.describe_checkout(str(tmp_path)), runner._describe_checkout()
    _repo(tmp_path)
    clean, same = both()
    assert clean and not clean.endswith("-dirty") and same == clean
    (tmp_path / "notes.txt").write_text("changed\n")          # not code
    assert both() == (clean, clean)
    (tmp_path / "new.py").write_text("")                       # untracked code
    assert both() == (clean + "-dirty",) * 2
    (tmp_path / "new.py").unlink()
    (tmp_path / "a.py").write_text("x = 2\n")                 # changed code
    assert both() == (clean + "-dirty",) * 2


def test_the_code_version_is_captured_once(monkeypatch):
    from block01.core import provenance as prov
    calls = []
    monkeypatch.setattr(prov, "_COMMIT", None)
    monkeypatch.setattr(prov, "describe_checkout", lambda repo: calls.append(repo) or "abc1234")
    assert prov.git_commit() == "abc1234" and prov.git_commit() == "abc1234"
    assert len(calls) == 1
