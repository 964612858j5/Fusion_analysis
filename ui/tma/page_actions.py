"""What the TMA page's buttons and menus do (F3,
docs/tma_f3_page_application.md §1-§3): confirm a numbering, number one
core, the piece / position menus, stepping through cores, Open in Step1,
the map sheet, Find cores, versions and the lock. A mixin of `TmaPage`;
every edit goes through `self.edit` (one undo step).
"""

from __future__ import annotations

from datetime import datetime

from PyQt5.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLabel,
                             QMenu, QMessageBox, QVBoxLayout)

from ...core.tma import layout as L
from ...core.tma import layout_lock as K
from . import workers

LIST_MAX = 25


class PageActions:

    # ── numbering ──
    def _confirm_numbering(self):
        plan = self.current_plan()
        if not plan["ok"]:
            self.say("; ".join(plan["problems"]))
            return
        if plan["conflicts"]:
            k = len(plan["conflicts"])
            if QMessageBox.question(self, "Rebind", f"{k} conflicting number(s) will be rebound; "
                                    "the cores pushed off keep their outlines. Continue?") != QMessageBox.Yes:
                return
        extra = {}
        if self.ctl.line is not None:
            extra = {"line": [list(self.ctl.line[0]), list(self.ctl.line[1])], "band": self.ctl.band}
        method = "line" if self.ctl.tool == "line" else "click"
        if self.edit(L.apply_binding, plan, method, rebind=bool(plan["conflicts"]), extra=extra) is not None:
            self.ctl.cancel()

    def _record_from_text(self, text):
        t = (text or "").strip().lower()
        if not t:
            return None
        recs = [r for r in self._all_records() if r["donor"]]
        hit = [r for r in recs if t.split()[0] == r["label"].lower()]
        if not hit:
            hit = [r for r in recs if t == r["donor"].lower() or t == r["sample"].lower()]
        return hit[0] if len(hit) == 1 else None

    def _bind_single(self, text):
        obj = self._selected_object()
        if obj is None:
            self.say("Select a core on the map first.")
            return
        rec = self._record_from_text(text)
        if rec is None:
            self.say(f"'{text}' is not one record of the map sheet (use Custom for a number not on it).")
            return
        rec = L.records_by_ids(self.study, [rec["position_id"]])[0]
        plan = L.plan_binding(self.holder.d, [obj], [rec])
        if not plan["ok"]:
            self.say("; ".join(plan["problems"]))
            return
        held = [c for c in plan["conflicts"] if c["kind"] == "record_held"]
        own = self.holder.d["cores"].get(plan["pairs"][0]["core"] or "", {}).get("position_key")
        preview = f"Selected core → {rec['label']}  {rec['donor']}"
        self.numbering.show_plan(plan, active=False)
        if not plan["conflicts"]:
            if QMessageBox.question(self, "Number", preview + "\n\nConfirm?") != QMessageBox.Yes:
                return
        else:
            box = QMessageBox(self)
            box.setWindowTitle("Number taken")
            box.setText(f"{preview}\n\nThis conflicts with another core's number. Rebind it (the other core "
                        "becomes not numbered, its outline kept) or exchange the two numbers?")
            rebind = box.addButton("Rebind", QMessageBox.AcceptRole)
            swap = box.addButton("Exchange", QMessageBox.ActionRole) if (held and own) else None
            box.addButton(QMessageBox.Cancel)
            box.exec_()
            if box.clickedButton() is swap and swap is not None:
                self.edit(L.swap, plan["pairs"][0]["core"], held[0]["core"])
                return
            if box.clickedButton() is not rebind:
                return
        self.edit(L.apply_binding, plan, "single", rebind=bool(plan["conflicts"]))

    def _custom_single(self, text):
        obj = self._selected_object()
        if obj is None:
            self.say("Select a core on the map first.")
            return
        self.edit(L.set_custom, obj, text)

    def _selected_object(self):
        if self.holder is None or self.selected is None:
            return None
        p = self.holder.d["pieces"].get(self.selected)
        if p is None or p["dropped"]:
            return None
        return p["owner"] or self.selected

    # ── table ──
    def _row_clicked(self, kind, ident, ctrl):
        if kind == "position" and self.numbering.order_key() == "custom" and ctrl:
            rec = self._labels_by_key().get(ident)
            if rec and rec["donor"]:
                self.numbering.add_custom(rec["position_id"])
            return
        d = self.holder.d if self.holder else None
        if d is None:
            return
        uid = None
        if kind == "position":
            uid = next((u for u, c in d["cores"].items() if c["position_key"] == ident), None)
        elif kind == "core":
            uid = ident
        if uid is not None:
            self.selected = d["cores"][uid]["pieces"][0]
            cy, cx = L.core_centre(d, uid)
        elif kind == "piece":
            self.selected = ident
            cy, cx = d["pieces"][ident]["cy"], d["pieces"][ident]["cx"]
        else:
            return
        self.map.centre_on(cy, cx, 3 * self.core_diameter())
        self.refresh_map()

    def _run_toggled(self, key, on):
        def tick(d, k, v):
            d["positions"][k]["run"] = bool(v)
        self.edit(tick, key, on)

    def _row_menu(self, kind, ident, pos):
        if self.holder is None or self.holder.read_only or kind != "position":
            return
        d = self.holder.d
        st = d["positions"][ident]
        rec = self._labels_by_key().get(ident, {})
        if not rec.get("donor"):
            return
        m = QMenu(self)
        bound = any(c["position_key"] == ident for c in d["cores"].values())
        if not bound:
            if st["observed"] == "missing":
                m.addAction("Clear 'missing'", lambda: self.edit(L.set_missing, ident, False))
            else:
                m.addAction("Confirm missing…", lambda: self._ask_missing(ident))
        if st["qc"] == "excluded":
            m.addAction("Include again", lambda: self.edit(L.set_qc, ident, "unreviewed"))
        else:
            m.addAction("Exclude…", lambda: self._ask_exclude(ident))
            m.addAction("Accept", lambda: self.edit(L.set_qc, ident, "accepted"))
        m.exec_(pos)

    def _ask_missing(self, key):
        reason, ok = QInputDialog.getText(self, "Confirm missing", "Reason (optional):")
        if ok:
            self.edit(L.set_missing, key, True, reason)

    def _ask_exclude(self, key):
        reason, ok = QInputDialog.getText(self, "Exclude", "Reason(s), comma-separated:")
        if ok and reason.strip():
            self.edit(L.set_qc, key, "excluded", [r.strip() for r in reason.split(",") if r.strip()])

    # ── map menu ──
    def piece_menu(self, pid, screen_pos):
        if self.holder is None or self.holder.read_only:
            return
        d = self.holder.d
        p = d["pieces"][pid]
        m = QMenu(self)
        if p["dropped"]:
            m.addAction("Restore", lambda: self.edit(L.restore_piece, pid))
        else:
            if p["owner"] is not None:
                c = d["cores"][p["owner"]]
                if len(c["pieces"]) > 1:
                    m.addAction("Remove from core", lambda: self.edit(L.unmerge, pid))
                if c["mapping"] == "review":
                    m.addAction("Confirm number", lambda: self.edit(L.confirm_number, p["owner"]))
                if c["position_key"]:
                    m.addAction("Remove number", lambda: self.edit(L.unbind, p["owner"]))
            m.addAction("Leave out…", lambda: self._ask_drop(pid))
        m.exec_(screen_pos.toPoint() if hasattr(screen_pos, "toPoint") else screen_pos)

    def _ask_drop(self, pid):
        reason, ok = QInputDialog.getText(self, "Leave out", "Reason:", text="not tissue")
        if ok:
            self.edit(L.drop_piece, pid, reason or "left out")

    # ── stepping, Step1 ──
    def _bound_in_order(self):
        recs = self._labels_by_key()
        d = self.holder.d
        out = [(recs[c["position_key"]]["position_id"], u) for u, c in d["cores"].items()
               if c["position_key"] in recs]
        return [u for _pid, u in sorted(out)]

    def step_core(self, delta):
        if self.holder is None:
            return
        order = self._bound_in_order()
        if not order:
            return
        cur = self.selected_core()
        i = order.index(cur) + delta if cur in order else (0 if delta > 0 else len(order) - 1)
        uid = order[i % len(order)]
        self.select(self.holder.d["cores"][uid]["pieces"][0])
        cy, cx = L.core_centre(self.holder.d, uid)
        self.map.centre_on(cy, cx, 3 * self.core_diameter())

    def _open_step1(self):
        uid = self.selected_core()
        if uid is None:
            self.say("Select a core first.")
            return
        from ...core.tma.layout_geometry import core_geometry
        g = core_geometry([self.holder.d["pieces"][p] for p in self.holder.d["cores"][uid]["pieces"]], self.shape)
        self.open_in_step1.emit(list(g["bbox_fullres"]))

    # ── study, cores ──
    def _choose_map(self):
        from ...core.tma.map_sheet import MapSheetError
        from ...core.tma.study import StudyError, create_study
        path, _ = QFileDialog.getOpenFileName(self, "Choose the TMA map sheet", "", "Excel (*.xlsx)")
        if not path:
            return
        try:
            create_study(self.project_dir, self.slide_path, path)
        except (MapSheetError, StudyError, ValueError) as exc:
            QMessageBox.warning(self, "Map sheet", str(exc))
            return
        self._load_project()

    def _find_cores(self):
        if self.study is None or self.nuc_channel is None:
            self.say("Finding cores needs the map sheet and a channel named DAPI.")
            return
        kw = {"channel": self.nuc_channel}
        mm = self.study["map_sheet"].get("diameter_mm")
        if self.pixel_um:
            if not mm:
                mm, ok = QInputDialog.getDouble(self, "Core diameter", "Core diameter (mm):", 1.0, 0.05, 10.0, 2)
                if not ok:
                    return
            kw["core_diameter_um"] = float(mm) * 1000.0
        else:
            px, ok = QInputDialog.getDouble(self, "Core diameter", "The slide has no pixel size. Core diameter "
                                            "in level-0 pixels:", 2000.0, 10.0, 1e6, 0)
            if not ok:
                return
            kw["core_diameter_px"] = float(px)
        self._detecting = True
        self.say("Finding cores…")
        self._sync_enabled()
        w = workers.keep(workers.DetectWorker(self._gen, self.project_dir, self.slide_path,
                                              self.study["slide_id"], kw))
        w.done.connect(self._detect_done)
        w.failed.connect(self._detect_failed)
        w.start()

    def _detect_done(self, gen, rec):
        if gen != self._gen:
            return
        self._detecting = False
        self.candidates = rec
        n = len(rec["candidates"])
        has_work = K.load_draft(self.project_dir) is not None       # any saved draft, edited or not
        if has_work and QMessageBox.question(
                self, "New candidates", f"Found {n} candidates (v{rec['version']}). Start a new draft from "
                "them? The current draft is replaced.") != QMessageBox.Yes:
            self.refresh()
            return
        self._open_draft(L.new_draft(self.study, rec, based_on=None))
        self.say(f"Candidates v{rec['version']}: {n} found")
        self.refresh()

    def _detect_failed(self, gen, msg):
        if gen != self._gen:
            return
        self._detecting = False
        self.say(f"Finding cores failed: {msg}")
        self._sync_enabled()

    # ── versions, lock ──
    def _version_chosen(self, index):
        if getattr(self, "study_mismatch", False) or not self.project_dir:
            return
        v = self.version_box.itemData(index)
        if v is None:
            draft = K.load_draft(self.project_dir)
            if draft is not None:
                self._open_draft(draft)
        else:
            self._open_locked(v)
        self.selected = None
        self.ctl.cancel()
        self.refresh()

    def _discard_draft(self):
        if QMessageBox.question(self, "Discard draft", "Delete the draft? Locked versions are kept.") != QMessageBox.Yes:
            return
        self._save_timer.stop()
        K.discard_draft(self.project_dir)
        self.holder = None
        versions = K.locked_versions(self.project_dir)
        if versions:
            self._open_locked(versions[-1])
        self.refresh()

    def _lock_or_branch(self):
        if self.holder is None:
            return
        if self.holder.read_only:
            if K.load_draft(self.project_dir) is not None and QMessageBox.question(
                    self, "New draft", "A draft exists. Replace it with a new draft from this version?") != QMessageBox.Yes:
                return
            self._open_draft(K.draft_from_locked(K.load_locked(self.project_dir, self.view_version)))
            self.refresh()
            return
        self.flush_draft()
        problems = K.lock_problems(self.holder.d, self.study, self.shape)
        if problems:
            more = f"\n… and {len(problems) - LIST_MAX} more" if len(problems) > LIST_MAX else ""
            QMessageBox.warning(self, "Cannot lock yet", "\n".join(problems[:LIST_MAX]) + more)
            return
        if not self._confirm_coverage():
            return
        self.holder.d["coverage_confirmed"] = datetime.now().isoformat(timespec="seconds")
        try:
            rec = K.lock(self.project_dir, self.holder.d, self.study, self.shape)
        except Exception as exc:                            # noqa: BLE001
            self.holder.d["coverage_confirmed"] = None
            QMessageBox.warning(self, "Lock", str(exc))
            return
        K.discard_draft(self.project_dir)
        self._open_locked(rec["version"])
        self.say(f"Locked v{rec['version']}")
        self.refresh()

    def _confirm_coverage(self) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Lock")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Before locking, confirm:"))
        a = QCheckBox("The numbering matches the map sheet (orientation checked)")
        b = QCheckBox("Every core's circle covers its tissue (coverage checked)")
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
