"""What the TMA page shows (F3, docs/tma_f3_page_application.md §2.1,
§2.4): the circles' colours and labels, the core table's rows and counts,
the numbering preview, and which controls are enabled. A mixin of
`TmaPage`; it reads the page's draft and never edits it.
"""

from __future__ import annotations

import os

from ...core.tma import layout as L
from .page_actions import PageActions

COLOR = {"matched": "#43a047", "review": "#fb8c00", "unmatched": "#fdd835", "piece": "#9e9e9e",
         "dropped": "#e53935", "excluded": "#e53935"}


class PageView(PageActions):

    # ── map records ──
    def _all_records(self):
        from ...core.tma.cores import position_key
        if self.study is None:
            return []
        sheet = self.study["map_sheet"]
        return [{"position_id": p["position_id"], "x": p["x"], "y": p["y"],
                 "label": f"X{p['x']}-Y{int(p['y']):02d}", "donor": p["donor_block_id"],
                 "sample": p["sample_id"],
                 "position_key": position_key(self.study["slide_id"], sheet["sha256"], p["position_id"])}
                for p in sheet["positions"]]

    def _labels_by_key(self):
        return {r["position_key"]: r for r in self._all_records()}

    # ── everything ──
    def refresh(self):
        self.ctl.prune()
        if self.holder is not None and self.selected is not None and self.selected not in self.holder.d["pieces"]:
            self.selected = None
        self._refresh_title()
        self._refresh_table()
        self.refresh_map()
        self.numbering_changed()
        self._sync_enabled()

    def _refresh_title(self):
        if self.study is not None:
            name = self.study["map_sheet"].get("block_name") or "TMA"
            self.lbl_study.setText(f"Study: {name}   Slide: {os.path.basename(self.slide_path or '')}")
        elif self.project_dir:
            self.lbl_study.setText("No study yet: choose the map sheet")
        self.version_box.blockSignals(True)
        self.version_box.clear()
        from ...core.tma import layout_lock as K
        if getattr(self, "study_mismatch", False):          # another slide's study: nothing to open
            self.version_box.setEnabled(False)
            self.version_box.blockSignals(False)
            return
        self.version_box.setEnabled(True)
        has_draft = bool(self.project_dir) and os.path.isfile(K.draft_path(self.project_dir))
        if has_draft or self.view_version is None and self.holder is not None:
            base = self.holder.d.get("based_on") if (self.holder and self.view_version is None) else None
            self.version_box.addItem("Draft" + (f" (based on v{base})" if base else ""), None)
        for v in (K.locked_versions(self.project_dir) if self.project_dir else []):
            self.version_box.addItem(f"v{v} (locked)", v)
        for i in range(self.version_box.count()):
            if self.version_box.itemData(i) == self.view_version:
                self.version_box.setCurrentIndex(i)
        self.version_box.blockSignals(False)
        if self.candidates is not None:
            info = f"Candidates v{self.candidates['version']}: {len(self.candidates['candidates'])} found"
            if not self._detecting:
                self.lbl_info.setText(info)

    def _sync_enabled(self):
        has = self.holder is not None
        ro = has and self.holder.read_only
        editable = has and not ro
        self.btn_map.setEnabled(bool(self.project_dir) and self.study is None
                                and not getattr(self, "study_mismatch", False))
        self.btn_find.setEnabled(self.study is not None and not self._detecting)
        for b in self.tool_group.buttons():
            b.setEnabled(editable or b.property("tool") == "select")
        self.btn_undo.setEnabled(editable and self.holder.can_undo)
        self.btn_redo.setEnabled(editable and self.holder.can_redo)
        for b in (self.btn_prev, self.btn_next, self.btn_step1):
            b.setEnabled(has)
        self.btn_lock.setEnabled(has)
        self.btn_lock.setText("New draft from this version" if ro else "Lock")
        self.btn_discard.setEnabled(editable)
        self.numbering.setEnabled(editable)

    # ── the map ──
    def refresh_map(self, extra=None):
        if self.holder is None:
            self.map.set_items([] if extra is None else [extra])
            return
        d = self.holder.d
        labels = self._labels_by_key()
        items = []
        for pid, p in d["pieces"].items():
            it = {"id": pid, "cy": p["cy"], "cx": p["cx"], "r": p["r"]}
            if p["dropped"]:
                it.update(color=COLOR["dropped"], dash=True)
            elif p["owner"] is None:
                it.update(color=COLOR["piece"], dash=True)
            else:
                c = d["cores"][p["owner"]]
                key = c["position_key"]
                if key and d["positions"][key]["qc"] == "excluded":
                    it["color"] = COLOR["excluded"]
                else:
                    it["color"] = COLOR[c["mapping"]]
                if c["pieces"][0] == pid:
                    if key:
                        r = labels.get(key, {})
                        it["label"] = f"{r.get('label', '?')} {r.get('donor', '')}".strip()
                    elif c["custom_label"]:
                        it["label"] = f"{c['custom_label']} (custom)"
            items.append(it)
        if extra is not None:
            items.append(extra)
        sel = set()
        core = self.selected_core()
        if core is not None:
            sel = set(d["cores"][core]["pieces"])
        elif self.selected is not None:
            sel = {self.selected}
        handles = d["pieces"].get(self.selected) if (self.selected and not self.holder.read_only) else None
        order = {}
        for k, obj in enumerate(self.ctl.sequence, start=1):
            first = d["cores"][obj]["pieces"][0] if obj in d["cores"] else obj
            order[first] = str(k)
        self.map.set_items(items, selected=sel, handles_for=handles, order_labels=order)

    # ── the table ──
    def _refresh_table(self):
        if self.holder is None:
            # the map sheet alone (before Find cores): every position, no core yet
            rows = [{"kind": "position", "id": r["position_key"], "run": None, "position": r["label"],
                     "donor": r["donor"], "sample": r["sample"], "status": "未对应" if r["donor"] else "空位",
                     "pieces": "", "tissue": ""} for r in self._all_records()]
            n = sum(1 for r in self._all_records() if r["donor"])
            self.table.set_rows(rows, f"With samples {n}. Find cores to start." if rows else "", True)
            return
        d = self.holder.d
        held = {c["position_key"]: u for u, c in d["cores"].items() if c["position_key"]}
        rows, n = [], {"numbered": 0, "review": 0, "open": 0, "missing": 0, "excluded": 0}
        for r in self._all_records():
            key, occ = r["position_key"], bool(r["donor"])
            st = L.status_text(d, key, occ)
            uid = held.get(key)
            rows.append({"kind": "position", "id": key, "run": d["positions"][key]["run"] if occ else None,
                         "position": r["label"], "donor": r["donor"], "sample": r["sample"], "status": st,
                         "pieces": len(d["cores"][uid]["pieces"]) if uid else "",
                         "tissue": self._tissue(uid)})
            if occ:
                head = st.split("·")[0]
                n["numbered"] += head == "已对应"
                n["review"] += head == "待复核"
                n["open"] += head == "未对应"
                n["missing"] += head == "缺失"
                n["excluded"] += "排除" in st
        for uid, c in d["cores"].items():
            if c["position_key"]:
                continue
            cy, cx = L.core_centre(d, uid)
            rows.append({"kind": "core", "id": uid, "run": None,
                         "position": c["custom_label"] or f"core at ({cy:.0f}, {cx:.0f})", "donor": "", "sample": "",
                         "status": "Custom: information to complete" if c["custom_label"] else "未对应",
                         "pieces": len(c["pieces"]), "tissue": self._tissue(uid)})
        free = dropped = 0
        for pid, p in d["pieces"].items():
            if p["owner"] is not None:
                continue
            if p["dropped"]:
                dropped += 1
            else:
                free += 1
            rows.append({"kind": "piece", "id": pid, "run": None,
                         "position": f"piece {pid} ({p['cy']:.0f}, {p['cx']:.0f})", "donor": "", "sample": "",
                         "status": f"Left out: {p['dropped']}" if p["dropped"] else "Unassigned piece",
                         "pieces": 1, "tissue": "" if p.get("tissue") is None else f"{p['tissue']:.2f}"})
        total = sum(1 for r in self._all_records() if r["donor"])
        counts = (f"With samples {total}: numbered {n['numbered']}, to confirm {n['review']}, "
                  f"not numbered {n['open']}, missing {n['missing']}, excluded {n['excluded']}. "
                  f"Pieces: unassigned {free}, left out {dropped}.")
        self.table.set_rows(rows, counts, self.holder.read_only)

    def _tissue(self, uid):
        if uid is None:
            return ""
        ps = [self.holder.d["pieces"][p] for p in self.holder.d["cores"][uid]["pieces"]]
        vals = [(p["r"] ** 2, p["tissue"]) for p in ps if p.get("tissue") is not None]
        if not vals:
            return ""
        return f"{sum(w * t for w, t in vals) / sum(w for w, _ in vals):.2f}"

    # ── the numbering preview ──
    def numbering_changed(self):
        if self.holder is None or self.study is None:
            self.numbering.show_plan(None)
            return
        active = self.ctl.tool in ("line", "click") and bool(self.ctl.sequence or self.ctl.line)
        if not active:
            self.numbering.show_plan(None)
            self.refresh_map()
            return
        plan = self.current_plan()
        self.numbering.show_plan(plan, active=True)
        self.refresh_map()

    def current_plan(self):
        try:
            records = self.target_records()
        except L.LayoutError as exc:
            return {"ok": False, "problems": [str(exc)], "pairs": [], "conflicts": [],
                    "count_text": f"Selected {len(self.ctl.sequence)} / target ?"}
        plan = L.plan_binding(self.holder.d, self.ctl.sequence, records)
        plan["count_text"] = f"Selected {len(self.ctl.sequence)} / target {len(records)}"
        if not plan["ok"]:
            plan["pairs"] = []
        return plan

    def target_records(self):
        t = self.numbering.target()
        if t[0] == "custom":
            return L.records_by_ids(self.study, t[1])
        _kind, a, b, order = t
        if a is None or b is None:
            raise L.LayoutError("choose the start and the end record")
        return L.target_records(self.study, a, b, order)
