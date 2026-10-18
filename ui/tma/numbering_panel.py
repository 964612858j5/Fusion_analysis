"""The TMA page's numbering panel (F3, docs/tma_f3_page_application.md
§1): which map records this segment is for, the preview of "image k ->
record", the count check and Confirm / Reverse / Cancel; and the single-core
number box.

The target is either a start and an end record with an order (along a
column / row, or the reverse), or a custom list made by Ctrl+clicking
table rows. The panel only holds and shows; the page computes the plan
(`core.tma.layout.plan_binding`) and hands it to `show_plan`.
"""

from __future__ import annotations

from typing import Dict, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QComboBox, QCompleter, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

ORDERS = (("Along column", "column"), ("Along row", "row"),
          ("Along column, reversed", "column_rev"), ("Along row, reversed", "row_rev"),
          ("Custom (Ctrl+click table rows)", "custom"))


class NumberingPanel(QWidget):
    target_changed = pyqtSignal()
    confirm = pyqtSignal()
    reverse = pyqtSignal()
    cancel = pyqtSignal()
    bind_single = pyqtSignal(str)         # the text in the number box
    custom_single = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        grid.addWidget(QLabel("Start:"), 0, 0)
        self.start = QComboBox()
        grid.addWidget(self.start, 0, 1)
        grid.addWidget(QLabel("End:"), 0, 2)
        self.end = QComboBox()
        grid.addWidget(self.end, 0, 3)
        grid.addWidget(QLabel("Order:"), 1, 0)
        self.order = QComboBox()
        for text, _key in ORDERS:
            self.order.addItem(text)
        grid.addWidget(self.order, 1, 1, 1, 3)
        lay.addLayout(grid)
        self.custom_label = QLabel("")
        self.custom_label.setWordWrap(True)
        lay.addWidget(self.custom_label)
        row = QHBoxLayout()
        self.count = QLabel("Selected 0 / target 0")
        row.addWidget(self.count, 1)
        self.btn_reverse = QPushButton("Reverse")
        self.btn_confirm = QPushButton("Confirm")
        self.btn_cancel = QPushButton("Cancel")
        for b in (self.btn_reverse, self.btn_confirm, self.btn_cancel):
            row.addWidget(b)
        lay.addLayout(row)
        self.preview = QListWidget()
        self.preview.setMaximumHeight(110)
        lay.addWidget(self.preview)
        single = QHBoxLayout()
        single.addWidget(QLabel("Selected core:"))
        self.number = QLineEdit()
        self.number.setPlaceholderText("position or donor, e.g. X1-Y02")
        single.addWidget(self.number, 1)
        self.btn_bind = QPushButton("Number")
        self.btn_custom = QPushButton("Custom")
        single.addWidget(self.btn_bind)
        single.addWidget(self.btn_custom)
        lay.addLayout(single)
        self.custom_ids: List[int] = []
        self._labels: Dict[int, str] = {}
        self.start.currentIndexChanged.connect(lambda _i: self.target_changed.emit())
        self.end.currentIndexChanged.connect(lambda _i: self.target_changed.emit())
        self.order.currentIndexChanged.connect(self._order_changed)
        self.btn_confirm.clicked.connect(self.confirm.emit)
        self.btn_reverse.clicked.connect(self.reverse.emit)
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.btn_bind.clicked.connect(lambda: self.bind_single.emit(self.number.text()))
        self.btn_custom.clicked.connect(lambda: self.custom_single.emit(self.number.text()))
        self.show_plan(None)

    # ── the map's records ──
    def set_records(self, records: List[Dict]):
        """Every non-empty map record: position_id, label, donor."""
        self._labels = {r["position_id"]: r["label"] for r in records}
        for box in (self.start, self.end):
            box.blockSignals(True)
            box.clear()
            for r in records:
                box.addItem(f"{r['label']}  {r['donor']}", r["position_id"])
            box.blockSignals(False)
        if records:
            self.end.setCurrentIndex(len(records) - 1)
        comp = QCompleter([f"{r['label']} {r['donor']}" for r in records])
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.number.setCompleter(comp)
        self.custom_ids = []
        self._order_changed()

    def order_key(self) -> str:
        return ORDERS[self.order.currentIndex()][1]

    def target(self):
        """("range", start_id, end_id, order) or ("custom", [ids])."""
        if self.order_key() == "custom":
            return ("custom", list(self.custom_ids))
        return ("range", self.start.currentData(), self.end.currentData(), self.order_key())

    def add_custom(self, position_id: int):
        if self.order_key() != "custom":
            return
        if position_id in self.custom_ids:
            self.custom_ids.remove(position_id)
        else:
            self.custom_ids.append(position_id)
        self._show_custom()
        self.target_changed.emit()

    def _order_changed(self, *_a):
        custom = self.order_key() == "custom"
        self.start.setEnabled(not custom)
        self.end.setEnabled(not custom)
        self._show_custom()
        self.target_changed.emit()

    def _show_custom(self):
        if self.order_key() != "custom":
            self.custom_label.setText("")
            return
        names = [self._labels.get(i, str(i)) for i in self.custom_ids]
        self.custom_label.setText("Target: " + (", ".join(names) if names else "Ctrl+click rows in the table"))

    # ── the preview ──
    def show_plan(self, plan, active: bool = False):
        """`plan` from `layout.plan_binding` (or None); `active` while a
        line / click sequence is being numbered."""
        self.preview.clear()
        for b in (self.btn_confirm, self.btn_reverse, self.btn_cancel):
            b.setEnabled(active)
        if plan is None:
            self.count.setText("Draw a line or click cores to number them")
            self.btn_confirm.setText("Confirm")
            return
        n_sel = len(plan["pairs"]) if plan["ok"] else None
        self.count.setText(plan.get("count_text") or (f"Selected {n_sel}" if n_sel is not None else ""))
        conflicts = {c["object"] for c in plan["conflicts"]}
        for k, pr in enumerate(plan["pairs"], start=1):
            r = pr["record"]
            it = QListWidgetItem(f"{k} → {r['label']}  {r['donor']}")
            if pr["object"] in conflicts:
                it.setForeground(QColor("#c62828"))
            self.preview.addItem(it)
        for p in plan["problems"]:
            it = QListWidgetItem(p)
            it.setForeground(QColor("#c62828"))
            self.preview.addItem(it)
        self.btn_confirm.setEnabled(active and plan["ok"])
        k = len(plan["conflicts"])
        self.btn_confirm.setText(f"Confirm and rebind {k}" if k else "Confirm")
