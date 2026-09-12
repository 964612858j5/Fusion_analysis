"""
block01/ui/step0/config_panel.py — Step1's ONE channel panel.

One flat channel list, and NO state of its own. Each row carries the four
things a channel has in Step1:

    click the row  -> it becomes the CURRENT channel (what Intensity edits)
    the checkbox   -> whether it is DRAWN
    the \u0192 box       -> whether it takes part in the FUSION
    the weight box -> its 0..1 scientific contribution

WHERE THE ANSWERS LIVE. Not here. Colour, the current channel and display
visibility belong to `ChannelDisplayState`; groups, per-group weights, the
nucleus, participation and weight provenance belong to `FusionDomainModel`.
Both are owned by `Block01DisplayServices`, so they answer before this page is
built and after it is gone. These rows are editors and projections.

TWO TICKS, TWO FACTS. There used to be one, and it meant both -- so hiding a
channel to look at another silently shrank the configuration a Save would
freeze, and a project could be changed by looking at it. Now:

  * the left box is DISPLAY. Hiding a channel changes the picture and nothing
    else: the configuration, the settings hash and the committed snapshot are
    the same afterwards, and a Search or Generate is not refused because of it;
  * the \u0192 box is SCIENCE. Unticking it takes the channel out of the effective
    configuration while keeping its group memberships and every per-group
    weight, so ticking it again brings that channel's own numbers back --
    an explicit 0.00 included, and an old project's 0.2/0.7 group for group;
  * the FIRST \u0192 tick of a channel nobody has ever weighted answers 1.0, once.
    It is an answer, not an edit: it does not unify a channel that sits in
    several groups at different weights;
  * clicking a row selects it and SHOWS it -- selecting something invisible is
    a dead end -- but it does not put it in the fusion;
  * moving a weight is the scientific edit: it applies to every group the
    channel belongs to, 0.00 included, and it never ticks anything.

`get_full_config` is the whole configuration, disabled channels included,
because that is what a session must round-trip. `effective_config` is the
enabled subset: what is fused and what a Save freezes.

Channel GROUPS are still carried, they are just not on screen. Group
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
from ...core.fusion_domain import ABSENT
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
    """One channel: select / show / fuse / weight.

    No hover text on the controls that are read at a glance -- the display
    tick box, the colour swatch, the 0..1 slider. A popup over every one of
    them in a list this long is in the way of the work rather than an
    explanation of it, and the rules they would recite live in this module's
    docstring, where they are read once.

    The hover text that is left is on the things a widget cannot show by
    itself: the read-only nucleus line, the warning on a row whose channel is
    in several groups at different weights, and the `\u0192` box -- the only
    control here whose meaning is not the obvious reading of its position,
    because it sits next to a tick box that means something else.
    """

    selected = pyqtSignal(str)
    visibility_toggled = pyqtSignal(str, bool)
    fusion_toggled = pyqtSignal(str, bool)
    weight_edited = pyqtSignal(str)
    color_clicked = pyqtSignal(str)

    def __init__(self, channel, weight=0.0, visible=False, color="#888888",
                 fusion=False, parent=None):
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

        # FUSION PARTICIPATION -- the scientific tick, and the one control
        # in this row that carries hover text, because it is the one whose
        # meaning is not the obvious reading of the box next to the name.
        # The left checkbox says whether you can SEE the channel; this says
        # whether it takes part in the fusion at all. They were one tick
        # until B3 and are two facts.
        self.fusion_box = QCheckBox("\u0192")
        self.fusion_box.setChecked(bool(fusion))
        self.fusion_box.setToolTip(
            "Take part in the FUSION. Unticking keeps this channel's groups "
            "and weights; it simply stops contributing. The box on the left "
            "only controls whether it is drawn.")
        self.fusion_box.setStyleSheet(template.CHECKBOX_INDICATOR_QSS)
        self.fusion_box.toggled.connect(self._on_fusion_toggled)
        template.add_accessory(lay, template.fit_accessory(self.fusion_box))

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

    # -- fusion participation -----------------------------------------------
    def is_fusion_enabled(self):
        return self.fusion_box.isChecked()

    def set_fusion_enabled(self, enabled):
        """Show the scientific answer. Silent: a row FOLLOWS the model."""
        if self.fusion_box.isChecked() == bool(enabled):
            return
        self.fusion_box.blockSignals(True)
        self.fusion_box.setChecked(bool(enabled))
        self.fusion_box.blockSignals(False)

    def _on_fusion_toggled(self, checked):
        self.fusion_toggled.emit(self.channel, bool(checked))

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
    """Step1's channel editor: a view over two owners, and neither is here.

    Display answers come from `ChannelDisplayState`, scientific answers from
    `FusionDomainModel`. This panel draws them and sends commands.
    """

    config_changed = pyqtSignal()                 # the configuration moved
    visibility_changed = pyqtSignal(str, bool)    # DISPLAY visibility only
    current_channel_changed = pyqtSignal(str)     # what Intensity should edit
    color_changed = pyqtSignal(str, str)          # overlay colour changed

    def __init__(self, all_channels, fusion=None):
        super().__init__()
        # INJECTED, never invented. A panel that made its own model when none
        # was passed would hide a missing wire: production would still be
        # correct while a page built without the services quietly edited a
        # model nobody else reads, and the failure would only show up as
        # weights that do not reach a Save.
        if fusion is None:
            raise ValueError(
                "ConfigPanel needs Block01's FusionDomainModel: pass "
                "`fusion=services.fusion` (a standalone caller may own a "
                "`FusionDomainModel()` of its own and pass that).")
        self.all_channels = list(all_channels or [])
        # THE scientific state, which this panel no longer owns. Groups,
        # per-group weights, the nucleus, who takes part and whose weight is
        # an answer all live in the model -- outside this widget, so Step0 and
        # Step3 can ask and answer without Step1 being built, and so tearing
        # this page down does not take the project's numbers with it.
        self._fusion = fusion
        self._fusion.weight_changed.connect(self._on_model_weight_changed)
        self._fusion.participation_changed.connect(
            self._on_model_participation_changed)
        self._fusion.draft_restored.connect(self._on_model_draft_restored)
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

    # ── the scientific model ──────────────────────────────────────────
    def fusion_model(self):
        """The one owner of this panel's scientific answers."""
        return self._fusion

    def _on_model_weight_changed(self, channel):
        """A weight moved in the model -- from this panel, the shared Weights
        window, Step0 or a restore. The row FOLLOWS it; it does not re-write
        it, so an edit cannot start a second lap."""
        self._sync_row_weight(channel, self._fusion.channel_weight(channel))
        self._refresh_ambiguity(channel)

    def _on_model_participation_changed(self, channel, enabled):
        row = self._rows.get(channel)
        if row is not None:
            row.set_fusion_enabled(bool(enabled))

    def _on_model_draft_restored(self):
        """A whole draft arrived at once: every row catches up, silently."""
        self._sync_rows_from_model()

    def _sync_rows_from_model(self):
        for ch, row in self._rows.items():
            row.set_weight(self._fusion.channel_weight(ch))
            row.set_fusion_enabled(self._fusion.fusion_enabled(ch))
            self._refresh_ambiguity(ch)
        self._refresh_nucleus_display()

    def _refresh_ambiguity(self, channel):
        """Name a channel whose groups disagree; one row cannot show two
        numbers, and quietly showing the largest without saying so is how an
        old project gets flattened by being looked at."""
        row = self._rows.get(channel)
        if row is None:
            return
        rep = self._fusion.representative_weight(channel)
        if rep.mixed:
            row.name_label.setText(f"{channel} *")
            row.setToolTip(
                f"{channel} is in several groups at different weights "
                f"({', '.join(str(v) for v in rep.values)}). The row shows the "
                "largest; editing it applies that weight to every group.")
        else:
            row.name_label.setText(str(channel))
            row.setToolTip("")

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
        """Make this panel a view over Block01's shared display state.

        Registered by the host that owns both. From here the panel reads the
        shared answer for colour, writes back to it, and repaints when it
        changes -- which is the whole of "one channel, one colour, in every
        step" -- and records the display answers for SELECTION and
        VISIBILITY.

        Recording, not delegating: in B2 this panel's checkbox still drives
        both display visibility and fusion participation, and its spinbox is
        still the weight. Splitting those is B3's, and nothing here reads the
        shared state back to decide behaviour.
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
            # The weight and the fusion tick come from the MODEL, not from
            # the row that is being replaced: a rebuild is the same channels
            # drawn again, and the row was only ever showing what the model
            # says.
            row = ChannelRow(ch,
                             weight=self._fusion.channel_weight(ch),
                             visible=visible.get(ch, False),
                             fusion=self._fusion.fusion_enabled(ch),
                             color=color)
            row.selected.connect(self._on_row_selected)
            row.visibility_toggled.connect(self._on_row_visibility)
            row.fusion_toggled.connect(self._on_row_fusion_toggled)
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

        for ch in self._rows:
            self._refresh_ambiguity(ch)
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
            # A click SHOWS a hidden channel, because selecting something you
            # cannot see is a dead end. It does not put the channel into the
            # fusion: that is the row's own scientific tick, and clicking a
            # name is not a scientific act.
            row.set_visible(True)
        changed = (channel != self._current)
        self._current = channel
        self._selecting = True
        try:
            self._list.setCurrentItem(self._items[channel])
        finally:
            self._selecting = False
        if newly_visible:
            self._record_display_visible(channel, True)
            self.visibility_changed.emit(channel, True)
        if changed:
            # The DISPLAY answer for "which channel is being edited", in the
            # one place every step reads it. A plain write: programmatic
            # restore reaches it too and still shows nothing, because
            # `auto_show` above is what a click means and it is already
            # decided by here.
            state = self._display_state
            if state is not None:
                state.set_selected_channel(channel, origin="step1-panel")
            self.current_channel_changed.emit(channel)

    def _on_row_visibility(self, channel, visible):
        """The user ticked or unticked the DISPLAY box.

        Display only, since B3. This tick used to do two jobs at once: it
        decided whether the channel was drawn AND whether it was part of the
        fusion, so hiding a channel silently shrank the configuration a Save
        would freeze. Whether a channel takes part is now the row's own
        `\u0192` box, which is a scientific command; this one is about the
        screen and nothing else.
        """
        self._record_display_visible(channel, visible)
        self.visibility_changed.emit(channel, bool(visible))

    def _on_row_fusion_toggled(self, channel, enabled):
        """The user ticked or unticked FUSION PARTICIPATION.

        A scientific command, straight to the model: it keeps the channel's
        groups and every per-group weight, and the first enable of a channel
        nobody has weighted answers 1.0 there -- once, and not as an edit.
        """
        self._fusion.set_fusion_enabled(channel, bool(enabled), origin="step1-row")

    def fusion_enabled(self, channel):
        return self._fusion.fusion_enabled(channel)

    def set_fusion_enabled(self, channel, enabled):
        """The same scientific act as clicking the row's `\u0192` box."""
        return self._fusion.set_fusion_enabled(channel, bool(enabled),
                                               origin="api")

    def fusion_channels(self):
        """The scientific input set, in the panel's channel order."""
        return [ch for ch in self.all_channels
                if self._fusion.fusion_enabled(ch)]

    def visible_channels(self):
        return [ch for ch in self.all_channels
                if ch in self._rows and self._rows[ch].is_visible()]

    def set_channel_visible(self, channel, visible):
        """Show or hide `channel` — the same act as clicking its box.

        Display only, so it has no scientific consequence at all: the
        configuration, the settings hash and the committed snapshot are the
        same afterwards. Use `set_fusion_enabled` for the scientific act.
        """
        row = self._rows.get(channel)
        if row is None or row.is_visible() == bool(visible):
            return
        row.set_visible(visible)
        self._record_display_visible(channel, visible)
        self.visibility_changed.emit(channel, bool(visible))

    def _record_display_visible(self, channel, visible):
        """Write the DISPLAY answer to its one owner, the shared state."""
        state = self._display_state
        if state is not None and channel:
            state.set_display_visible(channel, bool(visible),
                                      origin="step1-panel")

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
        """What the channel weighs scientifically. The ROW is not asked: it
        shows this number, it does not hold it."""
        return float(self._fusion.channel_weight(channel))

    def weight_provenance(self, channel):
        return self._fusion.weight_provenance(channel)

    def _sync_row_weight(self, channel, weight):
        """Put a number in a row, and say nothing about who chose it.

        The row catching up with the model is not an answer ABOUT the
        channel: it must not claim provenance and must not unify a channel
        that sits in several groups at different weights. Only
        `FusionDomainModel.edit_channel_weight` does either.
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
        self._fusion.edit_channel_weight(channel, weight, origin="api")

    def nucleus_channel(self):
        return self._fusion.nucleus()[0]

    def set_nucleus(self, channel, weight=None):
        """Adopt the nucleus Step0 handed over.  Not a user-editable choice."""
        self._fusion.set_nucleus(channel, weight)
        self._refresh_nucleus_display()

    def set_nucleus_weight(self, weight):
        self._fusion.set_nucleus_weight(weight)
        self._refresh_nucleus_display()

    def _refresh_nucleus_display(self):
        nuc, nuc_w = self._fusion.nucleus()
        self._nuc_value.setText(
            f"{nuc}  (weight {nuc_w:.2f})" if nuc else "\u2014")
        for ch, row in self._rows.items():
            is_nuc = bool(nuc) and ch == nuc
            row.set_weight_editable(not is_nuc)
            if is_nuc:
                row.set_weight(nuc_w)

    def _on_row_weight_edited(self, channel):
        """The user moved a weight: the scientific edit, straight to the model.

        An edited weight applies to EVERY group the channel belongs to -- the
        model's rule, stated there -- and it is an answer from then on, 0.00
        included. The ticks are not touched: how much a channel contributes is
        not whether it contributes.
        """
        # No `config_changed` from here: the MODEL announces the weight, and
        # the host follows that one signal. Announcing both drew the same
        # state twice -- once for the row and once for the number.
        self._fusion.edit_channel_weight(channel, self._rows[channel].weight(),
                                         origin="step1-row")

    def effective_config(self):
        """What is actually fused and what a Save freezes.

        The ENABLED subset, from the model. It used to be the VISIBLE subset,
        computed here from the row ticks, so hiding a channel quietly shrank
        the science; display visibility has no vote in it any more.
        """
        return self._fusion.effective_config()

    def ambiguous_channels(self):
        """Channels whose loaded weights disagree between groups."""
        return self._fusion.ambiguous_channels()

    def zero_marker_weights(self):
        """Every channel but the nucleus back to 0 — the fresh-project state.

        A real edit, not a repaint: the zero is what every group the channel
        belongs to reports afterwards, and it is an ANSWER, so a later first
        enable does not replace it with the default. The ticks are not
        touched: this resets weights, and a reset that also removed channels
        from the configuration would be doing something the button does not
        say.
        """
        nuc = self.nucleus_channel()
        spec = self._fusion.draft_snapshot()
        weights = dict(spec.get("channel_weight") or {})
        provenance = dict(spec.get("provenance") or {})
        group_weights = {name: dict(values) for name, values
                         in (spec.get("group_weights") or {}).items()}
        for ch in self._rows:
            if ch == nuc:
                continue
            weights[ch] = 0.0
            provenance[ch] = "explicit"
            for values in group_weights.values():
                if ch in values:
                    values[ch] = 0.0
        spec["channel_weight"] = weights
        spec["provenance"] = provenance
        spec["group_weights"] = group_weights
        # ONE transaction for the whole button: zeroing twenty markers is one
        # thing the user asked for, not twenty redraws and twenty saves.
        self._fusion.install_draft(spec)
        self._sync_rows_from_model()
        self.config_changed.emit()

    def _reset_all_channel_weights(self):
        self.zero_marker_weights()

    # ── groups: the model's, projected here ───────────────────────────
    def _group_of(self, channel):
        for name, members in self._fusion.groups().items():
            if channel in members:
                return name
        return None

    def _add_group(self, name, channel_weights=None):
        self._fusion.add_group(name, channel_weights)

    def _add_to_group(self, name, channel, weight=0.0):
        self._fusion.add_to_group(name, channel, weight)
        self._sync_row_weight(channel, self._fusion.channel_weight(channel))

    def _del_group(self, name):
        self._fusion.remove_group(name)

    def group_weight(self, name):
        return self._fusion.group_weight(name)

    def set_group_weight(self, name, weight):
        self._fusion.set_group_weight(name, weight)

    # ── the config contract (unchanged shape) ─────────────────────────
    def get_groups(self):
        return self._fusion.groups()

    def get_group_weights(self):
        return self._fusion.group_weights()

    def get_nucleus(self):
        return self._fusion.nucleus()

    def get_full_config(self):
        return self._fusion.full_config()

    def load_panel(self, groups, nuc_ch):
        """Load the panel for a freshly opened dataset.

        Defaults, deliberately: the nucleus is the only channel shown, the
        only one in the fusion and the only one with weight. Every marker
        starts absent -- not at zero-as-an-answer -- so its first enable
        answers 1.0.
        """
        # The colour MIRROR is emptied, not the answer. Re-dealing a palette
        # here is precisely what made the same channel two colours: Step0 had
        # already dealt one (and the user may already have changed it), and
        # this line then produced a second for the same channel the moment
        # Step1 opened. What follows `_rebuild_rows` takes them back from
        # the shared state, so Step1 ADOPTS Step0's colours rather than
        # inventing its own.
        self._colors = {}
        nuc = str(nuc_ch or "")
        spec_groups = {}
        for gname, channels in (groups or {}).items():
            spec_groups[str(gname)] = {
                "group_weight": 1.0,
                "channels": {str(ch): 0.0 for ch in channels
                             if str(ch) != nuc}}
        # ONE install: a new dataset inherits nothing from the slide before
        # it -- no provenance, no participation, no weights -- and nobody
        # observes it half-built.
        #
        # Except an answer that has nowhere to live yet: a weight named in
        # Step0, for THIS dataset, before Step1 built any group to hold it.
        # It is carried across and the groups below adopt it. A channel that
        # HAS been in a group is not pending -- its numbers belong to the
        # slide those groups describe, and they go with it.
        pending = self._fusion.pending_answers()
        provenance = {nuc: "authoritative"} if nuc else {}
        weights = {nuc: 1.0} if nuc else {}
        for ch, (value, prov) in pending.items():
            if ch == nuc or (self.all_channels and ch not in self.all_channels):
                continue
            provenance[ch] = prov
            weights[ch] = value
        self._fusion.install_draft({
            "groups": spec_groups,
            "nucleus": {"channel": nuc, "weight": 1.0 if nuc else 0.0},
            "enabled": [nuc] if nuc else [],
            "provenance": provenance,
            "channel_weight": weights,
        })
        # The groups were installed from the handoff's placeholder zeros; a
        # pending answer replaces its own channel's placeholder in every one
        # of them, the way the first group always adopts it.
        for ch, (value, prov) in pending.items():
            if ch in weights and ch != nuc:
                self._fusion.set_channel_answer(ch, value, prov)

        self._rebuild_rows()
        # The rows were just built; give them the canonical colours before
        # anything is shown. Without this line Step1 opens on its own palette
        # and the same channel is two colours -- which is the failure this
        # round closed, and the mutation the tests check for.
        self._resync_colors_from_state()

        for ch, row in self._rows.items():
            row.set_visible(False)
        self._refresh_nucleus_display()
        if nuc and nuc in self._rows:
            self._rows[nuc].set_visible(True)
            self._current = nuc
            self._selecting = True
            try:
                self._list.setCurrentItem(self._items[nuc])
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

        Weights that arrive in a file are ANSWERS, not absences: an explicit 0
        in a project's fusion config is a decision somebody made, and a later
        first enable must not overwrite it with 1.0.
        """
        cfg = dict(cfg or {})
        nucleus_cfg = cfg.get("nucleus") or {}
        groups_cfg = cfg.get("groups") or {}

        nuc_ch, nuc_w = self._fusion.nucleus()
        if adopt_nucleus or not nuc_ch:
            nuc_ch = str(nucleus_cfg.get("channel") or "")
            nuc_w = float(nucleus_cfg.get("weight", 0.0) or 0.0)

        spec_groups = {}
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
            spec_groups[str(gname)] = {
                "group_weight": float((gdata or {}).get("group_weight", 1.0)),
                "channels": channels}
        if dropped:
            print("[Step1] nucleus channel removed from marker groups "
                  f"(it would contribute twice): {sorted(dropped)}")

        provenance = {ch: "authoritative" for ch in loaded}
        if nuc_ch:
            provenance[nuc_ch] = "authoritative"
        # A config says what the science IS; it does not say what is on
        # screen. Participation comes with it (these are the channels the
        # configuration contains); visibility is restored separately.
        enabled = set(loaded) | ({nuc_ch} if nuc_ch else set())
        self._fusion.install_draft({
            "groups": spec_groups,
            "nucleus": {"channel": nuc_ch, "weight": nuc_w},
            "enabled": sorted(enabled),
            "provenance": provenance,
        })
        self._sync_rows_from_model()
        ambiguous = self.ambiguous_channels()
        if ambiguous:
            print(f"[Step1] channels in several groups at different weights: "
                  f"{sorted(ambiguous)}")
        self.config_changed.emit()

    def install_fusion_draft(self, spec, visibility=None):
        """Install a whole migrated draft, and the display answers with it.

        The one entry a session restore uses. The scientific fields go in as
        one transaction with one notice, and the ticks follow -- so no
        observer ever sees a restored group against the participation set of
        the project before it.
        """
        self._fusion.install_draft(spec or {})
        self._sync_rows_from_model()
        if visibility is not None:
            for ch, on in visibility.items():
                row = self._rows.get(str(ch))
                if row is not None:
                    row.set_visible(bool(on))
        self.config_changed.emit()

    def weight_initialized_channels(self):
        """The channels whose weight is an ANSWER rather than an absence.

        Kept for the sessions that carry the old marker. What it means now is
        "provenance is not absent"; the new schema writes the provenance
        itself, which says WHICH kind of answer it is.
        """
        nuc = self.nucleus_channel()
        return sorted(ch for ch in self._rows
                      if ch != nuc
                      and self._fusion.weight_provenance(ch) != ABSENT)

    def restore_weight_initialization(self, channels):
        """Put back exactly which weights are answers. Not a click."""
        if isinstance(channels, dict):
            channels = [ch for ch, flag in channels.items() if flag]
        known = set(self._rows) or set(self.all_channels or [])
        marked = {str(ch) for ch in (channels or []) if str(ch) in known}
        nuc = self.nucleus_channel()
        spec = self._fusion.draft_snapshot()
        provenance = dict(spec.get("provenance") or {})
        for ch in known:
            if ch == nuc:
                continue
            provenance[ch] = "authoritative" if ch in marked else ABSENT
        spec["provenance"] = provenance
        self._fusion.install_draft(spec)
        self._sync_rows_from_model()

    def restore_display_state(self, colors=None, visibility=None,
                              current_channel=""):
        """Put back a saved display state in one go, with no side effects.

        Restoring is not clicking: a channel the user left hidden must come
        back hidden even when it is the current one, and the host should redraw
        once at the end rather than once per channel.
        """
        # ONE PUBLIC TRANSACTION for every DISPLAY field this restore
        # resolved -- colours, visibility and the current channel -- so the
        # shared state comes back whole and every view (Step0's swatches, the
        # Intensity histogram, both main viewers, the Tissue Preview) follows
        # one completion notice. Registering only the colours left selection
        # and visibility in this panel's widgets, where a public getter would
        # have had to fall back to a QWidget to find them.
        #
        # DISPLAY ONLY. Fusion participation, groups, weights and their
        # provenance are not interpreted here; they stay this panel's until
        # B3 migrates the session schema.
        state = self._display_state
        if state is not None:
            payload = {}
            if colors:
                payload["colors"] = {str(ch): str(c)
                                     for ch, c in colors.items() if ch and c}
            if visibility:
                known = set(self.all_channels or []) or set(self._rows)
                merged = dict(state.display_visibility())
                merged.update({str(ch): bool(v)
                               for ch, v in visibility.items()
                               if str(ch) in known})
                payload["visibility"] = merged
            if current_channel and current_channel in self._rows:
                payload["selection"] = str(current_channel)
            if payload:
                state.install(payload)
        for ch, color in (colors or {}).items():
            ch = str(ch)
            if not color:
                continue
            self._colors[ch] = str(color)
            row = self._rows.get(ch)
            if row is not None:
                row.set_color(str(color))

        # The widgets follow the SAME resolved answer, silently. They are a
        # view of it, not a second place to look it up.
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
        """The channel universe changed -- another dataset, or a re-read.

        A channel that is gone takes its scientific history with it: coming
        back later it is new again, and its first enable answers 1.0 rather
        than inheriting a weight from a slide that no longer has it.
        """
        self.all_channels = list(channels or [])
        self._fusion.forget_channels_outside(self.all_channels)
        self._rebuild_rows()
