"""
block01/ui/step0/config_panel.py — Step1's ONE channel panel.

One flat channel list, one state owner. Each row carries the three things a
channel has in Step1:

    click the row  -> it becomes the CURRENT channel (what Intensity edits)
    the checkbox   -> whether it takes part in the multi-channel overlay
    the weight box -> its 0..1 contribution to the FUSION preview

THE STATE TABLE. Both previews read exactly this, and so does everything a
Save writes:

    selected/current  the channel the Intensity window edits
    checked/visible   whether this channel takes part in the picture at all
    weight 0..1       how strongly it takes part, in overlay and in fusion
    colour            what colour the overlay draws it in
    Min/Max/Gamma     the one live display mapping for this channel

Checked and weight are INDEPENDENT. The tick says whether a channel is part of
the configuration; the weight says how much it contributes. Neither moves the
other:

  * clicking a row selects it AND ticks it -- selecting something invisible is
    a dead end;
  * ticking or unticking some OTHER channel never moves the selection;
  * a weight of 0 on a ticked channel is a legal state: the channel is in the
    configuration and contributes nothing right now;
  * moving a weight never ticks or unticks anything, and `Reset weights` zeroes
    the markers without touching the ticks;
  * the FIRST tick a marker ever gets in this dataset sets its weight to 1.0,
    because a channel the user just asked to see and cannot see is not an
    answer to anything. It happens once, before the tick is announced, so
    nothing ever observes the channel ticked at 0;
  * every tick after that leaves the number alone: unticking by hand keeps the
    weight exactly as it was, so re-ticking brings that number back -- 0.00
    included, because a 0 the user chose, or `Reset weights` set, or a file
    supplied, is a decision and not an absence. Which zeros are which is
    recorded apart from the numbers (`_weight_initialized`), carried in the
    session, and cleared for a new dataset;
  * while a channel is unticked the effective configuration -- overlay, fusion
    preview, and what a Save fuses -- excludes it completely.

`get_full_config` is still the whole panel including unticked channels, because
that is what a session must round-trip. `effective_config` is what is drawn and
what is fused.

Channel GROUPS are still here, they are just not on screen any more. Group
membership and group weight remain in the model and in every file that carries
them, because folding `group_weight x channel_weight` into one number would
change old projects, the HQ workers, Mesmer and the on-disk fusion. New
projects get a single group at weight 1.0 and never notice.
"""

import os
import json

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QFileDialog, QCheckBox, QSlider,
    QDoubleSpinBox, QListWidget, QListWidgetItem, QColorDialog,
)
from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import QColor

from ...config import NUCLEUS_CONFIG
from ..widgets.channel_dock import template

DEFAULT_GROUP = "markers"

# THE palette and THE row height, from the shared template. This module used
# to carry a second palette of its own -- ten different colours in a different
# order -- and a second list stylesheet that was the dock's minus the
# `item:selected:hover` rule, so a selected row under the cursor turned green
# here and stayed blue in Step0. Both are gone; these names remain because
# callers and tests import them.
_PALETTE = template.CHANNEL_PALETTE
ROW_HEIGHT = template.ROW_HEIGHT


class ChannelRow(QWidget):
    """One channel: select / show / weight.

    No hover text on any of the three controls. A tick box, a colour swatch
    and a 0..1 slider in a list of channels are read at a glance, and a
    popup over every one of them in a list this long is in the way of the
    work rather than an explanation of it. The rules the tooltips used to
    recite live in this module's docstring, where they are read once. The
    only hover text left in the panel is on things that carry information
    the widget cannot show: the read-only nucleus line, and the warning on
    a row whose channel is in several groups at different weights.
    """

    selected = pyqtSignal(str)
    visibility_toggled = pyqtSignal(str, bool)
    weight_edited = pyqtSignal(str)
    color_clicked = pyqtSignal(str)

    def __init__(self, channel, weight=0.0, visible=False, color="#888888",
                 parent=None):
        super().__init__(parent)
        self.channel = channel
        self._busy = False

        # The common columns come from the shared template -- checkbox, the
        # always-present state slot, swatch and name -- so this row and the
        # dock's `ChannelRowBase` cannot drift apart in geometry or style.
        # Everything below is this row's ACCESSORY: the weight controls.
        # `tooltips=False`: this panel's rows carry no hover text on their
        # controls, by the product decision recorded in the class docstring.
        core = template.build_row_core(self, name=channel, tooltips=False)
        lay = core.layout
        self.checkbox = core.checkbox
        self.state_slot = core.state_slot
        self.swatch = core.swatch
        self.name_label = core.name_label

        self.checkbox.setChecked(bool(visible))
        self.checkbox.toggled.connect(self._on_toggled)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(float(weight) * 100)))
        self.slider.setFixedHeight(16)
        self.slider.setMinimumWidth(60)
        template.add_accessory(lay, self.slider, stretch=2)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(float(weight))
        self.spin.setFixedWidth(56)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setStyleSheet(template.ACCESSORY_SPINBOX_QSS)
        template.add_accessory(lay, template.fit_accessory(self.spin))

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.set_color(color)

    # -- weight (the spinbox is the authority; the slider follows it) -------
    def weight(self):
        return float(self.spin.value())

    def set_weight(self, value):
        if abs(self.weight() - float(value)) < 1e-9:
            return
        self._busy = True
        try:
            self.spin.setValue(float(value))
            self.slider.setValue(int(round(float(value) * 100)))
        finally:
            self._busy = False

    def _on_slider(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.spin.setValue(v / 100.0)
        finally:
            self._busy = False
        self.weight_edited.emit(self.channel)

    def _on_spin(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.slider.setValue(int(round(float(v) * 100)))
        finally:
            self._busy = False
        self.weight_edited.emit(self.channel)

    # -- visibility ---------------------------------------------------------
    def is_visible(self):
        return self.checkbox.isChecked()

    def set_weight_editable(self, editable):
        """A read-only row still shows its weight; it just cannot be moved.

        A disabled slider and a read-only box say that by themselves; the
        row's controls carry no hover text -- see the class docstring.
        """
        self.slider.setEnabled(bool(editable))
        self.spin.setReadOnly(not editable)
        self.spin.setButtonSymbols(
            QDoubleSpinBox.UpDownArrows if editable else QDoubleSpinBox.NoButtons)

    def set_visible(self, visible):
        if self.checkbox.isChecked() == bool(visible):
            return
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(bool(visible))
        self.checkbox.blockSignals(False)

    def _on_toggled(self, checked):
        self.visibility_toggled.emit(self.channel, bool(checked))

    # -- colour -------------------------------------------------------------
    def set_color(self, color):
        self._color = color
        template.apply_swatch_color(self.swatch, color)

    def color(self):
        return self._color

    # -- selection ----------------------------------------------------------
    def mousePressEvent(self, ev):
        self.selected.emit(self.channel)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.swatch.geometry().contains(ev.pos()):
            self.color_clicked.emit(self.channel)
        super().mouseReleaseEvent(ev)


class ConfigPanel(QWidget):
    """The one owner of Step1's channel state."""

    config_changed = pyqtSignal()                 # weights / nucleus changed
    visibility_changed = pyqtSignal(str, bool)    # overlay membership changed
    current_channel_changed = pyqtSignal(str)     # what Intensity should edit
    color_changed = pyqtSignal(str, str)          # overlay colour changed

    def __init__(self, all_channels):
        super().__init__()
        self.all_channels = list(all_channels or [])
        # group membership and group weight: model-only, never on screen
        self._groups = {}          # group -> {"weight": float, "members": [ch]}
        # The per-(group, channel) weights EXACTLY as they were loaded.  A row
        # can only show one number, but an old project may put the same channel
        # in two groups at two weights; those values are kept here so a session
        # nobody edited round-trips value for value.
        self._group_channel_weights = {}   # group -> {channel: float}
        self._nucleus_channel = ""
        self._nucleus_weight = 0.0
        # Channels whose weight the USER moved since the config was loaded.
        # Only those are written back, and then to every group they belong to.
        self._edited_channels = set()
        # Channels whose weight is an ANSWER rather than an absence.
        #
        # Kept apart from the numbers on purpose. A marker starts a dataset at
        # 0 because nobody has said anything about it yet, and a user can also
        # deliberately set one to 0.00 -- the same number, two different
        # facts, and only one of them may be replaced by a default. So this
        # records who said it rather than what it says: the user moved the
        # weight, `Reset weights` set it, or a config/weights file supplied
        # it. `weight == 0` can never be the test (it would overwrite a 0 the
        # user chose), and neither can `_edited_channels`, which already means
        # something else -- which channels' rows get written back to every
        # group they belong to in an old multi-group project.
        self._weight_initialized = set()
        self._rows = {}            # channel -> ChannelRow
        self._items = {}           # channel -> QListWidgetItem
        self._colors = {}          # channel -> "#rrggbb" -- A MIRROR, see below
        # Block01's shared display state, once a host registers it. While it
        # is set, IT is the answer to "what colour is this channel" and
        # `_colors` is only the copy the rows are drawn from. Before this
        # existed the panel dealt its own palette, so the same channel came up
        # one colour in Step0 and another in Step1 -- and `load_panel`
        # re-dealt it on every dataset, which is why a colour the user had
        # picked in Step0 did not survive the walk into Step1.
        self._display_state = None
        self._current = ""
        self._selecting = False
        self._setup_ui()
        self._rebuild_rows()

    # ── construction ──────────────────────────────────────────────────
    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        nuc_row = QHBoxLayout()
        nuc_row.setSpacing(4)
        nuc_lbl = QLabel("Nucleus:")
        nuc_lbl.setStyleSheet("color:#9bd0ff;font-size:10px;")
        nuc_row.addWidget(nuc_lbl)
        # Read-only on purpose.  The nucleus channel and its weight come from
        # the Step0 handoff; a flat per-channel list cannot express "this
        # channel is the nucleus at one weight AND a marker at another", so
        # Step1 shows the authoritative answer rather than inventing a second
        # one.  Changing the nucleus is a Step0 decision.
        self._nuc_value = QLabel("—")
        self._nuc_value.setStyleSheet("color:#dce5ef;font-size:10px;")
        self._nuc_value.setToolTip(
            "The nucleus channel and weight come from Step0's handoff and are "
            "read-only here.")
        nuc_row.addWidget(self._nuc_value, stretch=1)
        lay.addLayout(nuc_row)

        self._list = QListWidget()
        template.apply_list_style(self._list)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.currentItemChanged.connect(self._on_current_item)
        lay.addWidget(self._list, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        btn_reset = QPushButton("Reset weights")
        btn_reset.setStyleSheet(
            "QPushButton{color:#e5c07b;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:2px 8px;font-size:10px;}"
            "QPushButton:hover{background:#23354a;}")
        btn_reset.clicked.connect(self._reset_all_channel_weights)
        btn_row.addWidget(btn_reset)

        btn_load_weights = QPushButton("Load weights")
        btn_load_weights.setStyleSheet(
            "QPushButton{color:#9bd0ff;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:2px 8px;font-size:10px;}"
            "QPushButton:hover{background:#23354a;}")
        btn_load_weights.clicked.connect(self._load_weights_from_file)
        btn_row.addWidget(btn_load_weights)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    # ── rows ──────────────────────────────────────────────────────────
    def set_display_state(self, state):
        """Make this panel a mirror of Block01's canonical colours.

        Registered by the host that owns both. From here the panel reads the
        shared answer, writes back to it, and repaints when it changes --
        which is the whole of "one channel, one colour, in every step".
        """
        self._display_state = state
        if state is None:
            return
        state.color_changed.connect(self._adopt_shared_color)
        self._resync_colors_from_state()

    def _resync_colors_from_state(self):
        """Take every row's colour from the shared state, silently.

        Silent because the state has already decided: re-emitting from here
        would be a second opinion rather than a confirmation, and it is the
        loop that made "who wins" a question of signal order.
        """
        state = self._display_state
        if state is None:
            return
        for ch, row in self._rows.items():
            hexc = state.color(ch)
            self._colors[ch] = hexc
            row.set_color(hexc)

    def _adopt_shared_color(self, channel, color):
        """A colour settled anywhere: this panel's row takes it. No re-emit."""
        if not channel:
            return
        self._colors[channel] = str(color)
        row = self._rows.get(channel)
        if row is not None:
            row.set_color(str(color))

    def _default_color(self, ch):
        """The colour a channel wears before anyone picks one.

        Asked of the shared state when there is one, so the palette is dealt
        ONCE for the whole process. A panel standing alone (its own tests)
        falls back to the SHARED template palette -- the same list, in the
        same order, that the rest of Block01 deals from. It used to fall back
        to a private one, which meant "no shared state yet" was a licence to
        show a different colour.
        """
        state = self._display_state
        if state is not None:
            hexc = state.color(ch)
            if hexc:
                return hexc
        try:
            i = self.all_channels.index(ch)
        except ValueError:
            i = len(self._rows)
        return template.palette_color(i)

    def _rebuild_rows(self):
        """Rebuild the visible list from `all_channels`, keeping state.

        Scroll position and keyboard focus belong to the LIST, not to the
        data: rebuilding is the same channels drawn again, and landing the
        user back at the top of a long panel is not part of that. Same rule
        as `ChannelDock.rebuild`.
        """
        weights = {ch: row.weight() for ch, row in self._rows.items()}
        visible = {ch: row.is_visible() for ch, row in self._rows.items()}
        current = self._current
        keep_scroll = self._list.verticalScrollBar().value()
        keep_focus = self._list.hasFocus()

        self._list.clear()
        self._rows.clear()
        self._items.clear()

        for ch in self.all_channels:
            # `_default_color` asks the shared state first, so a rebuild takes
            # the canonical colour rather than re-dealing this panel's palette.
            color = self._colors.get(ch) or self._default_color(ch)
            row = ChannelRow(ch,
                             weight=weights.get(ch, 0.0),
                             visible=visible.get(ch, False),
                             color=color)
            row.selected.connect(self._on_row_selected)
            row.visibility_toggled.connect(self._on_row_visibility)
            row.weight_edited.connect(self._on_row_weight_edited)
            row.color_clicked.connect(self._pick_color)
            item = QListWidgetItem(self._list)
            item.setSizeHint(template.item_size_hint(row, width=200))
            self._list.setItemWidget(item, row)
            self._rows[ch] = row
            self._items[ch] = item

        # ONE name column for the list -- the longest name -- computed by the
        # same template helper the dock uses, so the two lists put the
        # accessory at the same x for the same channel names.
        template.uniform_name_width(list(self._rows.values()))
        self._list.verticalScrollBar().setValue(keep_scroll)
        if keep_focus:
            self._list.setFocus(Qt.OtherFocusReason)

        # A channel that is gone takes its history with it; one that is
        # still here keeps it, because rebuilding the rows is not a new
        # dataset.
        self._weight_initialized &= set(self._rows)

        self._refresh_nucleus_display()
        if current in self._rows:
            self.set_current_channel(current)
        else:
            self._current = ""

    # ── selection / visibility / colour ───────────────────────────────
    def _on_row_selected(self, channel):
        self.set_current_channel(channel)

    def _on_current_item(self, item, _prev):
        if item is None or self._selecting:
            return
        for ch, it in self._items.items():
            if it is item:
                self.set_current_channel(ch)
                return

    def current_channel(self):
        return self._current

    def set_current_channel(self, channel, auto_show=True):
        """Make `channel` current.

        A user CLICK also ticks a hidden channel, because selecting something
        you cannot see is a dead end. Restoring a saved session is not a click:
        it passes `auto_show=False` so a channel the user deliberately left
        hidden comes back hidden. Ticking some OTHER channel never moves the
        selection either way.
        """
        if channel not in self._rows:
            return
        row = self._rows[channel]
        newly_visible = auto_show and not row.is_visible()
        if newly_visible:
            # A click on a hidden row is that channel's first tick as much as
            # the checkbox is, so it goes through the same decision, before
            # the tick is announced.
            self._first_enable_weight(channel)
            row.set_visible(True)
        changed = (channel != self._current)
        self._current = channel
        self._selecting = True
        try:
            self._list.setCurrentItem(self._items[channel])
        finally:
            self._selecting = False
        if newly_visible:
            self.visibility_changed.emit(channel, True)
        if changed:
            self.current_channel_changed.emit(channel)

    def _mark_weight_given(self, channel):
        """Record that somebody named this channel's weight.

        The nucleus is never in this set: its weight comes from the Step0
        handoff, it is read-only here, and the first-tick rule does not apply
        to it -- so recording it would only put a channel in the session's
        history that the history has nothing to say about.
        """
        if not channel or channel == self._nucleus_channel:
            return
        self._weight_initialized.add(channel)

    def _first_enable_weight(self, channel):
        """Give a channel nobody has weighted yet the weight 1.0.

        The FIRST time a marker is ticked it becomes visible, and a channel
        that is visible at weight 0 is a channel the user asked to see and
        cannot: the tick is the moment to answer, and the answer is 1.0.
        Afterwards the weight is the user's, whatever it is -- so this runs
        once per channel per dataset and never again.

        Silent by design. The weight is set BEFORE the tick is announced, and
        `config_changed` is deliberately NOT emitted for it: the visibility
        handler already reloads the channel, redraws, marks the settings
        unsaved and schedules the session save, so announcing the weight
        separately would either draw the picture twice or draw it once with
        the channel ticked at 0 -- a blank frame the user sees before the real
        one. One logical act, one notification, the state already final when
        it arrives.

        The nucleus is untouched: its weight comes from the Step0 handoff and
        is read-only here.
        """
        if not channel or channel == self._nucleus_channel:
            return False
        if channel in self._weight_initialized:
            return False
        row = self._rows.get(channel)
        if row is None:
            return False
        self._mark_weight_given(channel)
        row.set_weight(1.0)
        self._edited_channels.add(channel)
        return True

    def _on_row_visibility(self, channel, visible):
        """The user ticked or unticked a row.

        Ticking for the FIRST time gives the channel weight 1.0 -- see
        `_first_enable_weight`. Ticking it again does not: unticking keeps the
        number, so re-ticking brings back exactly what the user left, 0.00
        included. Unticking never touches the weight at all.
        """
        if visible:
            self._first_enable_weight(channel)
        self.visibility_changed.emit(channel, bool(visible))

    def visible_channels(self):
        return [ch for ch in self.all_channels
                if ch in self._rows and self._rows[ch].is_visible()]

    def set_channel_visible(self, channel, visible):
        """Tick or untick `channel` — the same act as clicking its box.

        The same act, so the same rule: a first tick brings weight 1.0 with
        it, through the one decision in `_first_enable_weight`. Restoring a
        saved session does NOT come through here; it sets the rows directly,
        so a session brings back exactly what was saved -- including a marker
        the user never enabled, which stays at 0 and un-initialised.
        """
        row = self._rows.get(channel)
        if row is None or row.is_visible() == bool(visible):
            return
        if visible:
            self._first_enable_weight(channel)
        row.set_visible(visible)
        self.visibility_changed.emit(channel, bool(visible))

    def channel_color(self, channel):
        """`channel`'s colour -- the shared answer when there is one.

        The ROW is a mirror, not the store. It used to be asked first, which
        meant a row built before the shared state existed kept answering with
        the palette this panel dealt for itself.
        """
        state = self._display_state
        if state is not None:
            return state.color(channel)
        row = self._rows.get(channel)
        if row is not None:
            return row.color()
        return self._colors.get(channel) or self._default_color(channel)

    def channel_colors(self):
        return {ch: self.channel_color(ch) for ch in self._rows}

    def set_channel_color(self, channel, color):
        """Set `channel`'s colour. ONE write, wherever it came from.

        The shared state is written first, because it is the answer every
        other view reads; the row is repainted because it is this panel's
        copy of it. `color_changed` still fires for the host, and it is safe
        to fire: the state swallows a write that changes nothing, so an echo
        cannot start a second lap.
        """
        if not color:
            return
        state = self._display_state
        if state is not None:
            state.set_color(channel, color, origin="step1-panel")
        self._colors[channel] = color
        row = self._rows.get(channel)
        if row is not None:
            row.set_color(color)
        self.color_changed.emit(channel, color)

    def _pick_color(self, channel):
        current = QColor(self.channel_color(channel))
        picked = QColorDialog.getColor(current, self, f"Colour for {channel}")
        if picked.isValid():
            self.set_channel_color(channel, picked.name())

    # ── weights ───────────────────────────────────────────────────────
    def channel_weight(self, channel):
        row = self._rows.get(channel)
        return float(row.weight()) if row is not None else 0.0

    def _sync_row_weight(self, channel, weight):
        """Put a number in a row, and say nothing about who chose it.

        The panel's own bookkeeping uses this: showing the representative of
        an old project's several group weights, showing the nucleus weight the
        handoff gave. None of that is an answer ABOUT the channel -- it is the
        row catching up with the model -- so it must not mark the weight as
        given (which would defeat the first-tick default) and must not mark
        the channel as edited (which would write one row's number into every
        group it belongs to and flatten an old project's per-group weights).
        """
        row = self._rows.get(channel)
        if row is None:
            return
        row.set_weight(weight)

    def set_channel_weight(self, channel, weight):
        """Set a channel's weight from OUTSIDE the panel: the same act as
        the user moving its slider.

        So the same consequences, both of them. It is an answer, so a later
        first tick will not replace it with the default -- 0.00 included. And
        it is this channel's weight everywhere, so it goes to every group the
        channel belongs to, exactly as an edit does: without that, the row
        showed 0.5 while `get_groups`, the overlay, the fusion preview and
        every Save still read the 0 the group was loaded with. One number on
        screen and a different one in the picture is the split this panel
        exists to prevent.

        The panel's own row-syncing does NOT come through here; it uses
        `_sync_row_weight`, which claims neither.
        """
        row = self._rows.get(channel)
        if row is None:
            return
        self._mark_weight_given(channel)
        self._edited_channels.add(channel)
        row.set_weight(weight)

    def nucleus_channel(self):
        return self._nucleus_channel

    def set_nucleus(self, channel, weight=None):
        """Adopt the nucleus Step0 handed over.  Not a user-editable choice."""
        self._nucleus_channel = str(channel or "")
        if weight is not None:
            self._nucleus_weight = float(weight)
        self._refresh_nucleus_display()

    def set_nucleus_weight(self, weight):
        self._nucleus_weight = float(weight)
        self._refresh_nucleus_display()

    def _refresh_nucleus_display(self):
        nuc = self._nucleus_channel
        self._nuc_value.setText(
            f"{nuc}  (weight {self._nucleus_weight:.2f})" if nuc else "—")
        for ch, row in self._rows.items():
            is_nuc = bool(nuc) and ch == nuc
            row.set_weight_editable(not is_nuc)
            if is_nuc:
                row.set_weight(self._nucleus_weight)

    def _on_row_weight_edited(self, channel):
        """The user moved a weight: from now on it is theirs.

        The rule for a channel that an old project put in several groups is
        stated once, here: an edited weight applies to EVERY group the channel
        belongs to. Until it is edited, each group keeps the value it was
        loaded with.
        """
        self._edited_channels.add(channel)
        # And it is an answer from now on: a weight the user set is never
        # replaced by the first-tick default, including 0.00, which is a
        # deliberate "in the configuration, contributing nothing".
        self._mark_weight_given(channel)
        # The tick is not touched. A weight is how much a channel contributes,
        # not whether it is part of the configuration, and moving one must not
        # silently add or remove a channel behind the user.
        self.config_changed.emit()

    def effective_config(self):
        """What is actually drawn and actually fused.

        `get_full_config` is the panel's whole state, unticked channels
        included, because a session has to round-trip it. This is the subset
        that takes part: an unticked channel keeps its weight on screen and
        contributes nothing, here and on disk alike, so what is saved can
        never contain a marker the picture does not show.
        """
        cfg = self.get_full_config()
        shown = set(self.visible_channels())
        nuc = cfg.get("nucleus") or {}
        if nuc.get("channel") and nuc["channel"] not in shown:
            cfg["nucleus"] = {"channel": nuc.get("channel"), "weight": 0.0}
        groups = {}
        for name, data in (cfg.get("groups") or {}).items():
            channels = {ch: w for ch, w in (data.get("channels") or {}).items()
                        if ch in shown}
            groups[name] = {"group_weight": data.get("group_weight", 1.0),
                            "channels": channels}
        cfg["groups"] = groups
        return cfg

    def _stored_weights_for(self, channel):
        """Every weight this channel was LOADED with, one per group it is in
        (plus the nucleus slot when it is the nucleus)."""
        values = [weights[channel]
                  for weights in self._group_channel_weights.values()
                  if channel in weights]
        if channel and channel == self._nucleus_channel:
            values.append(float(self._nucleus_weight))
        return values

    def _representative_weight(self, channel):
        values = self._stored_weights_for(channel)
        if not values:
            return 0.0
        return max(values)

    def ambiguous_channels(self):
        """Channels whose loaded weights disagree between groups: one row
        cannot show two numbers, so the disagreement is named rather than
        quietly resolved."""
        out = {}
        for ch in self._rows:
            values = self._stored_weights_for(ch)
            if len(set(round(v, 6) for v in values)) > 1:
                out[ch] = sorted(set(round(v, 6) for v in values))
        return out

    def _effective_weight(self, group, channel):
        """What `get_groups` reports for one (group, channel) pair."""
        if channel in self._edited_channels:
            return self.channel_weight(channel)
        stored = self._group_channel_weights.get(group, {})
        if channel in stored:
            return float(stored[channel])
        return self.channel_weight(channel)

    def zero_marker_weights(self):
        """Every channel but the nucleus back to 0 — the fresh-project state.

        This is a real edit, not a repaint: the zero is what every group the
        channel belongs to reports afterwards, so what the user sees and what
        gets fused and saved are the same number.  One signal, at the end.
        """
        nuc = self._nucleus_channel
        for ch, row in self._rows.items():
            if ch != nuc:
                row.set_weight(0.0)
                self._edited_channels.add(ch)
                # The button is the user saying "zero", so these zeros are
                # answers: a channel unticked and re-ticked after a Reset
                # comes back at 0, not at the first-tick default.
                self._mark_weight_given(ch)
                # The ticks are NOT touched: this resets weights, and a reset
                # that also removed channels from the configuration would be
                # doing something the button does not say.
        self.config_changed.emit()

    def _reset_all_channel_weights(self):
        self.zero_marker_weights()

    # ── groups: model only ────────────────────────────────────────────
    def _group_of(self, channel):
        for name, data in self._groups.items():
            if channel in data["members"]:
                return name
        return None

    def _add_group(self, name, channel_weights=None):
        if name in self._groups:
            return
        self._groups[name] = {"weight": 1.0, "members": []}
        for ch, w in (channel_weights or {}).items():
            self._add_to_group(name, ch, w)

    def _add_to_group(self, name, channel, weight=0.0):
        data = self._groups.setdefault(name, {"weight": 1.0, "members": []})
        if channel not in data["members"]:
            data["members"].append(channel)
        self._group_channel_weights.setdefault(name, {})[channel] = float(weight)
        self._sync_row_weight(channel, self._representative_weight(channel))

    def _del_group(self, name):
        self._groups.pop(name, None)
        self._group_channel_weights.pop(name, None)

    def group_weight(self, name):
        data = self._groups.get(name)
        return float(data["weight"]) if data else 1.0

    def set_group_weight(self, name, weight):
        data = self._groups.setdefault(name, {"weight": 1.0, "members": []})
        data["weight"] = float(weight)

    # ── the config contract (unchanged shape) ─────────────────────────
    def get_groups(self):
        return {name: {ch: self._effective_weight(name, ch)
                       for ch in data["members"]}
                for name, data in self._groups.items()}

    def get_group_weights(self):
        return {name: float(data["weight"]) for name, data in self._groups.items()}

    def get_nucleus(self):
        return self._nucleus_channel, float(self._nucleus_weight)

    def get_full_config(self):
        nuc_ch, nuc_w = self.get_nucleus()
        return {
            "nucleus": {"channel": nuc_ch, "weight": nuc_w},
            "groups": {
                name: {"group_weight": float(data["weight"]),
                       "channels": {ch: self._effective_weight(name, ch)
                                    for ch in data["members"]}}
                for name, data in self._groups.items()
            },
        }

    def load_panel(self, groups, nuc_ch):
        """Load the panel for a freshly opened dataset.

        Defaults, deliberately: the nucleus is the only channel shown and the
        only one with weight; every marker starts at 0 and unticked.
        """
        # The colour MIRROR is emptied, not the answer. Re-dealing a palette
        # here is precisely what made the same channel two colours: Step0 had
        # already dealt one (and the user may already have changed it), and
        # this line then produced a second for the same channel the moment
        # Step1 opened. What follows `_rebuild_rows` takes them back from
        # the shared state, so Step1 ADOPTS Step0's colours rather than
        # inventing its own.
        self._colors = {}
        self._groups = {}
        self._group_channel_weights = {}
        self._edited_channels = set()
        self._nucleus_weight = 0.0
        nuc = str(nuc_ch or "")
        for gname, channels in (groups or {}).items():
            self._add_group(str(gname), {str(ch): 0.0 for ch in channels
                                         if str(ch) != nuc})

        self._rebuild_rows()
        # The rows were just built; give them the canonical colours before
        # anything is shown. Without this line Step1 opens on its own palette
        # and the same channel is two colours -- which is the failure this
        # round closed, and the mutation the tests check for.
        self._resync_colors_from_state()

        self._nucleus_channel = str(nuc_ch or "")

        for ch, row in self._rows.items():
            row.set_weight(0.0)
            row.set_visible(False)
        # A new dataset's markers have never been weighted by ANYONE: their 0
        # is an absence again, and the first tick of each will answer 1.0.
        # Cleared here, at the end, because building the groups and zeroing
        # the rows above writes those zeros through the same setters a host
        # would use -- and those, being somebody's answer, mark the weight as
        # given. Nothing is inherited from the slide that was open before.
        self._weight_initialized = set()
        if nuc_ch and nuc_ch in self._rows:
            self._nucleus_weight = 1.0
            self._sync_row_weight(nuc_ch, 1.0)
            self._refresh_nucleus_display()
            self._rows[nuc_ch].set_visible(True)
            self._current = nuc_ch
            self._selecting = True
            try:
                self._list.setCurrentItem(self._items[nuc_ch])
            finally:
                self._selecting = False
        else:
            self._current = ""
        self.config_changed.emit()

    def apply_full_config(self, cfg, adopt_nucleus=False):
        """Restore groups, group weights and per-channel weights.

        The nucleus is NOT taken from `cfg` by default. It is the Step0
        handoff's answer, and Step1 shows it read-only; letting a saved session
        replace it would create a hidden state the user cannot see or correct.
        `adopt_nucleus=True` is for the case where nothing has told us the
        nucleus yet (a legacy session restored without a handoff).

        The nucleus is also kept OUT of the marker groups: a channel that is
        both the nucleus and a marker would contribute twice, once as blue and
        once as red. Dropping it is announced rather than done quietly.
        """
        cfg = dict(cfg or {})
        nucleus_cfg = cfg.get("nucleus") or {}
        groups_cfg = cfg.get("groups") or {}

        if adopt_nucleus or not self._nucleus_channel:
            self._nucleus_channel = str(nucleus_cfg.get("channel") or "")
            self._nucleus_weight = float(nucleus_cfg.get("weight", 0.0) or 0.0)
        nuc_ch = self._nucleus_channel

        self._groups = {}
        self._group_channel_weights = {}
        self._edited_channels = set()
        # Weights that arrive in a file or a saved config are answers, not
        # absences -- an explicit 0 in a project's fusion config is a decision
        # somebody made, and a first tick afterwards must not overwrite it
        # with 1.0. Collected as the groups are read, below.
        loaded = set()
        dropped = []
        for gname, gdata in groups_cfg.items():
            channels = {}
            for ch, w in ((gdata or {}).get("channels") or {}).items():
                ch = str(ch)
                if self.all_channels and ch not in self.all_channels:
                    continue
                if nuc_ch and ch == nuc_ch:
                    dropped.append(f"{gname}:{ch}")
                    continue
                channels[ch] = float(w)
                loaded.add(ch)
            self._add_group(str(gname), channels)
            self.set_group_weight(str(gname),
                                  float((gdata or {}).get("group_weight", 1.0)))
        if dropped:
            print("[Step1] nucleus channel removed from marker groups "
                  f"(it would contribute twice): {sorted(dropped)}")
        self._weight_initialized = {ch for ch in loaded if ch != nuc_ch}
        if nuc_ch:
            self._sync_row_weight(nuc_ch,
                                  self._representative_weight(nuc_ch))
        self._refresh_nucleus_display()
        ambiguous = self.ambiguous_channels()
        for ch, values in ambiguous.items():
            row = self._rows.get(ch)
            if row is not None:
                row.name_label.setText(f"{ch} *")
                row.setToolTip(
                    f"{ch} is in several groups at different weights "
                    f"({', '.join(str(v) for v in values)}). The row shows the "
                    "largest; editing it applies that weight to every group.")
        if ambiguous:
            print(f"[Step1] channels in several groups at different weights: "
                  f"{sorted(ambiguous)}")
        self.config_changed.emit()

    def weight_initialized_channels(self):
        """The channels whose weight is an answer, for a session to carry.

        Without this a session cannot tell its own zeros apart: a marker
        nobody ever enabled and a marker the user deliberately set to 0.00
        both save as 0.0, and after a restart the first tick would either
        overwrite the user's decision or leave the untouched channel invisible
        at 0. The numbers alone cannot say which is which; this says it.
        """
        return sorted(self._weight_initialized)

    def restore_weight_initialization(self, channels):
        """Put back exactly which weights are answers. Not a click.

        Replaces whatever loading the config marked, because the session knows
        better: `apply_full_config` has to treat every weight in a file as
        authoritative (it cannot see who wrote it), while a session written by
        this panel recorded the truth. Channels that no longer exist are
        dropped rather than remembered.
        """
        if isinstance(channels, dict):
            channels = [ch for ch, flag in channels.items() if flag]
        known = set(self._rows) or set(self.all_channels or [])
        self._weight_initialized = {str(ch) for ch in (channels or [])
                                    if str(ch) in known
                                    and str(ch) != self._nucleus_channel}

    def restore_display_state(self, colors=None, visibility=None,
                              current_channel=""):
        """Put back a saved display state in one go, with no side effects.

        Restoring is not clicking: a channel the user left hidden must come
        back hidden even when it is the current one, and the host should redraw
        once at the end rather than once per channel.
        """
        state = self._display_state
        if state is not None and colors:
            # ONE transaction: the shared store is replaced and every view --
            # Step0's swatches, the Intensity histogram, both main viewers and
            # the Tissue Preview -- comes back together. Restoring only this
            # panel's rows would put the session's colours in Step1 and leave
            # Step0 on the ones it dealt.
            state.adopt_colors({str(ch): str(c) for ch, c in colors.items()
                                if ch and c}, origin="step1-session")
        for ch, color in (colors or {}).items():
            ch = str(ch)
            if not color:
                continue
            self._colors[ch] = str(color)
            row = self._rows.get(ch)
            if row is not None:
                row.set_color(str(color))

        if visibility:
            for ch in list(self.all_channels or []):
                if ch in visibility:
                    row = self._rows.get(ch)
                    if row is not None:
                        row.set_visible(bool(visibility[ch]))

        if current_channel and current_channel in self._rows:
            self.set_current_channel(current_channel, auto_show=False)

    def _load_weights_from_file(self):
        mw = self.window()
        out_dir = ""
        if hasattr(mw, "current_gui_work_dir"):
            out_dir = mw.current_gui_work_dir()
        if not out_dir and hasattr(mw, "_out_path_edit"):
            out_dir = mw._out_path_edit.text().strip()
        start_dir = out_dir if out_dir and os.path.exists(out_dir) else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Channel Weights", start_dir, "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Load Weights", f"Invalid JSON file:\n{e}")
            return
        try:
            self.apply_full_config(cfg)
        except Exception as e:
            QMessageBox.warning(
                self, "Load Weights",
                f"Failed to apply weight configuration:\n{e}")

    # ── channel universe ──────────────────────────────────────────────
    def set_channels(self, channels):
        self.all_channels = list(channels or [])
        self._rebuild_rows()
