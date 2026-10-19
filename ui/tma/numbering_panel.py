"""The numbering panel (F3, docs/tma_f3_page_application.md §1; user
2026-10-18): which map records this segment is for, the preview of "core k
-> record", the count check and Confirm / Reverse / Cancel; and the
single-core number box.

The target is a start and an end record, typed (matched as you type) or
picked; there is no order to choose -- the records run from start to end
(`numbering.walk_records`), and the cores are taken in the order they were
clicked or lined up. The panel only holds and shows; the page computes the
plan (`core.regions.numbering.plan_binding`) and hands it to `show_plan`.
"""

from __future__ import annotations

from typing import Dict, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QComboBox, QCompleter, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)


class RecordBox(QComboBox):
    """A record chooser you can type into: any part of "X1-Y05  donor"
    matches (the first match is taken when typing ends); unmatched text
    goes back to the current record."""

    picked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.lineEdit().editingFinished.connect(self._typed)
        self.currentIndexChanged.connect(lambda _i: self.picked.emit())

    def set_items(self, items):
        self.blockSignals(True)
        self.clear()
        for text, data in items:
            self.addItem(text, data)
        comp = QCompleter([t for t, _d in items], self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.activated.connect(self._typed)
        self.setCompleter(comp)
        self.blockSignals(False)

    def _typed(self, *_a):
        text = self.currentText().strip().lower()
        hit = next((i for i in range(self.count()) if self.itemText(i).lower() == text), None)
        if hit is None and text:
            hit = next((i for i in range(self.count()) if text in self.itemText(i).lower()), None)
        if hit is None:
            hit = self.currentIndex()
        if hit != self.currentIndex():
            self.setCurrentIndex(hit)
        elif hit >= 0:
            self.setEditText(self.itemText(hit))


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
        self.start = RecordBox()
        grid.addWidget(self.start, 0, 1)
        grid.addWidget(QLabel("End:"), 0, 2)
        self.end = RecordBox()
        grid.addWidget(self.end, 0, 3)
        lay.addLayout(grid)
        hint = QLabel("Type or pick the first and the last record. Cores are numbered in the order you "
                      "click them (Select ▸ Click, Ctrl+click for more), click them (Annotate ▸ Clicks) "
                      "or line them up (Annotate ▸ Line).")
        hint.setWordWrap(True)
        lay.addWidget(hint)
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
        self.start.picked.connect(self.target_changed.emit)
        self.end.picked.connect(self.target_changed.emit)
        self.btn_confirm.clicked.connect(self.confirm.emit)
        self.btn_reverse.clicked.connect(self.reverse.emit)
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.btn_bind.clicked.connect(lambda: self.bind_single.emit(self.number.text()))
        self.btn_custom.clicked.connect(lambda: self.custom_single.emit(self.number.text()))
        self.show_plan(None)

    # ── the map's records ──
    def set_records(self, records: List[Dict]):
        """Every non-empty map record: position_id, label, donor."""
        items = [(f"{r['label']}  {r['donor']}", r["position_id"]) for r in records]
        self.start.set_items(items)
        self.end.set_items(items)
        if records:
            self.end.setCurrentIndex(len(records) - 1)
        comp = QCompleter([f"{r['label']} {r['donor']}" for r in records])
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.number.setCompleter(comp)
        self.target_changed.emit()

    def target(self):
        """(start_id, end_id) -- None for one not chosen."""
        return self.start.currentData(), self.end.currentData()

    # ── the preview ──
    def show_plan(self, plan, active: bool = False):
        """`plan` from `layout.plan_binding` (or None); `active` while a
        line / click sequence is being numbered."""
        self.preview.clear()
        for b in (self.btn_confirm, self.btn_reverse, self.btn_cancel):
            b.setEnabled(active)
        if plan is None:
            self.count.setText("Select, click or line up cores to number them")
            self.btn_confirm.setText("Confirm")
            return
        n_sel = len(plan["pairs"]) if plan["ok"] else None
        self.count.setText(plan.get("count_text") or (f"Selected {n_sel}" if n_sel is not None else ""))
        conflicts = {c["object"] for c in plan["conflicts"]}
        for k, pr in enumerate(plan["pairs"], start=1):
            r = pr["record"]
            it = QListWidgetItem(f"{pr.get('number') or k} → {r['label']}  {r['donor']}")
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
