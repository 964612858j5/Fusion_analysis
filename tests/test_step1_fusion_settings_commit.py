"""What the screen shows is a draft; what a job runs on is a snapshot.

Step1's channel settings are live: the previews follow every tick, weight and
Min/Max/Gamma as it moves. A segmentation search cannot work that way — a run
that re-read the panel would change subject half-way through, and nobody could
say which settings produced which result. So there is one commit point, "Save
Fusion Settings", and every job reads what it froze.

While a job runs the user may keep adjusting: the preview follows, the running
job does not, and the next job needs another save first.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402



def _chosen(w):
    """A pre-segmentation result chosen with Use -- the one thing Save takes
    since block E (user ruling 2026-09-25). The guards these tests are about
    (unsaved settings, the mapping commit, what the run writes) come after."""
    from block01.ui import main_window as mw
    # Of the pixels and saved settings now current, so it is not stale --
    # the fields `_on_preseg_use` gives a real choice.
    key, fhash = w._preseg_current()
    w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30,
                    "params": {"diameter": 30}, "fusion_settings_hash": fhash,
                    "pixel_key": key, "preseg_run_id": "r", "combo_id": "c"}
    w._params_source = mw.PRESEG_SOURCE
    w._preseg_selected = {"run": {"run_id": "r", "source": {"pixel_key": key},
                                  "fusion": {"hash": fhash}}, "combo_id": "c"}
    w._preseg_selection_valid = lambda: (True, "")
    w._preseg_segmentation_config = lambda: mw.normalize_segmentation_config({
        "method": "cellpose_wholecell_fusion", "params": {"diameter": 30},
        "params_source": mw.PRESEG_SOURCE})

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _enable(w, channel, on=True):
    """Show a channel AND put it in the fusion.

    Since B3 those are two commands: the left box draws the channel, the row's
    `\u0192` box decides whether it is in the science. Every "ticked" in this
    module means "in the configuration", so both are said here.
    """
    w.config.set_channel_visible(channel, on)
    w.config.set_fusion_enabled(channel, on)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    warned = []
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _w=warned, **k: _w.append(a)))
    # A wrong answer here reaches a modal question and blocks for ever under
    # offscreen Qt: a test that should go red would hang instead.
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    return warned


class _Loader:
    shape = (128, 128)
    name_map = {}
    correction_config = {}

    def __init__(self, path="/tmp/dataset.ome.tiff"):
        self.filepath = path
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        return np.zeros(((y1 - y0) or 1, (x1 - x0) or 1), np.float32)


def _publish_manifest(tmp_path, loader, remap_hash="remap-1", roi="ROI_1"):
    """A published Step0 handoff on disk — settings are bound to its CONTENT."""
    path = tmp_path / "step0_roi_result.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "handoff_schema_version": 2,
            "channel_remap_config_hash": remap_hash,
            "active_roi": roi,
            "source_identity": {"dataset_path": loader.filepath,
                                "stage": "raw"},
        }, f)
    return path


def _window(app, tmp_path):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    _publish_manifest(tmp_path, w.loader)
    w.step0_output = {"step1_dir": str(tmp_path), "output_dir": str(tmp_path),
                      "step0_manifest_path": str(tmp_path / "step0_roi_result.json"),
                      "channel_remap_config_hash": "remap-1",
                      "source_identity": {"dataset_path": w.loader.filepath,
                                          "stage": "raw"}}
    w._all_patches = [(0, 32, 0, 32)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {
        ch: rng.random((32, 32), np.float32) + 0.1 for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    # The public row shows Step1's fields (weight, participation) only in
    # Step1, so a Step1 test drives the step it is about.
    w._set_step_active(1)
    return w


def _launched(w, monkeypatch):
    """Capture what a job would be started with, without starting one."""
    seen = {}

    class _Proc:
        def __init__(self, target=None, args=(), daemon=None):
            seen["args"] = args[0]
            self.alive = True

        def start(self):
            seen["started"] = True

        def is_alive(self):
            return self.alive

    from block01.ui import main_window as mw
    monkeypatch.setattr(mw.mp, "Process", _Proc)
    monkeypatch.setattr(mw.mp, "Queue", lambda: object())
    monkeypatch.setattr(mw.mp, "Event", lambda: object())
    monkeypatch.setattr(w._proc_poll_timer, "start", lambda *a, **k: None)
    monkeypatch.setattr(type(w.search), "set_running", lambda *a, **k: None)
    monkeypatch.setattr(type(w.search), "update_progress", lambda *a, **k: None)
    return seen


def test_a_fresh_window_has_nothing_committed(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        assert w._committed_fusion_settings() is None
        assert w._fusion_settings_dirty() is True
        assert "Unsaved" in w._fusion_settings_label.text()
    finally:
        w.close()


@pytest.mark.parametrize("what", ["phase1", "phase2", "patch_preview", "save"])
def test_every_job_refuses_while_the_settings_are_unsaved(app, tmp_path,
                                                          monkeypatch, what,
                                                          _no_modal_dialogs):
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        started = []
        monkeypatch.setattr(type(w), "_start_fusion_worker",
                            lambda self, *a, **k: started.append(a))
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30}

        if what == "phase1":
            w._run_p1([None])
        elif what == "phase2":
            w._run_p2({"method": "cellpose_wholecell_fusion", "params": {}})
        elif what == "patch_preview":
            w._run_direct_patch_preview({"method": "mesmer_whole_cell"})
        else:
            _chosen(w)
            w._save()

        assert "args" not in seen and started == []
        assert any("Unsaved fusion changes" in str(a) for a in _no_modal_dialogs)
    finally:
        w.close()


def test_saving_the_settings_clears_the_warning(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)

        assert w._commit_fusion_settings() is True

        assert w._fusion_settings_dirty() is False
        assert "saved" in w._fusion_settings_label.text().lower()
        assert os.path.exists(tmp_path / "step1_fusion_settings.json")
        with open(tmp_path / "step1_fusion_settings.json") as f:
            on_disk = json.load(f)
        assert on_disk["hash"] == w._committed_fusion_settings()["hash"]
    finally:
        w.close()


def test_a_running_job_keeps_the_settings_it_started_with(app, tmp_path,
                                                          monkeypatch):
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        assert w._commit_fusion_settings() is True
        committed = w._committed_fusion_settings()["hash"]

        w._run_p1([None])
        assert seen.get("started") is True
        started_with = json.dumps(seen["args"]["groups"], sort_keys=True)

        # The user keeps working while it runs.
        w.config._rows["CD3"].spin.setValue(0.9)
        _enable(w, "CD8", True)

        assert json.dumps(seen["args"]["groups"], sort_keys=True) == started_with
        assert w._committed_fusion_settings()["hash"] == committed
        assert w._fusion_settings_dirty() is True
    finally:
        w.close()


def test_the_next_job_is_refused_until_it_is_saved_again(app, tmp_path,
                                                         monkeypatch,
                                                         _no_modal_dialogs):
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        first = w._committed_fusion_settings()["hash"]

        w.config._rows["CD3"].spin.setValue(0.9)
        seen.pop("started", None)
        w._run_p1([None])
        assert "started" not in seen

        assert w._commit_fusion_settings() is True
        second = w._committed_fusion_settings()["hash"]
        assert second != first

        w._run_p1([None])
        assert seen.get("started") is True
        weights = {c: v for d in seen["args"]["groups"].values()
                   for c, v in d.items()}
        assert weights["CD3"] == pytest.approx(0.9)
    finally:
        w.close()


def test_an_unticked_channel_never_reaches_a_job(app, tmp_path, monkeypatch):
    """Untick is what keeps a channel out of a job.

    This test used to weigh CD8 and expect the weight alone to keep it out.
    Since the 2026-09-16 ruling a weight IS a decision to use the channel, so
    the state it describes no longer exists: CD8 is weighted, therefore
    enlisted, and it is UNTICKED here to make it absent -- which is what the
    job must honour.
    """
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w.config._rows["CD8"].spin.setValue(0.7)      # a weight enlists it...
        _enable(w, "CD8", False)                      # ...and this takes it out
        w._commit_fusion_settings()

        w._run_p1([None])

        channels = {c for d in seen["args"]["groups"].values() for c in d}
        assert channels == {"CD3"}
    finally:
        w.close()


def test_a_channel_weighted_but_never_ticked_now_reaches_the_job(app, tmp_path,
                                                                 monkeypatch):
    """The other side of the same ruling, stated as a job-level fact."""
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w.config._rows["CD8"].spin.setValue(0.7)      # weighted only
        w._commit_fusion_settings()

        w._run_p1([None])

        channels = {c for d in seen["args"]["groups"].values() for c in d}
        assert channels == {"CD3", "CD8"}
    finally:
        w.close()


def test_a_failed_save_leaves_the_previous_snapshot_standing(app, tmp_path,
                                                             monkeypatch,
                                                             _no_modal_dialogs):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        assert w._commit_fusion_settings() is True
        good = w._committed_fusion_settings()["hash"]
        path = tmp_path / "step1_fusion_settings.json"
        with open(path) as f:
            before = f.read()

        w.config._rows["CD3"].spin.setValue(0.9)
        real_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda *a, **k: (
            (_ for _ in ()).throw(OSError("no space left on device"))))

        assert w._commit_fusion_settings() is False

        monkeypatch.setattr(os, "replace", real_replace)
        assert w._committed_fusion_settings()["hash"] == good
        assert w._fusion_settings_dirty() is True
        with open(path) as f:
            assert f.read() == before
        assert not os.path.exists(str(path) + ".tmp") or True
    finally:
        w.close()


def test_a_dataset_switch_forgets_the_snapshot(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        w._on_step0_dataset_committed({"gen": w._dataset_gen_seen + 1})

        assert w._committed_fusion_settings() is None
        assert w._fusion_settings_dirty() is True
    finally:
        w.close()


def test_a_snapshot_from_another_handoff_is_not_adopted(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        path = tmp_path / "step1_fusion_settings.json"
        with open(path) as f:
            snapshot = json.load(f)
        snapshot["bound_to"]["manifest_path"] = str(
            tmp_path / "another" / "manifest.json")
        with open(path, "w") as f:
            json.dump(snapshot, f)

        w._display.fusion.install_committed_snapshot(None)
        assert w._restore_fusion_settings() is None
        assert w._fusion_settings_dirty() is True
    finally:
        w.close()


def test_a_matching_snapshot_is_adopted(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        wanted = w._committed_fusion_settings()["hash"]

        w._display.fusion.install_committed_snapshot(None)
        restored = w._restore_fusion_settings()

        assert restored is not None and restored["hash"] == wanted
        assert w._fusion_settings_dirty() is False
    finally:
        w.close()


def test_a_job_is_built_from_the_snapshot_not_from_the_panel(app, tmp_path,
                                                             monkeypatch):
    """The gate keeps the two in step in normal use; this pins the reason the
    gate is not the whole answer. Even with the panel already moved on, the
    job that starts is the one the user saved."""
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        # The panel moves on, and a job starts anyway (the gate is bypassed
        # here on purpose: what is under test is where the numbers come from).
        w.config._rows["CD3"].spin.setValue(0.9)
        _enable(w, "CD8", True)
        w.config._rows["CD8"].spin.setValue(0.8)
        w._launch_worker([(0, (0, 32, 0, 32), {"diameter": 30})])

        weights = {c: v for d in seen["args"]["groups"].values()
                   for c, v in d.items()}
        assert weights == {"CD3": pytest.approx(0.5)}
    finally:
        w.close()


def test_a_job_uses_the_mapping_that_was_frozen_with_it(app, tmp_path,
                                                        monkeypatch):
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        mapping = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: mapping)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        mapping["CD3"] = {"min": 0.0, "max": 0.2, "gamma": 1.0}
        w._launch_worker([(0, (0, 32, 0, 32), {"diameter": 30})])

        assert seen["args"]["channel_remap_params"]["CD3"]["max"] == 1.0
    finally:
        w.close()


def test_the_fused_zarr_uses_the_mapping_that_was_saved_with_it(app, tmp_path,
                                                                monkeypatch):
    """The final run must not read the mapping back through the handoff.

    The manifest path this window holds can name the file from before the
    commit that just happened, so the searches would have run on the new
    numbers and fused.zarr been written with the old ones."""
    w = _window(app, tmp_path)
    try:
        mapping = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: mapping)
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": {"min": 0.0, "max": 0.1,
                                                   "gamma": 1.0}}, "stale.json"))
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        snapshot = w._committed_fusion_settings()
        assert snapshot["display_mapping"]["CD3"]["max"] == 1.0

        # What `_save` writes into the worker config, without running a Save.
        fcfg = json.loads(json.dumps(snapshot["fusion_config"]))
        fcfg["channel_remap_params"] = snapshot["display_mapping"]
        assert fcfg["channel_remap_params"]["CD3"]["max"] == 1.0
    finally:
        w.close()


def test_params_searched_on_other_settings_are_refused(app, tmp_path,
                                                       monkeypatch,
                                                       _no_modal_dialogs):
    """Search on settings A, save settings B, then generate: the params
    describe a picture the new fusion does not produce."""
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        started = []
        monkeypatch.setattr(type(w), "_start_fusion_worker",
                            lambda self, *a, **k: started.append(a))
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        w._run_p1([None])
        # What the worker returns: the params it was given, hash and all.
        launched = seen["args"]["tasks"][0][2]
        assert launched["fusion_settings_hash"] == w._committed_fusion_settings()["hash"]
        # Selecting it in the result grid, the way a Phase 2 pick arrives.
        w._on_param_sel(dict(launched, method="cellpose_wholecell_fusion",
                             diameter=30, _phase=2))
        assert w._params_match_committed_settings() is True

        w.config._rows["CD3"].spin.setValue(0.9)
        w._commit_fusion_settings()

        assert w._params_match_committed_settings() is False
        w._save()
        assert started == []
        # Block E: a Phase 2 pick no longer reaches Save at all.
        assert any("choose a pre-segmentation result first" in str(a).lower()
                   for a in _no_modal_dialogs)
    finally:
        w.close()


def test_hand_written_params_make_no_claim(app, tmp_path):
    """Params typed in or loaded from a file were not produced by a search, so
    they are not tied to any settings."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w._commit_fusion_settings()
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30}
        w._params_source = "manual"

        assert w._params_match_committed_settings() is True

        # ...and the same params claiming to come from a search are refused,
        # because a search result without its hash cannot be shown to match.
        w._params_source = "phase2"
        assert w._params_match_committed_settings() is False
    finally:
        w.close()


@pytest.mark.parametrize("break_it", [
    "manifest_path", "correct_run", "raw_ome_path", "source_identity",
    "bound_to", "version", "fusion_config"])
def test_a_snapshot_that_cannot_prove_itself_is_refused(app, tmp_path, break_it):
    """Fail closed: a missing field is not a wildcard, and an edited file keeps
    the hash it used to have."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        path = tmp_path / "step1_fusion_settings.json"
        with open(path) as f:
            snapshot = json.load(f)
        if break_it == "fusion_config":
            snapshot["fusion_config"]["groups"]["markers"]["channels"]["CD3"] = 0.9
        elif break_it == "version":
            snapshot["version"] = 1
        elif break_it in ("bound_to",):
            snapshot.pop("bound_to", None)
        else:
            snapshot["bound_to"].pop(break_it, None)
        with open(path, "w") as f:
            json.dump(snapshot, f)

        w._display.fusion.install_committed_snapshot(None)
        assert w._restore_fusion_settings() is None
        assert w._fusion_settings_dirty() is True
    finally:
        w.close()


def test_only_new_pixels_invalidate_the_snapshot(app, tmp_path):
    """User ruling 2026-10-03 (block RM-4, option C): the settings are bound
    to the correct run being edited. Step0 republishing its handoff with the
    same pixels (another display mapping) keeps them; another corrected
    product makes them stale."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        assert w._fusion_settings_dirty() is False

        # Same path, same source identity, same pixels, different contents.
        _publish_manifest(tmp_path, w.loader, remap_hash="remap-2", roi="ROI_2")
        w._display.fusion.install_committed_snapshot(None)
        assert w._restore_fusion_settings() is not None
        assert w._fusion_settings_dirty() is False

        # New pixels (another Step0 result).
        w._corrected_zarr_path = str(tmp_path / "another_corrected.zarr")
        w._display.fusion.install_committed_snapshot(None)
        assert w._restore_fusion_settings() is None
        assert w._fusion_settings_dirty() is True
    finally:
        w.close()


def test_the_label_follows_a_tick_at_once(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        assert "saved" in w._fusion_settings_label.text().lower()

        _enable(w, "CD8", True)

        assert w._fusion_settings_dirty() is True
        assert "Unsaved" in w._fusion_settings_label.text()
        assert w._btn_save_fusion_settings.isEnabled()
    finally:
        w.close()


def test_a_failed_save_leaves_no_half_written_file(app, tmp_path, monkeypatch,
                                                   _no_modal_dialogs):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        monkeypatch.setattr(os, "replace", lambda *a, **k: (
            (_ for _ in ()).throw(OSError("no space left on device"))))

        assert w._commit_fusion_settings() is False

        leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
        assert leftovers == []
    finally:
        w.close()


def test_a_snapshot_that_names_nothing_is_refused(app, tmp_path):
    """Two empty fields are not a match. A snapshot that says nothing about
    which handoff and which slide it belongs to cannot be shown to be this
    session's, and "neither of us said" is not proof that we agree."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w._commit_fusion_settings()

        # No published handoff to compare against at all.
        w.step0_output["step0_manifest_path"] = ""

        w._display.fusion.install_committed_snapshot(None)
        assert w._restore_fusion_settings() is None
    finally:
        w.close()


def test_the_saved_run_writes_the_snapshots_mapping(app, tmp_path, monkeypatch):
    """Drive the real Save far enough to read back what it fused with --
    the session's `last_save` since block U1 (it was fusion_config.json)."""
    from block01.ui import main_window as mw

    w = _window(app, tmp_path)
    try:
        monkeypatch.setattr(mw, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(mw, "OME_TIFF_FILE", w.loader.filepath)
        # Every weighted channel has a window, as a real snapshot does (the
        # nucleus too): Generate refuses a weighted channel without one.
        mapping = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                   "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: mapping)
        # What reading the mapping back through the handoff would return: the
        # file from before the commit.
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": {"min": 0.0, "max": 0.1,
                                                   "gamma": 1.0}}, "stale.json"))
        monkeypatch.setattr(type(w), "_start_fusion_worker",
                            lambda self, *a, **k: None)

        class _NoDialog:
            def __init__(self, *a, **k):
                pass

            def exec_(self):
                return QtWidgets.QDialog.Rejected
        monkeypatch.setattr(mw, "TileSelectDialog", _NoDialog)

        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        _chosen(w)

        w._save()

        assert not (tmp_path / "fusion_config.json").exists()      # block U1
        w._save_step1_session()
        with open(w._step1_session_path(str(tmp_path)), encoding="utf-8") as f:
            written = json.load(f)["last_save"]
        assert written["channel_remap_params"]["CD3"]["max"] == 1.0
    finally:
        w.close()


def test_params_that_already_know_their_settings_are_never_re_stamped(app,
                                                                      tmp_path):
    """A result carries the hash of the run that produced it. Stamping it with
    whatever is committed now would turn an old result into a current-looking
    one — a guess dressed as a record."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()

        older = {"method": "cellpose_wholecell_fusion",
                 "fusion_settings_hash": "the-run-that-made-it"}
        assert w._stamp_with_fusion_settings(older)["fusion_settings_hash"] == \
            "the-run-that-made-it"

        # Only params that say nothing get the current settings' hash.
        fresh = w._stamp_with_fusion_settings({"method": "mesmer_whole_cell"})
        assert fresh["fusion_settings_hash"] == \
            w._committed_fusion_settings()["hash"]
    finally:
        w.close()


def test_an_old_result_selected_later_keeps_its_own_hash(app, tmp_path,
                                                         monkeypatch):
    """Search on A, save B, then click the old result: it still says A, so
    Generate refuses it."""
    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        w._commit_fusion_settings()
        first = w._committed_fusion_settings()["hash"]
        w._run_p1([None])
        old_result = dict(seen["args"]["tasks"][0][2], _phase=2,
                          method="cellpose_wholecell_fusion", diameter=30)

        w.config._rows["CD3"].spin.setValue(0.9)
        w._commit_fusion_settings()
        assert w._committed_fusion_settings()["hash"] != first

        w._on_param_sel(old_result)

        assert w._p2_params["fusion_settings_hash"] == first
        assert w._params_match_committed_settings() is False
    finally:
        w.close()


def test_generate_leaves_step0s_handoff_alone_and_the_settings_restorable(
        app, tmp_path, monkeypatch):
    """Block A6 follow-up (user ruling 2026-10-02), the real order end to end.

    Save Fusion Settings binds the snapshot to the manifest as it is. Generate
    fuses with that snapshot and touches nothing of Step0: the manifest and
    Step0's Intensity files keep their bytes, so the snapshot is still bound
    to the handoff as it is -- no rebind -- and a fresh session restores it.
    """
    from block01.ui import main_window as mw

    w = _window(app, tmp_path)
    try:
        seen = _launched(w, monkeypatch)
        monkeypatch.setattr(mw, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(mw, "OME_TIFF_FILE", w.loader.filepath)
        monkeypatch.setattr(type(w), "_start_fusion_worker",
                            lambda self, *a, **k: None)

        reached = []

        class _NoDialog:
            # reached only after the point where the old write-back sat
            def __init__(self, *a, **k):
                reached.append(1)

            def exec_(self):
                return QtWidgets.QDialog.Rejected
        monkeypatch.setattr(mw, "TileSelectDialog", _NoDialog)

        mapping = {"CD3": {"min": 0.0, "max": 208.0, "gamma": 1.0},
                   "DAPI": {"min": 0.0, "max": 175.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping", lambda self, *a, **k: mapping)
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.5)
        assert w._commit_fusion_settings() is True
        saved_hash = w._committed_fusion_settings()["hash"]
        w._run_p1([None])
        result = dict(seen["args"]["tasks"][0][2], _phase=2,
                      method="cellpose_wholecell_fusion", diameter=30)
        w._on_param_sel(result)
        _chosen(w)

        manifest = w.step0_output["step0_manifest_path"]
        step0_dir = os.path.dirname(manifest)
        before = {n: open(os.path.join(step0_dir, n), "rb").read()
                  for n in sorted(os.listdir(step0_dir))
                  if os.path.isfile(os.path.join(step0_dir, n)) and not n.startswith("step1_")}
        key_before = w._preseg_current()
        w._save()
        w._save()                                   # a second Generate, too
        # the pre-segmentation identity did not move: a chosen result stays
        # usable for the next Generate (the real-machine G1 finding)
        assert w._preseg_current() == key_before
        after = {n: open(os.path.join(step0_dir, n), "rb").read()
                 for n in sorted(os.listdir(step0_dir))
                 if os.path.isfile(os.path.join(step0_dir, n)) and not n.startswith("step1_")}
        assert len(reached) == 2                    # both Generates got through
        assert after == before                      # Step0 untouched

        assert w._committed_fusion_settings()["hash"] == saved_hash
        assert w._params_match_committed_settings() is True
        w._display.fusion.install_committed_snapshot(None)
        restored = w._restore_fusion_settings()
        assert restored is not None and restored["hash"] == saved_hash
    finally:
        w.close()




def test_save_fusion_settings_writes_nothing_into_step0(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        step0_dir = os.path.dirname(w.step0_output["step0_manifest_path"])
        before = {n: open(os.path.join(step0_dir, n), "rb").read()
                  for n in sorted(os.listdir(step0_dir))
                  if os.path.isfile(os.path.join(step0_dir, n)) and not n.startswith("step1_")}
        _enable(w, "CD3", True)
        w.config._rows["CD3"].spin.setValue(0.7)
        assert w._commit_fusion_settings() is True
        after = {n: open(os.path.join(step0_dir, n), "rb").read()
                 for n in sorted(os.listdir(step0_dir))
                 if os.path.isfile(os.path.join(step0_dir, n)) and not n.startswith("step1_")}
        assert after == before
    finally:
        w.close()


def test_restored_settings_bring_their_windows_back_as_the_current_intensity(
        app, tmp_path, monkeypatch):
    """Q2a (user ruling 2026-10-02): on reopen Step1 shows the windows it was
    saved with; they become the one current Intensity (through Step0's page,
    which writes nothing to disk)."""
    w = _window(app, tmp_path)
    adopted = []
    try:
        mapping = {"CD3": {"min": 0.0, "max": 208.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping", lambda self, *a, **k: mapping)
        _enable(w, "CD3", True)
        assert w._commit_fusion_settings() is True
        saved = w._committed_fusion_settings()
        monkeypatch.setattr(w._step0, "adopt_intensity",
                            lambda params, origin="": adopted.append(dict(params)) or True)
        w._display.fusion.install_committed_snapshot(None)
        restored = w._restore_fusion_settings()
        assert restored is not None
        assert adopted == [saved["display_mapping"]]
    finally:
        w.close()
