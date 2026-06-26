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
    # markers present, DAPI excluded (reference layer only)
    assert "CD68" in chans and "CK19" in chans
    assert "DAPI" not in chans


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


def test_patch_switch_reads_only_active_plus_dapi(app):
    p, ld = _fresh_page_with_counting_loader(app)
    p._sync_step0_to_workbench()          # workbench now "in use"
    ld.calls.clear()
    # simulate a patch switch's conditioning refresh
    p.current_patch_idx = 1
    p._maybe_refresh_conditioning()
    # at most 2 reads: the active marker + DAPI reference — NOT all 27
    assert len(ld.calls) <= 2, ld.calls
    assert "DAPI" in ld.calls
    assert len([c for c in ld.calls if c != "DAPI"]) == 1


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
    # all 27 markers present (DAPI excluded as reference), even those never read
    assert len(cfg["channels"]) == 27
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
