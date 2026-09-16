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
ACCESSORY -- Step0's correction combo, Step1's weight editor -- and nothing
else: the dock, the rows, the selection, the search text and the scroll
position are the same objects before and after.

STEP1'S ROW IS ONE COMMAND WITH TWO ENTRIES. The tick shows a channel and puts
it into the fusion; so does giving it a weight (user ruling, 2026-09-16).
There is no separate participation control: one was added once and rolled
back, and `UI_SURFACE_RULES.md` records why.

WHAT IT OWNS: nothing. The dock is a projection of the two owners:

    ChannelDisplayState   order / capabilities / selection / visibility /
                          colour / mapping
    FusionDomainModel     participation / representative weight / provenance

Every getter asks an owner and every control calls an owner command. There is
no third copy of a public field here, which is the whole point: the second
copy is what let two lists disagree.
"""

import weakref

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QEvent, QObject, pyqtSignal

from . import template

#: The steps whose public channel editing happens in this dock.
STEP0, STEP1, STEP2, STEP3 = 0, 1, 2, 3

#: What this row's combo offers: the channel's PREVIEW / automatic-compute
#: method -- what Step0 computes or prepares to LOOK at. `Both` belongs here
#: and means "prepare the TopHat and the cuCIM candidate so they can be
#: compared"; `Original` means "show the raw channel" and starts no run.
#:
#: This is NOT the final correction decision. That question -- what Save
#: publishes -- has three answers (original / tophat / cucim, never `both`)
#: and one user entry, Step0's `Per-Channel Decision` panel with its Apply.
#: The two never derive from one another.
PREVIEW_METHODS = ["Both", "Original", "TopHat", "cucim"]

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
    """Where a PREVIEW method sits in `PREVIEW_METHODS`, or -1."""
    lut = {name.lower(): i for i, name in enumerate(PREVIEW_METHODS)}
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
        #: The step whose fields are on screen, and the permissions the
        #: owners gave this channel. A control is operable only when BOTH
        #: agree: the step shows it and the channel allows it.
        self._step = None
        self._caps = None
        self._weight_editable = True

        core = template.build_row_core(self, name=(name or cid),
                                       tooltips=False)
        self._lay = core.layout
        self.checkbox = core.checkbox
        self.state_slot = core.state_slot
        #: Step0's historical name for the state slot, so the page code and
        #: the tests that write through it keep working.
        self.state_lbl = core.state_slot
        self.swatch = core.swatch
        # THE SWATCH EATS ITS OWN CLICKS. It is a control, not a piece of the
        # row: letting the press through reached the row (a selection) and
        # the LIST under it (a current-item change), so picking a colour for
        # a hidden channel also selected it and, by the click rule, showed
        # it. Consuming press and release here is what makes "the swatch only
        # changes a colour" true of a real mouse rather than of a signal.
        self.swatch.installEventFilter(self)
        self.name_label = core.name_label
        # NO HOVER TEXT ON THE CORE CONTROLS. The tick box, the swatch, the
        # name and the weight are read at a glance, and a popup over every one
        # of them in a list this long is in the way of the work; the rules
        # they follow are in this module's docstring, read once. The
        # accessories that DO carry hover text are the two whose meaning is
        # not the obvious reading of its position: Step0's correction combo
        # and its state glyph.
        self.checkbox.toggled.connect(self._on_visibility_toggled)

        # -- Step1's accessory: the representative weight editor -----------
        #
        # NOT core. The product ruling for B4-B is that a step shows the
        # fields it works with: Step0 shows the correction answer, Step1 the
        # scientific weight and participation, Step2 and Step3 the display
        # answers alone. The editor is built once and hidden in the steps
        # that do not edit weights -- hidden AND disabled AND unfocusable, so
        # a stale or programmatic signal cannot reach the fusion model
        # through a control the user cannot see.
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

        # NO SEPARATE PARTICIPATION CONTROL. Step1's tick box IS the
        # scientific act: it shows the channel and puts it into the fusion,
        # and there is no second control for the second half. The `f` box
        # added here was never asked for -- making the global dock reusable
        # licenses reusing the component, not inventing product surface --
        # and it split one gesture into two, so a user who ticked a channel
        # got a visible channel that the fusion silently ignored.
        self._acc_step1 = (self.slider, self.spin)

        # -- Step0's accessory: the PREVIEW method and the compute state ----
        self.method_cb = QtWidgets.QComboBox()
        self.method_cb.addItems(PREVIEW_METHODS)
        self.method_cb.setFixedWidth(64)
        self.method_cb.setStyleSheet(template.ACCESSORY_COMBO_QSS)
        self.method_cb.setToolTip(
            "What this channel is PREVIEWED / computed with: Both prepares "
            "the TopHat and the cuCIM candidate, Original shows the raw "
            "channel and starts no run. It does not decide what Save "
            "publishes — that is the Per-Channel Decision panel's Apply — "
            "and it shows and hides nothing.")
        self.method_cb.currentTextChanged.connect(self._on_method_text)
        # NO SECOND STATUS BADGE. Step0's compute state is the state slot
        # beside the checkbox; the right-edge label this row used to carry
        # was always empty, and B5 removed it along with the writes into it.
        self._acc_step0 = self._accessory(
            [template.fit_accessory(self.method_cb)])
        # The state slot is Step0's too: the compute-state glyph is a claim
        # about a correction result, and a step that does not correct must
        # not show one. The SLOT stays (its 12 px is what keeps the swatch
        # and the name at the same x in every step); what goes is its text
        # and its hover text.
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
        """Show this step's fields. Silent, and a rebuild of nothing.

            Step0   checkbox | correction state | swatch | name | method
            Step1   checkbox |                  | swatch | name | weight
            Step2   checkbox |                  | swatch | name
            Step3   checkbox |                  | swatch | name

        A control that is not this step's is hidden, disabled AND made
        unfocusable: hiding alone leaves a widget a signal can still be
        delivered to, and a Step1 spinbox that can still write a weight from
        Step3 is the two-lists problem again in one row.

        Nothing here is a command: no owner is written, no signal of this
        row's is emitted, and no row is rebuilt.
        """
        self._step = step
        self._retire(self._acc_step0, step == STEP0)
        self._retire(self._acc_step1, step == STEP1)
        # ...and the permissions again, because a step showing a control is
        # not the same as this channel being allowed to use it: the nucleus's
        # method combo is dead in Step0 too.
        self._apply_permissions()
        if step != STEP0:
            # Step0's compute state is Step0's claim. The slot keeps its
            # width -- that is B1's geometry -- and loses its content.
            self.state_slot.setText("")
            self.state_slot.setToolTip("")
            self.state_slot.setStyleSheet(template.STATE_SLOT_QSS)

    @staticmethod
    def _retire(widgets, active):
        """Show or fully retire one step's controls.

        Visibility and reachability only; whether an active control is
        ENABLED is `_apply_permissions`'s answer, because that is the
        channel's capability rather than the step's.

        NEVER SHOWS AN ORPHAN. A visible QWidget with no parent is Qt's
        definition of a top-level window, so a control that has lost its
        parent is hidden and left alone rather than floated over the
        application as a tiny nameless window.
        """
        for w in widgets:
            show = bool(active) and w.parentWidget() is not None
            w.setVisible(show)
            if not active:
                w.setEnabled(False)
            w.setFocusPolicy(Qt.StrongFocus if active else Qt.NoFocus)

    # -- capabilities -------------------------------------------------------
    def apply_capabilities(self, caps):
        """WHAT MAY BE DONE to this channel, as separate facts.

        Never derived from one another and never from `locked`: a channel
        that may not be background-corrected (the nucleus) is still shown and
        hidden like any other. Stored, because the step also has a say: a
        control is operable only when the step shows it AND the channel
        allows it.
        """
        self._caps = caps
        self._weight_editable = bool(getattr(caps, "weight_editable", True))
        self.checkbox.setEnabled(bool(getattr(caps, "display_toggleable",
                                              True)))
        self._apply_permissions()

    def _apply_permissions(self):
        """Enable what this step shows and this channel allows."""
        caps = self._caps
        step = self._step
        editable = self._weight_editable and step == STEP1
        self.slider.setEnabled(editable)
        # The number box stays live in Step1 even for a channel whose weight
        # is not editable (the nucleus): it SHOWS the answer, read-only. A
        # disabled box would grey out the number itself.
        self.spin.setEnabled(step == STEP1)
        self.spin.setReadOnly(not editable)
        self.spin.setButtonSymbols(
            QtWidgets.QDoubleSpinBox.UpDownArrows if editable
            else QtWidgets.QDoubleSpinBox.NoButtons)
        self.method_cb.setEnabled(
            step == STEP0
            and bool(getattr(caps, "correction_eligible", True)))

    def set_weight_editable(self, editable):
        """Legacy name kept for the Step1 callers: a read-only row still
        shows its weight, it just cannot be moved."""
        self._weight_editable = bool(editable)
        self._apply_permissions()

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

    def set_method(self, method):
        """Show this channel's PREVIEW method, `Both` included. A value that
        is not one of the four leaves the combo where it is."""
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

    def _on_method_text(self, text):
        self.method_changed.emit(self._cid, str(text))

    def _on_swatch(self, ev):
        """Is this press or release on the colour swatch?"""
        return self.swatch.geometry().contains(ev.pos())

    def eventFilter(self, obj, ev):
        """The swatch's own mouse handling: consume, and ask for a colour."""
        if obj is self.swatch:
            if ev.type() == QEvent.MouseButtonPress:
                return True
            if ev.type() == QEvent.MouseButtonRelease:
                if ev.button() == Qt.LeftButton:
                    self.color_clicked.emit(self._cid)
                return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, ev):
        """A click on the ROW selects the channel -- unless it landed on the
        swatch, which is a control of its own.

        THE SWATCH ONLY CHANGES A COLOUR. It used to sit inside the row's
        click target, so picking a colour for a hidden channel also selected
        it and, by the click rule, showed it: one press produced a selection,
        a visibility write and a colour write, and the two extra ones were
        not what the user asked for by clicking a colour chip.
        """
        if self._on_swatch(ev):
            # CONSUMED, not merely unhandled: an ignored press walks up to
            # the LIST, and the list selects whatever item it lands in -- so
            # letting it through would select the row by another route.
            ev.accept()
            return
        self.row_clicked.emit(self._cid)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        # The swatch's own filter handles a real click on it; this covers a
        # release that reaches the ROW over the swatch's rectangle (a drag
        # that started elsewhere, and the synthetic events tests post).
        if self._on_swatch(ev):
            if not self.swatch.underMouse():
                self.color_clicked.emit(self._cid)
            ev.accept()
            return
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

    def __init__(self, state, fusion, parent=None, title="",
                 show_search=True, show_bulk_buttons=False):
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
        #: A weakref to the Step0 page that answers the correction fields.
        #: Weak on purpose: this dock belongs to Block01's lifetime and a
        #: strong reference here would keep a torn-down page alive.
        self._correction_owner = None
        self._syncing = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # NO TITLE OF ITS OWN by default. THE PANEL is the host's: Step0's
        # blue `Channels` group box, which every other step reuses. A title
        # here drew a second heading inside that box -- two `Channels`
        # captions, one frame inside another -- which is what made the dock
        # look like a different panel instead of the one Step0 always had.
        if title:
            hdr = QtWidgets.QLabel(title)
            hdr.setStyleSheet("color:#9bd0ff;font-size:11px;font-weight:bold;")
            lay.addWidget(hdr)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search channels…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        self.search.setVisible(bool(show_search))
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
        # HIDDEN BY DEFAULT: Step0's panel has its own `Show all` tick in the
        # row above the list, and a second bulk control inside the list was
        # never part of that panel. `set_all_visible` is still the one sweep
        # both of them call.
        self.btn_show_all.setVisible(bool(show_bulk_buttons))
        self.btn_hide_all.setVisible(bool(show_bulk_buttons))
        self._tools_row = tools
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
            # The step's own ticks and current channel came on. A REDRAW, not
            # a command: `refresh` reads the owners and writes nothing back.
            state.scope_changed.connect(lambda _scope: self.refresh())
        if fusion is not None:
            fusion.weight_changed.connect(self._on_fusion_weight)
            fusion.participation_changed.connect(self._on_fusion_participation)
            fusion.draft_restored.connect(self.refresh)

    # ── where the one panel is mounted ────────────────────────────────
    def mount_into(self, layout, index=None, stretch=1):
        """Move THIS widget into a step's Channels panel.

        One instance, moved -- not one per step. A reparent keeps the object
        identity of the dock, the list, the search box and every row, but Qt
        resets the scroll bar and drops focus when a widget is re-inserted,
        so both are carried across by hand. Nothing here is a command: no
        owner is written and no row is rebuilt.
        """
        if layout is None or self.parent() is layout.parentWidget():
            return False
        bar = self.list_widget.verticalScrollBar()
        keep_scroll = bar.value()
        keep_focus = self.hasFocus() or self.search.hasFocus() \
            or self.list_widget.hasFocus()
        keep_search_focus = self.search.hasFocus()
        old_layout = self.parentWidget().layout() if self.parentWidget() else None
        if old_layout is not None:
            old_layout.removeWidget(self)
        if index is None:
            layout.addWidget(self, stretch)
        else:
            layout.insertWidget(index, self, stretch)
        self.setVisible(True)
        bar.setValue(keep_scroll)
        if keep_focus:
            (self.search if keep_search_focus else self.list_widget).setFocus(
                Qt.OtherFocusReason)
        return True

    def unmount(self):
        """Leave the host that is going away, alive.

        The dock is a CHILD of whichever step's Channels box it is mounted
        in, so that host's destruction would take the one public panel --
        and every row, the search box and the list -- down with it. A page
        being released calls this first: the dock loses its parent, keeps
        its identity and its contents, and the next `mount_into` puts it
        back. Nothing is written to an owner and no row is rebuilt.
        """
        if self.parentWidget() is None:
            return False
        old_layout = self.parentWidget().layout()
        if old_layout is not None:
            old_layout.removeWidget(self)
        self.setParent(None)
        self.setVisible(False)
        return True

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
        was = self._step
        self._step = step
        for row in self._rows.values():
            row.set_step(step)
        if step != was:
            # THE NAME FOLLOWS THE STEP, because the mixed-weight marker does:
            # a repaint of a label is not a command, so nothing is written
            # and no row is rebuilt.
            for cid in list(self._rows):
                self._refresh_name(cid)
        if step == STEP0 and was != STEP0:
            # Entering Step0 re-asks the correction owner for its answers.
            # Silently: a row catching up with a decision somebody already
            # made is not a decision.
            for cid in list(self._rows):
                self._refresh_correction(cid)

    # ── Step0's correction controller: one, attached and detached ─────
    #
    # The dock outlives every page. A provider that closed over Step0 would
    # therefore keep a destroyed page alive and answer through it, so the
    # controller is held WEAKLY and there is an explicit detach: a page that
    # is going away says so, and a page that is merely garbage collected
    # cannot be reached either way.
    def attach_step0_correction_controller(self, owner, *, method=None,
                                           state=None, name=None):
        """Install `owner` as THE Step0 correction controller.

        `method(channel)`, `state(channel)` and `name(channel)` are asked of
        the owner only while Step0 is the step on screen. Attaching replaces
        whatever was attached before, so re-entering Step0 cannot accumulate
        two controllers answering the same question twice.
        """
        self._correction_owner = weakref.ref(owner)
        self._method_provider = method
        self._state_provider = state
        self._name_provider = name
        if self._step == STEP0:
            for cid in list(self._rows):
                self._refresh_correction(cid)
                self._rows[cid].set_name(self._name_of(cid))

    def detach_step0_correction_controller(self, owner):
        """Remove `owner`'s providers. A no-op for anyone else's.

        The owner check is what makes a torn-down page safe to detach after
        its successor has already attached: the page that is leaving must not
        take the current controller's providers with it.
        """
        current = self._correction_owner() if self._correction_owner else None
        if current is not None and current is not owner:
            return False
        self._correction_owner = None
        self._method_provider = None
        self._state_provider = None
        self._name_provider = None
        for cid, row in self._rows.items():
            row.set_state("")
            row.set_name(cid)
        return True

    def correction_controller(self):
        """The live Step0 controller, or None once it is gone."""
        return self._correction_owner() if self._correction_owner else None

    # -- the legacy provider setters, in terms of the controller -----------
    def set_state_provider(self, fn):
        self._state_provider = fn

    def set_method_provider(self, fn):
        self._method_provider = fn

    def set_name_provider(self, fn):
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
            row.method_changed.connect(self._on_row_method)
            row.color_clicked.connect(self._on_color_clicked)
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
            # THE NUCLEUS'S WEIGHT COMES FROM THE STEP0 HANDOFF and is read
            # only wherever it is shown. Which channel that is belongs to the
            # scientific owner, so it is asked here rather than derived from
            # a display capability that a page may never have installed.
            if fusion.nucleus()[0] == cid:
                row.set_weight_editable(False)
        # THE MIXED MARKER BELONGS TO THE WEIGHT, so it is shown where the
        # weight is: in Step1. A `CD3 *` with its "several groups at
        # different weights" tooltip in Step0, Step2 or Step3 describes a
        # field that step does not show -- and Step2/Step3 draw a plain
        # channel name by the ruling.
        mixed = (rep.values if (rep and rep.mixed and self._step == STEP1)
                 else ())
        row.set_name(self._name_of(cid), mixed_values=mixed)
        # CORRECTION IS STEP0'S FIELD. Asking for it in another step would
        # draw a claim about a background-correction result in a step that
        # does not correct -- which is what left `not computed` glowing in
        # Step1, Step2 and Step3.
        if self._step == STEP0:
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

    def _on_color_clicked(self, cid):
        """A swatch was clicked: pick the colour here, and tell the hosts."""
        self._pick_color(cid)
        self.color_edit_requested.emit(cid)

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
        self._refresh_name(cid)

    def _on_fusion_participation(self, cid, enabled):
        """Participation moved in the model: the row's TICK is its view.

        In Step1 the tick box is both halves of one answer, so the model's
        notice lands on the same widget the display answer does. Silent --
        `set_visible_state` blocks the signal -- so drawing an answer is
        never mistaken for making one.
        """
        row = self._rows.get(cid)
        if row is not None and self._step == STEP1:
            row.set_visible_state(bool(enabled))

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
        """The tick box. What it means depends on the step -- deliberately.

        Step0, Step2, Step3: DISPLAY, and nothing else. No correction
        decision, no participation, no weight.

        STEP1: one gesture, one decision -- "use this channel". It shows the
        channel AND puts it into the fusion, and a channel nobody has
        weighted enters at 1.0. That is the product's own contract and it
        predates the dock; splitting it into a tick plus a second `f` control
        is what left a ticked channel visible while `effective_config()`
        filtered it out, so a whole slide fused to DAPI alone.

        The weight rule is the model's (`set_fusion_enabled`): a first enable
        of a channel with no answer writes 1.0 as an automatic answer, and an
        explicit 0.0 or a per-group 0.2/0.7 is never overwritten. Unticking
        keeps all of it; ticking again restores it rather than forcing 1.0.

        ONE LOGICAL COMMAND: the model raises its draft revision once and
        emits weight -> participation -> draft, so nothing observes "shown
        but not participating" or "participating at 0" in between.

        IN STEP0 A TICK IS A CLICK (user ruling, 2026-09-17): ticking a
        marker shows THAT marker and takes the one that was showing off, the
        same as clicking its row. Step0 shows one marker at a time.
        """
        self.use_channel(cid, visible, origin=f"dock-step{self._step}")
        if visible and self._step == STEP0:
            self._show_only_this_marker(cid)

    def channel_in_use(self, cid):
        """Is this channel fully on, by the step's own definition?

        Step1: shown AND in the fusion -- the two halves of one answer.
        Elsewhere: shown. This is what a user gesture that means "use this
        channel" is measured against, so a half-on channel is something a
        click can still complete.
        """
        state, fusion = self._state, self._fusion
        if state is None:
            return False
        visible = bool(state.display_visible(cid))
        if self._step != STEP1 or fusion is None:
            return visible
        if not getattr(state.capabilities(cid), "fusion_toggleable", True):
            return visible
        return visible and bool(fusion.fusion_enabled(cid))

    def use_channel(self, cid, used, origin="dock"):
        """THE Step1 command, wherever the user makes it.

        Every real Step1 gesture that turns a channel on resolves here -- the
        tick box, a click on a hidden channel's name -- so none of them can
        drift into meaning something else. Outside Step1 it is a display
        answer and nothing more.

        BOTH OWNERS ARE FINAL BEFORE EITHER IS ANNOUNCED. The model's notices
        are held (`deferred_notices`) until the display answer is written, so
        no observer sees "fused but still hidden" and none sees "shown but
        not fused".
        """
        used = bool(used)
        state, fusion = self._state, self._fusion
        scientific = (self._step == STEP1 and fusion is not None
                      and getattr(
                          state.capabilities(cid) if state is not None
                          else None, "fusion_toggleable", True))
        if not scientific:
            if state is not None:
                state.set_display_visible(cid, used, origin=origin)
            return
        with fusion.deferred_notices():
            fusion.set_fusion_enabled(cid, used, origin=origin)
            if state is not None:
                state.set_display_visible(cid, used, origin=origin)

    def _on_row_weight(self, cid, value):
        """The scientific edit, and only from the step that shows it.

        Step1 is where a weight is edited (the shared Weights window is the
        other entry, and it writes the same model). Step0, Step2 and Step3 do
        not show the editor at all, so a value arriving from one of them came
        from a control the user cannot see.

        GIVING A CHANNEL A WEIGHT ENLISTS IT (user ruling, 2026-09-16). Asking
        for a weight on a channel that is not in the fusion used to write a
        number nothing computed with and leave the channel hidden -- the user
        moved a slider and the picture did not change. So the edit carries the
        same answers the tick does: the weight, participation and visibility,
        in ONE deferred block, so no observer sees a channel weighted but not
        fused.

        The weight is written first for reading order, not for correctness:
        the first enable's automatic 1.0 only lands where no weight has been
        chosen, and inside one deferred block an observer sees the final pair
        either way (measured -- swapping the two lines breaks no test).

        A channel already in the fusion is only reweighted. Nothing is ever
        toggled OFF here: winding a weight back to zero is an explicit zero,
        and untick is the one gesture that removes a channel.
        """
        if self._step != STEP1:
            return
        fusion = self._fusion
        if fusion is None:
            return
        origin = f"dock-step{self._step}"
        state = self._state
        enlist = not fusion.fusion_enabled(cid) and bool(
            getattr(state.capabilities(cid) if state is not None else None,
                    "fusion_toggleable", True))
        with fusion.deferred_notices():
            fusion.edit_channel_weight(cid, value, origin=origin)
            if enlist:
                fusion.set_fusion_enabled(cid, True, origin=origin)
                if state is not None:
                    state.set_display_visible(cid, True, origin=origin)

    def _refresh_name(self, cid):
        """Draw one row's name, with the mixed marker only where it means
        something (Step1). Silent."""
        row = self._rows.get(cid)
        if row is None:
            return
        mixed = ()
        if self._fusion is not None and self._step == STEP1:
            rep = self._fusion.representative_weight(cid)
            if rep.mixed:
                mixed = rep.values
        row.set_name(self._name_of(cid), mixed_values=mixed)

    def _refresh_correction(self, cid):
        """Draw Step0's correction answers on one row. Silent."""
        row = self._rows.get(cid)
        if row is None or self._step != STEP0:
            return
        if self._state_provider is not None:
            row.set_state(self._state_provider(cid))
        if self._method_provider is not None:
            row.set_method(self._method_provider(cid))

    def _on_row_method(self, cid, text):
        """Step0's correction decision, and only while Step0 is the step.

        GATED, not merely hidden. A combo that is off screen can still be
        handed a signal -- by a stale connection, a restore, or a test -- and
        a correction decided from Step2 is a decision nobody made.
        """
        if self._step != STEP0:
            return
        self.correction_method_changed.emit(cid, text)

    def _pick_color(self, cid):
        """THE public colour pick: a dialog, and one write to the owner.

        It used to be routed through `Step0Page._on_channel_swatch_clicked`,
        so the swatch in a step that has nothing to do with Step0 raised
        `wrapped C/C++ object of type Step0Page has been deleted` once that
        page was really gone. Colour is a public display answer: the dialog
        belongs to whoever shows the swatch, and the answer belongs to
        `ChannelDisplayState`, which every view -- Step0's included --
        already follows.
        """
        if not cid or self._state is None:
            return
        current = QtGui.QColor(self._state.color(cid) or "#888888")
        picked = QtWidgets.QColorDialog.getColor(current, self,
                                                 f"Colour for {cid}")
        if not picked.isValid():
            return
        # ONE write. A pick of the colour it already has changes nothing, and
        # the state swallows it: no second notice, no extra frame, no save.
        self._state.set_color(cid, picked.name(), origin="dock-swatch")

    def _on_row_clicked(self, cid):
        """A real click: select, and show a marker the user clicked on.

        Selecting something invisible is a dead end. A programmatic
        selection -- a restore, a dataset switch, the landing rule -- reaches
        `set_selected_channel` directly and shows nothing.

        IN STEP0 THE PICTURE FOLLOWS THE CLICK, ONE MARKER AT A TIME (user
        ruling, 2026-09-17). Step0 has no Process any more: every correction
        candidate is computed by itself, so the tick there answers one
        question only -- what is on screen. Clicking a marker shows it and
        takes the marker that was showing off; DAPI is the reference layer
        and keeps its own answer. Step1's tick is still the fusion command
        and is untouched by this.
        """
        if self._state is not None:
            caps = self._state.capabilities(cid)
            if (getattr(caps, "display_toggleable", True)
                    and not self.channel_in_use(cid)):
                # THE SAME COMMAND THE TICK MAKES, and judged by the same
                # question: is this channel IN USE? Asking only "is it
                # hidden?" left the case a saved project can be in -- shown
                # but not in the fusion -- unfixable by the gesture a user
                # would reach for, so the slide went on fusing DAPI alone.
                self.use_channel(cid, True, origin="dock-row-click")
            if self._step == STEP0:
                self._show_only_this_marker(cid)
            self._state.set_selected_channel(cid, origin="dock-row-click")
        self.row_clicked.emit(cid)

    def _show_only_this_marker(self, cid):
        """Step0: the clicked marker is the one on screen.

        Every other marker is hidden. A channel whose display the owners have
        locked, and the nucleus -- which is a reference layer with a switch
        of its own -- are left where they are.
        """
        state = self._state
        if state is None:
            return
        # WHO THE NUCLEUS IS -- asked of every owner that knows, because no
        # single one always does. A real session logged
        # `channel.visibility channel=DAPI visible=False
        # origin=dock-step0-exclusive`: the capability flag was not set on
        # that page's rows, so the sweep below took the reference layer off
        # the screen. A standalone page has no fusion model instead. Either
        # answer is enough to protect it.
        fusion = self._fusion
        nucleus_named = ""
        if fusion is not None:
            try:
                nucleus_named = str(fusion.nucleus()[0] or "")
            except Exception:
                nucleus_named = ""

        def _is_nucleus(channel):
            if nucleus_named and channel == nucleus_named:
                return True
            return bool(getattr(state.capabilities(channel), "is_nucleus",
                                False))

        if _is_nucleus(cid):
            return
        for other in list(self.channel_order()):
            if other == cid or _is_nucleus(other):
                continue
            caps = state.capabilities(other)
            if not getattr(caps, "display_toggleable", True):
                continue
            # A sweep's own permission: the nucleus layer is not the only
            # channel a page may want left alone by a bulk move.
            if not getattr(caps, "bulk_toggleable", True):
                continue
            if state.display_visible(other):
                state.set_display_visible(other, False,
                                          origin="dock-step0-exclusive")

    def set_all_visible(self, visible):
        """Show all / Hide all: a SWEEP, which is its own permission.

        THROUGH THE SAME COMMAND, one channel at a time. A sweep in Step1 is
        the user turning those channels on or off, so it has to mean what a
        tick means -- writing visibility alone here left every swept-in
        marker visible and outside the fusion, which is the same "only DAPI
        is fused" state the tick used to produce.

        A channel that is shown and hidden deliberately (the nucleus layer)
        is left alone by a sweep, as before.
        """
        if self._state is None:
            return
        for cid in self._order:
            caps = self._state.capabilities(cid)
            if getattr(caps, "bulk_toggleable", True):
                self.use_channel(cid, bool(visible), origin="dock-bulk")

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
