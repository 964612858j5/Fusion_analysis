"""v15 Phase 1A: shared ChannelDock shell, per-page rows/editors, adapters.

Covers the v15 acceptance list (plan §11–12, test requirements 1–12):
same shell for all steps, Step0 bg-method semantics, Step1 weight-only rows,
Step3 display-only rows/inspector, search + show/hide all, weight two-way
sync, and cross-page channel state consistency.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _states(n=4, **kw):
    from block01.ui.widgets.channel_dock import ChannelState
    return [ChannelState(channel_id=f"CH{i}", color="#336699", **kw)
            for i in range(n)]


def _mk(app, row_cls, states=None):
    from block01.ui.widgets.channel_dock import ChannelDock, ChannelSetModel
    m = ChannelSetModel()
    m.set_channels(states if states is not None else _states())
    d = ChannelDock(m, row_factory=row_cls)
    return m, d


# ── 1. Same shared shell for all three steps ────────────────────────────────

def test_all_steps_use_same_shell_class(app):
    from block01.ui.widgets.channel_dock import (
        ChannelDock, Step0ChannelRow, WeightChannelRow, DisplayChannelRow)
    docks = [_mk(app, cls)[1]
             for cls in (Step0ChannelRow, WeightChannelRow, DisplayChannelRow)]
    assert all(type(d) is ChannelDock for d in docks)
    # shell affordances present on every page
    for d in docks:
        assert d.search is not None and d.list_widget is not None
        assert d.btn_show_all is not None and d.btn_hide_all is not None


# ── 2. Step0 rows: bg method + final decision status ────────────────────────

def test_step0_row_shows_method_and_final_state(app):
    from block01.ui.widgets.channel_dock import ChannelState, Step0ChannelRow
    sts = [ChannelState("CH0", bg_final_method="tophat", status="done"),
           ChannelState("CH1", bg_final_method="original")]
    m, d = _mk(app, Step0ChannelRow, sts)
    r0, r1 = d.row("CH0"), d.row("CH1")
    assert r0.method_cb.currentText() == "TopHat"
    assert r1.method_cb.currentText() == "Original"
    m.set_status("CH0", "done")
    m.set_status("CH0", "computing")
    assert r0.status_lbl.text() == "⟳"
    m.set_bg_final("CH1", "cucim")
    assert r1.method_cb.currentText() == "cucim"


# ── 3. Step1 rows: weights only, no bg method ────────────────────────────────

def test_step1_row_weight_only(app):
    from block01.ui.widgets.channel_dock import WeightChannelRow
    m, d = _mk(app, WeightChannelRow, _states(weight=0.5))
    r = d.row("CH0")
    assert hasattr(r, "slider") and hasattr(r, "spin")
    assert not hasattr(r, "method_cb")          # no background method on Step1


# ── 4. Step3 rows: visibility/color/name only ────────────────────────────────

def test_step3_row_is_minimal(app):
    from block01.ui.widgets.channel_dock import DisplayChannelRow
    m, d = _mk(app, DisplayChannelRow)
    r = d.row("CH0")
    assert not hasattr(r, "method_cb")
    assert not hasattr(r, "slider")             # no weight either
    assert r.checkbox is not None and r.swatch is not None


# ── 5. Step0/Step3 Min/Max/Gamma tool areas ────────────────────────────────────

def test_min_max_gamma_editors_exist(app):
    from block01.ui.widgets.channel_dock import Step0Inspector, Step3Inspector
    s0, s3 = Step0Inspector(), Step3Inspector()
    for insp in (s0, s3):
        assert insp.remap.min_spin is not None
        assert insp.remap.max_spin is not None
        assert insp.remap.gamma_spin is not None
    assert getattr(s3, "display_only") is True
    assert hasattr(s0, "compare_btn")           # Compare entry lives in Step0 tools
    assert hasattr(s0, "tophat_radius") and hasattr(s0, "cucim_sigma")


# ── 6. Step3 adapter never writes processing keys ────────────────────────────

def test_step3_adapter_writes_display_only(app):
    from block01.ui.step3_dock_adapter import Step3DisplayDockAdapter, DISPLAY_KEYS
    settings = {"CD3": {"visible": True, "color": "#ff0000",
                        "opacity": 100, "contrast": [1.0, 99.5]}}
    ad = Step3DisplayDockAdapter(settings)
    ad.model.select("CD3")
    ad.inspector.remap.min_spin.setValue(10.0)
    ad.model.set_visible("CD3", False)
    ad.model.set_color("CD3", "#00ff00")
    assert set(settings["CD3"].keys()) <= DISPLAY_KEYS
    assert settings["CD3"]["visible"] is False
    assert settings["CD3"]["color"] == "#00ff00"
    assert settings["CD3"]["display_min"] == 10.0
    for forbidden in ("method", "bg_final_method", "weight",
                      "tophat_radius", "cucim_sigma"):
        assert forbidden not in settings["CD3"]


# ── 7. Channel order / color / visibility / selection consistency ───────────

def test_state_transfers_between_docks(app):
    from block01.ui.widgets.channel_dock import (
        ChannelDock, ChannelSetModel, Step0ChannelRow, DisplayChannelRow)
    m = ChannelSetModel()
    m.set_channels(_states())
    m.set_color("CH2", "#aabbcc")
    m.set_visible("CH1", False)
    m.select("CH2")
    d0 = ChannelDock(m, row_factory=Step0ChannelRow)
    d3 = ChannelDock(m, row_factory=DisplayChannelRow)
    for d in (d0, d3):
        assert list(d.rows().keys()) == ["CH0", "CH1", "CH2", "CH3"]
        assert not d.row("CH1").checkbox.isChecked()
        assert d.list_widget.currentItem() is d.item("CH2")
    assert m.get("CH2").color == "#aabbcc"


# ── 8. Search / Show all / Hide all ─────────────────────────────────────────

def test_search_and_bulk_visibility(app):
    from block01.ui.widgets.channel_dock import DisplayChannelRow, ChannelState
    sts = [ChannelState("DAPI"), ChannelState("CD3"), ChannelState("CD20")]
    m, d = _mk(app, DisplayChannelRow, sts)
    d.search.setText("cd")
    assert d.visible_row_ids() == ["CD3", "CD20"]
    d.search.setText("")
    assert len(d.visible_row_ids()) == 3
    d.btn_hide_all.click()
    assert all(not m.get(c).visible for c in m.order())
    d.btn_show_all.click()
    assert all(m.get(c).visible for c in m.order())


# ── 9. Weight slider ↔ numeric input two-way sync ─────────────────────────────

def test_weight_slider_spin_two_way_sync(app):
    from block01.ui.widgets.channel_dock import WeightChannelRow
    m, d = _mk(app, WeightChannelRow, _states(weight=0.5))
    r = d.row("CH0")
    r.slider.setValue(80)
    assert abs(r.spin.value() - 0.8) < 1e-9
    assert abs(m.get("CH0").weight - 0.8) < 1e-9
    r.spin.setValue(0.25)
    assert r.slider.value() == 25
    assert abs(m.get("CH0").weight - 0.25) < 1e-9
    m.set_weight("CH0", 0.6)
    assert r.slider.value() == 60 and abs(r.spin.value() - 0.6) < 1e-9


# ── Step1 adapter: ConfigPanel ↔ dock mirror ──────────────────────────────────

def test_step1_adapter_two_way_mirror(app):
    from block01.ui.step0.config_panel import ConfigPanel
    from block01.ui.step1_dock_adapter import Step1FusionDockAdapter
    panel = ConfigPanel(["CH0", "CH1", "CH2"])
    panel._add_group("Tumor", {"CH0": 0.4, "CH1": 0.6})
    ad = Step1FusionDockAdapter(panel)
    cid = ad.channel_key("Tumor", "CH0")
    assert abs(ad.model.get(cid).weight - 0.4) < 1e-9
    # dock -> panel
    ad.model.set_weight(cid, 0.9)
    assert abs(panel._panels["Tumor"]._rows["CH0"].weight() - 0.9) < 1e-9
    # panel -> dock
    panel._panels["Tumor"]._rows["CH1"].spin.setValue(0.1)
    cid1 = ad.channel_key("Tumor", "CH1")
    assert abs(ad.model.get(cid1).weight - 0.1) < 1e-9


# ── Step0 adapter: legacy registry compatibility ─────────────────────────────

def test_step0_adapter_legacy_registry(app):
    from block01.ui.step0.step0_dock_adapter import Step0ChannelDockAdapter

    class _Loader:
        def channel_names(self):
            return ["DAPI", "CD3", "CD20"]

    class _Page:
        loader = _Loader()
        nucleus_channel = "DAPI"
        current_channel = None
        _channel_rows = {}
        _channel_order = []
        _channel_decisions = {"CD3": "tophat"}
        _channel_methods = {"CD3": "tophat"}
        _channel_colors = {"CD3": (255, 0, 0)}
        calls = []

        def _on_channel_checkbox_toggled(self, name, state):
            self.calls.append(("cb", name, state))

        def _on_channel_method_changed(self, name, txt):
            self.calls.append(("method", name, txt))

        def _refresh_channel_row(self, ch):
            pass

    page = _Page()
    ad = Step0ChannelDockAdapter.__new__(Step0ChannelDockAdapter)
    from PyQt5.QtCore import QObject
    QObject.__init__(ad)
    ad._page = page
    from block01.ui.widgets.channel_dock import ChannelSetModel, ChannelDock
    ad.model = ChannelSetModel(ad)
    ad.dock = ChannelDock(ad.model, row_factory=ad._make_row,
                          title="", show_bulk_buttons=False)
    ad.rebuild()

    assert page._channel_order == ["DAPI", "CD3", "CD20"]
    for key in ("checkbox", "label", "badge", "item",
                "method_cb", "status_lbl", "row_widget"):
        assert key in page._channel_rows["CD3"]
    # nucleus locked
    assert not page._channel_rows["DAPI"]["checkbox"].isEnabled()
    assert not page._channel_rows["DAPI"]["method_cb"].isEnabled()
    # selection skipped nucleus
    assert page.current_channel == "CD3"
    # method change reaches the legacy slot
    page._channel_rows["CD20"]["method_cb"].setCurrentText("Original")
    assert ("method", "CD20", "Original") in page.calls
    # saved decision reflected
    assert page._channel_rows["CD3"]["method_cb"].currentText() == "TopHat"


# ── Step0 fresh session: prior decisions don't seed combos; no dead swatch ───

def test_step0_prior_decisions_not_seeded_and_no_swatch(app, tmp_path):
    import json
    from block01.ui.step0.step0_page import Step0Page

    class _Loader:
        def channel_names(self):
            return ["DAPI", "CD3", "CD20", "CD8"]

    cfg = {"channel_decisions": {"CD3": "cucim", "CD20": "cucim", "CD8": "tophat"},
           "method_params": {"tophat_radius": 30, "cucim_sigma": 50}}
    (tmp_path / "correction_config.json").write_text(json.dumps(cfg))

    page = Step0Page()
    page.output_dir = str(tmp_path)
    page.loader = _Loader()
    page.nucleus_channel = "DAPI"
    page._load_existing_config()
    page._rebuild_channel_list()

    # previous run's final methods are reference only, not this session's
    assert page._channel_decisions == {}
    assert page._prior_channel_decisions == {
        "CD3": "cucim", "CD20": "cucim", "CD8": "tophat"}
    # unassigned rows mirror the global Method box (default Both), unchecked
    for ch in ("CD3", "CD20", "CD8"):
        row = page._channel_rows[ch]
        assert row["method_cb"].currentText() == "Both"
        assert not row["checkbox"].isChecked()
    # global method change mirrors into unassigned rows without assigning
    page._method_all.setCurrentText("TopHat")
    for ch in ("CD3", "CD20", "CD8"):
        assert page._channel_rows[ch]["method_cb"].currentText() == "TopHat"
    assert page._channel_decisions == {}
    # explicit assignment still sticks
    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")
    assert page._channel_decisions["CD3"] == "cucim"
    assert page._channel_rows["CD3"]["checkbox"].isChecked()
    # the color swatch is hidden in Step0 BG rows (read as a dead checkbox)
    assert not page._channel_rows["CD3"]["row_widget"].swatch.isVisibleTo(
        page._channel_rows["CD3"]["row_widget"])


# ── Step0 rows: names stay left-aligned at a fixed x through state changes ───

def test_step0_row_name_position_stable_after_done(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page

    class _Loader:
        def channel_names(self):
            return ["DAPI", "CD3", "CD20"]

    page = Step0Page()
    page.loader = _Loader()
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    dock = page._dock_adapter.dock
    dock.resize(360, 300)
    dock.show()
    QtWidgets.QApplication.processEvents()

    rows = {ch: page._channel_rows[ch]["row_widget"] for ch in ("CD3", "CD20")}
    before = {ch: r.name_label.x() for ch, r in rows.items()}
    # all names share one fixed left position
    assert len(set(before.values())) == 1

    page._set_channel_computing("CD3")
    page._set_channel_done("CD3")
    QtWidgets.QApplication.processEvents()
    after = {ch: r.name_label.x() for ch, r in rows.items()}
    assert after == before                      # green state must not shift names
    # checkbox footprint constant regardless of the green indicator restyle
    for r in rows.values():
        assert (r.checkbox.width(), r.checkbox.height()) == (22, 18)
    dock.hide()
