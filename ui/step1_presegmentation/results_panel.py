"""The Results part of the Pre-segmentation tab (plan block C, step 4).

A list, no images (user ruling (a), 2026-09-24: the outlines come with the
montage view of block D). One row per combination of the current run: the
method and its parameters, how far it got (patches done, failed, cancelled,
cells), whether it is out of date, and a `Use` button. Nothing is chosen
until the user presses `Use` (plan 4.4 / 7.7); a row that may not be chosen
says why in the row itself.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from ...utils import segmentation_param_schema as ps
from . import mask_layers
from .method_blocks import block_frame_qss

_GREY = "color:#999;font-size:10px;"
_WARN = "color:#ffd166;font-size:10px;"
_BAD = "color:#ff6b6b;font-size:10px;"


class ResultRow(QtWidgets.QFrame):
    use_clicked = QtCore.pyqtSignal(str)
    style_changed = QtCore.pyqtSignal(str, object)   # combo id, its outline style

    def __init__(self, combo, parent=None, index=0, outputs=("cell", "nucleus")):
        super().__init__(parent)
        self.combo_id = combo["combo_id"]
        self.style = mask_layers.default_style(index)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setObjectName("resultBlock")
        self.setStyleSheet(block_frame_qss("resultBlock"))       # as a Methods block
        # never squeezed: many combinations scroll instead (the list below)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(1)
        top = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(ps.display_name(combo["method"]), self)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight:bold;font-size:11px;")
        top.addWidget(self.title, 1)
        self.btn_use = QtWidgets.QPushButton("Use", self)
        self.btn_use.setMinimumWidth(44)
        self.btn_use.setMaximumWidth(64)
        self.btn_use.clicked.connect(lambda: self.use_clicked.emit(self.combo_id))
        top.addWidget(self.btn_use)
        lay.addLayout(top)
        values = {k: [v] for k, v in combo["params"].items()}
        self.params = QtWidgets.QLabel(ps.summary(combo["method"], values), self)
        self.params.setWordWrap(True)
        self.params.setStyleSheet(_GREY)
        lay.addWidget(self.params)
        self.status = QtWidgets.QLabel("", self)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        # How this combination draws on the montage (user ruling 2026-09-25:
        # here, in its own box, not in a panel beside the picture).
        ctl = QtWidgets.QHBoxLayout()
        ctl.setSpacing(4)
        self.chk_cells = QtWidgets.QCheckBox("Cells", self)
        self.chk_nuclei = QtWidgets.QCheckBox("Nuclei", self)
        for chk, kind, key in ((self.chk_cells, "cell", "cells"),
                               (self.chk_nuclei, "nucleus", "nuclei")):
            chk.setStyleSheet("font-size:10px;")
            chk.setEnabled(kind in outputs)          # plan 4.5: greyed when not produced
            chk.toggled.connect(lambda on, k=key: self._set(k, bool(on)))
            ctl.addWidget(chk)
        ctl.addStretch(1)
        self.btn_color = QtWidgets.QToolButton(self)
        self.btn_color.setFixedSize(16, 16)
        self.btn_color.clicked.connect(self._pick_color)
        ctl.addWidget(self.btn_color)
        self.btn_more = QtWidgets.QToolButton(self)
        self.btn_more.setText("▾")
        self.btn_more.setFixedSize(16, 16)
        self.btn_more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.menu = QtWidgets.QMenu(self)
        self._width_actions = {}
        group = QtWidgets.QActionGroup(self.menu)
        for wdt in mask_layers.WIDTHS:
            act = self.menu.addAction(f"Line width {wdt:g} px")
            act.setCheckable(True)
            group.addAction(act)
            act.triggered.connect(lambda _c, v=wdt: self._set("width", v))
            self._width_actions[wdt] = act
        self.menu.addSeparator()
        self.act_cell_dashed = self.menu.addAction("Cell outline dashed")
        self.act_nucleus_dashed = self.menu.addAction("Nucleus outline dashed")
        for act, key in ((self.act_cell_dashed, "cell_dashed"),
                         (self.act_nucleus_dashed, "nucleus_dashed")):
            act.setCheckable(True)
            act.toggled.connect(lambda on, k=key: self._set(k, bool(on)))
        self.btn_more.setMenu(self.menu)
        ctl.addWidget(self.btn_more)
        lay.addLayout(ctl)
        self._show_style()

    # ── the outline style ────────────────────────────────────────────
    def set_style(self, **changes):
        """Change the style (a panel-wide All / None, or a test)."""
        for k, v in changes.items():
            if k == "cells" and not self.chk_cells.isEnabled():
                continue
            if k == "nuclei" and not self.chk_nuclei.isEnabled():
                continue
            self._set(k, v)

    def _set(self, key, value):
        if self.style.get(key) == value:
            return
        self.style = dict(self.style, **{key: value})
        self._show_style()
        self.style_changed.emit(self.combo_id, dict(self.style))

    def _pick_color(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.style["color"]), self,
                                                "Outline colour")
        if color.isValid():
            self._set("color", color.name())

    def _show_style(self):
        st = self.style
        for chk, key in ((self.chk_cells, "cells"), (self.chk_nuclei, "nuclei")):
            chk.blockSignals(True)
            chk.setChecked(bool(st[key]))
            chk.blockSignals(False)
        self.btn_color.setStyleSheet(f"background:{st['color']};border:1px solid #666;")
        for wdt, act in self._width_actions.items():
            act.setChecked(abs(wdt - st["width"]) < 1e-6)
        for act, key in ((self.act_cell_dashed, "cell_dashed"),
                         (self.act_nucleus_dashed, "nucleus_dashed")):
            act.blockSignals(True)
            act.setChecked(bool(st[key]))
            act.blockSignals(False)

    def show_state(self, text, level, can_use, in_use):
        self.status.setText(text)
        self.status.setStyleSheet({"bad": _BAD, "warn": _WARN}.get(level, _GREY))
        self.btn_use.setText("In use" if in_use else "Use")
        self.btn_use.setEnabled(bool(can_use) and not in_use)
        self.setStyleSheet(block_frame_qss("resultBlock", "#6fcf97" if in_use else "#555"))


class _AllCheck(QtWidgets.QCheckBox):
    """Ticked: every box shows this kind; unticked: none does (user ruling,
    2026-09-25). Half-ticked only SAYS the boxes disagree; a click from
    there shows them all."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self):  # noqa: N802
        self.setCheckState(QtCore.Qt.Unchecked if self.checkState() == QtCore.Qt.Checked
                           else QtCore.Qt.Checked)


class ResultsPanel(QtWidgets.QWidget):
    use_requested = QtCore.pyqtSignal(str)
    style_changed = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(3, 2, 3, 2)            # inside its section frame
        lay.setSpacing(3)
        # the title is the section frame's ("Results"), not a label in here
        # One tick for every combination's cells, one for their nuclei
        # (user ruling 2026-09-25, instead of All / None buttons); off at first.
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.chk_all = {}
        for kind, label in (("cells", "Cells"), ("nuclei", "Nuclei")):
            chk = _AllCheck(label, self)
            chk.setStyleSheet("font-size:10px;")
            chk.clicked.connect(lambda _c, k=kind: self._on_all_clicked(k))
            row.addWidget(chk)
            self.chk_all[kind] = chk
        row.addStretch(1)
        lay.addLayout(row)
        self.summary = QtWidgets.QLabel("No run yet.", self)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(_GREY)
        lay.addWidget(self.summary)
        # The boxes scroll when there are more than the column can hold: a
        # box squeezed below its height is unreadable (seen with a short tab).
        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._rows_host = QtWidgets.QWidget(self.scroll)
        self._rows = QtWidgets.QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(3)
        self._rows.addStretch(1)
        self.scroll.setWidget(self._rows_host)
        self.scroll.setMinimumWidth(0)
        lay.addWidget(self.scroll, 1)
        self._by_combo = {}
        self._fit_scroll()

    # ── contents ─────────────────────────────────────────────────────
    def set_combos(self, combos, outputs_of=None):
        """A new run: one row per combination, in the plan's order.
        `outputs_of(method)` -> the masks the method produces (plan 4.5)."""
        for row in self._by_combo.values():
            row.setParent(None)
            row.deleteLater()
        self._by_combo = {}
        for i, combo in enumerate(combos or []):
            outputs = outputs_of(combo["method"]) if outputs_of else ("cell", "nucleus")
            row = ResultRow(combo, self._rows_host, index=i, outputs=outputs)
            row.use_clicked.connect(self.use_requested)
            row.style_changed.connect(self.style_changed)
            row.style_changed.connect(lambda *_a: self._show_all_state())
            self._rows.insertWidget(self._rows.count() - 1, row)
            self._by_combo[combo["combo_id"]] = row
        self._fit_scroll()
        self._show_all_state()

    def _fit_scroll(self):
        """As tall as its boxes (none: nothing), scrolling beyond that."""
        need = sum(r.sizeHint().height() + 3 for r in self._by_combo.values())
        self.scroll.setMaximumHeight(max(0, need + 2))
        self.scroll.setVisible(bool(self._by_combo))

    def clear(self, text="No run yet."):
        self.set_combos([])
        self.summary.setText(text)

    def set_summary(self, text):
        self.summary.setText(text)

    def set_all(self, kind, on):
        """Cells or nuclei of every combination on or off (greyed ones stay)."""
        for row in self._by_combo.values():
            row.set_style(**{kind: bool(on)})
        self._show_all_state()

    def _on_all_clicked(self, kind):
        self.set_all(kind, self.chk_all[kind].checkState() == QtCore.Qt.Checked)

    def _show_all_state(self):
        """Each tick says what the boxes say: all on, none on, or some."""
        for kind, chk in getattr(self, "chk_all", {}).items():
            box = {"cells": "chk_cells", "nuclei": "chk_nuclei"}[kind]
            able = [r for r in self._by_combo.values() if getattr(r, box).isEnabled()]
            on = sum(1 for r in able if r.style[kind])
            state = (QtCore.Qt.Checked if able and on == len(able)
                     else QtCore.Qt.Unchecked if on == 0 else QtCore.Qt.PartiallyChecked)
            chk.blockSignals(True)
            chk.setCheckState(state)
            chk.blockSignals(False)
            chk.setEnabled(bool(able))

    def styles(self):
        return {cid: dict(row.style) for cid, row in self._by_combo.items()}

    def rows(self):
        return list(self._by_combo.values())

    def row(self, combo_id):
        return self._by_combo.get(combo_id)

    def show_state(self, combo_id, text, level="", can_use=False, in_use=False):
        row = self._by_combo.get(combo_id)
        if row is not None:
            row.show_state(text, level, can_use, in_use)
