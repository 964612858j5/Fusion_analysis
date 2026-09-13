"""THE one public channel dock: Step0, Step1, Step2 and Step3 edit here.

WHY THIS MODULE EXISTS. Block01 had a public channel list per step -- Step0's
`ChannelDock` in the Background Correction tab, Step1's private
`ConfigPanel` list, Step3's hand-built overlay rows -- each with its own
widgets and its own idea of what a tick means. Walking Step0 -> Step1 -> Step0
destroyed and rebuilt them, so a search string, a scroll position and the row
the user was working on were gone every time, and the same channel could be
shown in one list and hidden in another.

There is now ONE dock, built once by `Block01DisplayServices` and mounted by
the main window OUTSIDE the stacked pages. A step change switches the
ACCESSORY -- Step0's correction combo, Step1's `f` participation box -- and
nothing else: the dock, the rows, the selection, the search text and the
scroll position are the same objects before and after.

WHAT IT OWNS: nothing. The dock is a projection of the two owners:

    ChannelDisplayState   order / capabilities / selection / visibility /
                          colour / mapping
    FusionDomainModel     participation / representative weight / provenance

Every getter asks an owner and every control calls an owner command. There is
no third copy of a public field here, which is the whole point: the second
copy is what let two lists disagree.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, QObject, pyqtSignal

from . import template

#: The steps whose public channel editing happens in this dock.
STEP0, STEP1, STEP2, STEP3 = 0, 1, 2, 3

#: The final background-correction answers, in the order Step0 stores them.
#: `both` is deliberately absent: it names computing two candidates to
#: compare, and Save can only write one of these three.
CORRECTION_METHODS = ["Original", "TopHat", "cucim"]

#: The compute-state glyph beside the checkbox (Step0's vocabulary).
STATE_GLYPHS = {
    "": ("", "", ""),
    "not-computed": ("○", "color:#6d8196;font-size:11px;",
                     "not computed — edit the TopHat radius or the cuCIM "
                     "sigma and press Enter to compute it"),
    "computed": ("✓", "color:#56d990;font-size:11px;font-weight:bold;",
                 "computed — the cached result matches the current method, "
                 "parameters and patches"),
    "stale": ("!", "color:#f4c45e;font-size:12px;font-weight:bold;",
              "stale — the method or parameters changed since this channel "
              "was computed; press Enter in that parameter to recompute"),
    "computing": ("⟳", "color:#f4c45e;font-size:12px;", "computing…"),
    "done": ("✓", "color:#56d990;font-size:12px;", "computed"),
    "unsaved": ("●", "color:#f4c45e;font-size:11px;", "unsaved"),
    "nucleus": ("★", "color:#56b6c2;font-size:11px;",
                "reference channel — never background-corrected"),
}


def _method_index(method):
    """Where a FINAL decision sits in `CORRECTION_METHODS`, or -1."""
    lut = {name.lower(): i for i, name in enumerate(CORRECTION_METHODS)}
    return lut.get(str(method or "").strip().lower(), -1)


class GlobalChannelRow(QtWidgets.QWidget):
    """One channel, in every step: the template core plus fixed accessories.

    THE CORE never changes with the step:

        display checkbox | state slot | swatch | name | weight editor

    THE ACCESSORIES are built once, all of them, and shown or hidden by
    `set_step`. Building them per step would mean a row rebuild on every
    transition, which is exactly the flicker-and-forget behaviour the one
    dock exists to remove.

    The row emits user INTENT and never writes an answer: the dock turns an
    intent into an owner command, so a widget cannot become a second owner
    of a public field.
    """

    visibility_toggled = pyqtSignal(str, bool)
    weight_edited = pyqtSignal(str, float)
    fusion_toggled = pyqtSignal(str, bool)
    method_changed = pyqtSignal(str, str)
    color_clicked = pyqtSignal(str)
    #: A REAL mouse click, told apart from a programmatic selection: a
    #: restore and a dataset switch move the selection too, and "show me this
    #: one" is a thing only a click means.
    row_clicked = pyqtSignal(str)

    def __init__(self, cid, name="", parent=None):
        super().__init__(parent)
        self._cid = str(cid)
        self._busy = False
        self._color = "#888888"

        core = template.build_row_core(self, name=(name or cid),
                                       tooltips=False)
        self._lay = core.layout
        self.checkbox = core.checkbox
        self.state_slot = core.state_slot
        #: Step0's historical name for the state slot, so the page code and
        #: the tests that write through it keep working.
        self.state_lbl = core.state_slot
        self.swatch = core.swatch
        self.name_label = core.name_label
        # NO HOVER TEXT ON THE CORE CONTROLS. The tick box, the swatch, the
        # name and the weight are read at a glance, and a popup over every one
        # of them in a list this long is in the way of the work; the rules
        # they follow are in this module's docstring, read once. The
        # accessories that DO carry hover text are the two whose meaning is
        # not the obvious reading of their position: the `f` participation box
        # and Step0's correction combo and state glyph.
        self.checkbox.toggled.connect(self._on_visibility_toggled)

        # -- the core's weight editor (representative weight) ---------------
        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setFixedHeight(16)
        self.slider.setMinimumWidth(60)
        template.add_accessory(self._lay, self.slider, stretch=2)

        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setFixedWidth(56)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setStyleSheet(template.ACCESSORY_SPINBOX_QSS)
        template.add_accessory(self._lay, template.fit_accessory(self.spin))
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)

        # -- Step1's accessory: fusion participation ------------------------
        self.fusion_box = QtWidgets.QCheckBox("ƒ")
        self.fusion_box.setToolTip(
            "Take part in the FUSION. Unticking keeps this channel's groups "
            "and weights; it simply stops contributing. The box on the left "
            "only controls whether it is drawn.")
        self.fusion_box.setStyleSheet(template.CHECKBOX_INDICATOR_QSS)
        self.fusion_box.toggled.connect(self._on_fusion_toggled)
        self._acc_step1 = self._accessory([self.fusion_box])


        # -- Step0's accessory: the correction decision and its state -------
        self.method_cb = QtWidgets.QComboBox()
        self.method_cb.addItems(CORRECTION_METHODS)
        self.method_cb.setFixedWidth(64)
        self.method_cb.setStyleSheet(template.ACCESSORY_COMBO_QSS)
        self.method_cb.setToolTip(
            "The FINAL background-correction answer for this channel — what "
            "Save writes. Correction only: it shows and hides nothing.")
        self.method_cb.currentTextChanged.connect(self._on_method_text)
        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setFixedWidth(20)
        self.status_lbl.setStyleSheet(f"color:{template.COLOR_MUTED};"
                                      "font-size:12px;")
        self._acc_step0 = self._accessory(
            [template.fit_accessory(self.method_cb), self.status_lbl])

        self.set_step(None)
        template.apply_swatch_color(self.swatch, self._color)

    # -- construction helpers ----------------------------------------------
    def _accessory(self, widgets):
        """One step's accessory: the widgets themselves, in the ROW's layout.

        Not a nested container. The accessory has to start exactly where the
        template's name column ends -- that is the geometry B1 pinned and the
        thing two channel lists disagreed about -- and a wrapper widget puts
        its children at its own origin instead.
        """
        for w in widgets:
            template.add_accessory(self._lay, w)
        return tuple(widgets)

    # -- identity -----------------------------------------------------------
    @property
    def channel_id(self):
        return self._cid

    def accessory(self, step):
        """The widgets this step's accessory is made of."""
        return {STEP0: self._acc_step0, STEP1: self._acc_step1}.get(step, ())

    def set_step(self, step):
        """Show this step's accessory. Silent: switching what is on screen
        is not a user command, so nothing is written and nothing is emitted.

        Step2 and Step3 have no accessory here: they consume the public
        display and weight answers, and their own controls (segmentation,
        result, opacity, Auto) stay on their pages. An empty accessory is the
        honest answer -- a second panel invented so every step has one would
        be a control with nothing behind it.
        """
        for w in self._acc_step0:
            w.setVisible(step == STEP0)
        for w in self._acc_step1:
            w.setVisible(step == STEP1)

    # -- capabilities -------------------------------------------------------
    def apply_capabilities(self, caps):
        """WHAT MAY BE DONE to this channel, as separate facts.

        Never derived from one another and never from `locked`: a channel
        that may not be background-corrected (the nucleus) is still shown and
        hidden like any other.
        """
        self.checkbox.setEnabled(bool(getattr(caps, "display_toggleable",
                                              True)))
        editable = bool(getattr(caps, "weight_editable", True))
        self.slider.setEnabled(editable)
        self.spin.setReadOnly(not editable)
        self.spin.setButtonSymbols(
            QtWidgets.QDoubleSpinBox.UpDownArrows if editable
            else QtWidgets.QDoubleSpinBox.NoButtons)
        self.method_cb.setEnabled(bool(getattr(caps, "correction_eligible",
                                               True)))
        self.fusion_box.setEnabled(bool(getattr(caps, "fusion_toggleable",
                                                True)))

    def set_weight_editable(self, editable):
        """Legacy name kept for the Step1 callers: a read-only row still
        shows its weight, it just cannot be moved."""
        self.slider.setEnabled(bool(editable))
        self.spin.setReadOnly(not editable)
        self.spin.setButtonSymbols(
            QtWidgets.QDoubleSpinBox.UpDownArrows if editable
            else QtWidgets.QDoubleSpinBox.NoButtons)

    # -- owner -> widget (all silent) ---------------------------------------
    def set_visible_state(self, visible):
        if self.checkbox.isChecked() == bool(visible):
            return
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(bool(visible))
        self.checkbox.blockSignals(False)

    #: The Step1 panel's name for the same silent write.
    set_visible = set_visible_state

    def is_visible(self):
        return self.checkbox.isChecked()

    def set_color(self, color):
        self._color = str(color or "#888888")
        template.apply_swatch_color(self.swatch, self._color)

    def color(self):
        return self._color

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

    def is_fusion_enabled(self):
        return self.fusion_box.isChecked()

    def set_fusion_enabled(self, enabled):
        if self.fusion_box.isChecked() == bool(enabled):
            return
        self.fusion_box.blockSignals(True)
        self.fusion_box.setChecked(bool(enabled))
        self.fusion_box.blockSignals(False)

    def set_method(self, method):
        """Show the FINAL correction answer. A `both` -- a computation, not
        an answer -- leaves the combo where it is."""
        idx = _method_index(method)
        if idx < 0 or idx == self.method_cb.currentIndex():
            return
        self.method_cb.blockSignals(True)
        self.method_cb.setCurrentIndex(idx)
        self.method_cb.blockSignals(False)

    def method(self):
        return self.method_cb.currentText()

    def set_state(self, state):
        """The compute-state glyph beside the checkbox. An unknown state
        shows nothing rather than a placeholder."""
        glyph, style, tip = STATE_GLYPHS.get(str(state or ""), ("", "", ""))
        self.state_slot.setText(glyph)
        self.state_slot.setStyleSheet(style or template.STATE_SLOT_QSS)
        self.state_slot.setToolTip(tip)

    def set_name(self, name, mixed_values=()):
        """The name, plus the one thing a name has to carry: that this
        channel's groups disagree. Showing the largest silently is how an old
        project gets flattened by being looked at."""
        if mixed_values:
            self.name_label.setText(f"{name} *")
            self.setToolTip(
                f"{name} is in several groups at different weights "
                f"({', '.join(str(v) for v in mixed_values)}). The row shows "
                "the largest; editing it applies that weight to every group.")
        else:
            self.name_label.setText(str(name))
            self.setToolTip("")

    # -- widget -> intent ---------------------------------------------------
    def _on_visibility_toggled(self, checked):
        self.visibility_toggled.emit(self._cid, bool(checked))

    def _on_slider(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.spin.setValue(v / 100.0)
        finally:
            self._busy = False
        self.weight_edited.emit(self._cid, v / 100.0)

    def _on_spin(self, v):
        if self._busy:
            return
        self._busy = True
        try:
            self.slider.setValue(int(round(float(v) * 100)))
        finally:
            self._busy = False
        self.weight_edited.emit(self._cid, float(v))

    def _on_fusion_toggled(self, checked):
        self.fusion_toggled.emit(self._cid, bool(checked))

    def _on_method_text(self, text):
        self.method_changed.emit(self._cid, str(text))

    def mousePressEvent(self, ev):
        self.row_clicked.emit(self._cid)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.swatch.geometry().contains(ev.pos()):
            self.color_clicked.emit(self._cid)
        super().mouseReleaseEvent(ev)


class GlobalChannelDock(QtWidgets.QWidget):
    """The one public channel editor, projecting the two owners.

    Built once per window, outside the stacked pages. `set_step` switches the
    accessory; it does not rebuild anything. Rows are rebuilt only when the
    dataset's channel UNIVERSE changes -- a different set or order of
    channels -- because that is a different list, not the same list again.
    """

    #: A swatch was clicked. The host opens the colour dialog: which dialog,
    #: and what a nucleus colour means, is a page's business.
    color_edit_requested = pyqtSignal(str)
    #: A real click on a row (after the dock has moved the selection).
    row_clicked = pyqtSignal(str)
    #: Step0's correction decision moved in a row. The Step0 controller is
    #: the only thing that may act on it.
    correction_method_changed = pyqtSignal(str, str)

    def __init__(self, state, fusion, parent=None, title="Channels"):
        super().__init__(parent)
        self.setObjectName("ChannelDockRoot")
        self.setStyleSheet(template.DOCK_QSS)
        self._state = state
        self._fusion = fusion
        self._rows = {}
        self._items = {}
        self._order = []
        self._filter_text = ""
        self._step = STEP0
        self._state_provider = None
        self._method_provider = None
        self._name_provider = None
        self._syncing = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        if title:
            hdr = QtWidgets.QLabel(title)
            hdr.setStyleSheet("color:#9bd0ff;font-size:11px;font-weight:bold;")
            lay.addWidget(hdr)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search channels…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        lay.addWidget(self.search)

        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(4)
        self.btn_show_all = QtWidgets.QPushButton("Show all")
        self.btn_hide_all = QtWidgets.QPushButton("Hide all")
        for b in (self.btn_show_all, self.btn_hide_all):
            b.setObjectName("dockTool")
            tools.addWidget(b)
        tools.addStretch(1)
        self.header_extra = QtWidgets.QHBoxLayout()
        self.header_extra.setSpacing(4)
        tools.addLayout(self.header_extra)
        self.btn_show_all.clicked.connect(lambda: self.set_all_visible(True))
        self.btn_hide_all.clicked.connect(lambda: self.set_all_visible(False))
        lay.addLayout(tools)

        self.list_widget = QtWidgets.QListWidget()
        template.apply_list_style(self.list_widget)
        self.list_widget.setSelectionMode(QtWidgets.QListWidget.SingleSelection)
        self.list_widget.currentItemChanged.connect(self._on_current_item)
        lay.addWidget(self.list_widget, stretch=1)

        self.tool_area = QtWidgets.QVBoxLayout()
        self.tool_area.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.tool_area)
        self._tool_widget = None

        if state is not None:
            state.visibility_changed.connect(self._on_state_visibility)
            state.color_changed.connect(self._on_state_color)
            state.selection_changed.connect(self._on_state_selection)
            state.state_installed.connect(self._on_state_installed)
        if fusion is not None:
            fusion.weight_changed.connect(self._on_fusion_weight)
            fusion.participation_changed.connect(self._on_fusion_participation)
            fusion.draft_restored.connect(self.refresh)

    # ── the owners ────────────────────────────────────────────────────
    def display_state(self):
        return self._state

    def fusion_model(self):
        return self._fusion

    def step(self):
        return self._step

    def set_step(self, step):
        """Switch the accessory. NOT a rebuild and NOT a command.

        The dock, the rows, the selection, the search text and the scroll
        position are the same objects afterwards; only which accessory is on
        screen changed.
        """
        step = int(step)
        self._step = step
        for row in self._rows.values():
            row.set_step(step)

    # ── providers a host page answers (never a stored copy) ───────────
    def set_state_provider(self, fn):
        """`fn(channel) -> compute-state key`, asked of Step0's signature
        bookkeeping. The dock stores no compute state of its own."""
        self._state_provider = fn

    def set_method_provider(self, fn):
        """`fn(channel) -> the FINAL correction answer`, asked of Step0's
        `_channel_decisions`, which is its one authority."""
        self._method_provider = fn

    def set_name_provider(self, fn):
        """`fn(channel) -> the name to draw` (Step0 marks the nucleus)."""
        self._name_provider = fn

    # ── structure ──────────────────────────────────────────────────────
    def set_channels(self, order=None, force=False):
        """Make the list show `order`.

        SAME UNIVERSE, SAME ROWS: a call whose order matches what is on
        screen refreshes the values and returns, so a step change, a repaint
        or a re-entry keeps every row object, the selection, the search text
        and the scroll position.
        """
        if order is None:
            order = list(self._state.channel_order()) if self._state else []
        order = [str(c) for c in order]
        if not force and order == self._order:
            self.refresh()
            return False
        self._rebuild(order)
        return True

    def channel_order(self):
        return list(self._order)

    def _rebuild(self, order):
        keep_scroll = self.list_widget.verticalScrollBar().value()
        keep_focus = self.list_widget.hasFocus()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._rows.clear()
        self._items.clear()
        self._order = list(order)
        for cid in self._order:
            row = GlobalChannelRow(cid, name=self._name_of(cid))
            row.set_step(self._step)
            row.visibility_toggled.connect(self._on_row_visibility)
            row.weight_edited.connect(self._on_row_weight)
            row.fusion_toggled.connect(self._on_row_fusion)
            row.method_changed.connect(self._on_row_method)
            row.color_clicked.connect(self.color_edit_requested.emit)
            row.row_clicked.connect(self._on_row_clicked)
            item = QtWidgets.QListWidgetItem(self.list_widget)
            item.setSizeHint(template.item_size_hint(row, width=200))
            self.list_widget.setItemWidget(item, row)
            self._rows[cid] = row
            self._items[cid] = item
        template.uniform_name_width(list(self._rows.values()))
        self.list_widget.blockSignals(False)
        self.refresh()
        self._apply_filter()
        self.list_widget.verticalScrollBar().setValue(keep_scroll)
        if keep_focus:
            self.list_widget.setFocus(Qt.OtherFocusReason)

    def row(self, cid):
        return self._rows.get(cid)

    def rows(self):
        return dict(self._rows)

    def item(self, cid):
        return self._items.get(cid)

    def set_tool_widget(self, w):
        if self._tool_widget is not None:
            self.tool_area.removeWidget(self._tool_widget)
            self._tool_widget.setParent(None)
        self._tool_widget = w
        if w is not None:
            self.tool_area.addWidget(w)

    def tool_widget(self):
        return self._tool_widget

    # ── owner -> dock ─────────────────────────────────────────────────
    def refresh(self):
        """Draw every row from the owners. One pass, no commands."""
        for cid in list(self._rows):
            self.refresh_row(cid)
        self._refresh_selection()

    def refresh_row(self, cid):
        row = self._rows.get(cid)
        if row is None:
            return
        state, fusion = self._state, self._fusion
        if state is not None:
            row.apply_capabilities(state.capabilities(cid))
            row.set_visible_state(state.display_visible(cid))
            row.set_color(state.color(cid) or "#888888")
        rep = None
        if fusion is not None:
            rep = fusion.representative_weight(cid)
            row.set_weight(rep.value)
            row.set_fusion_enabled(fusion.fusion_enabled(cid))
            # THE NUCLEUS'S WEIGHT COMES FROM THE STEP0 HANDOFF and is read
            # only wherever it is shown. Which channel that is belongs to the
            # scientific owner, so it is asked here rather than derived from
            # a display capability that a page may never have installed.
            if fusion.nucleus()[0] == cid:
                row.set_weight_editable(False)
        row.set_name(self._name_of(cid),
                     mixed_values=(rep.values if (rep and rep.mixed) else ()))
        if self._state_provider is not None:
            row.set_state(self._state_provider(cid))
        if self._method_provider is not None:
            row.set_method(self._method_provider(cid))

    def _name_of(self, cid):
        if self._name_provider is not None:
            try:
                return self._name_provider(cid)
            except Exception:
                pass
        return cid

    def _on_state_visibility(self, cid, visible):
        row = self._rows.get(cid)
        if row is not None:
            row.set_visible_state(visible)

    def _on_state_color(self, cid, color):
        row = self._rows.get(cid)
        if row is not None:
            row.set_color(color)

    def _on_state_selection(self, cid):
        self._refresh_selection()

    def _on_state_installed(self, _binding):
        """An owner installed a whole dataset at once: catch up in ONE pass.

        The install has already decided; a dock that answered it control by
        control would be producing user commands out of a restore.
        """
        order = list(self._state.channel_order()) if self._state else []
        if order and order != self._order:
            self._rebuild(order)
        else:
            self.refresh()

    def _on_fusion_weight(self, cid):
        row = self._rows.get(cid)
        if row is None or self._fusion is None:
            return
        rep = self._fusion.representative_weight(cid)
        row.set_weight(rep.value)
        row.set_name(self._name_of(cid),
                     mixed_values=(rep.values if rep.mixed else ()))

    def _on_fusion_participation(self, cid, enabled):
        row = self._rows.get(cid)
        if row is not None:
            row.set_fusion_enabled(bool(enabled))

    def _refresh_selection(self):
        if self._state is None:
            return
        cid = self._state.selected_channel()
        item = self._items.get(cid)
        if item is None or self.list_widget.currentItem() is item:
            return
        self.list_widget.blockSignals(True)
        self.list_widget.setCurrentItem(item)
        self.list_widget.blockSignals(False)

    # ── dock -> owner (the only writes) ───────────────────────────────
    def _on_row_visibility(self, cid, visible):
        """The display tick, in every step. Display and nothing else: no
        correction decision, no fusion participation, no weight."""
        if self._state is not None:
            self._state.set_display_visible(cid, bool(visible),
                                            origin=f"dock-step{self._step}")

    def _on_row_weight(self, cid, value):
        """The scientific edit, in every step: it applies to every group the
        channel belongs to, 0.00 included, and it ticks nothing."""
        if self._fusion is not None:
            self._fusion.edit_channel_weight(cid, value,
                                             origin=f"dock-step{self._step}")

    def _on_row_fusion(self, cid, enabled):
        if self._fusion is not None:
            self._fusion.set_fusion_enabled(cid, bool(enabled),
                                            origin=f"dock-step{self._step}")

    def _on_row_method(self, cid, text):
        """Step0's correction decision. Only Step0 may act on it, and only
        while Step0 is the step on screen -- a hidden accessory cannot be
        clicked, and a programmatic sync is silent."""
        self.correction_method_changed.emit(cid, text)

    def _on_row_clicked(self, cid):
        """A real click: select, and show a marker the user clicked on.

        Selecting something invisible is a dead end. A programmatic
        selection -- a restore, a dataset switch, the landing rule -- reaches
        `set_selected_channel` directly and shows nothing.
        """
        if self._state is not None:
            caps = self._state.capabilities(cid)
            if (not self._state.display_visible(cid)
                    and getattr(caps, "display_toggleable", True)):
                self._state.set_display_visible(cid, True,
                                                origin="dock-row-click")
            self._state.set_selected_channel(cid, origin="dock-row-click")
        self.row_clicked.emit(cid)

    def set_all_visible(self, visible):
        """Show all / Hide all: a SWEEP, which is its own permission. A
        channel that is shown and hidden deliberately (the nucleus layer) is
        left alone by it."""
        if self._state is None:
            return
        for cid in self._order:
            caps = self._state.capabilities(cid)
            if getattr(caps, "bulk_toggleable", True):
                self._state.set_display_visible(cid, bool(visible),
                                                origin="dock-bulk")

    def _on_current_item(self, current, _prev):
        if current is None or self._state is None:
            return
        for cid, item in self._items.items():
            if item is current:
                self._state.set_selected_channel(cid, origin="dock-list")
                return

    # ── filtering ─────────────────────────────────────────────────────
    def filter_rows(self, text):
        self._filter_text = (text or "").strip().lower()
        self._apply_filter()

    def _apply_filter(self):
        for cid, item in self._items.items():
            name = self._name_of(cid).lower()
            item.setHidden(bool(self._filter_text)
                           and self._filter_text not in name)

    def visible_row_ids(self):
        return [cid for cid, it in self._items.items() if not it.isHidden()]
