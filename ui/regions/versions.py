"""The Regions page's versions and lock (plan §3): the title line, the
`Version` box (draft, locked versions, F3's read-only versions), `Discard
draft`, `Lock` / `New draft from this version` and the two-tick lock
dialog. A mixin of `RegionsPage`: one document, one version line.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime

from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QMessageBox, QVBoxLayout

from ...core.regions import document as D
from ...core.regions import lock as K

LIST_MAX = 25


class VersionsMixin:

    def _refresh_title(self):
        st = self.cores.study
        slide = os.path.basename(self.ctx.get("slide_path") or "")
        name = (st["map_sheet"].get("block_name") or "TMA") if st else "no map sheet"
        self.lbl_study.setText(f"Slide: {slide}   Study: {name}" if slide else "No dataset: Save in Step0 first")
        self.version_box.blockSignals(True)
        self.version_box.clear()
        pd = self.ctx.get("project_dir")
        if pd:
            from ...core.regions import migrate as M
            if self.view_version is None or os.path.isfile(K.draft_path(pd)):
                base = self.holder.d.get("based_on") if (self.holder and self.view_version is None) else None
                self.version_box.addItem("Draft" + (f" (based on v{base})" if base else ""), None)
            for v in K.locked_versions(pd):
                self.version_box.addItem(f"v{v} (locked)", ("regions", v))
            for v in M.f3_locked_versions(pd):
                self.version_box.addItem(f"F3 v{v} (read-only)", ("f3", v))
            for i in range(self.version_box.count()):
                if self.version_box.itemData(i) == self.view_version:
                    self.version_box.setCurrentIndex(i)
        self.version_box.blockSignals(False)

    # ── versions, lock ──
    def _version_chosen(self, index):
        if not self.ctx.get("project_dir"):
            return
        which = self.version_box.itemData(index)
        if which is None:
            from ...core.regions import migrate as M
            try:
                draft = M.migrate_draft(self.ctx["project_dir"], self.shape(), self.ctx["slide_id"])
            except D.RegionsError as exc:
                self.say(str(exc))
                draft = None
            if draft is not None:
                self._open_draft(draft)
        else:
            self.flush_draft()
            self._open_locked(which)
        self.selected = None
        self.ctl.cancel()
        self.refresh()

    def _discard_draft(self):
        if QMessageBox.question(self, "Discard draft", "Delete the draft? Locked versions are kept.") != QMessageBox.Yes:
            return
        self._save_timer.stop()
        K.discard_draft(self.ctx["project_dir"])
        self.holder = None
        versions = K.locked_versions(self.ctx["project_dir"])
        if versions:
            self._open_locked(("regions", versions[-1]))
        else:
            self._open_draft(D.new_document(self.ctx["slide_id"], self.shape()))
        self.refresh()

    def _lock_or_branch(self):
        if self.holder is None:
            return
        pd = self.ctx["project_dir"]
        if self.holder.read_only:
            if K.load_draft(pd) is not None and QMessageBox.question(
                    self, "New draft", "A draft exists. Replace it with a new draft from this version?") != QMessageBox.Yes:
                return
            kind, n = self.view_version
            if kind == "regions":
                try:
                    d = K.draft_from_locked(K.load_locked(pd, n, self.ctx["slide_id"]))
                except (D.RegionsError, ValueError, OSError) as exc:
                    self.say(f"Version {n} cannot be opened: {exc}")
                    return
            else:
                d = copy.deepcopy(self.holder.d)          # an F3 version read as a document
                d["based_on"] = None
            self._open_draft(d)
            self.refresh()
            return
        self.flush_draft()
        try:
            problems = K.lock_problems(self.holder.d, self.cores.study, self.ctx.get("channel_names"))
        except D.RegionsError as exc:
            problems = [str(exc)]
        if self.cores.study is not None and not self.holder.d.get("map_sha256"):
            problems.insert(0, "the map sheet is chosen but the cores are not found yet: Find cores first")
        if problems:
            more = f"\n… and {len(problems) - LIST_MAX} more" if len(problems) > LIST_MAX else ""
            QMessageBox.warning(self, "Cannot lock yet", "\n".join(problems[:LIST_MAX]) + more)
            return
        if not self._confirm_coverage():
            return
        self.holder.d["coverage_confirmed"] = datetime.now().isoformat(timespec="seconds")
        try:
            rec = K.lock(pd, self.holder.d, self.cores.study, self.ctx.get("channel_names"))
        except Exception as exc:                            # noqa: BLE001
            self.holder.d["coverage_confirmed"] = None
            QMessageBox.warning(self, "Lock", str(exc))
            return
        K.discard_draft(pd)
        self._open_locked(("regions", rec["version"]))
        self.say(f"Locked v{rec['version']}")
        self.refresh()

    def _confirm_coverage(self) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Lock")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Before locking, confirm:"))
        a = QCheckBox("The numbering matches the map sheet (orientation checked)")
        b = QCheckBox("Every outline covers its tissue (coverage checked)")
        lay.addWidget(a)
        lay.addWidget(b)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = box.button(QDialogButtonBox.Ok)
        ok.setEnabled(False)
        for c in (a, b):
            c.toggled.connect(lambda _v: ok.setEnabled(a.isChecked() and b.isChecked()))
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        lay.addWidget(box)
        return dlg.exec_() == QDialog.Accepted

