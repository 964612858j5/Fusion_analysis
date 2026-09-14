"""Step0's channel rows, in the ONE public dock.

WHAT THIS MODULE USED TO BE. The v15 Phase-1A acceptance list for a reusable
`ChannelDock` shell with a per-page row factory over a `ChannelSetModel`:
"the same shell for all steps", "Step1 weight-only rows", "Step3
display-only rows", "state transfers between docks". Those parts are gone
with B5 -- there is one dock, one row class and no second model to transfer
state between -- and what survives here is what they were really about,
written against the real page: Step0's correction row, the shared search and
bulk sweep, the weight editor's two-way sync, and the row geometry.

The per-step field table, the row identity through a step walk and the
command gating live in `test_global_channel_dock.py`.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    def __init__(self, names=("DAPI", "CD3", "CD20")):
        self._names = list(names)

    def channel_names(self):
        return list(self._names)


def _page(app, names=("DAPI", "CD3", "CD20")):
    """A real Step0 page bound to the public dock."""
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page()
    page.loader = _Loader(names)
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    return page


# ── Step0's row: the correction decision and its compute state ──────────────

def test_step0_row_shows_the_preview_method_and_the_compute_state(app):
    """The row's combo is the PREVIEW method -- what the channel is computed
    and looked at with. What Save publishes is the Per-Channel Decision
    panel's answer and is deliberately not drawn here."""
    page = _page(app)
    dock = page._dock_adapter.dock
    dock.set_step(0)
    page._set_channel_preview_method("CD3", "tophat")
    page._channel_decisions["CD3"] = "cucim"      # the OTHER layer
    page._rebuild_channel_list()

    assert dock.row("CD3").method_cb.currentText() == "TopHat"
    # ...and a channel nobody set previews with the bulk box's default.
    assert dock.row("CD20").method_cb.currentText() == "Both"

    # The compute state is DERIVED from the page's signature bookkeeping --
    # the row holds none of its own -- so a run in flight is what makes the
    # glyph a spinner.
    page._pending_signatures["CD3"] = "sig"
    page._set_channel_computing("CD3")
    assert dock.row("CD3").state_slot.text() == "⟳"
    page._set_channel_preview_method("CD20", "cucim")
    page._refresh_channel_row("CD20")
    assert dock.row("CD20").method_cb.currentText() == "cucim"
    # the decision the page holds for CD3 never reached its combo
    assert page._channel_final_decision("CD3") == "cucim"
    assert dock.row("CD3").method_cb.currentText() == "TopHat"


# ── the selected-channel tool areas (Min/Max/Gamma) ─────────────────────────

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


# ── search, and a bulk sweep that asks the capability ───────────────────────

def test_search_and_bulk_visibility(app):
    page = _page(app)
    dock = page._dock_adapter.dock
    state = page.display.state

    dock.search.setText("cd")
    assert dock.visible_row_ids() == ["CD3", "CD20"]
    dock.search.setText("")
    assert len(dock.visible_row_ids()) == 3

    state.set_display_visible("DAPI", True, origin="test")
    dock.set_all_visible(False)
    # A SWEEP IS ITS OWN PERMISSION: the DAPI layer is shown and hidden
    # deliberately, so Hide all leaves it alone while the markers go.
    assert state.display_visible("DAPI") is True
    assert state.display_visible("CD3") is False
    assert state.display_visible("CD20") is False
    dock.set_all_visible(True)
    assert state.display_visible("CD3") is True


# ── the weight editor: slider and number, one answer ────────────────────────

def test_weight_slider_spin_two_way_sync(app):
    from block01.core.fusion_domain import FusionDomainModel
    from block01.ui.widgets.channel_dock.global_dock import (
        GlobalChannelDock, STEP1)

    fusion = FusionDomainModel()
    fusion.add_group("markers", {"CD3": 0.5})
    dock = GlobalChannelDock(None, fusion)
    dock.set_channels(["CD3"])
    dock.set_step(STEP1)
    r = dock.row("CD3")

    r.slider.setValue(80)
    assert abs(r.spin.value() - 0.8) < 1e-9
    assert abs(fusion.channel_weight("CD3") - 0.8) < 1e-9
    r.spin.setValue(0.25)
    assert r.slider.value() == 25
    assert abs(fusion.channel_weight("CD3") - 0.25) < 1e-9
    fusion.edit_channel_weight("CD3", 0.6, origin="test")
    assert r.slider.value() == 60 and abs(r.spin.value() - 0.6) < 1e-9
    dock.deleteLater()


# ── Step0 adapter: legacy registry compatibility ─────────────────────────────

def test_step0_adapter_legacy_registry(app):
    """Step0 binds to the ONE public dock and still fills its row registry.

    The adapter used to CONSTRUCT a dock of its own; since B4-B it asks
    `Block01DisplayServices` for the public one, so this test drives a real
    page rather than a hand-assembled adapter -- there is no second dock to
    assemble any more.
    """
    from block01.ui.step0.step0_page import Step0Page

    class _Loader:
        def channel_names(self):
            return ["DAPI", "CD3", "CD20"]

    page = Step0Page()
    page.loader = _Loader()
    page.nucleus_channel = "DAPI"
    page._set_channel_preview_method("CD3", "tophat")
    page._rebuild_channel_list()

    assert page._dock_adapter.dock is page.display.channel_dock()
    assert page._channel_order == ["DAPI", "CD3", "CD20"]
    # The registry's keys, after B5 took the empty right-edge badge out:
    # `badge` and `status_lbl` named a label that was never shown.
    assert set(page._channel_rows["CD3"]) == {
        "checkbox", "label", "item", "method_cb", "row_widget"}
    # nucleus locked FOR CORRECTION: its method combo is dead. Its checkbox
    # is not a processing checkbox at all -- it is the DAPI layer's show/hide
    # switch -- so it stays enabled.
    assert page._channel_rows["DAPI"]["checkbox"].isEnabled()
    assert not page._channel_rows["DAPI"]["method_cb"].isEnabled()
    # the preview method is reflected, and a change reaches the page's
    # correction domain through the dock's Step0 accessory
    assert page._channel_rows["CD3"]["method_cb"].currentText() == "TopHat"
    page._channel_rows["CD20"]["method_cb"].setCurrentText("TopHat")
    assert page._channel_methods["CD20"] == "tophat"
    assert page._channel_final_decision("CD20") == "original"


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
    # A row nobody has set shows the bulk box's PREVIEW default (`Both`:
    # prepare both candidates so they can be compared) while the page
    # publishes `original` for it, because no decision has been made. Two
    # questions, two answers. The checkbox says nothing about either: it is
    # display visibility, and a fresh slide shows DAPI and its FIRST marker
    # while hiding the others.
    for ch in ("CD3", "CD20", "CD8"):
        assert page._channel_rows[ch]["method_cb"].currentText() == "Both"
        assert page._channel_preview_method(ch) == "both"
        assert page._channel_final_decision(ch) == "original"
    assert page._channel_rows["CD3"]["checkbox"].isChecked()
    for ch in ("CD20", "CD8"):
        assert not page._channel_rows[ch]["checkbox"].isChecked(), ch
    assert page._channel_rows["DAPI"]["checkbox"].isChecked()
    # The global Method box COMMANDS the PREVIEW layer: every
    # correction-eligible channel, including the ones nobody had touched. It
    # used to move only their combos, so a row said TopHat while the page
    # computed something else.
    page._method_all.setCurrentText("TopHat")
    for ch in ("CD3", "CD20", "CD8"):
        assert page._channel_rows[ch]["method_cb"].currentText() == "TopHat"
        assert page._channel_preview_method(ch) == "tophat"
        # ...and it published nothing: Save still writes the raw channel.
        assert page._channel_final_decision(ch) == "original"
    assert "DAPI" not in page._channel_methods
    assert "DAPI" not in page._channel_decisions
    # explicit assignment still sticks -- and does NOT show the channel.
    # Assigning a correction method used to tick the row, which is how
    # "corrected" and "on screen" became one answer.
    page._channel_rows["CD20"]["method_cb"].setCurrentText("cucim")
    assert page._channel_methods["CD20"] == "cucim"
    assert page._channel_final_decision("CD20") == "original"
    assert not page._channel_rows["CD20"]["checkbox"].isChecked()
    assert page.display.state.display_visibility().get("CD20") is False
    # every Step0 BG row carries its own display-colour swatch (the colour
    # buttons that used to sit in the Patch Preview header are gone)
    assert page._channel_rows["CD3"]["row_widget"].swatch.isVisibleTo(
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
    for r in rows.values():           # force a real layout pass (offscreen)
        r.resize(300, 26)
        r.layout().activate()
    before = {ch: r.name_label.x() for ch, r in rows.items()}
    # all names share one fixed left position
    assert len(set(before.values())) == 1
    # combos aligned and directly after the (uniform-width) name column
    # In STEP0 the row shows `checkbox | state | swatch | name | method`:
    # the weight editor and the participation box are Step1's fields and are
    # hidden here, so the correction combo starts where the name column ends.
    for r in rows.values():
        assert r.slider.isHidden() and r.spin.isHidden()
        assert r.fusion_box.isHidden()
        assert r.method_cb.x() == r.name_label.x() + r.name_label.width() + 5
    assert len({r.method_cb.x() for r in rows.values()}) == 1

    page._set_channel_computing("CD3")
    page._set_channel_done("CD3")
    QtWidgets.QApplication.processEvents()
    for r in rows.values():
        r.layout().activate()
    after = {ch: r.name_label.x() for ch, r in rows.items()}
    assert after == before                      # green state must not shift names
    # checkbox footprint constant regardless of the green indicator restyle
    for r in rows.values():
        assert (r.checkbox.width(), r.checkbox.height()) == (22, 18)
    dock.hide()


def test_a_bulk_sweep_asks_bulk_toggleable_not_locked(app):
    """`locked` used to stand for four decisions at once, so a bulk sweep
    skipped a channel because it was "special" rather than because a sweep
    may not move it. The two are separate facts, and this test is only
    meaningful while they can disagree.

    Written against the capabilities on `ChannelDisplayState` and the public
    dock's sweep -- the `ChannelSetModel` it used to drive is gone.
    """
    from block01.core.display_identity import ChannelCapabilities
    from block01.ui.block01_display import Block01DisplayServices
    from block01.ui.widgets.channel_dock.global_dock import GlobalChannelDock
    from block01.core import display_identity

    services = Block01DisplayServices()
    state = services.state
    state.bind(display_identity.DatasetIdentity("/tmp/bulk.ome.tiff", "1:1"))
    state.install({
        "order": ("A", "B"),
        "capabilities": {
            # swept: a bulk sweep may move it, whatever else it is
            "A": ChannelCapabilities(bulk_toggleable=True),
            # not swept: it is shown and hidden deliberately
            "B": ChannelCapabilities(is_nucleus=True, bulk_toggleable=False),
        },
        "visibility": {"A": False, "B": False},
    })
    dock = GlobalChannelDock(state, services.fusion)
    dock.set_channels(["A", "B"])

    dock.set_all_visible(True)

    assert state.display_visible("A") is True, "a sweepable channel was skipped"
    assert state.display_visible("B") is False, "a deliberate channel was swept"
    services.shutdown("test")
    dock.deleteLater()
