"""What the `Cores` tab's buttons and menus do (plan §4; ported from F3's
`ui/tma/page_actions.py` onto the Regions document): confirm a numbering,
number one core, the piece / position menus, the map sheet,
Find cores. Every edit goes through `page.edit` (one undo step). A mixin
of `CoresPanel`.
"""

from __future__ import annotations

from ...qt.QtGui import QCursor
from ...qt.QtWidgets import QFileDialog, QInputDialog, QMenu, QMessageBox

from ..theme import dialogs as theme_dialogs

from ...core.regions import document as D
from ...core.regions import geometry as G
from ...core.regions import numbering as N


class CoresActions:

    # ── numbering ──
    def confirm_numbering(self):
        page = self.page
        if not self.numbering_allowed():
            self.say(self.map_blocker() or "Numbering is not possible here.")
            return
        plan = self.current_plan()
        if not plan["ok"]:
            self.say("; ".join(plan["problems"]))
            return
        if plan["conflicts"]:
            if QMessageBox.question(page, "Rebind", f"{len(plan['conflicts'])} conflicting number(s) will be rebound; "
                                    "the cores pushed off keep their outlines. Continue?") != QMessageBox.Yes:
                return
        extra = {}
        if page.ctl.line is not None:
            extra = {"line": [list(page.ctl.line[0]), list(page.ctl.line[1])], "band": page.ctl.band}
        method = {"line": "line", "click": "click"}.get(page.ctl.tool, "select")
        if page.edit(N.apply_binding, plan, method, rebind=bool(plan["conflicts"]), extra=extra) is not None:
            self.reversed = False
            if page.ctl.tool == "select":
                page.select(None)                 # done: the next Ctrl+Z undoes this numbering
            page.ctl.cancel()
            self.say(f"Numbered {len(plan['pairs'])} core(s).")

    def _record_from_text(self, text):
        t = (text or "").strip().lower()
        if not t:
            return None
        recs = [r for r in self.records() if r["donor"]]
        hit = [r for r in recs if t.split()[0] == r["label"].lower()]
        if not hit:
            hit = [r for r in recs if t in (r["donor"].lower(), r["sample"].lower())]
        return hit[0] if len(hit) == 1 else None

    def _selected_object(self):
        h, pid = self.page.holder, self.page.selected
        if h is None or pid is None:
            return None
        p = h.d["pieces"].get(pid)
        if p is None or p["dropped"]:
            return None
        return p["owner"] or pid

    def bind_single(self, text):
        page = self.page
        if not self.numbering_allowed():
            self.say(self.map_blocker() or "Numbering is not possible here.")
            return
        obj = self._selected_object()
        if obj is None:
            self.say("Select a core on the viewer first.")
            return
        rec = self._record_from_text(text)
        if rec is None:
            self.say(f"'{text}' is not one record of the map sheet (use Custom for a number not on it).")
            return
        rec = N.records_by_ids(self.study, [rec["position_id"]])[0]
        plan = N.plan_binding(page.holder.d, [obj], [rec])
        if not plan["ok"]:
            self.say("; ".join(plan["problems"]))
            return
        held = [c for c in plan["conflicts"] if c["kind"] == "record_held"]
        own = page.holder.d["cores"].get(plan["pairs"][0]["core"] or "", {}).get("position_key")
        preview = f"Selected core → {rec['label']}  {rec['donor']}"
        self.numbering.show_plan(plan, active=False)
        if not plan["conflicts"]:
            if QMessageBox.question(page, "Number", preview + "\n\nConfirm?") != QMessageBox.Yes:
                return
        else:
            box = QMessageBox(page)
            box.setWindowTitle("Number taken")
            box.setText(f"{preview}\n\nThis conflicts with another core's number. Rebind it (the other core "
                        "becomes not numbered, its outline kept) or exchange the two numbers?")
            rebind = box.addButton("Rebind", QMessageBox.AcceptRole)
            swap = box.addButton("Exchange", QMessageBox.ActionRole) if (held and own) else None
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if swap is not None and box.clickedButton() is swap:
                page.edit(N.swap, plan["pairs"][0]["core"], held[0]["core"])
                return
            if box.clickedButton() is not rebind:
                return
        page.edit(N.apply_binding, plan, "single", rebind=bool(plan["conflicts"]))

    def custom_single(self, text):
        obj = self._selected_object()
        if obj is None:
            self.say("Select a core on the viewer first.")
            return
        self.page.edit(N.set_custom, obj, text)

    # ── table ──
    def row_clicked(self, kind, ident, ctrl):
        page = self.page
        h = page.holder
        if h is None:
            return
        d = h.d
        uid = None
        if kind == "position":
            uid = next((u for u, c in d["cores"].items() if c["position_key"] == ident), None)
        elif kind == "core":
            uid = ident
        if uid is not None:
            sid = d["cores"][uid]["pieces"][0]
            polys = [d["pieces"][p]["polygon"] for p in d["cores"][uid]["pieces"]]
        elif kind == "piece":
            sid = ident
            polys = [d["pieces"][ident]["polygon"]]
        else:
            return
        self._jump_to(polys)
        page.select(sid)

    def _jump_to(self, polys):
        self.page.jump_to_shapes(polys)

    def run_toggled(self, key, on):
        self.page.edit(D.set_run, key, on)

    def runs_set(self, keys, on):
        self.page.edit(D.set_runs, list(keys), on)

    def row_menu(self, kind, ident, pos):
        page = self.page
        h = page.holder
        if h is None or h.read_only or kind != "position" or not page.cores_editable():
            return
        d = h.d
        rec = self.labels_by_key().get(ident, {})
        if not rec.get("donor") or ident not in d["positions"]:
            return
        st = d["positions"][ident]
        m = QMenu(page)
        bound = any(c["position_key"] == ident for c in d["cores"].values())
        if not bound:
            if st["observed"] == "missing":
                m.addAction("Clear 'missing'", lambda: page.edit(D.set_missing, ident, False))
            else:
                m.addAction("Confirm missing…", lambda: self._ask_missing(ident))
        if st["qc"] == "excluded":
            m.addAction("Include again", lambda: page.edit(D.set_qc, ident, "unreviewed"))
        else:
            m.addAction("Exclude…", lambda: self._ask_exclude(ident))
            m.addAction("Accept", lambda: page.edit(D.set_qc, ident, "accepted"))
        m.exec(pos)

    def _ask_missing(self, key):
        reason, ok = QInputDialog.getText(self.page, "Confirm missing", "Reason (optional):")
        if ok:
            self.page.edit(D.set_missing, key, True, reason)

    def _ask_exclude(self, key):
        reason, ok = QInputDialog.getText(self.page, "Exclude", "Reason(s), comma-separated:")
        if ok and reason.strip():
            self.page.edit(D.set_qc, key, "excluded", [r.strip() for r in reason.split(",") if r.strip()])

    def piece_menu(self, pid):
        page = self.page
        h = page.holder
        if h is None or h.read_only:
            return
        d = h.d
        p = d["pieces"][pid]
        m = QMenu(page)
        ids = page.selected_pieces() if pid in page.selection else [pid]
        if p["dropped"]:
            m.addAction("Restore", lambda: page.edit(D.restore_piece, pid))   # an old "left out" piece
        else:
            if p["owner"] is not None:
                c = d["cores"][p["owner"]]
                if len(c["pieces"]) > 1:
                    m.addAction("Remove from core", lambda: page.edit(D.unmerge, pid))
                if c["mapping"] == "review":
                    m.addAction("Confirm number", lambda: page.edit(N.confirm_number, p["owner"]))
                if c["position_key"]:
                    m.addAction("Remove number", lambda: page.edit(D.unbind, p["owner"]))
        m.addAction("Delete" if len(ids) == 1 else f"Delete {len(ids)} pieces",
                    lambda: page.edit(D.delete_pieces, ids))
        if not p["dropped"]:
            sub = m.addMenu("Make exclusion" if len(ids) == 1 else f"Make {len(ids)} exclusions")
            for reason, text in (("blur", "Blur"), ("fold", "Fold"), ("other", "Other")):
                sub.addAction(text, lambda r=reason: page.pieces_to_exclusions(ids, r))
        m.exec(QCursor.pos())

    # ── study, cores ──
    def choose_map(self):
        """`Choose map sheet…` (no study yet), or `Attach map sheet` (the
        study exists, the open draft's cores have none: after an Undo or a
        Revert). Cores already found keep their outlines and get the
        positions (docs/v17_find_cores_without_map_plan.md §3.2.3-4)."""
        from ...core.tma.map_sheet import MapSheetError
        from ...core.tma.study import StudyError, create_study
        page = self.page
        if self.study is None:
            path, _ = QFileDialog.getOpenFileName(page, "Choose the TMA map sheet", "", "Excel (*.xlsx)")
            if not path:
                return
            try:
                create_study(page.ctx["project_dir"], page.ctx["slide_path"], path)
            except (MapSheetError, StudyError, ValueError) as exc:
                QMessageBox.warning(page, "Map sheet", str(exc))
                return
            self.load_study()
        if self.study is not None and self.can_attach_map():
            if page.edit(D.attach_map, self.study) is not None:
                self.say(f"Map sheet attached to the {len(page.holder.d['cores'])} core(s) found: number them.")
        page.refresh()

    def find_cores(self):
        from ..tma import workers
        page = self.page
        nuc = self.nucleus_channel()
        if nuc is None:
            self.say("Finding cores needs a channel named DAPI.")
            return
        # no diameter: the core size is estimated from the slide (plan §3.1),
        # with or without a map sheet
        kw = {"channel": nuc}
        self._detecting = True
        self.say("Finding cores…")
        page.refresh()
        w = workers.keep(workers.DetectWorker(self._gen, page.ctx["project_dir"], page.ctx["slide_path"],
                                              page.ctx["slide_id"], kw))
        w.done.connect(self._detect_done)
        w.failed.connect(self._detect_failed)
        w.start()

    def _detect_done(self, gen, rec):
        if gen != self._gen:
            return
        page = self.page
        self._detecting = False
        self.candidates = rec
        if not page.cores_editable():                 # an ROI workspace never changes the cores
            page.refresh()
            return
        n = len(rec["candidates"])
        size = self.size_text(rec)
        h = page.holder
        has_cores = h is not None and not h.read_only and (bool(h.d["pieces"]) or bool(h.d.get("map_sha256")))
        # replaces the draft's cores: the red choice, never the default
        # (button restyle plan §3); the words stay Yes / No
        if has_cores and not theme_dialogs.confirm_danger(
                page, "New candidates", f"Found {n} candidates (v{rec['version']}). Replace the cores of the draft "
                "with them? All numbering and position marks (missing, QC, Run) are cleared; the exclusions are "
                "kept.", action="Yes", cancel="No"):
            page.refresh()
            return
        if h is None or h.read_only:
            page._open_draft(D.new_document(page.ctx["slide_id"], page.shape()))
        page.selected = None
        page.ctl.cancel()
        if self.study is not None:
            page.edit(D.attach_tma, self.study, rec)
            self.say(f"Candidates v{rec['version']}: {n} found ({size})")
        else:
            page.edit(D.attach_candidates, rec)
            self.say(f"Candidates v{rec['version']}: {n} found ({size}). Choose the map sheet to number them.")
        page.refresh()

    def size_text(self, rec):
        """How big the cores were taken to be, with the map sheet's
        diameter beside it when there is one."""
        px = float(rec.get("core_diameter_px") or 0)
        um = rec.get("pixel_um")
        what = "estimated" if rec.get("core_diameter_source", "given") == "estimated" else "given"
        text = (f"core size {what} {px * (float(um[0]) + float(um[1])) / 2 / 1000:.2f} mm" if um
                else f"core size {what} {px:.0f} px")
        mm = ((self.study or {}).get("map_sheet") or {}).get("diameter_mm")
        return text + (f"; map sheet: {float(mm):g} mm" if mm else "")

    def _detect_failed(self, gen, msg):
        if gen != self._gen:
            return
        self._detecting = False
        self.say(f"Finding cores failed: {msg}")
        self.page.refresh()
