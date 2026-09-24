"""The Methods part of the Pre-segmentation tab (plan block B, 4.1).

A `+` that opens the method editor; one block per method -- its name, a
summary of its values and its combination count, `Edit` and `×`; the live
total `k patches × m combinations = N tasks`; `Save plan` and `Load plan…`
(the host does the file work, `plan_store`). Nothing here computes anything:
running is block C.

Same-name methods (R9, revised by the user 2026-09-24): adding a method that
already has a block asks whether to merge. Yes -> into the first block of
that method: every list parameter takes the union of both sides and a
single-value parameter that differs is decided by the user, all conflicts in
one dialog; No -> the new values are KEPT as a block of their own (the first
ruling cancelled the addition). No filtering or de-duplication of the
expanded combinations. `Edit` may change a block's method; a change onto a
method another block has asks the same question.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from ...utils import segmentation_param_schema as ps
from .method_editor import MethodEditorDialog


def block_frame_qss(name, color="#555"):
    """The grey frame round each block -- a Methods block and a Results box
    alike (user ruling 2026-09-25: the boxes show their edges the same way)."""
    return f"QFrame#{name}{{border:1px solid {color};border-radius:4px;}}"


class MethodBlock(QtWidgets.QFrame):
    edit_clicked = pyqtSignal(int)            # block uid
    remove_clicked = pyqtSignal(int)          # block uid
    _next_uid = 1

    def __init__(self, method, values, parent=None):
        super().__init__(parent)
        self.uid = MethodBlock._next_uid
        MethodBlock._next_uid += 1
        self.method = method
        self.setObjectName("methodBlock")
        self.setStyleSheet(block_frame_qss("methodBlock"))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)
        head = QtWidgets.QHBoxLayout()
        # The title wraps rather than widening the channel column; the count
        # has its own line under it, so neither is ever cut by the other.
        self.title = QtWidgets.QLabel(ps.display_name(method), self)
        self.title.setStyleSheet("font-weight:bold;font-size:11px;")
        self.title.setWordWrap(True)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        head.addWidget(self.title, 1)
        self.btn_edit = QtWidgets.QPushButton("Edit", self)
        self.btn_edit.setMinimumWidth(44)
        self.btn_remove = QtWidgets.QPushButton("×", self)
        self.btn_remove.setFixedWidth(24)
        for b in (self.btn_edit, self.btn_remove):
            b.setStyleSheet("font-size:11px;")
            head.addWidget(b)
        lay.addLayout(head)
        self.count = QtWidgets.QLabel("", self)
        self.count.setStyleSheet("color:#bbb;font-size:10px;")
        lay.addWidget(self.count)
        self.summary = QtWidgets.QLabel("", self)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#aaa;font-size:10px;")
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        lay.addWidget(self.summary)
        self.btn_edit.clicked.connect(lambda: self.edit_clicked.emit(self.uid))
        self.btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self.uid))
        self.set_values(values)

    def set_method(self, method, values):
        self.method = method
        self.title.setText(ps.display_name(method))
        self.set_values(values)

    def set_values(self, values):
        self.values = {k: list(v) for k, v in values.items()}
        n = ps.combo_count(self.method, self.values)
        self.count.setText(f"{n} combination{'s' if n != 1 else ''}")
        self.summary.setText(ps.summary(self.method, self.values))


class ConflictDialog(QtWidgets.QDialog):
    """Every single-value parameter that differs, each decided here (R9)."""

    def __init__(self, method, conflicts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge: choose one value")
        form = QtWidgets.QFormLayout(self)
        form.addRow(QtWidgets.QLabel(
            f"{ps.display_name(method)} already has a block. These parameters take "
            "one value only; choose which to keep.", self))
        by_key = {s.key: s for s in ps.specs(method)}
        self._choices = {}
        for key, (old, new) in conflicts.items():
            spec = by_key[key]
            combo = QtWidgets.QComboBox(self)
            combo.addItem(f"{ps.format_values(spec, [old])}  (current)", old)
            combo.addItem(f"{ps.format_values(spec, [new])}  (new)", new)
            form.addRow(spec.label + ":", combo)
            self._choices[key] = combo
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def choice(self, key):
        return self._choices[key]

    def chosen(self):
        return {k: c.currentData() for k, c in self._choices.items()}


class MethodsPanel(QtWidgets.QWidget):
    plan_changed = pyqtSignal()
    save_plan_requested = pyqtSignal()
    load_plan_requested = pyqtSignal()
    run_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    #: Seams for the dialogs (tests drive them; the product opens them).
    editor_class = MethodEditorDialog
    conflict_class = ConflictDialog

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self._blocks = []                         # in the order they were added
        self._n_patches = 0
        self._running = False
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(4)

        title = QtWidgets.QLabel("Methods", self)
        title.setStyleSheet("font-weight:bold;color:#ccc;font-size:11px;")
        outer.addWidget(title)
        self._list = QtWidgets.QVBoxLayout()
        self._list.setSpacing(4)
        outer.addLayout(self._list)
        self._empty = QtWidgets.QLabel("No method yet — add one with +", self)
        self._empty.setStyleSheet("color:#888;font-size:11px;")
        self._empty.setWordWrap(True)
        outer.addWidget(self._empty)

        # `+` on a line of its own, the plan buttons under it (plan 4.1): side
        # by side the three would widen the channel column.
        row_add = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("+", self)
        self.btn_add.setFixedWidth(28)
        self.btn_add.setStyleSheet("font-size:11px;")
        row_add.addWidget(self.btn_add)
        row_add.addStretch(1)
        outer.addLayout(row_add)
        row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save plan", self)
        self.btn_load = QtWidgets.QPushButton("Load plan…", self)
        for b in (self.btn_save, self.btn_load):
            b.setStyleSheet("font-size:11px;")
            row.addWidget(b)
        row.addStretch(1)
        outer.addLayout(row)
        self.total = QtWidgets.QLabel("", self)
        self.total.setStyleSheet("color:#bbb;font-size:11px;")
        self.total.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        outer.addWidget(self.total)
        # Run / Stop under the total they act on (plan block C, step 4).
        row_run = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("Run", self)
        self.btn_stop = QtWidgets.QPushButton("Stop", self)
        for b in (self.btn_run, self.btn_stop):
            b.setMinimumWidth(44)
            b.setStyleSheet("font-size:11px;")
            row_run.addWidget(b)
        row_run.addStretch(1)
        outer.addLayout(row_run)
        self.progress = QtWidgets.QLabel("", self)
        self.progress.setStyleSheet("color:#bbb;font-size:11px;")
        self.progress.setWordWrap(True)
        self.progress.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        outer.addWidget(self.progress)

        self.btn_run.clicked.connect(self.run_requested.emit)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_add.clicked.connect(self.add_method)
        self.btn_save.clicked.connect(self.save_plan_requested.emit)
        self.btn_load.clicked.connect(self.load_plan_requested.emit)
        self._refresh()

    # ── model ────────────────────────────────────────────────────────
    def methods(self):
        """[{"method", "values"}] in block order -- what a plan stores."""
        return [{"method": b.method, "values": {k: list(v) for k, v in b.values.items()}}
                for b in self._blocks]

    def set_methods(self, entries):
        """Replace every block (loading a plan). Unknown or hidden methods are
        skipped; returns how many were."""
        for b in list(self._blocks):
            self._drop_block(b)
        skipped = 0
        for e in entries or []:
            method = e.get("method")
            if method not in ps.UI_METHODS:
                skipped += 1
                continue
            vals = ps.default_values(method)
            vals.update({k: list(v) for k, v in (e.get("values") or {}).items() if k in vals})
            self._add_block(method, vals)
        self._refresh()
        self.plan_changed.emit()
        return skipped

    def blocks(self):
        return list(self._blocks)

    def block(self, method):
        """The first block of `method`, or None."""
        return next((b for b in self._blocks if b.method == method), None)

    def blocks_of(self, method):
        return [b for b in self._blocks if b.method == method]

    def block_by_uid(self, uid):
        return next((b for b in self._blocks if b.uid == uid), None)

    def total_combinations(self):
        return sum(ps.combo_count(b.method, b.values) for b in self._blocks)

    def set_patch_count(self, n):
        self._n_patches = int(n)
        self._refresh()

    def task_count(self):
        return self._n_patches * self.total_combinations()

    def set_running(self, running):
        """While a run is going, Run is off and Stop on -- said, not silent."""
        self._running = bool(running)
        self._refresh()

    def set_progress(self, text):
        self.progress.setText(text)

    # ── gestures ─────────────────────────────────────────────────────
    def add_method(self):
        dlg = self.editor_class(self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return self.adopt(dlg.method(), dlg.values())

    def adopt(self, method, values):
        """Put `method` with `values` in the plan: a new block, or -- when the
        method already has one and the user says so -- a merge into it (R9).
        Returns the block that holds the values, or None if cancelled."""
        existing = self.block(method)
        if existing is not None:
            target = self._merge_into(existing, method, values)
            if target is not False:
                if target is not None:
                    self._refresh()
                    self.plan_changed.emit()
                return target
        blk = self._add_block(method, values)
        self._refresh()
        self.plan_changed.emit()
        return blk

    def _merge_into(self, existing, method, values):
        """Ask; Yes -> merge into `existing` and return it; No -> False (keep
        a block of its own); a cancelled conflict choice -> None."""
        answer = QtWidgets.QMessageBox.question(
            self, "Same method",
            f"{ps.display_name(method)} is already in the plan. Merge the new "
            "values into it? (No keeps them as a block of their own.)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.Yes)
        if answer != QtWidgets.QMessageBox.Yes:
            return False
        merged, conflicts = ps.merge(method, existing.values, values)
        if conflicts:
            dlg = self.conflict_class(method, conflicts, self)
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return None
            for key, value in dlg.chosen().items():
                merged[key] = [value]
        existing.set_values(merged)
        return existing

    def _edit(self, uid):
        blk = self.block_by_uid(uid)
        if blk is None:
            return
        # The method may be changed too (user ruling, 2026-09-24).
        dlg = self.editor_class(self, method=blk.method, values=blk.values, lock_method=False)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        method, values = dlg.method(), dlg.values()
        if method != blk.method:
            other = next((b for b in self.blocks_of(method) if b is not blk), None)
            if other is not None:
                target = self._merge_into(other, method, values)
                if target is None:
                    return
                if target is not False:          # merged into the other block
                    self._drop_block(blk)
                    self._refresh()
                    self.plan_changed.emit()
                    return
            blk.set_method(method, values)
        else:
            blk.set_values(values)
        self._refresh()
        self.plan_changed.emit()

    def _remove(self, uid):
        blk = self.block_by_uid(uid)
        if blk is not None:
            self._drop_block(blk)
            self._refresh()
            self.plan_changed.emit()

    # ── internals ────────────────────────────────────────────────────
    def _add_block(self, method, values):
        blk = MethodBlock(method, values, self)
        blk.edit_clicked.connect(self._edit)
        blk.remove_clicked.connect(self._remove)
        self._list.addWidget(blk)
        self._blocks.append(blk)
        return blk

    def _drop_block(self, blk):
        self._blocks.remove(blk)
        self._list.removeWidget(blk)
        blk.setParent(None)
        blk.deleteLater()

    def _refresh(self):
        self._empty.setVisible(not self._blocks)
        self.btn_save.setEnabled(bool(self._blocks))
        self.btn_run.setEnabled(not self._running and self.task_count() > 0)
        self.btn_stop.setEnabled(self._running)
        m = self.total_combinations()
        n = self._n_patches
        self.total.setText(
            f"Total: {n} patch{'es' if n != 1 else ''} × {m} combination"
            f"{'s' if m != 1 else ''} = {n * m} task{'s' if n * m != 1 else ''}")
