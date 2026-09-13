"""Lightweight behavioral checks for the Step0 -> Step1 handoff contract."""

import collections
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5")


class Loader:
    #: The slide's channels. A test that needs a THIRD marker -- one that is
    #: really in the dataset, so a weight named for it before the first
    #: handoff is a present-channel pending answer rather than a name the
    #: prune drops -- passes its own.
    CHANNELS = ["DAPI", "CD68"]

    def __init__(self, path, channels=None):
        self.filepath = str(path)
        self.correction = None
        self.corrected = None
        self._channels = list(channels or self.CHANNELS)

    def channel_names(self):
        return list(self._channels)

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


def make_panel(channels, fusion, state):
    """THE REAL `ConfigPanel`, with the calls a test wants to see recorded.

    The stand-in this replaced did not emit `config_changed`, so a restore
    could not be shown to keep quiet on the signal the window actually
    follows; and the calls it recorded were the only thing several tests
    asserted on. Recording WRAPS the production method here -- the real one
    runs, and the log says it ran.
    """
    from PyQt5 import QtWidgets
    from block01.ui.step0.config_panel import ConfigPanel
    # A real widget needs a real application. Tests that never asked for the
    # `app` fixture used a panel that was not a widget at all; the panel is
    # the thing under test now, so the application is ensured here rather
    # than left to each caller to remember.
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    panel = ConfigPanel(list(channels or []), fusion=fusion)
    panel.set_display_state(state)
    panel.loaded_panels = []
    panel.restored_display = None
    panel.restored_weight_history = None
    panel.pruned = None

    def record(name, fn):
        real = getattr(panel, name)

        def wrapper(*args, **kwargs):
            fn(*args, **kwargs)
            return real(*args, **kwargs)
        setattr(panel, name, wrapper)

    record("load_panel", lambda groups, nuc: panel.loaded_panels.append(
        (dict(groups or {}), nuc)))
    record("set_channels", lambda channels, prune=True: setattr(
        panel, "pruned", bool(prune)))
    record("restore_display_state",
           lambda colors=None, visibility=None, current_channel="": setattr(
               panel, "restored_display",
               {"colors": dict(colors or {}),
                "visibility": dict(visibility or {}),
                "current": str(current_channel or "")}))
    record("restore_weight_initialization", lambda channels: setattr(
        panel, "restored_weight_history", list(channels or [])))
    return panel


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


def make_window(run, schema=1, loader_path=None, display=None):
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
    w._overlay_display_cache = {}
    # The preview's caches and its frame clock: this double is a MainWindow
    # that never ran __init__, so state the production paths touch has to be
    # named here or Qt answers the attribute lookup with "super-class
    # __init__() was never called".
    w._signal_cache = collections.OrderedDict()
    w._frame_input_rev = 0
    w._frame_pending_rev = None
    w._frame_drawing_rev = None
    w._frame_coalesced = 0
    w._frame_in_flight = False
    w._frame_last_publish = 0.0
    w._frame_input_at = 0.0
    w._pending_channel_demand, w._loader_channels, w._failed_channels = {}, {}, {}
    w._restoring_display_state = False
    w._preview_update_pending = False
    # Step1 now has a commit point between the live settings and the settings a
    # job runs on; a restore consults it, so the double needs the same fields.
    # The committed snapshot belongs to the FUSION MODEL since B3, so the
    # double carries a real one -- parentless, because this MainWindow never
    # ran `__init__` and cannot be a QObject parent.
    # THE REAL SERVICES OBJECT: it owns both halves, wires the dataset bind
    # between them, and is where the session restore transaction lives. A
    # namespace with two models in it could not be asked to restore a
    # session, which is the whole of what this round changed.
    from block01.ui.block01_display import Block01DisplayServices
    # ONE services object per PROCESS, like production: a test that walks to
    # another slide passes the one it already has rather than giving the
    # second window a second pair of owners.
    w._display = display if display is not None else Block01DisplayServices()
    state, fusion = w._display.state, w._display.fusion
    w._fusion_settings_label = None
    w._btn_save_fusion_settings = None
    w._step1_preview_mode = "overlay"
    w.prev_img = SimpleNamespace(image=None, clear=lambda: None,
                                 setImage=lambda *a, **k: None)
    w._preview_patch_idx = -1
    w._step2, w._step4 = StepPage(), StepPage()
    w.config = make_panel(w.loader.channel_names(), fusion, state)
    w.prev_status = SimpleNamespace(setText=lambda _v: None)
    w._stop_all_loaders = lambda: None
    w._filter_patches_to_roi = lambda p, _r: p
    w._on_rois_changed = lambda _r: None
    w._on_patches = lambda _p: None
    w._show_active_roi_preview = lambda: None
    w._schedule_step1_session_save = lambda: None
    w._log_step1_layout = lambda _where: None
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
def ready_window(monkeypatch, run, app):
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


def test_loading_a_handoff_hands_step0_the_geometry_baseline(ready_window, run,
                                                             app):
    """The real load entry, not a page that just saved.

    Step0's revision counter is per PAGE and starts at zero, while the
    revisions on disk belong to the DIRECTORY. A page bound to an existing
    project must take the baseline from the manifest it was bound to, or its
    first patch edit is numbered over a file that manifest is pointing at.
    """
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page()
    ready_window._step0 = page
    try:
        assert page._geometry_revision == 0
        published = dict(run.manifest)
        published["geometry_revision"] = 7
        write_json(run.manifest_path, published)

        assert ready_window._load_step0_roi_result(auto=True) is True
        assert page._geometry_revision == 7
    finally:
        del ready_window._step0
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
    # A REAL file, so the identity it names could be resolved if anything
    # were foolish enough to trust the session about its own slide.
    evil_raw = tmp_path / "evil.ome.tif"
    evil_raw.write_bytes(b"evil")
    write_json(session_path, {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
        "raw_ome_path": str(evil_raw),
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
    # ...and the SCIENCE is bound to the same slide the manifest named. The
    # loader was already proved un-redirectable; the fusion draft used to take
    # its identity from the session's own `raw_ome_path`, so a session that
    # could not move the pixels could still split the identity in two.
    identity = w._display.fusion.scientific_identity()
    assert identity is not None
    assert identity.path == str(run.raw), identity.path
    binding = w._display.state.binding()
    assert binding is not None
    assert binding.identity.path == str(run.raw)
    # ONE semantic identity for both owners, and the evil path in neither.
    assert binding.identity == identity
    assert str(evil_raw) != str(run.raw)
    assert identity.path != str(evil_raw)
    assert binding.identity.path != str(evil_raw)
    assert w._display.fusion.lifecycle() == w._display.fusion.INITIALIZED

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


# ── the older path migrates the whole scientific state too ───────────────
#
# Both restore paths now go through one migration, so the older one no longer
# has to remember to put a separate weight-history field back. What it must
# install is the RULED answer for the shape it was handed: a grouped session
# with no visibility field is one in which group membership itself was the
# participating set, so every member comes back enabled and every restored
# weight is authoritative -- zero included, and whatever the old marker says.

def test_the_v1_restore_migrates_a_grouped_session_whole(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    session = run.step1 / "v1-session.json"
    write_json(session, {
        "raw_ome_path": str(run.raw),
        "corrected_zarr_path": str(run.corrected),
        "output_dir": str(run.step0),
        "step0_dir": str(run.step0),
        "step1_dir": str(run.step1),
        "step2_dir": str(run.step2),
        "rois": [], "patches": [],
        "fusion_config": {
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {"markers": {"group_weight": 1.0,
                                   "channels": {"CD68": 0.0}}},
        },
        # Written by this program: CD68's 0 is nobody's answer.
        "channel_weight_initialized": [],
    })

    w = make_window(run)
    w.step0_output = {}
    w._update_next_button = lambda: None
    assert w._load_previous_step1_session(auto=True, path=str(session)) is True

    # THE MODELS, not a call log: the restore is a transaction on the two
    # owners now, and what it installed is what they say afterwards.
    model = w._display.fusion
    assert model.enabled_channels() == ["CD68", "DAPI"], (
        "group membership WAS the participating set before the split")
    assert model.weight_provenance("CD68") == "authoritative", (
        "a zero-weight group member is a member whose weight somebody wrote; "
        "an empty marker cannot turn it back into an absence")
    assert w._display.state.display_visibility() == {"CD68": True,
                                                     "DAPI": True}


def test_a_v1_session_without_the_field_migrates_the_same_way(tmp_path,
                                                              monkeypatch):
    """The marker's absence changes nothing: the shape is read from which
    fields the session has, and a grouped session with no visibility is
    migrated by the same rule whether or not it carries the old marker."""
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    session = run.step1 / "old-v1-session.json"
    write_json(session, {
        "raw_ome_path": str(run.raw),
        "corrected_zarr_path": str(run.corrected),
        "output_dir": str(run.step0),
        "step0_dir": str(run.step0),
        "step1_dir": str(run.step1),
        "step2_dir": str(run.step2),
        "rois": [], "patches": [],
        "fusion_config": {"nucleus": {"channel": "DAPI", "weight": 1.0},
                          "groups": {}},
    })

    w = make_window(run)
    w.step0_output = {}
    w._update_next_button = lambda: None
    assert w._load_previous_step1_session(auto=True, path=str(session)) is True

    model = w._display.fusion
    assert model.enabled_channels() == ["DAPI"]  # no group members to enable
    assert model.draft_snapshot()["provenance"] == {"DAPI": "authoritative"}


# ── a republished handoff is not a new project ───────────────────────────
#
# The user sets weights in Step1, goes back to Step0 to move an ROI, saves,
# and the authoritative reader runs again. It used to call `load_panel`
# unconditionally -- a FRESH DATASET's initialisation -- so a legal draft for
# the same slide came back as groups at 0.0 with no provenance and nothing
# taking part. The earlier test for this drove `state.bind(same identity)`,
# which is not the entry that clears anything.

def test_republishing_the_same_handoff_keeps_the_fusion_draft(
        tmp_path, monkeypatch, app):
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output, step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    assert w._load_step0_roi_result(auto=True) is True
    assert w.config.loaded_panels, "the first load did not initialise the panel"

    # The user's project for THIS slide.
    model = w._display.fusion
    model.install_draft({
        "groups": {"A": {"group_weight": 1.0, "channels": {"CD68": 0.2}},
                   "B": {"group_weight": 1.0, "channels": {"CD68": 0.7}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": ["CD68", "DAPI"],
        "provenance": {"CD68": "authoritative", "DAPI": "authoritative"},
    })
    before_full = model.full_config()
    before_effective = model.effective_config()
    before_draft = model.draft_snapshot()
    w.config.loaded_panels = []

    # ROI moved, handoff republished, the authoritative reader runs again.
    assert w._load_step0_roi_result(auto=True) is True

    assert w.config.loaded_panels == [], \
        "a republished handoff re-initialised the panel as a new dataset"
    groups = model.groups()
    assert groups["A"]["CD68"] == 0.2 and groups["B"]["CD68"] == 0.7
    assert model.weight_provenance("CD68") == "authoritative"
    assert model.fusion_enabled("CD68") is True
    assert model.full_config() == before_full
    assert model.effective_config() == before_effective
    assert model.draft_snapshot() == before_draft
    assert model.scientific_identity() is not None
    assert model.scientific_identity().path == str(run.raw)


def test_another_dataset_still_starts_from_fresh_defaults(
        tmp_path, monkeypatch, app):
    """Only a REAL new dataset gets the new-project initialisation."""
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    run = make_run(first_dir)
    other = make_run(second_dir)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    assert w._load_step0_roi_result(auto=True) is True
    model = w._display.fusion
    model.install_draft({
        "groups": {"g": {"group_weight": 1.0, "channels": {"CD68": 0.4}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": ["CD68"], "provenance": {"CD68": "explicit"},
    })
    w.config.loaded_panels = []

    # Another slide, through the same authoritative reader.
    second = make_window(other, display=w._display)   # one services object
    second.step0_output = dict(second.step0_output,
                               step0_manifest_path=str(other.manifest_path))
    second._update_next_button = lambda: None
    assert second._load_step0_roi_result(auto=True) is True

    assert second.config.loaded_panels, \
        "another dataset did not get the fresh-project initialisation"
    assert model.weight_provenance("CD68") == "absent"
    assert model.fusion_enabled("CD68") is False
    assert model.scientific_identity().path == str(other.raw)


# ── the dataset lifecycle, in the order the product runs it ──────────────
#
# Step0 binds the display in the MIDDLE of its load, and the science follows
# that bind. So by the time the authoritative reader runs, the identity has
# usually stopped moving -- and the slide has still never had a handoff.
# Asking "did the bind move the identity" answered the wrong question and
# skipped the first initialisation; the reader asks the LIFECYCLE instead.

def _prebind(w, path):
    """What Step0 does in the middle of a load, before it announces."""
    from block01.core.display_identity import resolve_identity
    w._display.state.bind(resolve_identity(str(path)))
    return w._display.fusion


def test_the_first_handoff_initialises_even_when_step0_bound_first(
        tmp_path, monkeypatch, app):
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    model = _prebind(w, run.raw)
    assert model.lifecycle() == model.BOUND_UNINITIALIZED

    # Weights named for THIS slide before Step1 exists.
    assert w._display.set_render_weight("CD68", 0.0) is True
    # A name this slide does not have IS accepted as a command -- the model
    # cannot know the channel list -- but the initialisation below drops it:
    # only this dataset's channels get a place in this dataset's project.
    assert w._display.set_render_weight("DRAQ5", 0.25) is True
    assert model.lifecycle() == model.BOUND_UNINITIALIZED, \
        "a pending answer is not a handoff"
    restored = []
    model.draft_restored.connect(lambda: restored.append(1))

    assert w._load_step0_roi_result(auto=True) is True

    assert w.config.loaded_panels, "the first handoff built no groups"
    groups = model.groups()
    assert groups, "marker groups were never established"
    marker = next(iter(groups.values()))
    assert marker.get("CD68") == 0.0, groups
    assert model.weight_provenance("CD68") == "explicit"
    assert model.nucleus()[0] == "DAPI"
    assert "DRAQ5" not in model.channels(), model.channels()
    assert model.lifecycle() == model.INITIALIZED
    assert len(restored) == 1, f"{len(restored)} completion notices"

    # ...and the first enable does not replace an explicit zero.
    model.set_fusion_enabled("CD68", True)
    assert model.channel_weight("CD68") == 0.0


def test_a_full_republish_keeps_the_project_and_its_nucleus_weight(
        tmp_path, monkeypatch, app):
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    _prebind(w, run.raw)
    assert w._load_step0_roi_result(auto=True) is True
    model = w._display.fusion

    model.install_draft({
        "groups": {"A": {"group_weight": 1.0, "channels": {"CD68": 0.2}},
                   "B": {"group_weight": 1.0, "channels": {"CD68": 0.7}}},
        "nucleus": {"channel": "DAPI", "weight": 0.6},
        "enabled": ["DAPI"],
        "provenance": {"CD68": "authoritative", "DAPI": "authoritative"},
    })
    before_full = model.full_config()
    before_effective = model.effective_config()
    before_draft = model.draft_snapshot()
    w.config.loaded_panels = []
    changed = []
    model.draft_changed.connect(lambda: changed.append(1))

    # The real loop: the handoff stops holding, Step0 saves, the reader runs
    # again. The invalidation's own page teardown is a MainWindow method this
    # stand-in cannot run, so it is recorded rather than executed -- what is
    # under test is the reader that follows it.
    discarded = []
    w._discard_step1_dataset_state = lambda **kw: discarded.append(kw)
    w._return_to_step0 = lambda _why="": None
    w._on_step0_handoff_invalidated(
        {"step0_manifest_path": str(run.manifest_path),
         "reason": "geometry", "message": "run Step0 Save"})
    assert discarded, "the invalidation did not reach the page teardown"
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    assert w._load_step0_roi_result(auto=True) is True

    assert w.config.loaded_panels == [], "a republish re-initialised the slide"
    assert model.groups()["A"]["CD68"] == pytest.approx(0.2)
    assert model.groups()["B"]["CD68"] == pytest.approx(0.7)
    assert model.weight_provenance("CD68") == "authoritative"
    assert model.fusion_enabled("DAPI") is True
    assert model.fusion_enabled("CD68") is False
    assert model.nucleus() == ("DAPI", pytest.approx(0.6)), \
        "the reader wrote 1.0 back over the project's nucleus weight"
    assert model.full_config() == before_full
    assert model.effective_config() == before_effective
    assert model.draft_snapshot() == before_draft
    assert changed == [], f"an unchanged re-read announced {len(changed)} times"


def test_another_slide_is_initialised_even_when_bound_first(
        tmp_path, monkeypatch, app):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    run = make_run(first_dir)
    other = make_run(second_dir)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    _prebind(w, run.raw)
    assert w._load_step0_roi_result(auto=True) is True
    model = w._display.fusion
    model.edit_channel_weight("CD68", 0.4)
    assert model.weight_provenance("CD68") == "explicit"

    # Step0 binds C in the middle of ITS load, then the reader runs.
    second = make_window(other, display=w._display)   # one services object
    second.step0_output = dict(second.step0_output,
                               step0_manifest_path=str(other.manifest_path))
    second._update_next_button = lambda: None
    _prebind(second, other.raw)
    assert model.lifecycle() == model.BOUND_UNINITIALIZED, \
        "binding another slide left it looking initialised"
    assert second._load_step0_roi_result(auto=True) is True

    assert second.config.loaded_panels, "the new slide got no initialisation"
    assert model.lifecycle() == model.INITIALIZED
    assert model.weight_provenance("CD68") == "absent", \
        "the previous slide's weights leaked into this one"
    assert model.channel_weight("CD68") == 0.0
    assert model.nucleus()[0] == "DAPI"
    assert model.scientific_identity().path == str(other.raw)


def test_a_session_restore_is_one_fact_for_both_owners(
        tmp_path, monkeypatch, app):
    """E: every observable callback sees ONE slide and the final project.

    The restore used to announce the science first and bind the display
    after, so a handler reading both -- the frame clock is one -- saw a new
    project against the previous slide.
    """
    run = make_run(tmp_path, remap=True)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    session_path = run.step1 / "atomic-v2-session.json"
    run.step1.mkdir(parents=True, exist_ok=True)
    write_json(session_path, {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
        "rois": [], "patches": [],
        "version": 2,
        "fusion_draft": {
            "groups": {"A": {"weight": 1.0, "members": ["CD68"]},
                       "B": {"weight": 1.0, "members": ["CD68"]}},
            "group_weights": {"A": {"CD68": 0.2}, "B": {"CD68": 0.7}},
            "nucleus": {"channel": "DAPI", "weight": 0.6},
            "enabled": ["CD68", "DAPI"],
            "provenance": {"CD68": "authoritative", "DAPI": "authoritative"},
            "channel_weight": {},
        },
        "display_visibility": {"DAPI": True, "CD68": False},
    })

    w = make_window(run)
    w.step0_output = {}
    w._update_next_button = lambda: None
    model, state = w._display.fusion, w._display.state
    # Step0 binds the slide in the middle of its own load, which is the state
    # the product is in when a session is restored; binding here keeps the
    # harness faithful to that rather than to a window that never loaded.
    _prebind(w, run.raw)
    seen = []

    phase = {"restoring": False}

    def look(*_a):
        binding = state.binding()
        seen.append({
            "restoring": phase["restoring"],
            "display": None if binding is None else binding.identity,
            "fusion": model.scientific_identity(),
            "groups": model.groups(),
            "nucleus": model.nucleus(),
            "enabled": model.enabled_channels(),
            "provenance": dict(model.draft_snapshot()["provenance"]),
        })

    state.dataset_changed.connect(look)
    state.state_installed.connect(look)
    model.dataset_bound.connect(look)
    model.draft_restored.connect(look)
    model.draft_changed.connect(look)
    saves = []
    w._schedule_step1_session_save = lambda: saves.append(1)
    # WHICH EVENT a callback belongs to. A session load runs two of them: the
    # authoritative reader initialising this slide, and the restore putting
    # the saved project back. The identity invariant holds across both; the
    # final-project invariant is about the restore.
    real_restore = w._restore_step1_scientific_state

    def restoring(sess, source_path=""):
        phase["restoring"] = True
        try:
            return real_restore(sess, source_path=source_path)
        finally:
            phase["restoring"] = False

    w._restore_step1_scientific_state = restoring

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    assert seen, "the restore announced nothing at all"
    final_identity = model.scientific_identity()
    assert final_identity is not None
    for shot in seen:
        assert shot["display"] is not None, "a callback saw no display slide"
        assert shot["display"] == final_identity, shot["display"]
        assert shot["fusion"] == final_identity, shot["fusion"]
    restored_shots = [shot for shot in seen if shot["restoring"]]
    assert restored_shots, "the restore itself announced nothing"
    for shot in restored_shots:
        assert shot["groups"]["A"]["CD68"] == pytest.approx(0.2), shot
        assert shot["groups"]["B"]["CD68"] == pytest.approx(0.7), shot
        assert shot["nucleus"] == ("DAPI", pytest.approx(0.6)), shot
        assert shot["enabled"] == ["CD68", "DAPI"], shot
        assert shot["provenance"]["CD68"] == "authoritative", shot
    assert model.lifecycle() == model.INITIALIZED

    # F: the same session again moves nothing and says nothing.
    quiet = []
    model.draft_changed.connect(lambda: quiet.append(1))
    model.draft_restored.connect(lambda: quiet.append(1))
    before = model.draft_snapshot()
    restored = w._restore_step1_scientific_state(
        json.loads(session_path.read_text(encoding="utf-8")),
        source_path=str(run.raw))
    assert model.draft_snapshot() == before
    assert restored.visibility["CD68"] is False
    assert restored.changed is False, "a repeated restore reported a change"
    assert quiet == [], f"a repeated restore announced {len(quiet)} times"


def test_a_channel_that_disappears_is_one_revision_and_one_notice(
        tmp_path, monkeypatch, app):
    """F: pruning a channel out of the project is a real change, announced
    once; re-reading the same channel set is silent."""
    run = make_run(tmp_path)
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader", lambda path: Loader(path))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())

    w = make_window(run)
    w.step0_output = dict(w.step0_output,
                          step0_manifest_path=str(run.manifest_path))
    w._update_next_button = lambda: None
    _prebind(w, run.raw)
    assert w._load_step0_roi_result(auto=True) is True
    model = w._display.fusion
    model.edit_channel_weight("CD68", 0.4)
    _ = model.draft_revision()
    changed = []
    model.draft_changed.connect(lambda: changed.append(1))
    rev = model.draft_revision()

    # Re-reading the same universe says nothing.
    w.config.set_channels(["DAPI", "CD68"])
    assert changed == []
    assert model.draft_revision() == rev

    # ...and a channel that is gone takes its answers with it, once.
    w.config.set_channels(["DAPI"])
    assert changed == [1]
    assert model.draft_revision() == rev + 1
    assert model.weight_provenance("CD68") == "absent"


# ── the session restore is ONE transaction across BOTH owners ────────────
#
# A session names one slide's science AND its display answers, and the two
# live in two objects. Restoring them one after the other -- in either order
# -- leaves a window in which a synchronous handler reads the restored
# project against the previous session's visibility, selection and colours.
# The transaction that closes that window lives in `Block01DisplayServices`;
# these tests drive it through the real window, the real panel and the real
# display state, because the half-state used to be visible on exactly the
# signal a stand-in panel did not emit.

def _atomic_session(run, name="atomic-session.json", **overrides):
    """A v2 session carrying BOTH halves: a project and a display state."""
    payload = {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(run.manifest_path),
        "source_identity": run.manifest["source_identity"],
        "rois": [], "patches": [],
        "version": 2,
        "fusion_draft": {
            "groups": {"A": {"weight": 1.0, "members": ["CD68"]},
                       "B": {"weight": 1.0, "members": ["CD68"]}},
            "group_weights": {"A": {"CD68": 0.2}, "B": {"CD68": 0.7}},
            "nucleus": {"channel": "DAPI", "weight": 0.6},
            "enabled": ["CD68", "DAPI"],
            "provenance": {"CD68": "authoritative", "DAPI": "authoritative"},
            "channel_weight": {},
        },
        "display_visibility": {"DAPI": True, "CD68": False},
        "channel_colors": {"CD68": "#ff0000", "DAPI": "#0000ff"},
        "current_channel": "CD68",
    }
    payload.update(overrides)
    run.step1.mkdir(parents=True, exist_ok=True)
    path = run.step1 / name
    write_json(path, payload)
    return path


def _restore_window(run, monkeypatch, loader_channels=None):
    """A window whose session load runs for real, with Step0's bind already
    done -- which is the state the product is in when a session is read."""
    import block01.ui.main_window as mw
    monkeypatch.setattr(mw, "OMETIFFLoader",
                        lambda path: Loader(path, loader_channels))
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    w = make_window(run, loader_path=run.raw)
    if loader_channels:
        w.loader = Loader(run.raw, loader_channels)
        w.config.set_channels(w.loader.channel_names())
    w.step0_output = {}
    w._update_next_button = lambda: None
    return w


def _full_snapshot(w):
    """Everything a callback could read from EITHER owner, at this instant."""
    state, model = w._display.state, w._display.fusion
    binding = state.binding()
    return {
        "display_identity": None if binding is None else binding.identity,
        "generation": state.generation(),
        "visibility": dict(state.display_visibility()),
        "selection": state.selected_channel(),
        "colors": {ch: state.color(ch) for ch in ("DAPI", "CD68")},
        "fusion_identity": model.scientific_identity(),
        "groups": model.groups(),
        "nucleus": model.nucleus(),
        "enabled": model.enabled_channels(),
        "provenance": dict(model.draft_snapshot()["provenance"]),
        "lifecycle": model.lifecycle(),
        "revision": model.draft_revision(),
    }


def _wire_window(w, watchers=None):
    """Wire the window's REAL entries and count what a restore produces.

    Production wiring, not a stand-in: the window follows the transaction's
    completion notice and the draft signal, and the panel's compatibility
    signal is watched rather than muted -- a restore that emits it while one
    owner is still behind is the defect this round closed.
    """
    counts = collections.Counter()
    shots = []
    state, model = w._display.state, w._display.fusion
    # WHICH EVENT a callback belongs to. A session load runs two of them: the
    # authoritative reader initialising this slide, and the session overlay
    # putting the saved answers back. Counting them together would let one
    # hide the other, so the overlay's own effects are counted under
    # "<name>@restore".
    phase = {"restoring": False}
    real_restore = w._restore_step1_scientific_state

    def restoring(sess, source_path=""):
        phase["restoring"] = True
        try:
            return real_restore(sess, source_path=source_path)
        finally:
            phase["restoring"] = False

    w._restore_step1_scientific_state = restoring

    def watch(name):
        def handler(*_a):
            counts.update([name])
            if phase["restoring"]:
                counts.update([name + "@restore"])
            shots.append((name, dict(_full_snapshot(w),
                                     restoring=phase["restoring"])))
        return handler

    def count(name):
        def bump(*_a, **_k):
            counts.update([name])
            if phase["restoring"]:
                counts.update([name + "@restore"])
        return bump

    # PRODUCTION WIRING, through a lambda: this MainWindow was built with
    # `__new__` and never ran `QObject.__init__`, so a bound method of it is
    # not a receiver Qt will deliver to. The window's real handlers still run.
    w._display.session_restored.connect(lambda result: w._on_session_restored(result))
    model.draft_changed.connect(lambda: w._on_fusion_draft_changed())
    for signal, name in (
            (w.config.config_changed, "config_changed"),
            (state.dataset_changed, "display.dataset_changed"),
            (state.state_installed, "display.state_installed"),
            (state.color_changed, "display.color_changed"),
            (state.mapping_changed, "display.mapping_changed"),
            (state.selection_changed, "display.selection_changed"),
            (state.visibility_changed, "display.visibility_changed"),
            (model.dataset_bound, "fusion.dataset_bound"),
            (model.draft_restored, "fusion.draft_restored"),
            (model.draft_changed, "fusion.draft_changed")):
        signal.connect(watch(name))
    # The leaves of the window's real refresh path: counted, not performed,
    # because this double never ran `__init__` and has no viewer to draw on.
    w._ensure_channels_cached = count("cache")
    w._schedule_preview_update = count("preview")
    w._refresh_published_render_spec = count("render_spec")
    w._refresh_weight_editor = count("weight_editor")
    w._update_fusion_settings_state = count("settings")
    w._schedule_step1_session_save = count("session_save")
    w._refresh_patch_preview = count("patch_preview")
    w._restore_fusion_settings = count("restore_settings")
    def set_preview_mode(mode, force=False, reconcile=True):
        # FAITHFUL: the real setter records the mode, and "is it already in
        # this mode" is the question the restore asks before touching it.
        count("preview_mode")()
        w._step1_preview_mode = mode

    w.set_preview_mode = set_preview_mode
    w._display.coordinator.request_frame = count("frame")
    if watchers:
        for signal, name in watchers:
            signal.connect(watch(name))
    return counts, shots


def test_a_restore_never_reaches_the_panel_signal_half_done(tmp_path,
                                                            monkeypatch, app):
    """A: the REAL `ConfigPanel`'s compatibility signal, during a restore.

    `config_changed` is what this window followed before the model existed,
    and the panel emitted it from inside `install_fusion_draft` -- before the
    display half had been bound at all. A handler on it therefore read a new
    project against the previous slide. The restore must not emit it; the
    models' own notices are what the window follows now.
    """
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, shots = _wire_window(w)
    _prebind(w, run.raw)

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    assert counts["config_changed@restore"] == 0, (
        "the restore reached the window through the panel's compatibility "
        "signal; that signal is emitted from a widget, before the "
        "transaction is whole")
    # And the panel is nevertheless a correct VIEW of the result.
    assert w.config.get_groups()["A"]["CD68"] == pytest.approx(0.2)
    assert w.config.nucleus_channel() == "DAPI"


def test_every_restore_callback_sees_both_owners_final(tmp_path, monkeypatch,
                                                       app):
    """B: identity, visibility, selection, colours AND the project, at once."""
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, shots = _wire_window(w)
    _prebind(w, run.raw)
    baseline = len(shots)

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    identity = w._display.fusion.scientific_identity()
    assert identity is not None
    manifest_raw = run.manifest["raw_ome_path"]
    assert identity.path == manifest_raw
    restore_shots = [(name, shot) for name, shot in shots[baseline:]
                     if shot["restoring"]]
    assert restore_shots, "the restore announced nothing at all"
    for name, shot in restore_shots:
        assert shot["display_identity"] == identity, (name, shot)
        assert shot["fusion_identity"] == identity, (name, shot)
        assert shot["visibility"] == {"DAPI": True, "CD68": False}, (name, shot)
        assert shot["selection"] == "CD68", (name, shot)
        assert shot["colors"] == {"CD68": "#ff0000", "DAPI": "#0000ff"}, \
            (name, shot)
        assert shot["groups"]["A"]["CD68"] == pytest.approx(0.2), (name, shot)
        assert shot["groups"]["B"]["CD68"] == pytest.approx(0.7), (name, shot)
        assert shot["nucleus"] == ("DAPI", pytest.approx(0.6)), (name, shot)
        assert shot["enabled"] == ["CD68", "DAPI"], (name, shot)
        assert shot["provenance"]["CD68"] == "authoritative", (name, shot)
        assert shot["lifecycle"] == "initialized", (name, shot)


def test_a_real_restore_costs_one_refresh_one_frame_and_one_save(
        tmp_path, monkeypatch, app):
    """C: the session overlay's OWN side effects, counted apart from the
    authoritative reader's first initialisation of the same slide."""
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, run.raw)

    # The reader initialises this slide first; that is a different event and
    # is not what this test counts.
    assert w._load_step0_roi_result(auto=True) is True
    before_rev = w._display.fusion.draft_revision()
    before_gen = w._display.state.generation()
    counts.clear()

    sess = json.loads(session_path.read_text(encoding="utf-8"))
    restored = w._restore_step1_scientific_state(
        sess, source_path=str(run.raw))
    assert restored.visibility == {"DAPI": True, "CD68": False}
    assert restored.changed is True

    assert w._display.fusion.draft_revision() == before_rev + 1, \
        "a restore is ONE logical command on the draft"
    assert w._display.state.generation() == before_gen, \
        "the same slide was rebound by a restore that only put state back"
    assert counts["frame"] == 1, counts
    assert counts["preview"] == 1, counts
    assert counts["session_save"] == 1, counts
    assert counts["config_changed"] == 0, counts
    assert counts["fusion.draft_changed"] == 1, counts
    assert counts["fusion.draft_restored"] == 1, counts
    assert counts["display.state_installed"] == 1, counts
    assert counts["display.dataset_changed"] == 0, (
        "the slide did not change; announcing a dataset change would retire "
        "every frame, seed and read in flight")


def test_the_same_session_restored_again_is_a_true_no_op(tmp_path,
                                                         monkeypatch, app):
    """D: nothing moves, nothing is announced, nothing is asked for."""
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, run.raw)

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True
    settled = _full_snapshot(w)
    selection_before = w.config.current_channel()
    counts.clear()

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    after = _full_snapshot(w)
    assert after["generation"] == settled["generation"], \
        "an identical session burned a display binding generation"
    assert after["revision"] == settled["revision"], \
        "an identical session bumped the draft revision"
    assert after == settled
    assert w.config.current_channel() == selection_before
    # NOTHING was announced by either owner, anywhere in the load...
    for name in ("config_changed", "display.dataset_changed",
                 "display.state_installed", "display.color_changed",
                 "display.mapping_changed", "display.selection_changed",
                 "display.visibility_changed", "fusion.dataset_bound",
                 "fusion.draft_restored", "fusion.draft_changed"):
        assert counts[name] == 0, (name, counts)
    # ...and the session overlay itself asked for nothing. The authoritative
    # reader's own save is its own event, not this one: a handoff read is
    # what it is whether or not a session follows it.
    for name in ("frame", "preview", "session_save", "cache"):
        assert counts[name + "@restore"] == 0, (name, counts)
    # The reader's own save is the only one in the whole load.
    assert counts["session_save"] == 1, counts


def test_a_restore_of_another_slide_moves_the_binding_once(tmp_path,
                                                           monkeypatch, app):
    """E: B -> A migrates the binding once, and every callback sees A."""
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    other = make_run(first_dir)            # B: the slide already on screen
    run = make_run(second_dir, remap=True)  # A: the session's slide
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, shots = _wire_window(w)
    _prebind(w, other.raw)                 # bound to B when the session opens
    before_gen = w._display.state.generation()
    counts.clear()
    baseline = len(shots)

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    identity = w._display.fusion.scientific_identity()
    assert identity.path == str(run.raw)
    assert w._display.state.identity() == identity
    assert w._display.state.generation() == before_gen + 1, \
        "another slide is ONE new binding, not one per field"
    assert counts["display.dataset_changed"] == 1, counts
    for name, shot in shots[baseline:]:
        if not shot["restoring"]:
            continue
        assert shot["display_identity"] == identity, (name, shot)
        assert shot["fusion_identity"] == identity, (name, shot)
        assert shot["visibility"] == {"DAPI": True, "CD68": False}, (name, shot)


def test_the_same_slide_with_other_answers_installs_without_rebinding(
        tmp_path, monkeypatch, app):
    """E: A -> A with a CHANGED session moves fields, not the binding."""
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    first = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, run.raw)
    assert w._load_previous_step1_session(auto=True, path=str(first)) is True
    gen = w._display.state.generation()
    rev = w._display.fusion.draft_revision()
    counts.clear()

    changed = _atomic_session(
        run, name="atomic-session-2.json",
        display_visibility={"DAPI": True, "CD68": True},
        current_channel="DAPI")
    assert w._load_previous_step1_session(auto=True, path=str(changed)) is True

    assert w._display.state.generation() == gen, "the slide did not change"
    assert w._display.state.display_visibility() == {"DAPI": True,
                                                     "CD68": True}
    assert w._display.state.selected_channel() == "DAPI"
    assert counts["display.state_installed"] == 1, counts
    assert counts["display.dataset_changed"] == 0, counts
    assert counts["frame@restore"] == 1, counts
    assert counts["session_save@restore"] == 1, counts
    # TWO for the whole load, and no more: the authoritative reader saves
    # once for the handoff it read, the restore saves once for the session.
    # The display half used to ask for a third, after the transaction had
    # already asked for its one.
    assert counts["session_save"] == 2, counts
    # The science did not move, so the DRAFT said nothing -- and the window
    # still refreshed once, from the transaction's own completion notice.
    assert w._display.fusion.draft_revision() == rev, counts
    assert counts["fusion.draft_changed"] == 0, counts


def test_a_present_channel_pending_answer_reaches_every_group(tmp_path,
                                                              monkeypatch,
                                                              app):
    """The first handoff adopts weights named for channels the slide HAS.

    The previous round's evidence used a channel the dataset does not carry,
    which the prune drops -- so it showed nothing about a real marker whose
    weight was set before Step1 built a group for it. Here both markers are
    in the loader, one is in two groups, and `0.0` is as much an answer as
    `0.25`.
    """
    channels = ["DAPI", "CD68", "CD8"]
    run = make_run(tmp_path)
    run.manifest["panel_groups"] = {"markers": {"CD68": 1.0, "CD8": 1.0},
                                    "second": {"CD8": 1.0}}
    write_json(run.manifest_path, run.manifest)
    w = _restore_window(run, monkeypatch, loader_channels=channels)
    w.step0_output = dict(make_window(run).step0_output,
                          step0_manifest_path=str(run.manifest_path))
    model = _prebind(w, run.raw)
    assert model.lifecycle() == model.BOUND_UNINITIALIZED

    assert w._display.set_render_weight("CD68", 0.0) is True
    assert w._display.set_render_weight("CD8", 0.25) is True
    assert model.weight_provenance("CD68") == "explicit"
    assert model.weight_provenance("CD8") == "explicit"

    assert w._load_step0_roi_result(auto=True) is True

    groups = model.groups()
    assert set(groups) == {"markers", "second"}
    assert groups["markers"]["CD68"] == pytest.approx(0.0)
    assert groups["markers"]["CD8"] == pytest.approx(0.25)
    # In EVERY group it belongs to, not only the first one built.
    assert groups["second"]["CD8"] == pytest.approx(0.25)
    assert model.weight_provenance("CD68") == "explicit"
    assert model.weight_provenance("CD8") == "explicit"
    assert model.lifecycle() == model.INITIALIZED

    # A first enable answers 1.0 only for a channel nobody has weighted.
    assert model.set_fusion_enabled("CD68", True) is True
    assert model.set_fusion_enabled("CD8", True) is True
    assert model.groups()["markers"]["CD68"] == pytest.approx(0.0)
    assert model.groups()["markers"]["CD8"] == pytest.approx(0.25)
    assert model.groups()["second"]["CD8"] == pytest.approx(0.25)


def test_a_failed_prepare_leaves_neither_owner_moved(tmp_path, monkeypatch,
                                                     app):
    """A prepare that raises part way through takes itself back.

    Staging is a WRITE. The rollback point used to be armed only after those
    writes had succeeded, so a bind or an install that raised left the display
    on the new slide with nothing recorded to undo it: the host's
    `cancel_restore` found no pending restore, and the transaction ended with
    the science rolled back to A and the display standing on B.
    """
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    other = make_run(first_dir)             # A: the slide on screen
    run = make_run(second_dir, remap=True)  # B: the session's slide
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, other.raw)
    state, model = w._display.state, w._display.fusion
    state.set_display_visible("DAPI", True, origin="test")
    state.set_color("CD68", "#123456", origin="test")
    state.set_mapping("DAPI", 3.0, 4.0, 1.0, origin="test")
    before = {
        "identity": state.identity(),
        "generation": state.generation(),
        "visibility": dict(state.display_visibility()),
        "selection": state.selected_channel(),
        "colors": {ch: state.color(ch) for ch in ("DAPI", "CD68")},
        "mapping": state.mapping("DAPI"),
        "mapping_rev": state.mapping_revision(),
        "color_rev": state.color_revision(),
        "namespaces": list(state.namespace_identities()),
        "fusion_identity": model.scientific_identity(),
        "draft": model.draft_snapshot(),
        "revision": model.draft_revision(),
        "lifecycle": model.lifecycle(),
    }
    counts.clear()

    boom = ValueError("the payload could not be written")

    def explode(_ns, _payload):
        raise boom

    monkeypatch.setattr(state, "_install_into", explode)

    sess = json.loads(session_path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        w._restore_step1_scientific_state(sess, source_path=str(run.raw))

    assert state.restore_pending() is False
    assert model.restore_pending() is False
    assert state.identity() == before["identity"], "the display kept the slide"
    assert state.generation() == before["generation"], \
        "a failed prepare burned a binding generation"
    assert dict(state.display_visibility()) == before["visibility"]
    assert state.selected_channel() == before["selection"]
    assert {ch: state.color(ch) for ch in ("DAPI", "CD68")} == before["colors"]
    assert state.mapping("DAPI") == before["mapping"]
    assert state.mapping_revision() == before["mapping_rev"]
    assert state.color_revision() == before["color_rev"]
    assert list(state.namespace_identities()) == before["namespaces"]
    assert model.scientific_identity() == before["fusion_identity"], \
        "the science moved to the session's slide while the display did not"
    assert model.draft_snapshot() == before["draft"]
    assert model.draft_revision() == before["revision"]
    assert model.lifecycle() == before["lifecycle"]
    for name in ("config_changed", "display.dataset_changed",
                 "display.state_installed", "display.color_changed",
                 "display.mapping_changed", "display.selection_changed",
                 "display.visibility_changed", "fusion.dataset_bound",
                 "fusion.draft_restored", "fusion.draft_changed",
                 "frame", "preview", "session_save", "cache",
                 "patch_preview", "restore_settings"):
        assert counts[name] == 0, (name, counts)


def test_an_already_loaded_session_costs_nothing_in_the_whole_load(
        tmp_path, monkeypatch, app):
    """The WHOLE second load, not only the part inside the transaction.

    The transaction returned "nothing moved" and the window went on into the
    display-restore chain anyway, so an identical session still reloaded the
    Fusion Settings, re-checked the channel cache and redrew the patch
    preview. Counting only what happened while the restore was on the stack
    could not see it, because that chain runs after the restore returns.
    """
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    session_path = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, run.raw)

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True
    settled = _full_snapshot(w)
    counts.clear()

    assert w._load_previous_step1_session(
        auto=True, path=str(session_path)) is True

    assert _full_snapshot(w) == settled
    for name in ("cache", "patch_preview", "restore_settings", "preview",
                 "frame", "preview_mode", "config_changed",
                 "display.state_installed", "fusion.draft_changed"):
        assert counts[name] == 0, (name, counts)
    # The authoritative reader's own save is the only work the second load
    # does, and it is the reader's, not the session overlay's.
    assert counts["session_save"] == 1, counts


def test_a_session_that_only_changes_the_preview_mode_refreshes_once(
        tmp_path, monkeypatch, app):
    """Mode moved, owners did not: one mode switch and ONE refresh chain."""
    run = make_run(tmp_path, remap=True)
    w = _restore_window(run, monkeypatch)
    first = _atomic_session(run)
    counts, _shots = _wire_window(w)
    _prebind(w, run.raw)
    assert w._load_previous_step1_session(auto=True, path=str(first)) is True
    assert w._step1_preview_mode == "overlay"
    gen = w._display.state.generation()
    rev = w._display.fusion.draft_revision()
    counts.clear()

    other_mode = _atomic_session(run, name="atomic-session-fusion.json",
                                 preview_mode="fusion")
    assert w._load_previous_step1_session(
        auto=True, path=str(other_mode)) is True

    assert w._step1_preview_mode == "fusion"
    assert counts["preview_mode"] == 1, counts
    # ONE redraw for the mode, and no second one: the owners did not move, so
    # nothing announced a restore and the chain ran exactly once.
    assert counts["restore_settings"] == 1, counts
    assert counts["patch_preview"] == 1, counts
    assert counts["cache"] == 1, counts
    assert counts["frame"] == 0, counts
    assert counts["display.state_installed"] == 0, counts
    assert counts["fusion.draft_changed"] == 0, counts
    assert w._display.state.generation() == gen
    assert w._display.fusion.draft_revision() == rev

    # ...and loading it AGAIN, now that the mode matches, costs nothing.
    counts.clear()
    assert w._load_previous_step1_session(
        auto=True, path=str(other_mode)) is True
    for name in ("preview_mode", "restore_settings", "patch_preview", "cache",
                 "frame", "preview"):
        assert counts[name] == 0, (name, counts)
