"""Lightweight behavioral checks for the Step0 -> Step1 handoff contract."""

import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5")


class Loader:
    def __init__(self, path):
        self.filepath = str(path)
        self.correction = None
        self.corrected = None

    def channel_names(self):
        return ["DAPI", "CD68"]

    def set_correction_config(self, value):
        self.correction = value

    def set_corrected_zarr_store(self, path, decisions):
        self.corrected = (path, decisions)


class Root:
    attrs = {"mode": "roi_only"}

    def group_keys(self):
        return []


class Combo:
    def clear(self): pass
    def addItems(self, _items): pass
    def findText(self, _value): return 0
    def setCurrentIndex(self, _value): pass


class Config:
    all_channels = []
    _panels = {}
    nuc_combo = Combo()
    nuc_row = SimpleNamespace(spin=SimpleNamespace(setValue=lambda _v: None))

    def load_panel(self, *_args): pass


class StepPage:
    def __init__(self):
        self._out_edit = SimpleNamespace(setText=lambda _v: None)
        self._ome_edit = SimpleNamespace(setText=lambda _v: None)

    def set_roi_context(self, **_kwargs): pass


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fingerprint(path):
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def make_run(tmp_path, remap=False):
    base = tmp_path / "roi-1"
    step0, step1, step2 = (base / n for n in ("step0", "step1", "step2"))
    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"raw")
    corrected = step0 / "corrected_channels.zarr"
    corrected.mkdir(parents=True, exist_ok=True)
    cfg, roi, patch = (step0 / n for n in (
        "correction_config.json", "roi_config.json", "patch_config.json"))
    write_json(cfg, {"channel_decisions": {"CD68": "tophat"}})
    write_json(roi, [{"name": "R1", "bbox_fullres": [0, 10, 0, 10]}])
    write_json(patch, [{"coords": [0, 5, 0, 5]}])
    remap_path = step0 / "step0_channel_remap.json"
    remap_hash = ""
    if remap:
        from block01.utils.channel_remap_config import (
            channel_remap_config_hash, default_channel_remap_config,
        )
        remap_cfg = default_channel_remap_config(["CD68"])
        write_json(remap_path, remap_cfg)
        remap_hash = channel_remap_config_hash(remap_cfg)
    manifest = {
        "handoff_schema_version": 2,
        "step0_dir": str(step0), "step1_dir": str(step1), "step2_dir": str(step2),
        "raw_ome_path": str(raw),
        "source_identity": {"dataset_path": str(raw),
                            "dataset_fingerprint": fingerprint(raw)},
        "corrected_zarr_path": str(corrected),
        "correction_config_path": str(cfg), "roi_config_path": str(roi),
        "patch_config_path": str(patch),
        "channel_remap_config_path": str(remap_path),
        "channel_remap_config_hash": remap_hash,
        "panel_groups": {"markers": {"CD68": 1.0}},
        "panel_nucleus": "DAPI",
    }
    manifest_path = step0 / "step0_roi_result.json"
    write_json(manifest_path, manifest)
    return SimpleNamespace(base=base, step0=step0, step1=step1, step2=step2,
                           raw=raw, corrected=corrected, cfg=cfg, roi=roi,
                           patch=patch, remap=remap_path, manifest=manifest,
                           manifest_path=manifest_path)


def make_window(run, schema=1, loader_path=None):
    from block01.ui.main_window import MainWindow
    w = MainWindow.__new__(MainWindow)
    w.loader = Loader(loader_path or run.raw)
    w.step0_output = {
        "step0_dir": str(run.base / "wrong-step0"),
        "step1_dir": str(run.base / "wrong-step1"),
        "step2_dir": str(run.base / "wrong-step2"),
        "output_dir": str(run.base / "wrong-output"),
        "step0_manifest_path": str(run.manifest_path),
        "handoff_schema_version": schema,
        "corrected_zarr_path": str(run.base / "wrong.zarr"),
        "correction_config": {"wrong": True}, "rois": [{"name": "wrong"}],
        "patches": [(99, 100, 99, 100)],
        "channel_remap_config_path": str(run.base / "private.json"),
    }
    w._corrected_zarr_path = ""
    w._corrected_zarr_mode = ""
    w._corrected_decisions = {}
    w._remap_cache = None
    w._rois, w._active_roi = [], None
    w._all_patches = []
    w._patch_channel_cache, w._patch_load_ready = {}, set()
    w._preview_patch_idx = -1
    w._step2, w._step4 = StepPage(), StepPage()
    w.config = Config()
    w.prev_status = SimpleNamespace(setText=lambda _v: None)
    w._stop_all_loaders = lambda: None
    w._filter_patches_to_roi = lambda p, _r: p
    w._on_rois_changed = lambda _r: None
    w._on_patches = lambda _p: None
    w._show_active_roi_preview = lambda: None
    w._schedule_step1_session_save = lambda: None
    w._log_step1_layout = lambda _where: None
    w._zero_marker_weights = lambda: None
    w._set_gui_work_dir = lambda p: setattr(w, "_gui_work_dir", p)
    return w


@pytest.fixture()
def run(tmp_path):
    return make_run(tmp_path)


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def ready_window(monkeypatch, run):
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    return make_window(run)


def test_real_step0_writer_publishes_authoritative_manifest(app, tmp_path, monkeypatch):
    """Drive the production Step0 persistence/writer, not the fixture writer."""
    import zarr
    import block01.ui.step0.step0_page as step0_module
    from block01.ui.step0.step0_page import Step0Page
    from block01.utils.channel_remap_config import (
        channel_remap_config_hash, default_channel_remap_config,
        load_channel_remap_config,
    )

    base = tmp_path / "real-roi"
    step0, step1, step2 = (base / n for n in ("step0", "step1", "step2"))
    raw = tmp_path / "real.ome.tif"
    raw.write_bytes(b"real-raw")
    corrected = step0 / "corrected_channels.zarr"
    corrected.parent.mkdir(parents=True)
    zarr.open_group(str(corrected), mode="w")

    page = Step0Page()
    try:
        page.ome_path = str(raw)
        page.output_dir = str(step0)
        page.loader = Loader(raw)
        page.patches = [(0, 5, 0, 5)]
        page.current_patch_idx = 0
        page.nucleus_channel = "DAPI"
        page.panel_csv_path = ""
        page.panel_groups = {"markers": {"CD68": 1.0}}
        page._roi_context = {
            "roi_id": "roi-1",
            "roi_dir": str(base),
            "project_dir": str(tmp_path),
            "step_dirs": {"step0": str(step0), "step1": str(step1),
                           "step2": str(step2)},
        }
        page._is_full_wsi_mode = lambda: False
        page._clean_correction_config = lambda value: value
        page._standard_rois = lambda: [{
            "name": "R1", "bbox_fullres": [0, 5, 0, 5], "shape": [5, 5],
        }]
        page._standard_patches = lambda _rois: [{"coords": [0, 5, 0, 5]}]
        page._cond_workbench = SimpleNamespace(
            has_channel_data=lambda: True,
            build_config=lambda: default_channel_remap_config(["CD68"]),
        )
        # The test targets persistence + manifest publication. Source-aware
        # pixel calibration is covered by its dedicated suite.
        page._apply_source_aware_identity = lambda cfg: cfg
        assert page._persist_step0_remap_config() is True
        monkeypatch.setattr(
            step0_module, "mark_roi_step",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("index unavailable")),
        )

        _, _, _, written = page._write_step0_handoff(
            {"method_params": {}, "channel_decisions": {}}, str(corrected))
        manifest_path = step0 / "step0_roi_result.json"
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert on_disk["handoff_schema_version"] == 2
        identity = on_disk["source_identity"]
        assert identity["dataset_path"] == str(raw.resolve())
        assert identity["dataset_fingerprint"] == fingerprint(raw)
        assert on_disk["channel_remap_config_path"] == str(
            (step0 / "step0_channel_remap.json").resolve())
        assert on_disk["channel_remap_config_hash"] == channel_remap_config_hash(
            load_channel_remap_config(on_disk["channel_remap_config_path"]))
        assert written["step0_roi_result_path"] == str(manifest_path.resolve())

        # Mutation sanity: each v2 authority field is required by the real
        # Step1 reader; deleting any one must reject the handoff.
        import block01.ui.main_window as mw
        monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
        run = SimpleNamespace(
            base=base, step0=step0, step1=step1, step2=step2, raw=raw,
            corrected=corrected, cfg=step0 / "correction_config.json",
            roi=step0 / "roi_config.json", patch=step0 / "patch_config.json",
            remap=step0 / "step0_channel_remap.json", manifest=on_disk,
            manifest_path=manifest_path,
        )
        for field in ("source_identity", "channel_remap_config_path",
                      "channel_remap_config_hash"):
            mutated = dict(on_disk)
            mutated.pop(field)
            write_json(manifest_path, mutated)
            assert make_window(run)._load_step0_roi_result(auto=True) is False, field
            write_json(manifest_path, on_disk)

        # Manifest publication is atomic: a failed replace leaves the prior
        # committed marker untouched and removes the temporary candidate.
        old_manifest_bytes = manifest_path.read_bytes()
        real_replace = step0_module.os.replace
        def fail_replace(src, dst):
            if os.path.abspath(dst) == str(manifest_path.resolve()):
                raise OSError("publish failed")
            return real_replace(src, dst)
        monkeypatch.setattr(step0_module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="publish failed"):
            page._write_step0_handoff(
                {"method_params": {}, "channel_decisions": {}}, str(corrected))
        assert manifest_path.read_bytes() == old_manifest_bytes
        assert not manifest_path.with_name(
            manifest_path.name + f".tmp.{os.getpid()}").exists()
    finally:
        page.teardown()
        page.deleteLater()


def test_manifest_is_authoritative_over_conflicting_payload(ready_window, run):
    # An explicit v2 payload cannot be silently downgraded by a stale v1
    # manifest (the authority is the committed protocol, not legacy fallback).
    stale = dict(run.manifest)
    stale["handoff_schema_version"] = 1
    write_json(run.manifest_path, stale)
    ready_window.step0_output["handoff_schema_version"] = 2
    assert ready_window._load_step0_roi_result(auto=True) is False
    write_json(run.manifest_path, run.manifest)
    assert ready_window._load_step0_roi_result(auto=True) is True
    assert ready_window._corrected_zarr_path == str(run.corrected)
    assert ready_window.step0_output["step1_dir"] == str(run.step1)
    assert ready_window._rois[0]["name"] == "R1"


def test_v2_missing_artifact_is_rejected(ready_window, run):
    run.patch.unlink()
    assert ready_window._load_step0_roi_result(auto=True) is False


def test_v2_bad_artifact_is_rejected(ready_window, run):
    run.cfg.write_text("not-json", encoding="utf-8")
    assert ready_window._load_step0_roi_result(auto=True) is False


def test_v2_identity_mismatch_is_rejected(ready_window, run):
    run.raw.write_bytes(b"changed")
    assert ready_window._load_step0_roi_result(auto=True) is False


def test_v2_remap_hash_mismatch_is_rejected_without_fallback(monkeypatch, run):
    run = make_run(run.base.parent, remap=True)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["channel_remap_config_hash"] = "tampered"
    write_json(run.manifest_path, manifest)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    w = make_window(run)
    w._step0 = SimpleNamespace(_last_saved_remap_path=str(run.base / "private.json"))
    assert w._load_step0_roi_result(auto=True) is False
    assert w._load_step0_remap_params() == ({}, "")

    # A v2 manifest with neither remap declaration is malformed, even though
    # the remap itself is optional when the writer emits an explicit path.
    manifest.pop("channel_remap_config_path")
    manifest.pop("channel_remap_config_hash")
    write_json(run.manifest_path, manifest)
    assert make_window(run)._load_step0_roi_result(auto=True) is False


def test_step1_navigation_does_not_trust_arbitrary_loader(monkeypatch):
    from block01.ui.main_window import MainWindow
    w = MainWindow.__new__(MainWindow)
    w._step1_context_ready = False
    w.step0_done = False
    w.loader = object()  # stale loader from a cancelled/partial load
    w.step0_output = {}
    w.prev_status = SimpleNamespace(setText=lambda value: setattr(w, "status", value))
    called = []
    w._load_step0_roi_result = lambda **_kwargs: called.append(True)
    w._stack = SimpleNamespace(currentIndex=lambda: 0)
    w._go_to_step1()
    assert called == []
    assert w._stack.currentIndex() == 0


def test_legacy_handoff_keeps_payload_compatibility(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    run.manifest_path.unlink()
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    w = make_window(run, schema=1)
    w.step0_output.update(step0_dir=str(run.step0), output_dir=str(run.step0),
                          step0_manifest_path="",
                          corrected_zarr_path=str(run.corrected))
    assert w._load_step0_roi_result(auto=True) is True

    # Restart/bootstrap must fail closed on a malformed schema value rather
    # than leaking ValueError into navigation.
    manifest = dict(run.manifest)
    manifest["handoff_schema_version"] = "not-an-integer"
    write_json(run.manifest_path, manifest)
    from block01.ui.main_window import MainWindow
    bootstrap_window = MainWindow.__new__(MainWindow)
    bootstrap_window._out_path_edit = SimpleNamespace(text=lambda: str(run.step0))
    bootstrap_window._ome_path_edit = SimpleNamespace(text=lambda: "")
    assert bootstrap_window._bootstrap_step1_context_from_disk(auto=True) is False


def test_legacy_remap_uses_compatibility_fallback(tmp_path):
    run = make_run(tmp_path)
    legacy_path = run.step0 / "legacy-remap.json"
    from block01.utils.channel_remap_config import default_channel_remap_config
    write_json(legacy_path, default_channel_remap_config(["CD68"]))
    w = make_window(run, schema=1)
    w.step0_output["step0_manifest_path"] = ""
    w.step0_output["step0_dir"] = str(run.step0)
    w._step0 = SimpleNamespace(_last_saved_remap_path=str(legacy_path))
    params, path = w._load_step0_remap_params()
    assert path == str(legacy_path)
    assert "CD68" in params


def test_session_search_stays_in_current_step1_dir(tmp_path):
    run = make_run(tmp_path)
    from block01.ui.main_window import MainWindow
    w = MainWindow.__new__(MainWindow)
    w.step0_output = {"step1_dir": str(run.step1),
                      "handoff_schema_version": 2,
                      "step0_manifest_path": str(run.manifest_path),
                      "source_identity": run.manifest["source_identity"]}
    sibling = run.base / "roi-2" / "step1"
    sibling.mkdir(parents=True)
    write_json(sibling / "step1_session.json", {
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
    })
    assert w._find_step1_session() == ""

    # Even in the current Step1 directory, v2 restore requires the current
    # handoff to carry both binding fields; they are not wildcard matches.
    run.step1.mkdir(parents=True)
    current = run.step1 / "step1_session.json"
    write_json(current, {
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
    })
    assert w._find_step1_session() == str(current)
    w.step0_output["step0_manifest_path"] = ""
    assert w._find_step1_session() == ""
    w.step0_output["step0_manifest_path"] = str(run.manifest_path)
    w.step0_output["source_identity"] = {}
    assert w._find_step1_session() == ""
    w.step0_output["source_identity"] = run.manifest["source_identity"]
    current.write_text("[]", encoding="utf-8")
    assert w._find_step1_session() == ""


def test_session_identity_mismatch_is_rejected(tmp_path, app, monkeypatch):
    run = make_run(tmp_path)
    from block01.ui.main_window import MainWindow
    w = MainWindow.__new__(MainWindow)
    w.step0_output = {"step1_dir": str(run.step1),
                      "handoff_schema_version": 2,
                      "step0_manifest_path": str(run.manifest_path),
                      "source_identity": run.manifest["source_identity"]}
    run.step1.mkdir(parents=True)
    write_json(run.step1 / "step1_session.json", {
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": {"dataset_path": "other.ome.tif",
                             "dataset_fingerprint": "bad"},
    })
    assert w._find_step1_session() == ""

    # A v2 session hint may not downgrade through a v1 manifest during manual
    # restore either.
    session = run.step1 / "v2-session.json"
    write_json(session, {"handoff_schema_version": 2,
                         "step0_manifest_path": str(run.manifest_path),
                         "source_identity": run.manifest["source_identity"]})
    stale_manifest = dict(run.manifest)
    stale_manifest["handoff_schema_version"] = 1
    write_json(run.manifest_path, stale_manifest)
    from block01.ui.main_window import MainWindow
    restore_window = MainWindow.__new__(MainWindow)
    restore_window.step0_output = {}
    assert restore_window._load_previous_step1_session(
        auto=True, path=str(session)) is False

    # A valid v2 session restores successfully, but conflicting session
    # workspace hints cannot redirect the verified manifest handoff.
    write_json(run.manifest_path, run.manifest)
    valid_session = run.step1 / "valid-v2-session.json"
    write_json(valid_session, {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
        "output_dir": str(tmp_path / "evil-output"),
        "step1_dir": str(tmp_path / "evil-step1"),
        "raw_ome_path": str(tmp_path / "evil.ome.tif"),
        "corrected_zarr_path": str(tmp_path / "evil.zarr"),
        "rois": [], "patches": [], "fusion_config": {},
    })
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    valid_window = mw.MainWindow()
    try:
        valid_window._apply_step1_fusion_config = lambda _cfg: None
        valid_window._stop_all_loaders = lambda: None
        valid_window._on_rois_changed = lambda _rois: None
        valid_window._show_active_roi_preview = lambda: None
        assert valid_window._load_previous_step1_session(
            auto=True, path=str(valid_session)) is True
        assert valid_window._gui_work_dir == str(run.step1.resolve())
        assert valid_window.step0_output["step1_dir"] == str(run.step1)
        assert valid_window.step1_output["output_dir"] == str(run.step1)
        assert valid_window.step1_output["correction_config_path"] == str(run.cfg)
        assert valid_window.loader.filepath == str(run.raw)
        assert valid_window._corrected_zarr_path == str(run.corrected)
        assert valid_window._rois == [{"name": "R1", "bbox_fullres": [0, 10, 0, 10]}]
        assert valid_window._all_patches == [(0, 5, 0, 5)]
    finally:
        valid_window.close()


def test_v2_session_cannot_override_manifest_geometry_sources_or_remap(
        tmp_path, monkeypatch):
    """A v2 session is a Step1 state overlay, never a second Step0 reader."""
    run = make_run(tmp_path, remap=True)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    session_path = run.step1 / "conflicting-v2-session.json"
    run.step1.mkdir(parents=True, exist_ok=True)
    write_json(session_path, {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
        "raw_ome_path": str(tmp_path / "evil.ome.tif"),
        "corrected_zarr_path": str(tmp_path / "evil.zarr"),
        "output_dir": str(tmp_path / "evil-output"),
        "step0_dir": str(tmp_path / "evil-step0"),
        "step1_dir": str(tmp_path / "evil-step1"),
        "step2_dir": str(tmp_path / "evil-step2"),
        "roi_dir": str(tmp_path / "evil-roi"),
        "rois": [],
        "patches": [],
        "channel_remap_config_path": str(tmp_path / "evil-remap.json"),
        "channel_remap_config_hash": "evil",
        "fusion_config": {},
    })

    w = make_window(run)
    w.step0_output = {}
    w._apply_step1_fusion_config = lambda _cfg: None
    w._update_next_button = lambda: None
    assert w._load_previous_step1_session(auto=True, path=str(session_path)) is True
    assert w.loader.filepath == str(run.raw)
    assert w._corrected_zarr_path == str(run.corrected)
    assert w.step0_output["step0_dir"] == str(run.step0)
    assert w.step0_output["step1_dir"] == str(run.step1)
    assert w.step0_output["step2_dir"] == str(run.step2)
    assert w.step0_output["roi_dir"] == str(run.base)
    assert w._rois == [{"name": "R1", "bbox_fullres": [0, 10, 0, 10]}]
    assert w._all_patches == [(0, 5, 0, 5)]
    assert w.step0_output["channel_remap_config_path"] == str(run.remap)
    assert w.step0_output["channel_remap_config_hash"] == run.manifest[
        "channel_remap_config_hash"]

    # A stale session hint must not downgrade a schema-2 manifest to the
    # legacy session reader; its conflicting geometry remains ignored.
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["handoff_schema_version"] = 1
    write_json(session_path, session)
    legacy_claim = make_window(run)
    legacy_claim.step0_output = {}
    legacy_claim._apply_step1_fusion_config = lambda _cfg: None
    legacy_claim._update_next_button = lambda: None
    assert legacy_claim._load_previous_step1_session(
        auto=True, path=str(session_path)) is True
    assert legacy_claim.loader.filepath == str(run.raw)
    assert legacy_claim._corrected_zarr_path == str(run.corrected)
    assert legacy_claim._rois == [{"name": "R1", "bbox_fullres": [0, 10, 0, 10]}]
    assert legacy_claim._all_patches == [(0, 5, 0, 5)]

    manifest = dict(run.manifest)
    manifest["channel_remap_config_hash"] = "tampered"
    write_json(run.manifest_path, manifest)
    failed = make_window(run)
    failed.step0_output = {}
    failed._apply_step1_fusion_config = lambda _cfg: None
    failed._update_next_button = lambda: None
    assert failed._load_previous_step1_session(
        auto=True, path=str(session_path)) is False


@pytest.mark.parametrize("tamper", ["remap", "identity"])
def test_failed_v2_session_restore_clears_ready_and_blocks_step1(
        tmp_path, monkeypatch, tamper):
    """A failed overlay cannot reuse the ready state from an older session."""
    run = make_run(tmp_path, remap=(tamper == "remap"))
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    session_path = run.step1 / "failed-v2-session.json"
    run.step1.mkdir(parents=True, exist_ok=True)
    session_identity = run.manifest["source_identity"]
    if tamper == "identity":
        session_identity = {
            "dataset_path": str(run.raw),
            "dataset_fingerprint": "tampered",
        }
    else:
        manifest = dict(run.manifest)
        manifest["channel_remap_config_hash"] = "tampered"
        write_json(run.manifest_path, manifest)
    write_json(session_path, {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": session_identity,
        "fusion_config": {},
    })

    w = make_window(run)
    w.step0_done = True
    w._step1_context_ready = True
    w._apply_step1_fusion_config = lambda _cfg: None
    w._update_next_button = lambda: None
    w._set_step_active = lambda _idx: None
    w._log_step1_layout = lambda _where: None
    reader_calls = []
    if tamper == "identity":
        # The pre-reader binding check must reject this session before the
        # authoritative artifact reader is reached.
        w._load_step0_roi_result = lambda **_kwargs: reader_calls.append(True) or False
    stack = {"page": 0}
    w._stack = SimpleNamespace(
        currentIndex=lambda: stack["page"],
        setCurrentIndex=lambda page: stack.__setitem__("page", page),
    )

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is False
    if tamper == "identity":
        assert reader_calls == []
    assert w.step0_done is False
    assert w._step1_context_ready is False
    w._go_to_step1()
    assert stack["page"] == 0


def test_step0_write_failure_does_not_emit(monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    emitted = []
    page = Step0Page.__new__(Step0Page)
    page._btn_continue = SimpleNamespace(setEnabled=lambda _v: None)
    page._btn_load = SimpleNamespace(setEnabled=lambda _v: None)
    page._persist_step0_remap_config = lambda: True
    page._write_step0_handoff = lambda *_a: (_ for _ in ()).throw(OSError("disk full"))
    page.step0_complete = SimpleNamespace(emit=lambda payload: emitted.append(payload))
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QMessageBox.warning", lambda *_a, **_k: None)
    assert page._emit_complete({}, "out.zarr", {}) is False
    assert emitted == []
