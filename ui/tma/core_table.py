"""The TMA page's core table (F3, docs/tma_f3_page_application.md §2.1):
one row per map position, then the cores without a number and the
unassigned pieces. Columns: Run (tick, F4's choice), Position, Donor,
Sample, Status (derived from the four separate marks), Pieces, Tissue.

A click on a row is reported (`row_clicked(kind, ident, ctrl)`) -- the page
centres the map on it, or, in the custom-target mode, appends the record;
the Run tick is reported as `run_toggled(position_key, bool)`.
"""

from __future__ import annotations

from typing import Dict, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

COLUMNS = ("Run", "Position", "Donor", "Sample", "Status", "Pieces", "Tissue")
FILTERS = ("All", "Open items", "Numbered", "Not numbered", "Missing", "Excluded", "Empty")
STATUS_COLOR = {"已对应": "#2e7d32", "待复核": "#b26a00", "未对应": "#c62828", "缺失": "#6a1b9a",
                "空位": "#757575"}
STATUS_EN = {"已对应": "Numbered", "待复核": "To confirm", "未对应": "Not numbered", "缺失": "Missing",
             "空位": "Empty", "排除": "Excluded"}


def status_en(text: str) -> str:
    return " · ".join(STATUS_EN.get(p, p) for p in text.split("·"))


class CoreTable(QWidget):
    row_clicked = pyqtSignal(str, str, bool)      # kind ("position" / "core" / "piece"), id, ctrl held
    run_toggled = pyqtSignal(str, bool)
    runs_set = pyqtSignal(list, bool)                 # Run all / Run none on the rows shown
    context_requested = pyqtSignal(str, str, object)   # kind, id, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        row.addWidget(self.filter)
        row.addWidget(QLabel("Find:"))
        self.find = QLineEdit()
        self.find.setPlaceholderText("position / donor / sample")
        row.addWidget(self.find, 1)
        # user ruling 2026-10-20: tick or untick Run for every core SHOWN (the
        # Show / Find filter narrows it), in one edit
        self.btn_run_all = QPushButton("Run all")
        self.btn_run_none = QPushButton("Run none")
        for b, on in ((self.btn_run_all, True), (self.btn_run_none, False)):
            b.setToolTip(("Tick" if on else "Untick") + " Run for every position shown in the list "
                         "(use Show / Find to narrow it first).")
            b.clicked.connect(lambda _c=False, on=on: self._set_shown_runs(on))
            row.addWidget(b)
        lay.addLayout(row)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("tma_core_table")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        self.counts = QLabel("")
        self.counts.setWordWrap(True)
        lay.addWidget(self.counts)
        self._rows: List[Dict] = []
        self._filling = False
        self.filter.currentIndexChanged.connect(self._apply_filter)
        self.find.textChanged.connect(self._apply_filter)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemChanged.connect(self._item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context)

    def set_rows(self, rows: List[Dict], counts_text: str, read_only: bool):
        """`rows`: kind, id, run (None = no tick), position, donor, sample,
        status (Chinese key from `layout.status_text` or an English text),
        pieces, tissue."""
        self._filling = True
        self._rows = list(rows)
        # one layout pass for the whole fill: with ResizeToContents every
        # setItem re-measured the columns over all rows (17 s for 300 rows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            run = QTableWidgetItem("")
            if r["run"] is not None:
                flags = Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
                if not read_only:
                    flags |= Qt.ItemIsEnabled
                run.setFlags(flags)
                run.setCheckState(Qt.Checked if r["run"] else Qt.Unchecked)
            else:
                run.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(i, 0, run)
            status = r["status"]
            cells = (r["position"], r["donor"], r["sample"], status_en(status), r["pieces"], r["tissue"])
            for j, v in enumerate(cells, start=1):
                it = QTableWidgetItem("" if v is None else str(v))
                if j == 4:
                    key = status.split("·")[0]
                    if key in STATUS_COLOR:
                        it.setForeground(QColor(STATUS_COLOR[key]))
                self.table.setItem(i, j, it)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)
        self.counts.setText(counts_text)
        self._read_only = bool(read_only)
        for b in (self.btn_run_all, self.btn_run_none):
            b.setEnabled(not read_only)
        self._filling = False
        self._apply_filter()

    def select_ident(self, ident: str):
        for i, r in enumerate(self._rows):
            if r["id"] == ident:
                self.table.blockSignals(True)
                self.table.selectRow(i)
                self.table.blockSignals(False)
                self.table.scrollToItem(self.table.item(i, 1))
                return

    def _apply_filter(self):
        f = self.filter.currentText()
        q = self.find.text().strip().lower()
        for i, r in enumerate(self._rows):
            st = r["status"]
            show = {"All": True,
                    "Open items": r["kind"] != "position" or st.split("·")[0] in ("未对应", "待复核"),
                    "Numbered": st.startswith("已对应") or st.startswith("待复核"),
                    "Not numbered": st.startswith("未对应") or r["kind"] != "position",
                    "Missing": st.startswith("缺失"),
                    "Excluded": "排除" in st,
                    "Empty": st == "空位"}[f]
            if q and not any(q in str(r.get(k) or "").lower() for k in ("position", "donor", "sample")):
                show = False
            self.table.setRowHidden(i, not show)

    def _clicked(self, row, col):
        if col == 0 or not (0 <= row < len(self._rows)):
            return
        from PyQt5.QtWidgets import QApplication
        ctrl = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
        r = self._rows[row]
        self.row_clicked.emit(r["kind"], r["id"], ctrl)

    def _context(self, pos):
        row = self.table.rowAt(pos.y())
        if 0 <= row < len(self._rows):
            r = self._rows[row]
            self.context_requested.emit(r["kind"], r["id"], self.table.viewport().mapToGlobal(pos))

    def _set_shown_runs(self, on):
        """Run on / off for every tickable position row the filter shows."""
        if getattr(self, "_read_only", False):
            return
        keys = [r["id"] for i, r in enumerate(self._rows)
                if r["run"] is not None and not self.table.isRowHidden(i) and bool(r["run"]) != on]
        if keys:
            self.runs_set.emit(keys, bool(on))

    def _item_changed(self, item):
        if self._filling or item.column() != 0:
            return
        r = self._rows[item.row()]
        if r["run"] is None:
            return
        self.run_toggled.emit(r["id"], item.checkState() == Qt.Checked)
