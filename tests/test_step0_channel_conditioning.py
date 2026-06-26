"""v14.1b: Step0 hosts the Channel Conditioning / Remap surface.

Step0 is the v14 third host of the shared ChannelWorkbench (alongside the
Step1.5 creator and Step3 reviewer). These tests pin:

- Step0 exposes two main tabs (Background Correction + Channel Conditioning /
  Remap).
- The conditioning tab hosts the SAME shared ChannelWorkbench class (not forked).
- Step0 owns/uses the same context pieces Step1.5 used (loader / output_dir /
  patches / nucleus_channel) and can load current-patch channels with a fake
  loader.
- Saving writes ONLY a preview-only config with a REGISTERED created_from_step
  constant and an honest legacy_storage_path.

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


# ── 1. Two main tabs ─────────────────────────────────────────────────────────
def test_step0_has_two_main_tabs(page):
    assert hasattr(page, "_step0_tabs")
    tabs = [page._step0_tabs.tabText(i) for i in range(page._step0_tabs.count())]
    assert "Background Correction" in tabs
    assert any("Channel Conditioning" in t for t in tabs)


# ── 2. Conditioning tab hosts the shared ChannelWorkbench class ──────────────
def test_conditioning_tab_hosts_shared_channel_workbench(page):
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


# ── 5. Save writes ONLY a preview-only config with registered provenance ─────
def test_step0_save_writes_preview_only_registered_config(page, tmp_path, monkeypatch):
    from block01.utils import channel_remap_config as crc
    _inject_context(page, tmp_path)
    page._sync_step0_to_workbench()

    out_path = str(tmp_path / "out_step0_remap.json")
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (out_path, "JSON (*.json)"))
    monkeypatch.setattr(
        "block01.ui.step0.step0_page.QMessageBox.information",
        lambda *a, **k: None)

    page._save_step0_remap_config()
    assert os.path.isfile(out_path)

    saved = crc.load_channel_remap_config(out_path)
    sp = saved["source_policy"]
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False
    assert saved["created_from_step"] == "step0_channel_conditioning"
    # registered/enumerated provenance, not an ad-hoc free string
    assert crc.is_registered_created_from_step(saved["created_from_step"])
    assert saved["created_from_step"] == crc.CREATED_FROM_STEP0_CONDITIONING
    # legacy physical storage path recorded honestly (legacy step1_5 location)
    assert "legacy_storage_path" in saved
    assert saved["legacy_storage_path"].endswith(
        os.path.join("step1_5", "channel_remap_configs"))


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
    p = _page_with_mixed(app)
    p._sync_step0_to_workbench()
    assert p._cond_workbench._colors.get("DAPI") == "#3366ff"


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
def test_step0_conditioning_tab_has_no_load_buttons(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # no visible "Load ..." button remains in the conditioning tab (the page-level
    # btn_load was removed; the workbench load buttons are hidden for Step0).
    load_btns = [b for b in s._cond_tab.findChildren(QtWidgets.QPushButton)
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
def _find_layout_with_widget(layout, widget):
    """Recursively find the QLayout directly containing `widget`."""
    from PyQt5 import QtWidgets
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is widget:
            return layout
        child = item.layout()
        if child is not None:
            found = _find_layout_with_widget(child, widget)
            if found is not None:
                return found
    return None


def test_conditioning_save_is_right_aligned(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    save = next(b for b in s._cond_tab.findChildren(QtWidgets.QPushButton)
                if b.text() == "Save")
    bar = _find_layout_with_widget(s._cond_tab.layout(), save)
    assert bar is not None
    # Save is the last item, and a stretch/spacer precedes it (right-aligned,
    # matching the BG tab's save_row order).
    save_idx = next(i for i in range(bar.count())
                    if bar.itemAt(i).widget() is save)
    assert save_idx == bar.count() - 1
    assert any(bar.itemAt(i).spacerItem() is not None for i in range(save_idx))
