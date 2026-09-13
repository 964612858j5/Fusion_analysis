"""Per-page channel row widgets for the shared ChannelDock.

All rows share the base structure (visibility checkbox, color swatch, name);
page-specific extras stay in subclasses:

- Step0ChannelRow: + background-method combo (preview/decision) + status badge.
- WeightChannelRow: + weight slider + aligned numeric input (Step1).
- DisplayChannelRow: base only (Step3 rows: visibility/color/name only).

Rows write user interaction into the ChannelSetModel and follow model signals
for their channel; they never touch config files.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from . import template
from .model import ChannelSetModel

# Re-exported so the existing importers keep working; the definition lives in
# `template`, which is the one place a channel row's look is decided.
CHECKBOX_INDICATOR_QSS = template.CHECKBOX_INDICATOR_QSS
_NAME_STYLE = template.NAME_QSS


class ChannelRowBase(QtWidgets.QWidget):
    """The shared row: the template's core, plus this class's model wiring.

    The core -- checkbox, state slot, swatch, name -- is built by
    `channel_dock.template`, so this row and Step1's `ConfigPanel.ChannelRow`
    cannot drift apart in geometry or style. A subclass appends its accessory
    to `self._extras_layout` and touches nothing else.
    """

    color_clicked = pyqtSignal(str)
    #: A REAL mouse click on this row. `ChannelSetModel.selection_changed`
    #: cannot say who moved the selection -- a restore and a dataset switch
    #: move it too -- and "show me this one" is a thing only a click means.
    row_clicked = pyqtSignal(str)

    def __init__(self, model: ChannelSetModel, cid: str, parent=None,
                 show_visibility=True):
        super().__init__(parent)
        self._model = model
        self._cid = cid
        st = model.get(cid)

        core = template.build_row_core(
            self, name=(st.name if st else cid),
            show_visibility=show_visibility,
            # MAY THIS CHANNEL BE SHOWN AND HIDDEN -- not "is it special".
            checkable=(st.display_toggleable if st else True))
        self.checkbox = core.checkbox
        self.state_slot = core.state_slot
        self.swatch = core.swatch
        self.name_label = core.name_label
        self._extras_layout = core.layout   # subclasses append here

        self.checkbox.setChecked(bool(st and st.visible))
        self.checkbox.toggled.connect(self._on_visibility_toggled)
        self._apply_color(st.color if st else "#888888")

        model.color_changed.connect(self._on_model_color)
        model.visibility_changed.connect(self._on_model_visibility)

    # -- properties ------------------------------------------------------
    @property
    def channel_id(self) -> str:
        return self._cid

    # -- model -> widget ---------------------------------------------------
    def _on_model_color(self, cid, color):
        if cid == self._cid:
            self._apply_color(color)

    def _on_model_visibility(self, cid, visible):
        if cid == self._cid and self.checkbox.isChecked() != visible:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(visible)
            self.checkbox.blockSignals(False)

    def _apply_color(self, color):
        template.apply_swatch_color(self.swatch, color)

    # -- widget -> model ---------------------------------------------------
    def _on_visibility_toggled(self, checked):
        self._model.set_visible(self._cid, bool(checked))

    def mousePressEvent(self, ev):
        self._model.select(self._cid)
        self.row_clicked.emit(self._cid)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.swatch.geometry().contains(ev.pos()):
            self.color_clicked.emit(self._cid)
        super().mouseReleaseEvent(ev)


class Step0ChannelRow(ChannelRowBase):
    """Step0 row: color, name, background-method combo, status badge.

    The combo is the single control for the assigned/final method (as in v14);
    the badge shows preview/compute status (e.g. "", computing, done, unsaved).
    Method parameters and Compare live in the selected-channel inspector, not
    in the row.

    Next to the CHECKBOX -- not at the right edge with the legacy badge --
    sits the compute-state glyph (`state_lbl`). The checkbox is what selects
    a channel for the Process run, so "is this one already computed, and is
    that result still current?" belongs beside it, where the eye already is
    when ticking boxes. It is fed the same `status_changed` signal as the
    legacy badge; the host decides the vocabulary (see `STATE_GLYPHS`).
    """

    method_changed = pyqtSignal(str, str)      # (channel_id, method text)

    #: The three FINAL answers, in the order the page's `_METHOD_IDX` uses.
    #: `Both` was in this list and is not an answer: it names computing two
    #: candidates to compare, which is what the TopHat/cuCIM parameter boxes
    #: do, while Save can only write one of these three.
    METHODS = ["Original", "TopHat", "cucim"]

    # state -> (glyph, stylesheet, tooltip). The three states the Background
    # Correction page derives from its signature bookkeeping, plus the two
    # transient/structural ones it already had.
    STATE_GLYPHS = {
        "": ("", "", ""),
        "not-computed": (
            "○", "color:#6d8196;font-size:11px;",
            "not computed — tick this channel and press Process"),
        "computed": (
            "✓", "color:#56d990;font-size:11px;font-weight:bold;",
            "computed — the cached result matches the current "
            "method, parameters and patches"),
        "stale": (
            "!", "color:#f4c45e;font-size:12px;font-weight:bold;",
            "stale — the method or parameters changed since this channel "
            "was computed; Process recomputes it"),
        "computing": (
            "⟳", "color:#f4c45e;font-size:12px;", "computing…"),
        "nucleus": (
            "★", "color:#56b6c2;font-size:11px;",
            "reference channel — never background-corrected"),
    }

    def __init__(self, model, cid, parent=None):
        super().__init__(model, cid, parent)
        st = model.get(cid)

        # The checkbox footprint, the state column and the name geometry are
        # the TEMPLATE's now -- this row no longer sets any of them, which is
        # what stops Step0 and Step1 from drifting apart again. `state_lbl`
        # is the template's state slot under its historical name, so the
        # existing callers and tests keep working.
        self.state_lbl = self.state_slot
        self.set_state(st.status if st else "")

        self.method_cb = QtWidgets.QComboBox()
        self.method_cb.addItems(self.METHODS)
        self.method_cb.setFixedWidth(64)
        # MAY THIS CHANNEL BE BACKGROUND-CORRECTED. The nucleus cannot, and
        # that is the reason its combo is off -- not that it is "locked",
        # which also meant it could not be shown.
        self.method_cb.setEnabled(st.correction_eligible if st else True)
        self.method_cb.setStyleSheet(template.ACCESSORY_COMBO_QSS)
        if st and st.bg_final_method:
            idx = self._method_index(st.bg_final_method)
            if idx >= 0:
                self.method_cb.setCurrentIndex(idx)
        self.method_cb.currentTextChanged.connect(
            lambda txt: self.method_changed.emit(self._cid, txt))
        template.add_accessory(self._extras_layout,
                               template.fit_accessory(self.method_cb))

        self.status_lbl = QtWidgets.QLabel("—")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setFixedWidth(20)
        self.status_lbl.setStyleSheet(f"color:{template.COLOR_MUTED};"
                                      "font-size:12px;")
        # After the template's trailing stretch, so the badge stays at the
        # right edge while the method combo sits next to the name.
        self._extras_layout.addWidget(self.status_lbl)

        model.status_changed.connect(self._on_model_status)
        model.bg_final_changed.connect(self._on_model_final)

    @classmethod
    def _method_index(cls, method: str) -> int:
        """Where a FINAL decision sits in `METHODS`, or -1 if it is none.

        `both` is deliberately not in the table: it is a computation, and a
        row asked to show it would be claiming a choice Save cannot write.
        A host handing one in gets -1 and the combo is left where it is.
        """
        lut = {name.lower(): i for i, name in enumerate(cls.METHODS)}
        return lut.get(str(method).strip().lower(), -1)

    def _on_model_final(self, cid, method):
        if cid != self._cid:
            return
        idx = self._method_index(method)
        if idx >= 0 and idx != self.method_cb.currentIndex():
            self.method_cb.blockSignals(True)
            self.method_cb.setCurrentIndex(idx)
            self.method_cb.blockSignals(False)

    def set_state(self, state):
        """Set the compute-state glyph beside the checkbox.

        An unknown state shows nothing rather than a placeholder: the glyph
        is a claim about the channel's result, and inventing one for a
        vocabulary this row does not know would be a false claim.
        """
        glyph, style, tip = self.STATE_GLYPHS.get(str(state or ""),
                                                  ("", "", ""))
        self.state_lbl.setText(glyph)
        self.state_lbl.setStyleSheet(style)
        self.state_lbl.setToolTip(tip)

    def _on_model_status(self, cid, status):
        if cid != self._cid:
            return
        self.set_state(status)
        style = {"computing": ("⟳", "color:#f4c45e;font-size:13px;"),
                 "done": ("✓", "color:#56d990;font-size:12px;"),
                 "unsaved": ("●", "color:#f4c45e;font-size:11px;"),
                 "nucleus": ("★", "color:#56b6c2;font-size:12px;")}.get(
            status, ("—", "color:#6d8196;font-size:12px;"))
        self.status_lbl.setText(style[0])
        self.status_lbl.setStyleSheet(style[1])


class WeightChannelRow(ChannelRowBase):
    """Step1 row: color, name, weight slider + aligned numeric input.

    Slider and spinbox are two-way synced; no background-correction method and
    no Min/Max/Gamma editors here (those are Step0 semantics).
    """

    def __init__(self, model, cid, parent=None):
        super().__init__(model, cid, parent)
        st = model.get(cid)
        w = st.weight if (st and st.weight is not None) else 1.0
        self._busy = False

        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(w * 100)))
        self.slider.setFixedHeight(16)
        self.slider.setMinimumWidth(70)
        template.add_accessory(self._extras_layout, self.slider, stretch=2)

        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(w)
        self.spin.setFixedWidth(52)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setStyleSheet(template.ACCESSORY_SPINBOX_QSS)
        template.add_accessory(self._extras_layout,
                               template.fit_accessory(self.spin))

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        model.weight_changed.connect(self._on_model_weight)

    def weight(self) -> float:
        return float(self.spin.value())

    def _on_slider(self, v):
        if self._busy:
            return
        self._busy = True
        self.spin.setValue(v / 100.0)
        self._busy = False
        self._model.set_weight(self._cid, v / 100.0)

    def _on_spin(self, v):
        if self._busy:
            return
        self._busy = True
        self.slider.setValue(int(round(v * 100)))
        self._busy = False
        self._model.set_weight(self._cid, float(v))

    def _on_model_weight(self, cid, w):
        if cid != self._cid or self._busy:
            return
        self._busy = True
        self.slider.setValue(int(round(w * 100)))
        self.spin.setValue(w)
        self._busy = False


class DisplayChannelRow(ChannelRowBase):
    """Step3 row: visibility, color and name only (display/QC)."""
    pass
