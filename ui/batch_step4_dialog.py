"""
block01/ui/batch_step4_dialog.py — Batch Step 4 (Cell Feature Extraction).

Auto-scan a root directory for finished samples, present them in an editable
table (path cells get a "..." browse button), import/export the plan as CSV,
then run FeatureExtractWorker sequentially over the checked rows. A failing
sample is recorded and the batch continues with the next one.

The discovery helpers (discover_samples / _find_ome_tiff / _find_mask) are
plain os/glob functions with no Qt dependency so they can be unit-tested
headlessly.
"""

import csv
import glob
import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QCheckBox, QWidget, QFileDialog,
    QMessageBox, QStyledItemDelegate, QHeaderView, QGroupBox,
)

from ..config import OUTPUT_DIR
from ..core.bg_correction import _load_correction_config
from ..core.io_loader import OMETIFFLoader
from ..workers.feature_extract_worker import FeatureExtractWorker

# ── column layout ──────────────────────────────────────────────────────
COL_CHECK      = 0
COL_SAMPLE     = 1
COL_OME_TIFF   = 2
COL_MASK       = 3
COL_OUTPUT_DIR = 4
COL_PREFIX     = 5
COL_STATUS     = 6

_COLUMNS = [
    ("☑",            30),
    ("Sample Name",  200),
    ("OME-TIFF",     300),
    ("Mask",         300),
    ("Output Dir",   300),
    ("File Prefix",  150),
    ("Status",        90),
]

_PATH_COLS = (COL_OME_TIFF, COL_MASK, COL_OUTPUT_DIR)

_BG_BAD     = QColor(80, 20, 20)
_BG_CLEAR   = QColor(0, 0, 0, 0)
_BG_DONE    = QColor(20, 60, 20)
_BG_RUNNING = QColor(60, 60, 20)

_NOT_FOUND = "(not found)"


# ══════════════════════════════════════════════════════════════════════
#  Discovery (pure — no Qt)
# ══════════════════════════════════════════════════════════════════════

def _find_ome_tiff(sample_dir):
    """Find the original image under sample_dir/Scan*/ (.ome.tiff/.qptiff)."""
    for scan in sorted(glob.glob(os.path.join(sample_dir, "Scan*"))):
        if not os.path.isdir(scan):
            continue
        for f in sorted(os.listdir(scan)):
            if f.endswith(('.ome.tiff', '.ome.tif', '.qptiff')):
                return os.path.join(scan, f)
    return None


def _find_mask(seg_run_dir):
    """Find a segmentation mask file inside a seg_run directory."""
    for candidate in ("global_mask.ome.tiff", "global_mask_ROI_1.ome.tiff",
                      "global_mask.dat"):
        p = os.path.join(seg_run_dir, candidate)
        if os.path.exists(p):
            return p
    return None


def discover_samples(root_dir):
    """Scan ``root_dir`` for finished samples ready for feature extraction.

    Returns a list of dicts, one per sample directory that has at least one
    segmentation run. Missing inputs are reported as ``"(not found)"`` and the
    row is left unchecked; already-extracted samples are flagged ``done ✓`` and
    also unchecked (re-running is an explicit user choice).
    """
    results = []
    if not root_dir or not os.path.isdir(root_dir):
        return results

    for sample_dir in sorted(os.listdir(root_dir)):
        full = os.path.join(root_dir, sample_dir)
        if not os.path.isdir(full):
            continue

        ome_tiff = _find_ome_tiff(full)

        seg_runs = sorted(glob.glob(os.path.join(
            full, "**", "segmentation_runs", "seg_*"), recursive=True))
        if not seg_runs:
            continue
        latest_seg = seg_runs[-1]

        mask = _find_mask(latest_seg)
        existing = glob.glob(os.path.join(latest_seg, "*_cell_features.h5ad"))

        results.append({
            "checked": mask is not None and ome_tiff is not None and not existing,
            "sample_name": sample_dir,
            "ome_tiff": ome_tiff or _NOT_FOUND,
            "mask": mask or _NOT_FOUND,
            "output_dir": latest_seg,
            "file_prefix": f"{sample_dir}_v10",
            "status": "done ✓" if existing else "pending",
            "ome_tiff_ok": ome_tiff is not None,
            "mask_ok": mask is not None,
        })
    return results


def _find_correction_config(output_dir):
    """Look for correction_config.json in output_dir, then a few parents."""
    cur = output_dir
    for _ in range(4):
        if not cur:
            break
        cfg = os.path.join(cur, "correction_config.json")
        if os.path.exists(cfg):
            try:
                return _load_correction_config(cfg)
            except Exception:
                return None
        parent = os.path.dirname(cur.rstrip(os.sep))
        if parent == cur:
            break
        cur = parent
    return None


# ══════════════════════════════════════════════════════════════════════
#  Path cell delegate (text + "..." browse button)
# ══════════════════════════════════════════════════════════════════════

class PathDelegate(QStyledItemDelegate):
    """Editor for a path column: a QLineEdit plus a "..." browse button."""

    def __init__(self, mode="file", parent=None):
        super().__init__(parent)
        self.mode = mode  # "file" or "dir"

    def createEditor(self, parent, option, index):
        editor = QWidget(parent)
        layout = QHBoxLayout(editor)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        line_edit = QLineEdit(editor)
        browse_btn = QPushButton("...", editor)
        browse_btn.setFixedWidth(30)
        layout.addWidget(line_edit)
        layout.addWidget(browse_btn)
        browse_btn.clicked.connect(lambda: self._browse(line_edit))
        editor.line_edit = line_edit
        editor.setFocusProxy(line_edit)
        return editor

    def _browse(self, line_edit):
        start = line_edit.text()
        if start == _NOT_FOUND:
            start = ""
        if self.mode == "file":
            path, _ = QFileDialog.getOpenFileName(
                None, "Select file", start,
                "Images (*.ome.tiff *.ome.tif *.qptiff *.dat *.tiff *.tif)")
        else:
            path = QFileDialog.getExistingDirectory(None, "Select directory", start)
        if path:
            line_edit.setText(path)

    def setEditorData(self, editor, index):
        editor.line_edit.setText(index.data() or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.line_edit.text())


# ══════════════════════════════════════════════════════════════════════
#  Batch dialog
# ══════════════════════════════════════════════════════════════════════

class BatchStep4Dialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch — Cell Feature Extraction")
        self.setModal(True)
        self.resize(1280, 640)

        self._loading = False          # suppress validation while populating
        self._current_worker = None
        self._batch_tasks = []
        self._batch_stats = []
        self._batch_idx = 0

        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Top: root directory + scan
        top = QHBoxLayout()
        top.addWidget(QLabel("Root dir:"))
        self._root_edit = QLineEdit()
        self._root_edit.setPlaceholderText("Directory containing one folder per sample")
        top.addWidget(self._root_edit, stretch=1)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_root)
        top.addWidget(btn_browse)
        btn_scan = QPushButton("Scan")
        btn_scan.clicked.connect(self._scan)
        top.addWidget(btn_scan)
        root.addLayout(top)

        # Middle: editable table
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in _COLUMNS])
        for col, (_label, width) in enumerate(_COLUMNS):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        for col in _PATH_COLS:
            mode = "dir" if col == COL_OUTPUT_DIR else "file"
            self.table.setItemDelegateForColumn(col, PathDelegate(mode, self))
        self.table.cellChanged.connect(self._on_cell_changed)
        root.addWidget(self.table, stretch=1)

        # Row operations
        ops = QHBoxLayout()
        btn_add = QPushButton("Add Row")
        btn_add.clicked.connect(self._add_empty_row)
        ops.addWidget(btn_add)
        btn_rm = QPushButton("Remove Selected")
        btn_rm.clicked.connect(self._remove_selected)
        ops.addWidget(btn_rm)
        ops.addSpacing(20)
        btn_imp = QPushButton("Import CSV")
        btn_imp.clicked.connect(self._import_csv_dialog)
        ops.addWidget(btn_imp)
        btn_exp = QPushButton("Export CSV")
        btn_exp.clicked.connect(self._export_csv_dialog)
        ops.addWidget(btn_exp)
        ops.addStretch()
        root.addLayout(ops)

        # Statistics
        stat_box = QGroupBox("Statistics (multi-select, at least one)")
        sl = QHBoxLayout(stat_box)
        self._stat_checkboxes = {}
        for key, label, default in (
            ("mean", "mean", True), ("sum", "sum", False),
            ("median", "median", False), ("std", "std", False),
        ):
            cb = QCheckBox(label)
            cb.setChecked(default)
            sl.addWidget(cb)
            self._stat_checkboxes[key] = cb
        sl.addStretch()
        root.addWidget(stat_box)

        # Bottom: run / close
        bottom = QHBoxLayout()
        self._run_btn = QPushButton("Run Batch")
        self._run_btn.setStyleSheet(
            "QPushButton{background:#2a5;color:white;border-radius:4px;"
            "padding:7px 22px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
            "QPushButton:disabled{background:#333;color:#555;}")
        self._run_btn.clicked.connect(self._run_batch)
        bottom.addWidget(self._run_btn)
        bottom.addStretch()
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        bottom.addWidget(self._close_btn)
        root.addLayout(bottom)

    # ── scanning ──────────────────────────────────────────────────────

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select root directory", self._root_edit.text() or OUTPUT_DIR)
        if d:
            self._root_edit.setText(d)

    def _scan(self):
        root_dir = self._root_edit.text().strip()
        if not root_dir or not os.path.isdir(root_dir):
            QMessageBox.warning(self, "Invalid directory",
                                "Please choose an existing root directory.")
            return
        samples = discover_samples(root_dir)
        self.table.setRowCount(0)
        for s in samples:
            self._add_row(
                checked=s["checked"], sample_name=s["sample_name"],
                ome_tiff=s["ome_tiff"], mask=s["mask"],
                output_dir=s["output_dir"], file_prefix=s["file_prefix"],
                status=s["status"],
            )
        self._validate_all()
        if not samples:
            QMessageBox.information(self, "Scan complete",
                                    "No samples with a segmentation run were found.")

    # ── table row management ──────────────────────────────────────────

    def _add_row(self, checked=True, sample_name="", ome_tiff="", mask="",
                 output_dir="", file_prefix="", status="pending"):
        self._loading = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)

            cb = QCheckBox()
            cb.setChecked(bool(checked))
            self.table.setCellWidget(row, COL_CHECK, cb)

            for col, value in (
                (COL_SAMPLE, sample_name), (COL_OME_TIFF, ome_tiff),
                (COL_MASK, mask), (COL_OUTPUT_DIR, output_dir),
                (COL_PREFIX, file_prefix),
            ):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

            status_item = QTableWidgetItem(str(status))
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            if "done" in status:
                status_item.setBackground(_BG_DONE)
            self.table.setItem(row, COL_STATUS, status_item)
        finally:
            self._loading = False
        self._validate_row(row)
        return row

    def _add_empty_row(self):
        self._add_row(checked=False)

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()},
                      reverse=True)
        for row in rows:
            self.table.removeRow(row)

    # ── validation ────────────────────────────────────────────────────

    def _on_cell_changed(self, row, col):
        if self._loading:
            return
        if col in _PATH_COLS:
            self._validate_row(row)

    def _validate_row(self, row):
        for col in _PATH_COLS:
            item = self.table.item(row, col)
            if item is None:
                continue
            path = item.text()
            ok = bool(path) and path != _NOT_FOUND and os.path.exists(path)
            item.setBackground(_BG_CLEAR if ok else _BG_BAD)

    def _validate_all(self):
        for row in range(self.table.rowCount()):
            self._validate_row(row)

    # ── CSV ───────────────────────────────────────────────────────────

    def _export_csv_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export batch CSV", "batch_step4.csv", "CSV (*.csv)")
        if path:
            self.export_csv(path)

    def _import_csv_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import batch CSV", "", "CSV (*.csv)")
        if path:
            self.import_csv(path)

    def export_csv(self, path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["checked", "sample_name", "ome_tiff", "mask",
                             "output_dir", "file_prefix"])
            for row in range(self.table.rowCount()):
                cb = self.table.cellWidget(row, COL_CHECK)
                writer.writerow([
                    "1" if (cb and cb.isChecked()) else "0",
                    self._cell_text(row, COL_SAMPLE),
                    self._cell_text(row, COL_OME_TIFF),
                    self._cell_text(row, COL_MASK),
                    self._cell_text(row, COL_OUTPUT_DIR),
                    self._cell_text(row, COL_PREFIX),
                ])

    def import_csv(self, path):
        self.table.setRowCount(0)
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row_data in reader:
                self._add_row(
                    checked=row_data.get("checked", "1") == "1",
                    sample_name=row_data.get("sample_name", ""),
                    ome_tiff=row_data.get("ome_tiff", ""),
                    mask=row_data.get("mask", ""),
                    output_dir=row_data.get("output_dir", ""),
                    file_prefix=row_data.get("file_prefix", ""),
                )
        self._validate_all()

    def _cell_text(self, row, col):
        item = self.table.item(row, col)
        return item.text() if item else ""

    # ── batch execution ───────────────────────────────────────────────

    def _run_batch(self):
        tasks = []
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, COL_CHECK)
            if not cb or not cb.isChecked():
                continue
            ome_tiff = self._cell_text(row, COL_OME_TIFF)
            mask = self._cell_text(row, COL_MASK)
            output_dir = self._cell_text(row, COL_OUTPUT_DIR)
            prefix = self._cell_text(row, COL_PREFIX)
            if not os.path.exists(ome_tiff) or not os.path.exists(mask):
                self._set_status(row, "error ✖ (path missing)")
                continue
            tasks.append({"row": row, "ome_tiff": ome_tiff, "mask": mask,
                          "output_dir": output_dir, "prefix": prefix})

        if not tasks:
            QMessageBox.warning(self, "No tasks", "No valid samples selected.")
            return

        stats = [s for s, cb in self._stat_checkboxes.items() if cb.isChecked()]
        if not stats:
            QMessageBox.warning(self, "No statistics",
                                "Select at least one statistic.")
            return

        self._run_btn.setEnabled(False)
        self._batch_tasks = tasks
        self._batch_stats = stats
        self._batch_idx = 0
        self._run_next()

    def _run_next(self):
        if self._batch_idx >= len(self._batch_tasks):
            self._run_btn.setEnabled(True)
            self._current_worker = None
            QMessageBox.information(
                self, "Done",
                f"Batch complete: {len(self._batch_tasks)} samples processed.")
            return

        task = self._batch_tasks[self._batch_idx]
        row = task["row"]
        self._set_status(row, "running...")

        try:
            loader = OMETIFFLoader(task["ome_tiff"])
            ch_names = loader.channel_names
        except Exception as e:
            self._set_status(row, f"error ✖ ({e})")
            self._advance()
            return

        correction_config = _find_correction_config(task["output_dir"])

        worker = FeatureExtractWorker(
            mask_path=task["mask"],
            ome_tiff_path=task["ome_tiff"],
            output_dir=task["output_dir"],
            channel_names=ch_names,
            statistics=self._batch_stats,
            file_prefix=task["prefix"],
            correction_config=correction_config,
        )
        worker.progress.connect(
            lambda cur, tot, msg, r=row: self._set_status(r, f"running... {msg}"))
        # FeatureExtractWorker emits finished(output_dir, base_name) on success.
        worker.finished.connect(lambda *_a, r=row: self._on_worker_done(r))
        worker.error.connect(lambda e, r=row: self._on_worker_error(r, e))

        self._current_worker = worker
        worker.start()

    def _advance(self):
        self._batch_idx += 1
        self._run_next()

    def _on_worker_done(self, row):
        self._set_status(row, "done ✓")
        self._advance()

    def _on_worker_error(self, row, err):
        self._set_status(row, "error ✖")
        # A failed sample never aborts the batch — move to the next one.
        self._advance()

    def _set_status(self, row, text):
        item = self.table.item(row, COL_STATUS)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, COL_STATUS, item)
        item.setText(text)
        if "error" in text:
            item.setBackground(_BG_BAD)
        elif "done" in text:
            item.setBackground(_BG_DONE)
        elif "running" in text:
            item.setBackground(_BG_RUNNING)
        else:
            item.setBackground(_BG_CLEAR)
