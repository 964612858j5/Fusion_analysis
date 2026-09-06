"""Step0 keeps ChannelWorkbench as an internal Intensity/remap owner.

These tests pin the one user-facing Background Correction tab, the hidden shared
workbench, the canonical preview-only remap config, and Background Save's
validate/persist-before-handoff contract.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import os

import numpy as np
import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


class _FakeLoader:
    """Minimal OMETIFFLoader stand-in with the attrs Step0 conditioning reads."""

    def __init__(self, names=("DAPI", "CD68", "CK19"),
                 filepath="/tmp/fake.ome.tif", shape=(64, 64)):
        self._names = list(names)
        self.filepath = filepath
        self.shape = shape

    def channel_names(self):
        return list(self._names)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        # deterministic non-zero data so markers register as 2-D images
        a = np.ones((y1 - y0, x1 - x0), dtype=np.float32)
        return a


@pytest.fixture(scope="module")
def page(app):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    yield p
    p.deleteLater()


def _inject_context(page, tmp_path):
    """Equivalent to the old Step1.5 set_context(loader, output_dir, patches,
    nucleus_channel) — Step0 owns these as native attributes."""
    page.loader = _FakeLoader()
    page.output_dir = str(tmp_path)
    page.patches = [(0, 32, 0, 32), (0, 32, 32, 64)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._channel_order = ["DAPI", "CD68", "CK19"]
    page._roi_context = None


def _inject_roi_context(page, tmp_path):
    """Give the page a Step0 ROI context whose step0 dir is the unified
    save location (<roi_dir>/step0/), mirroring a real Save-and-continue."""
    roi_dir = os.path.join(str(tmp_path), "rois", "roi1")
    step0_dir = os.path.join(roi_dir, "step0")
    os.makedirs(step0_dir, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1",
        "roi_dir": roi_dir,
        "step_dirs": {"step0": step0_dir, "step1": os.path.join(roi_dir, "step1")},
    }
    return step0_dir


# ── 1. One visible tab; remap state has one hidden owner ─────────────────────
def test_step0_has_one_main_tab(page):
    assert hasattr(page, "_step0_tabs")
    tabs = [page._step0_tabs.tabText(i) for i in range(page._step0_tabs.count())]
    assert tabs == ["Background Correction"]
    assert not hasattr(page, "_cond_tab_index")


# ── 2. Hidden host owns the shared ChannelWorkbench class ─────────────────────
def test_hidden_host_owns_shared_channel_workbench(page):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    assert hasattr(page, "_cond_workbench")
    assert isinstance(page._cond_workbench, ChannelWorkbench)


# ── 3 + 4. Context pieces + load current patch channels with a fake loader ───
def test_step0_conditioning_loads_patch_channels(page, tmp_path):
    _inject_context(page, tmp_path)
    # Step0 owns the same four context pieces Step1.5 received via set_context
    assert page.loader is not None
    assert page.output_dir == str(tmp_path)
    assert len(page.patches) == 2
    assert page.nucleus_channel == "DAPI"

    page._sync_step0_to_workbench()
    assert page._cond_workbench.has_channel_data()
    cfg = page._cond_workbench.build_config()
    chans = set(cfg.get("channels", {}))
    # markers + DAPI present (#6: DAPI is now a normal conditionable channel,
    # no longer a reference-only layer).
    assert "CD68" in chans and "CK19" in chans
    assert "DAPI" in chans


# ── 5. Save AUTO-writes a preview-only config to the ROI step0 path (no dialog) ─
def test_step0_save_writes_preview_only_registered_config(page, tmp_path, monkeypatch):
    from block01.utils import channel_remap_config as crc
    _inject_context(page, tmp_path)
    step0_dir = _inject_roi_context(page, tmp_path)
    page._sync_step0_to_workbench()

    # normal Save must NOT open a file dialog
    def _boom(*a, **k):
        raise AssertionError("normal Save opened a QFileDialog")
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QFileDialog.getSaveFileName", _boom)
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QMessageBox.information",
        lambda *a, **k: None)

    page._save_step0_remap_config()
    # auto-saved to the UNIFIED <roi_dir>/step0/step0_channel_remap.json path
    out_path = page._step0_conditioning_config_path()
    assert out_path == os.path.join(step0_dir, "step0_channel_remap.json")
    assert os.path.isfile(out_path)
    # the old global step1_5/channel_remap_configs path was NOT written
    legacy = os.path.join(str(tmp_path), "step1_5", "channel_remap_configs",
                          "step0_channel_remap.json")
    assert not os.path.exists(legacy)

    saved = crc.load_channel_remap_config(out_path)
    sp = saved["source_policy"]
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False
    assert saved["created_from_step"] == "step0_channel_conditioning"
    # registered/enumerated provenance, not an ad-hoc free string
    assert crc.is_registered_created_from_step(saved["created_from_step"])
    assert saved["created_from_step"] == crc.CREATED_FROM_STEP0_CONDITIONING
    # physical storage dir recorded honestly = the ROI step0 dir
    assert saved["storage_dir"] == step0_dir


def test_step0_save_is_auto_and_idempotent_path(page, tmp_path, monkeypatch):
    # re-saving overwrites the SAME canonical file (no timestamped proliferation)
    _inject_context(page, tmp_path)
    step0_dir = _inject_roi_context(page, tmp_path)
    page._sync_step0_to_workbench()
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dialog opened")))
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QMessageBox.information",
        lambda *a, **k: None)
    page._save_step0_remap_config()
    page._save_step0_remap_config()
    cfg_dir = page._step0_conditioning_out_dir()
    assert cfg_dir == step0_dir
    jsons = [f for f in os.listdir(cfg_dir) if f.endswith(".json")]
    assert jsons == ["step0_channel_remap.json"]   # single stable file


def test_step0_save_falls_back_to_legacy_path_without_roi_context(page, tmp_path, monkeypatch):
    # No ROI context yet -> explicit fallback to the legacy step1_5 location.
    _inject_context(page, tmp_path)                # sets _roi_context = None
    page._sync_step0_to_workbench()
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dialog opened")))
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QMessageBox.information",
        lambda *a, **k: None)
    page._save_step0_remap_config()
    out_path = page._step0_conditioning_config_path()
    assert out_path.endswith(
        os.path.join("step1_5", "channel_remap_configs", "step0_channel_remap.json"))
    assert os.path.isfile(out_path)


# ── 7. ChannelWorkbench is the single shared class (not forked) ──────────────
def test_channel_workbench_not_forked(page):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    # Step0 host uses the shared class — same identity Step1.5/Step3 use.
    assert type(page._cond_workbench).__name__ == "ChannelWorkbench"
    assert isinstance(page._cond_workbench, ChannelWorkbench)
    # no Step0-only subclass/copy was introduced
    assert type(page._cond_workbench) is ChannelWorkbench


# ── step0-fix-patch-switch-perf: lazy-load (read only active channel) ─────────
class _CountingLoader(_FakeLoader):
    """Fake loader that records every read_region call (channel name)."""

    def __init__(self, n_markers=27, shape=(48, 48)):
        names = [f"M{i}" for i in range(n_markers)] + ["DAPI"]
        super().__init__(names=tuple(names), shape=shape)
        self.calls = []

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        self.calls.append(ch)
        return np.random.rand(y1 - y0, x1 - x0).astype(np.float32)


def _fresh_page_with_counting_loader(app):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    ld = _CountingLoader()
    p.loader = ld
    p.output_dir = "/tmp"
    p.patches = [(0, 24, 0, 24), (24, 48, 24, 48)]
    p.current_patch_idx = 0
    p.nucleus_channel = "DAPI"
    p._channel_order = [f"M{i}" for i in range(27)] + ["DAPI"]
    return p, ld


def test_patch_switch_reads_only_active_channel(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()          # workbench now "in use"
    ld.calls.clear()
    # simulate a patch switch's conditioning refresh
    p.current_patch_idx = 1
    p._maybe_refresh_conditioning()
    # ONLY the active channel is read (#6: DAPI is a normal lazy channel now, no
    # separate reference read) — NOT all 28. Single read, not the whole panel.
    assert len(ld.calls) == 1, ld.calls


def test_unloaded_channel_lazy_loads_on_switch(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()
    wb = p._cond_workbench
    target = next(n for n in wb._names if wb._raw.get(n) is None)
    ld.calls.clear()
    wb._on_active_changed(target)         # user selects a not-yet-loaded channel
    assert ld.calls == [target]           # one lazy read fired
    assert wb._raw.get(target) is not None  # data now available


def test_loaded_channel_switch_is_cache_hit(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()
    wb = p._cond_workbench
    target = next(n for n in wb._names if wb._raw.get(n) is None)
    wb._on_active_changed(target)         # load it once
    ld.calls.clear()
    wb._on_active_changed(target)         # re-select -> no new read
    assert ld.calls == []


def test_build_config_covers_all_channels_incl_unloaded(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()
    cfg = p._cond_workbench.build_config()
    # all 27 markers + DAPI present (#6: DAPI is a normal channel), even those
    # never read.
    assert len(cfg["channels"]) == 28
    assert "DAPI" in cfg["channels"]
    for i in range(27):
        params = cfg["channels"][f"M{i}"]
        for key in ("min", "max", "gamma", "brightness", "contrast", "enabled"):
            assert key in params


def test_bg_preview_display_unaffected_by_lazy_load(app, monkeypatch):
    p, ld = _fresh_page_with_counting_loader(app)
    p._rebuild_patch_buttons()
    p._sync_step0_to_workbench()
    # BG triple-preview path is cache-driven: spy it, drive a patch switch, and
    # confirm it still fires without adding read_region calls.
    called = []
    monkeypatch.setattr(p, "_show_channel_from_cache",
                        lambda ch: called.append(ch))
    monkeypatch.setattr(p, "_has_any_cache", lambda ch: True)
    p.current_channel = "M0"
    ld.calls.clear()
    p._select_patch(1)
    assert called == ["M0"]                       # BG cache display still invoked
    assert len(ld.calls) <= 2                      # BG display adds no reads (cache)


def test_patch_switch_highlight_only_new_button_checked(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._rebuild_patch_buttons()
    p._select_patch(1)
    from PyQt5 import QtWidgets
    checked = []
    for i in range(p._patch_buttons_row.count()):
        w = p._patch_buttons_row.itemAt(i).widget()
        if isinstance(w, QtWidgets.QPushButton):
            if w.isChecked():
                checked.append(w.text())
    assert checked == ["P2"]                       # only the new patch button


# ── step0-channel-list-cleanup (#6 list filter / DAPI normal, #8 no Enabled) ──
class _MixedLoader(_FakeLoader):
    """Loader exposing markers + DAPI + non-conditioning product channels."""

    def __init__(self):
        super().__init__(
            names=("DAPI", "CD68", "CK19", "cell_mask", "wholecell_fusion"),
            shape=(48, 48))

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        return np.random.rand(y1 - y0, x1 - x0).astype(np.float32)


def _page_with_mixed(app):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    p.loader = _MixedLoader()
    p.output_dir = "/tmp"
    p.patches = [(0, 24, 0, 24)]
    p.current_patch_idx = 0
    p.nucleus_channel = "DAPI"
    p._channel_order = ["DAPI", "CD68", "CK19", "cell_mask", "wholecell_fusion"]
    return p


def test_channel_list_markers_and_dapi_only(app):
    p = _page_with_mixed(app)
    p._sync_step0_to_workbench()
    names = set(p._cond_workbench._names)
    assert names == {"DAPI", "CD68", "CK19"}      # markers + DAPI
    assert "cell_mask" not in names               # mask filtered (#6)
    assert "wholecell_fusion" not in names         # fusion filtered (#6)


def test_dapi_is_conditionable_in_build_config(app):
    p = _page_with_mixed(app)
    p._sync_step0_to_workbench()
    wb = p._cond_workbench
    wb._on_active_changed("DAPI")                 # select DAPI -> lazy load
    wb._params["DAPI"]["min"] = 10.0
    wb._params["DAPI"]["max"] = 250.0
    wb._params["DAPI"]["gamma"] = 1.8
    cfg = wb.build_config()
    assert "DAPI" in cfg["channels"]
    dp = cfg["channels"]["DAPI"]
    assert dp["min"] == 10.0 and dp["max"] == 250.0 and dp["gamma"] == 1.8


def test_dapi_default_color_blue(app):
    """DAPI keeps its own blue -- and it is now the SAME blue the compare
    panels and the full-image overlay use (`_nuc_color`), not a second one
    the workbench kept privately."""
    p = _page_with_mixed(app)
    p._sync_step0_to_workbench()
    assert p._cond_workbench._colors.get("DAPI") == p._channel_color_hex("DAPI")
    assert p._cond_workbench._colors.get("DAPI") == "#007fff"


def test_no_enabled_checkbox_in_step0_workbench(app):
    p = _page_with_mixed(app)
    assert not hasattr(p._cond_workbench, "_chk_enabled")


def test_build_config_all_enabled_true(app):
    p = _page_with_mixed(app)
    p._sync_step0_to_workbench()
    cfg = p._cond_workbench.build_config()
    assert all(c["enabled"] is True for c in cfg["channels"].values())


def test_reference_module_off_for_step0(app):
    p = _page_with_mixed(app)
    wb = p._cond_workbench
    # no reference UI (ref bar turned off for Step0)
    assert wb._ref_chk == {}
    assert wb._ref_op == {}
    # reference availability is empty / unused (no DAPI-as-reference overlay)
    assert not any(wb.reference_layer_availability().values())
    # set_reference_layers is a no-op (does not crash, registers nothing)
    wb.set_reference_layers(dapi=np.ones((24, 24), np.float32))
    assert not any(wb.reference_layer_availability().values())


def test_dapi_lazy_loads_like_a_marker(app):
    p, ld = _fresh_page_with_counting_loader(app)   # nucleus=DAPI, 27 M + DAPI
    p._sync_step0_to_workbench()
    wb = p._cond_workbench
    assert "DAPI" in wb._names                        # normal channel in the list
    if wb._raw.get("DAPI") is None:                   # DAPI not the eager active
        ld.calls.clear()
        wb._on_active_changed("DAPI")
        assert ld.calls == ["DAPI"]                   # lazy-loaded on demand
        assert wb._raw.get("DAPI") is not None


# ── step0-conditioning-cleanup-and-all-toggle: Step0-host integration ────────
def test_internal_conditioning_host_has_no_visible_load_buttons(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert s._conditioning_host.isHidden()
    load_btns = [b for b in
                 s._conditioning_host.findChildren(QtWidgets.QPushButton)
                 if "Load" in b.text() and not b.isHidden()]
    assert load_btns == []


def test_step0_workbench_load_buttons_hidden(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    wb = s._cond_workbench
    assert wb._btn_host_refresh.isHidden()
    assert wb._btn_demo.isHidden()
    assert wb._btn_file.isHidden()


def test_step0_workbench_has_all_toggle(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert hasattr(s._cond_workbench, "_chk_all")


# ── step0-all-toggle-perf-and-save-position: Save right-aligned ──────────────
def test_background_save_replaces_the_separate_conditioning_save(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert s._btn_continue.text() == "Save"
    labels = [b.text() for b in
              s._conditioning_host.findChildren(QtWidgets.QPushButton)]
    assert "Save" not in labels



# ── step0-fix-patch-switching: patch selector + change propagation ───────────
def _page_with_patches(app, n=3):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    p.loader = _FakeLoader()
    p.nucleus_channel = "DAPI"
    p._channel_order = ["DAPI", "CD68", "CK19"]
    p.patches = [(i * 30, i * 30 + 30, 0, 30) for i in range(n)]
    p.current_patch_idx = 0
    p._rebuild_patch_buttons()
    return p


def _row_buttons(row):
    from PyQt5 import QtWidgets
    return [row.itemAt(i).widget() for i in range(row.count())
            if isinstance(row.itemAt(i).widget(), QtWidgets.QPushButton)]


def test_only_background_correction_has_a_patch_selector(app):
    p = _page_with_patches(app, n=3)
    assert not hasattr(p, "_cond_patch_buttons_row")
    assert len(_row_buttons(p._patch_buttons_row)) == 3


def test_conditioning_patch_button_switches_without_resync(app):
    """The conditioning row still selects the patch -- and does NOT re-feed
    the workbench: its pixels are the whole slide, which the selected patch
    does not change, and the rebuild would reset the per-channel params that
    ARE the page's display mapping."""
    p = _page_with_patches(app, n=3)
    p._sync_step0_to_workbench()                 # engage conditioning
    calls = []
    orig = p._sync_step0_to_workbench
    p._sync_step0_to_workbench = lambda: calls.append(p.current_patch_idx) or orig()
    btn = next(b for b in _row_buttons(p._patch_buttons_row) if b.text() == "P3")
    btn.click()
    assert p.current_patch_idx == 2
    assert calls == []                           # no re-sync on a patch switch


def test_new_patches_rebuild_buttons_without_resyncing_conditioning(app):
    """Drawing or deleting patches rebuilds the patch buttons and nothing
    else: the workbench reads the whole slide, so a patch change has nothing
    to tell it -- and the re-sync it used to trigger reset every channel's
    display window (six drawn patches turned the panels solid)."""
    p = _page_with_patches(app, n=3)
    p._sync_step0_to_workbench()                 # engage conditioning
    calls = []
    p._sync_step0_to_workbench = lambda: calls.append(p.current_patch_idx)
    p._on_patches_changed([(0, 40, 0, 40), (40, 80, 40, 80)])
    assert len(_row_buttons(p._patch_buttons_row)) == 2
    assert not hasattr(p, "_cond_patch_buttons_row")
    assert calls == []                           # conditioning left alone


def test_delete_all_patches_keeps_the_conditioning_view(app):
    """Deleting every patch no longer empties the hidden remap owner. The
    workbench shows the whole slide, which exists with no patch drawn at all
    -- this page even lands in that state."""
    p = _page_with_patches(app, n=3)
    p._sync_step0_to_workbench()                 # engage -> sticky in-use flag
    assert p._conditioning_in_use is True
    p._on_patches_changed([])                    # delete all
    assert p._cond_workbench.has_channel_data() is True
    assert p._conditioning_in_use is True
    p._on_patches_changed([(0, 30, 0, 30), (30, 60, 30, 60)])   # recreate
    assert len(_row_buttons(p._patch_buttons_row)) == 2
    assert not hasattr(p, "_cond_patch_buttons_row")
    assert p._cond_workbench.has_channel_data() is True


def test_patch_switch_reads_no_channel_for_the_workbench(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()
    ld.calls.clear()
    p._select_patch(1)                           # uses live _select_patch chain
    # The workbench holds the whole slide: a patch switch re-reads nothing
    # for it (and so cannot re-seed anyone's display window).
    assert ld.calls == [], ld.calls


# ── step0-preload-architecture: background preload + BG hot-swap ─────────────
class _CorrLoader(_FakeLoader):
    """Loader that records reads and returns CORRECTED pixels (×9 for CD68)
    once set_corrected_zarr_store has been called."""

    def __init__(self):
        super().__init__(names=("DAPI", "CD68", "CK19"), shape=(120, 120))
        self.calls = []
        self._corr = False

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        self.calls.append(ch)
        base = np.ones((y1 - y0, x1 - x0), np.float32)
        return base * (9.0 if (self._corr and ch == "CD68") else 1.0)

    def set_corrected_zarr_store(self, path, decisions):
        self._corr = True


def _page_for_preload(app):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    p.loader = _CorrLoader()
    p.nucleus_channel = "DAPI"
    p._channel_order = ["DAPI", "CD68", "CK19"]
    p.patches = [(0, 30, 0, 30), (30, 60, 30, 60), (60, 90, 60, 90)]
    p.current_patch_idx = 0
    return p


def _warm_cache_sync(p):
    """Fill the preload cache deterministically by running the worker in-thread."""
    from block01.ui.step0.step0_page import PreloadWorker
    p._preload_gen += 1
    w = PreloadWorker(p.loader, p.patches, p._conditioning_channels(),
                      p._preload_gen)
    w.channel_loaded.connect(p._on_preload_channel)
    w.finished_gen.connect(p._on_preload_finished)
    p._preload_worker = w
    w.run()                                  # synchronous -> direct signals


def test_preload_worker_emits_all_tiles(app):
    from block01.ui.step0.step0_page import PreloadWorker
    ld = _CorrLoader()
    patches = [(0, 20, 0, 20), (20, 40, 20, 40), (40, 60, 40, 60)]
    loaded, fin = [], []
    w = PreloadWorker(ld, patches, ["DAPI", "CD68", "CK19"], 1)
    w.channel_loaded.connect(lambda g, p, n, a: loaded.append((p, n)))
    w.finished_gen.connect(lambda g: fin.append(g))
    w.run()
    assert len(loaded) == 9                  # 3 patches × 3 channels
    assert fin == [1]
    assert {p for p, _ in loaded} == {0, 1, 2}


def test_preload_trigger_cancels_and_restarts(app):
    p = _page_for_preload(app)
    p._on_patches_changed(list(p.patches))   # starts preload #1
    w1 = p._preload_worker
    gen1 = p._preload_gen
    p._on_patches_changed([(0, 10, 0, 10)])  # patches change -> cancel + restart
    assert w1._cancelled is True             # old worker cancelled
    assert p._preload_gen == gen1 + 1        # new generation
    # let any live threads finish so teardown is clean
    for w in (w1, p._preload_worker):
        if w is not None:
            w.wait(2000)


def test_preload_cache_hit_zero_io(app):
    p = _page_for_preload(app)
    _warm_cache_sync(p)
    p.loader.calls.clear()
    arr = p._provide_channel_pixels("CD68")  # warm -> no read_region
    assert arr is not None
    assert p.loader.calls == []


def test_sync_passes_the_active_channel_eagerly_and_the_rest_lazily(app):
    """The workbench eats the WHOLE SLIDE now, not the patch, so the warm
    per-patch preload cache is no longer what it is fed: the active channel
    is read eagerly (the inspector is never blank on first open) and every
    other channel is a lazy placeholder the pixel provider fills when it is
    selected or checked."""
    p = _page_for_preload(app)
    _warm_cache_sync(p)
    p._sync_step0_to_workbench()
    wb = p._cond_workbench
    active = wb.active_channel()
    assert active is not None
    assert wb._raw.get(active) is not None, "the inspector opened empty"
    assert [n for n in wb._names if wb._raw.get(n) is not None] == [active]


def test_bg_hotswap_updates_corrected_only(app):
    p = _page_for_preload(app)
    _warm_cache_sync(p)
    before_cd68 = float(p._preload_cache[0]["CD68"].mean())
    before_dapi = float(p._preload_cache[0]["DAPI"].mean())
    p._on_wsi_finished({}, "/tmp/corr.zarr", {"CD68": "tophat", "DAPI": "original"})
    after_cd68 = float(p._preload_cache[0]["CD68"].mean())
    assert after_cd68 != before_cd68         # corrected channel hot-swapped
    assert float(p._preload_cache[0]["DAPI"].mean()) == before_dapi  # untouched


def test_preload_cold_cache_falls_back_to_read(app):
    p = _page_for_preload(app)
    p._preload_cache = {}
    p.loader.calls.clear()
    p._provide_channel_pixels("CK19")
    assert p.loader.calls == ["CK19"]        # lazy-load fallback fired


def test_stale_preload_signals_ignored(app):
    p = _page_for_preload(app)
    p._preload_gen = 5
    p._on_preload_channel(4, 0, "CD68", np.ones((4, 4), np.float32))  # stale gen
    assert 0 not in p._preload_cache         # cancelled worker's write dropped


def test_preload_build_config_unchanged(app):
    p = _page_for_preload(app)
    _warm_cache_sync(p)
    p._sync_step0_to_workbench()
    cfg = p._cond_workbench.build_config()
    assert set(cfg["channels"]) == {"DAPI", "CD68", "CK19"}


# ── step0-conditioning-patch-local-viewport-state (#4) ───────────────────────
def _page_two_patches(app, bbox0=(0, 50, 0, 50), bbox1=(0, 80, 0, 90)):
    from block01.ui.step0.step0_page import Step0Page
    import numpy as np

    class _L:
        filepath = "/x.ome.tif"
        shape = (400, 400)
        ch_map = {"DAPI": 0, "CD68": 1}
        def channel_names(self):
            return ["DAPI", "CD68"]
        def read_region(self, ch, y0, y1, x0, x1, normalize=False):
            return np.random.rand(y1 - y0, x1 - x0).astype(np.float32)

    p = Step0Page()
    p.loader = _L()
    p.nucleus_channel = "DAPI"
    p._channel_order = ["DAPI", "CD68"]
    p.patches = [bbox0, bbox1]
    p.current_patch_idx = 0
    p._rebuild_patch_buttons()
    p._sync_step0_to_workbench()          # engage conditioning, paint P0 (fit)
    return p


def _rng(p):
    return p._cond_workbench.viewer._vb.viewRange()


def test_unvisited_patch_does_not_inherit_zoom(app):
    p = _page_two_patches(app)
    vb = p._cond_workbench.viewer._vb
    vb.setRange(xRange=(5, 15), yRange=(6, 16), padding=0)      # zoom P0
    p0_zoom = vb.viewRange()
    p._select_patch(1)                                         # P1 never visited
    p1 = vb.viewRange()
    # P1 must NOT inherit P0's zoom (it fits to its own image instead)
    assert not (np.allclose(p0_zoom[0], p1[0]) and np.allclose(p0_zoom[1], p1[1]))


def test_revisited_patch_restores_its_viewport(app):
    p = _page_two_patches(app)
    vb = p._cond_workbench.viewer._vb
    vb.setRange(xRange=(5, 15), yRange=(6, 16), padding=0)
    p0_zoom = vb.viewRange()
    p._select_patch(1)
    p._select_patch(0)                                        # return to P0
    p0_restored = vb.viewRange()
    assert np.allclose(p0_zoom[0], p0_restored[0]) and np.allclose(p0_zoom[1], p0_restored[1])


def test_patches_keep_independent_viewports(app):
    p = _page_two_patches(app)
    vb = p._cond_workbench.viewer._vb
    vb.setRange(xRange=(5, 15), yRange=(6, 16), padding=0)
    p0z = vb.viewRange()
    p._select_patch(1)
    vb.setRange(xRange=(20, 30), yRange=(21, 31), padding=0)   # zoom P1
    p1z = vb.viewRange()
    p._select_patch(0)
    assert np.allclose(_rng(p)[0], p0z[0]) and np.allclose(_rng(p)[1], p0z[1])
    p._select_patch(1)
    assert np.allclose(_rng(p)[0], p1z[0]) and np.allclose(_rng(p)[1], p1z[1])


def test_remap_params_stay_channel_global_across_patches(app):
    p = _page_two_patches(app)
    wb = p._cond_workbench
    wb._on_active_changed("DAPI")
    wb._sp_min.setValue(33.0)
    wb._sp_max.setValue(222.0)
    wb._on_minmax_changed()
    p._select_patch(1)
    p._select_patch(0)
    # viewport is patch-local but Min/Max are channel-global -> persist
    assert wb._params["DAPI"]["min"] == 33.0
    assert wb._params["DAPI"]["max"] == 222.0


def test_display_param_change_does_not_reset_current_patch_zoom(app):
    p = _page_two_patches(app)
    wb = p._cond_workbench
    vb = wb.viewer._vb
    vb.setRange(xRange=(5, 15), yRange=(6, 16), padding=0)
    before = vb.viewRange()
    wb._on_active_changed("DAPI")
    wb._sp_min.setValue(40.0)
    wb._on_minmax_changed()                # same-patch display change -> no refit
    after = vb.viewRange()
    assert np.allclose(before[0], after[0]) and np.allclose(before[1], after[1])

# ── Removed Remap tab: Background Save owns validation + persistence ──────────
def _fresh_save_contract_page(app, tmp_path):
    from block01.ui.step0.step0_page import Step0Page
    page = Step0Page()
    _inject_context(page, tmp_path)
    _inject_roi_context(page, tmp_path)
    return page


def _stub_handoff(page):
    page._write_step0_handoff = lambda config, zarr_path: (
        config,
        [],
        [],
        {
            "corrected_zarr_path": zarr_path,
            "step0_dir": os.path.dirname(zarr_path),
            "analysis_region_type": "full_wsi",
        },
    )


def test_background_save_persists_remap_before_emitting_handoff(
        app, tmp_path, monkeypatch):
    page = _fresh_save_contract_page(app, tmp_path)
    try:
        page._sync_step0_to_workbench()
        _stub_handoff(page)
        path = page._step0_conditioning_config_path()
        timeline = []
        real_persist = page._persist_step0_remap_config

        def persist():
            result = real_persist()
            timeline.append(("remap", result, os.path.isfile(path)))
            return result

        page._persist_step0_remap_config = persist
        page.step0_complete.connect(
            lambda _payload: timeline.append(("handoff", os.path.isfile(path))))
        monkeypatch.setattr(
            "block01.ui.step0.step0_page.QMessageBox.information",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("Background Save showed the old Remap success dialog")))

        zarr_path = os.path.join(
            page._roi_context["step_dirs"]["step0"], "corrected_channels.zarr")
        assert page._emit_complete({}, zarr_path, {}) is True
        assert timeline == [("remap", True, True), ("handoff", True)]
        assert page._last_saved_remap_path == path
    finally:
        page.teardown()
        page.deleteLater()


def test_invalid_remap_blocks_the_step0_handoff(app, tmp_path, monkeypatch):
    page = _fresh_save_contract_page(app, tmp_path)
    try:
        page._sync_step0_to_workbench()
        writes = []
        page._write_step0_handoff = lambda *_a: writes.append(1)
        emitted = []
        page.step0_complete.connect(lambda payload: emitted.append(payload))
        warnings = []
        monkeypatch.setattr(
            "block01.ui.step0.step0_page.save_channel_remap_config",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad gamma")))
        monkeypatch.setattr(
            "block01.ui.step0.step0_page.QMessageBox.warning",
            lambda *a, **k: warnings.append(a))

        zarr_path = os.path.join(
            page._roi_context["step_dirs"]["step0"], "corrected_channels.zarr")
        assert page._emit_complete({}, zarr_path, {}) is False
        assert writes == []
        assert emitted == []
        assert warnings and "bad gamma" in str(warnings[0])
    finally:
        page.teardown()
        page.deleteLater()


def test_no_intensity_edits_need_no_remap_file_but_handoff_continues(
        app, tmp_path):
    page = _fresh_save_contract_page(app, tmp_path)
    try:
        assert page._cond_workbench.has_channel_data() is False
        _stub_handoff(page)
        emitted = []
        page.step0_complete.connect(lambda payload: emitted.append(payload))
        zarr_path = os.path.join(
            page._roi_context["step_dirs"]["step0"], "corrected_channels.zarr")

        assert page._emit_complete({}, zarr_path, {}) is True
        assert len(emitted) == 1
        assert not os.path.exists(page._step0_conditioning_config_path())
        assert getattr(page, "_last_saved_remap_path", "") == ""
    finally:
        page.teardown()
        page.deleteLater()
