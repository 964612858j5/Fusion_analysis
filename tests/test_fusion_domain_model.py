"""Block01's fusion state, outside the Step1 page.

WHAT IS PINNED HERE. Step1's scientific answer used to live in a QWidget on a
stacked page, so asking what CD3 weighs required that widget to be alive and
populated, and hiding a channel to look at another silently shrank the
configuration a Save would freeze. This module holds the model to the three
things that replaced it:

  * ONE owner, outside the page. `Block01DisplayServices` holds it before the
    first page exists and after the last one is destroyed.
  * THREE commands. Display visibility, fusion participation and a scientific
    weight edit are separate, and each one does only its own job.
  * FOUR session shapes, migrated by rule. Legacy flat, grouped, the current
    one-tick format and the new split schema, each turned into one explicit
    install rather than sniffed field by field in every handler.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import fusion_domain  # noqa: E402
from block01.core.fusion_domain import (  # noqa: E402
    ABSENT, AUTHORITATIVE, AUTO, EXPLICIT, FusionDomainModel,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """A refusal is a message box, and a modal box offscreen is a hang."""
    warned = []
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _w=warned, **k: _w.append(a)))
    return warned


class _Loader:
    filepath = "/tmp/dataset.ome.tiff"
    shape = (256, 256)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads = []

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        self.reads.append(channel)
        rng = np.random.default_rng(abs(hash(channel)) % 1000)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _bare_window():
    """A real MainWindow that has NOT been to Step1 and has no dataset."""
    from block01.ui.main_window import MainWindow
    return MainWindow()


def _window(app, size=32):
    w = _bare_window()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, size, 0, size)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {
        ch: rng.random((size, size), dtype=np.float32) + 0.1
        for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    return w


def _pump():
    QtWidgets.QApplication.processEvents()


# ── A. lifetime, and one scientific answer ───────────────────────────────

def test_the_model_answers_before_step1_has_ever_been_opened(app):
    """It is the services' object, built with them. The page is an editor."""
    w = _bare_window()
    try:
        model = w._display.fusion
        assert isinstance(model, FusionDomainModel)
        assert w.config.fusion_model() is model, \
            "the panel made a second model instead of editing this one"
        # Usable with no dataset, no page shown, nothing loaded.
        assert model.edit_channel_weight("CD3", 0.4) is True
        assert w._display.render_weight("CD3") == pytest.approx(0.4)
    finally:
        w.close()


def test_a_weight_written_before_step1_is_adopted_by_the_first_group(app):
    """A weight edited in Step0 has nowhere to live yet -- Step1 has built no
    groups. It waits in the model rather than inventing a group to hold it,
    and the first group that takes the channel takes the answer with it."""
    w = _bare_window()
    try:
        w._display.set_render_weight("CD3", 0.0)      # a deliberate zero
        assert w._display.fusion.weight_provenance("CD3") == EXPLICIT

        w.loader = _Loader()
        w.config.set_channels(w.loader.channel_names())
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
        w.config.set_nucleus("DAPI", 1.0)
        w.config.set_fusion_enabled("CD3", True)

        assert w.config.channel_weight("CD3") == 0.0, \
            "the first enable overwrote a zero somebody chose"
        assert w.config.get_groups()["markers"]["CD3"] == 0.0
    finally:
        w.close()


def test_the_answers_outlive_the_step1_page(app):
    """A destroyed panel takes no scientific state with it, and nothing falls
    back to reading its rows."""
    import sip
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        w.config.edit_weight = None
        w._display.fusion.edit_channel_weight("CD3", 0.42)
        panel = w.config
        panel.setParent(None)
        sip.delete(panel)
        _pump()

        assert w._display.render_weight("CD3") == pytest.approx(0.42)
        model = w._display.fusion
        assert model.effective_config()["groups"]["markers"]["channels"]["CD3"] \
            == pytest.approx(0.42)
        assert model.fusion_enabled("CD3") is True
    finally:
        w.close()


def test_a_row_cannot_answer_for_the_model(app):
    """The row is a projection. Moving its spin box WITHOUT the model -- the
    way a stale mirror would -- changes nothing that is asked for."""
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        w._display.fusion.edit_channel_weight("CD3", 0.25)
        row = w.config._rows["CD3"]
        row.spin.blockSignals(True)
        row.spin.setValue(0.99)                 # a lie in the widget
        row.spin.blockSignals(False)

        assert w.config.channel_weight("CD3") == pytest.approx(0.25)
        assert w._display.render_weight("CD3") == pytest.approx(0.25)
        eff = w._effective_fusion_config()
        assert eff["groups"]["markers"]["channels"]["CD3"] == \
            pytest.approx(0.25)
    finally:
        w.close()


def test_a_draft_snapshot_belongs_to_its_caller(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.set_fusion_enabled("CD3", True)
        snap = model.draft_snapshot()
        snap["enabled"].append("CD8")
        snap["groups"]["markers"]["members"].append("NOPE")
        snap["provenance"]["CD3"] = "nonsense"

        assert model.fusion_enabled("CD8") is False
        assert "NOPE" not in model.groups()["markers"]
        assert model.weight_provenance("CD3") == AUTO
    finally:
        w.close()


# ── B. three commands, three effects ─────────────────────────────────────

def test_hiding_a_channel_changes_nothing_scientific(app):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        w._display.fusion.install_committed_snapshot(
            {"hash": w._fusion_settings_hash()})
        assert w._fusion_settings_dirty() is False
        before = w.config.get_full_config()
        eff_before = w._effective_fusion_config()
        hash_before = w._fusion_settings_hash()

        w.config.set_channel_visible("CD3", False)

        assert w.config.get_full_config() == before
        assert w._effective_fusion_config() == eff_before
        assert w._fusion_settings_hash() == hash_before
        assert w._fusion_settings_dirty() is False, \
            "looking at a different channel made a saved project unsaved"
        assert w._require_committed_fusion_settings("A search") is True
    finally:
        w.close()


def test_leaving_the_fusion_keeps_every_number_and_changes_the_science(app):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        w._display.fusion.edit_channel_weight("CD3", 0.4)
        w.config.set_channel_visible("CD3", True)
        w._display.fusion.install_committed_snapshot(
            {"hash": w._fusion_settings_hash()})

        w.config.set_fusion_enabled("CD3", False)

        assert "CD3" in w.config.visible_channels()     # still drawn
        assert w.config.get_full_config()["groups"]["markers"]["channels"][
            "CD3"] == pytest.approx(0.4)
        assert "CD3" not in w._effective_fusion_config()[
            "groups"]["markers"]["channels"]
        assert w._fusion_settings_dirty() is True
        assert w._require_committed_fusion_settings("A search") is False
    finally:
        w.close()


def test_an_explicit_zero_comes_back_as_a_zero(app):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        w._display.fusion.edit_channel_weight("CD3", 0.0)
        assert w._display.fusion.weight_provenance("CD3") == EXPLICIT

        w.config.set_fusion_enabled("CD3", False)
        w.config.set_fusion_enabled("CD3", True)

        assert w.config.channel_weight("CD3") == 0.0
    finally:
        w.close()


def test_a_first_enable_answers_one_without_claiming_an_edit(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.install_draft({
            "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                       "b": {"group_weight": 1.0, "channels": {"CD3": 0.7}}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": [], "provenance": {"CD3": AUTHORITATIVE},
        })

        model.set_fusion_enabled("CD3", True)

        groups = model.groups()
        assert groups["a"]["CD3"] == pytest.approx(0.2), \
            "an automatic first enable flattened an old project's groups"
        assert groups["b"]["CD3"] == pytest.approx(0.7)
        assert model.weight_provenance("CD3") == AUTHORITATIVE

        # ...and a channel nobody has weighted DOES get the default, once.
        model.set_fusion_enabled("CD8", True)
        assert model.channel_weight("CD8") == 1.0
        assert model.weight_provenance("CD8") == AUTO
    finally:
        w.close()


def test_an_explicit_edit_unifies_every_group_including_zero(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.install_draft({
            "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                       "b": {"group_weight": 1.0, "channels": {"CD3": 0.7}}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["CD3"], "provenance": {"CD3": AUTHORITATIVE},
        })
        rep = model.representative_weight("CD3")
        assert (rep.value, rep.mixed) == (pytest.approx(0.7), True)

        model.edit_channel_weight("CD3", 0.0)

        assert model.groups()["a"]["CD3"] == 0.0
        assert model.groups()["b"]["CD3"] == 0.0
        assert model.weight_provenance("CD3") == EXPLICIT
        assert model.representative_weight("CD3").mixed is False
    finally:
        w.close()


def test_a_repaint_is_not_a_scientific_command(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.install_draft({
            "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                       "b": {"group_weight": 1.0, "channels": {"CD3": 0.7}}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["CD3"], "provenance": {"CD3": AUTHORITATIVE},
        })
        commands = []
        model.participation_changed.connect(
            lambda *a: commands.append(("participation",) + a))
        model.weight_changed.connect(lambda ch: commands.append(("weight", ch)))

        w.config._rebuild_rows()
        w.config.set_current_channel("CD3", auto_show=True)
        w.config.restore_display_state(colors={"CD3": "#00ccff"},
                                       visibility={"CD3": True},
                                       current_channel="CD3")
        _pump()

        assert commands == [], commands
        assert w.config.get_groups()["a"]["CD3"] == pytest.approx(0.2)
        assert w.config.get_groups()["b"]["CD3"] == pytest.approx(0.7)
    finally:
        w.close()


# ── C. the four session shapes ───────────────────────────────────────────

def _s2_session():
    return {
        "version": 1,
        "fusion_config": {
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {
                "a": {"group_weight": 1.0, "channels": {"CD3": 0.2, "CD8": 0.0}},
                "b": {"group_weight": 1.0, "channels": {"CD3": 0.7}}},
        },
    }


def test_the_four_shapes_are_told_apart_once(app):
    assert fusion_domain.classify_session(
        {"channel_weights": {"DAPI": 1.0, "CD3": 0.5}}) == fusion_domain.S1_FLAT
    assert fusion_domain.classify_session(_s2_session()) == \
        fusion_domain.S2_GROUPED
    s3 = dict(_s2_session(), channel_visibility={"CD3": False})
    assert fusion_domain.classify_session(s3) == fusion_domain.S3_VISIBILITY
    assert fusion_domain.classify_session(
        {"version": fusion_domain.SESSION_SCHEMA_VERSION,
         "fusion_draft": {}}) == fusion_domain.S4_SPLIT


def test_an_s1_flat_session_restores_every_member_enabled(app):
    w = _window(app)
    try:
        sess = {"version": 1,
                "channel_weights": {"DAPI": 1.0, "CD3": 0.0, "CD8": 0.6}}
        visibility = w._restore_step1_scientific_state(sess)

        model = w._display.fusion
        assert model.fusion_enabled("CD3") is True, \
            "a zero-weight member was the participating set before visibility"
        assert model.weight_provenance("CD3") == AUTHORITATIVE
        assert model.channel_weight("CD8") == pytest.approx(0.6)
        assert visibility["CD3"] is True
        # ...and the restored zero is an answer, so a later enable keeps it.
        model.set_fusion_enabled("CD3", False)
        model.set_fusion_enabled("CD3", True)
        assert model.channel_weight("CD3") == 0.0
    finally:
        w.close()


def test_an_s2_grouped_session_keeps_every_group_value(app):
    w = _window(app)
    try:
        w._restore_step1_scientific_state(_s2_session())

        model = w._display.fusion
        groups = model.groups()
        assert groups["a"]["CD3"] == pytest.approx(0.2)
        assert groups["b"]["CD3"] == pytest.approx(0.7)
        rep = model.representative_weight("CD3")
        assert (rep.value, rep.mixed) == (pytest.approx(0.7), True)
        assert w.config._rows["CD3"].name_label.text() == "CD3 *"
        assert model.fusion_enabled("CD8") is True   # zero-weight member

        # Neither a repaint nor an enable flattens it.
        w.config._rebuild_rows()
        model.set_fusion_enabled("CD3", False)
        model.set_fusion_enabled("CD3", True)
        groups = model.groups()
        assert groups["a"]["CD3"] == pytest.approx(0.2)
        assert groups["b"]["CD3"] == pytest.approx(0.7)
    finally:
        w.close()


def test_an_s3_session_keeps_a_hidden_members_numbers_and_leaves_it_out(app):
    w = _window(app)
    try:
        sess = dict(_s2_session(),
                    channel_visibility={"DAPI": True, "CD3": False,
                                        "CD8": True})
        visibility = w._restore_step1_scientific_state(sess)

        model = w._display.fusion
        assert model.fusion_enabled("CD3") is False, \
            "a channel that was off came back on merely because it has a weight"
        assert visibility["CD3"] is False
        assert model.groups()["a"]["CD3"] == pytest.approx(0.2)
        assert model.groups()["b"]["CD3"] == pytest.approx(0.7)
        assert "CD3" not in model.effective_config()["groups"]["a"]["channels"]
        # Enabling it later restores its own values, not a default.
        model.set_fusion_enabled("CD3", True)
        assert model.groups()["a"]["CD3"] == pytest.approx(0.2)
    finally:
        w.close()


def test_an_s3_empty_marker_cannot_erase_a_real_number(app):
    w = _window(app)
    try:
        sess = dict(_s2_session(),
                    channel_visibility={"DAPI": True, "CD3": True,
                                        "CD8": True},
                    channel_weight_initialized=[])
        w._restore_step1_scientific_state(sess)

        model = w._display.fusion
        assert model.weight_provenance("CD3") == AUTHORITATIVE, \
            "an empty marker turned a real 0.2/0.7 into an absence"
        assert model.weight_provenance("CD8") == ABSENT, \
            "a genuinely unclaimed zero is still an absence"
        # So the enable default reaches CD8 and cannot reach CD3.
        model.set_fusion_enabled("CD3", False)
        model.set_fusion_enabled("CD3", True)
        assert model.representative_weight("CD3").value == pytest.approx(0.7)
        model.set_fusion_enabled("CD8", False)
        model.set_fusion_enabled("CD8", True)
        assert model.channel_weight("CD8") == 1.0
    finally:
        w.close()


def test_the_new_schema_says_display_and_fusion_separately(app):
    w = _window(app)
    try:
        # Visible but out of the science, and in the science but not drawn.
        w.config.set_channel_visible("CD3", True)
        w.config.set_fusion_enabled("CD3", False)
        w.config.set_channel_visible("CD8", False)
        w.config.set_fusion_enabled("CD8", True)
        w._display.fusion.edit_channel_weight("CD8", 0.0)
        payload = json.loads(json.dumps(w._step1_session_payload()))

        assert payload["version"] == fusion_domain.SESSION_SCHEMA_VERSION
        assert payload["display_visibility"]["CD3"] is True
        assert payload["fusion_enabled"] == ["CD8", "DAPI"]
        assert payload["channel_weight_provenance"]["CD8"] == EXPLICIT
    finally:
        w.close()

    w = _window(app)
    try:
        visibility = w._restore_step1_scientific_state(payload)
        model = w._display.fusion

        assert visibility["CD3"] is True and model.fusion_enabled("CD3") is False
        assert visibility["CD8"] is False and model.fusion_enabled("CD8") is True
        assert model.weight_provenance("CD8") == EXPLICIT
        assert model.channel_weight("CD8") == 0.0, \
            "an explicit zero and an absence are the same number on disk"
    finally:
        w.close()


def test_a_draft_and_a_committed_snapshot_survive_apart(app, tmp_path):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        committed = {"hash": w._fusion_settings_hash(),
                     "fusion_config": w._effective_fusion_config()}
        w._display.fusion.install_committed_snapshot(committed)
        assert w._fusion_settings_dirty() is False

        # The draft moves on; the committed fact does not.
        w._display.fusion.edit_channel_weight("CD3", 0.33)

        assert w._committed_fusion_settings()["hash"] == committed["hash"]
        assert w._fusion_settings_hash() != committed["hash"]
        assert w._fusion_settings_dirty() is True
        assert w.config.channel_weight("CD3") == pytest.approx(0.33)
    finally:
        w.close()


def test_a_failed_commit_leaves_the_committed_snapshot_alone(app, monkeypatch):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        good = {"hash": w._fusion_settings_hash()}
        w._display.fusion.install_committed_snapshot(good)
        w._display.fusion.edit_channel_weight("CD3", 0.2)
        monkeypatch.setattr(type(w), "_handoff_identity", lambda self: None)

        assert w._commit_fusion_settings() is False

        assert w._committed_fusion_settings() == good
    finally:
        w.close()


# ── D. edits made from another step, and the consumers ───────────────────

def test_a_weight_edited_in_step3_reaches_the_draft_and_the_picture(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_fusion_enabled("CD3", True)
        w._display.fusion.install_committed_snapshot(
            {"hash": w._fusion_settings_hash()})
        w._set_step_active(3)
        _pump()
        editor = w.weight_editor_widget()
        spin = editor.spin_for("CD3")
        assert spin is not None, "the shared Weights window has no CD3 control"

        spin.setValue(0.3)                       # the real control, in Step3
        _pump()

        model = w._display.fusion
        assert model.channel_weight("CD3") == pytest.approx(0.3)
        assert model.groups()["markers"]["CD3"] == pytest.approx(0.3)
        assert w._fusion_settings_dirty() is True
        spec = w._display.render_spec() or {}
        weights = spec.get("weights") or {}
        if "CD3" in weights:
            assert weights["CD3"] == pytest.approx(0.3)

        # Back in Step1 the row shows the same number, and so does a Save.
        w._set_step_active(1)
        _pump()
        assert w.config._rows["CD3"].weight() == pytest.approx(0.3)
        assert w._effective_fusion_config()["groups"]["markers"]["channels"][
            "CD3"] == pytest.approx(0.3)
    finally:
        w.close()


def test_the_overlay_reads_visibility_and_the_fusion_reads_participation(app):
    """Set deliberately apart, they give different and correct sets."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_fusion_enabled("CD3", False)
        w.config.set_channel_visible("CD8", False)
        w.config.set_fusion_enabled("CD8", True)

        assert set(w.config.visible_channels()) == {"DAPI", "CD3"}
        fused = set(w._effective_fusion_config()["groups"]["markers"]["channels"])
        assert fused == {"CD8"}
    finally:
        w.close()


# ── E. atomicity and no-ops ──────────────────────────────────────────────

def test_a_restore_is_one_notice_over_a_whole_draft(app):
    w = _window(app)
    try:
        model = w._display.fusion
        seen = []
        model.draft_restored.connect(lambda: seen.append((
            model.groups(), model.enabled_channels(),
            dict(model.draft_snapshot()["provenance"]))))
        commands = []
        model.participation_changed.connect(
            lambda *a: commands.append(("participation",) + a))
        model.weight_changed.connect(lambda ch: commands.append(("weight", ch)))

        w._restore_step1_scientific_state(dict(
            _s2_session(), channel_visibility={"CD3": True, "CD8": False}))

        assert len(seen) == 1, seen
        groups, enabled, provenance = seen[0]
        assert groups["a"]["CD3"] == pytest.approx(0.2)
        assert groups["b"]["CD3"] == pytest.approx(0.7)
        assert enabled == ["CD3"]
        assert provenance["CD3"] == AUTHORITATIVE
        assert commands == [], \
            "a restore ran a user command instead of installing a draft"
    finally:
        w.close()


def test_installing_the_same_committed_snapshot_changes_no_dirty_state(app):
    w = _window(app)
    try:
        w.config.set_fusion_enabled("CD3", True)
        snapshot = {"hash": w._fusion_settings_hash()}
        w._display.fusion.install_committed_snapshot(snapshot)
        assert w._fusion_settings_dirty() is False

        w._display.fusion.install_committed_snapshot(snapshot)

        assert w._fusion_settings_dirty() is False
        assert w._committed_fusion_settings() == snapshot
    finally:
        w.close()


def test_a_no_op_scientific_write_says_nothing(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.set_fusion_enabled("CD3", True)
        model.edit_channel_weight("CD3", 0.5)
        seen = []
        model.draft_changed.connect(lambda: seen.append("draft"))
        model.weight_changed.connect(lambda ch: seen.append(("weight", ch)))
        model.participation_changed.connect(
            lambda *a: seen.append(("participation",) + a))

        assert model.set_fusion_enabled("CD3", True) is False
        assert model.edit_channel_weight("CD3", 0.5) is False

        assert seen == []
    finally:
        w.close()


def test_the_weight_notice_does_not_come_from_a_step1_widget(app):
    """Step0 asks the model and is told by the model.

    The query and the live notice both have to work with no Step1 page built,
    because Step0's Tissue Preview reads the representative scientific weight
    through the shared render chain. Applying it to Step0's own compositing is
    B4's; not depending on a QWidget for the answer is this block's.
    """
    w = _bare_window()
    try:
        model = w._display.fusion
        told = []
        model.weight_changed.connect(told.append)

        assert w._display.set_render_weight("CD3", 0.6) is True

        assert told == ["CD3"]
        assert w._display.render_weight("CD3") == pytest.approx(0.6)
        assert w.config._rows == {}, \
            "this window has no channel rows at all, and answered anyway"
    finally:
        w.close()


def test_a_visual_fallback_is_never_saved_as_a_scientific_weight(app):
    """A channel with no answer shows a number; the model still says absent,
    and the session writes that rather than the number on screen."""
    w = _window(app)
    try:
        model = w._display.fusion
        rep = model.representative_weight("CD8")

        assert rep.absent is True
        assert model.weight_provenance("CD8") == ABSENT
        payload = w._step1_session_payload()
        assert payload["channel_weight_provenance"].get("CD8", ABSENT) == ABSENT
        assert "CD8" not in payload["fusion_enabled"]
    finally:
        w.close()
