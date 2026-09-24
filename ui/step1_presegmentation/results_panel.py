"""The Results part of the Pre-segmentation tab (plan block C, step 4).

A list, no images (user ruling (a), 2026-09-24: the outlines come with the
montage view of block D). One row per combination of the current run: the
method and its parameters, how far it got (patches done, failed, cancelled,
cells), whether it is out of date, and a `Use` button. Nothing is chosen
until the user presses `Use` (plan 4.4 / 7.7); a row that may not be chosen
says why in the row itself.
"""

from PyQt5 import QtCore, QtWidgets

from ...utils import segmentation_param_schema as ps

_GREY = "color:#999;font-size:10px;"
_WARN = "color:#ffd166;font-size:10px;"
_BAD = "color:#ff6b6b;font-size:10px;"


class ResultRow(QtWidgets.QFrame):
    use_clicked = QtCore.pyqtSignal(str)

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self.combo_id = combo["combo_id"]
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
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

    def show_state(self, text, level, can_use, in_use):
        self.status.setText(text)
        self.status.setStyleSheet({"bad": _BAD, "warn": _WARN}.get(level, _GREY))
        self.btn_use.setText("In use" if in_use else "Use")
        self.btn_use.setEnabled(bool(can_use) and not in_use)
        self.setStyleSheet("ResultRow{border:1px solid #6fcf97;}" if in_use else "")


class ResultsPanel(QtWidgets.QWidget):
    use_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(3)
        head = QtWidgets.QLabel("Results", self)
        head.setStyleSheet("font-weight:bold;color:#ccc;font-size:11px;")
        lay.addWidget(head)
        self.summary = QtWidgets.QLabel("No run yet.", self)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(_GREY)
        lay.addWidget(self.summary)
        self._rows_host = QtWidgets.QWidget(self)
        self._rows = QtWidgets.QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(3)
        lay.addWidget(self._rows_host)
        self._by_combo = {}

    # ── contents ─────────────────────────────────────────────────────
    def set_combos(self, combos):
        """A new run: one row per combination, in the plan's order."""
        for row in self._by_combo.values():
            row.setParent(None)
            row.deleteLater()
        self._by_combo = {}
        for combo in combos or []:
            row = ResultRow(combo, self._rows_host)
            row.use_clicked.connect(self.use_requested)
            self._rows.addWidget(row)
            self._by_combo[combo["combo_id"]] = row

    def clear(self, text="No run yet."):
        self.set_combos([])
        self.summary.setText(text)

    def set_summary(self, text):
        self.summary.setText(text)

    def rows(self):
        return list(self._by_combo.values())

    def row(self, combo_id):
        return self._by_combo.get(combo_id)

    def show_state(self, combo_id, text, level="", can_use=False, in_use=False):
        row = self._by_combo.get(combo_id)
        if row is not None:
            row.show_state(text, level, can_use, in_use)
