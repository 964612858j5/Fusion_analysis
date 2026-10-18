"""The Regions page's `Cores` tab (docs/region_qc_module_plan.md §4): the map
sheet, Find cores, the numbering panel and the core table -- F3's TMA page
widgets (`ui/tma/numbering_panel.py`, `ui/tma/core_table.py`) on the
Regions document (`core.regions`). What it shows lives here; what its
buttons and menus do is `CoresActions`.

TMA numbering is done in a whole-slide workspace only (plan §3): in an
ROI workspace the tab is read-only with a note.
"""

from __future__ import annotations

import os

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.regions import document as D
from ...core.regions import geometry as G
from ...core.regions import numbering as N
from ..tma.core_table import CoreTable
from ..tma.numbering_panel import NumberingPanel
from .cores_actions import CoresActions

COLOR = {"matched": "#43a047", "review": "#fb8c00", "unmatched": "#fdd835", "piece": "#9e9e9e",
         "dropped": "#e53935", "excluded": "#e53935"}


class CoresPanel(QWidget, CoresActions):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.study = self.candidates = None
        self._detecting = False
        self._gen = 0
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.btn_map = QPushButton("Choose map sheet…")
        self.btn_find = QPushButton("Find cores")
        top.addWidget(self.btn_map)
        top.addWidget(self.btn_find)
        lay.addLayout(top)
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        self.lbl_roi = QLabel("TMA numbering is done in a whole-slide workspace; this ROI workspace shows it read-only.")
        self.lbl_roi.setWordWrap(True)
        self.lbl_roi.setVisible(False)
        lay.addWidget(self.lbl_roi)
        self.numbering = NumberingPanel()
        lay.addWidget(self.numbering)
        self.table = CoreTable()
        lay.addWidget(self.table, 1)
        self.btn_map.clicked.connect(self.choose_map)
        self.btn_find.clicked.connect(self.find_cores)
        self.numbering.target_changed.connect(self.page.numbering_changed)
        self.numbering.confirm.connect(self.confirm_numbering)
        self.numbering.reverse.connect(self.page.ctl.reverse)
        self.numbering.cancel.connect(self.page.ctl.cancel)
        self.numbering.bind_single.connect(self.bind_single)
        self.numbering.custom_single.connect(self.custom_single)
        self.table.row_clicked.connect(self.row_clicked)
        self.table.run_toggled.connect(self.run_toggled)
        self.table.context_requested.connect(self.row_menu)

    # ── state ──
    def reset(self):
        self.reset_jobs()
        self.study = self.candidates = None

    def reset_jobs(self):
        """Results of jobs started before now are dropped when they arrive."""
        self._gen += 1
        self._detecting = False

    def load_study(self):
        from ...core.tma import dearray as da
        from ...core.tma.study import StudyError, load_study
        ctx = self.page.ctx
        try:
            st = load_study(ctx["project_dir"])
        except StudyError as exc:
            self.say(str(exc))
            st = None
        if st is not None and st["slide_id"] != ctx["slide_id"]:
            self.say(f"This project's TMA study is for another slide ({st.get('slide_path')}).")
            st = None
        self.study = st
        self.candidates = None
        if st is not None:
            n = da.next_version(ctx["project_dir"]) - 1
            if n >= 1:
                self.candidates = da.load_candidates(ctx["project_dir"], n)
            self.numbering.set_records([r for r in self.records() if r["donor"]])

    def records(self):
        from ...core.tma.cores import position_key
        if self.study is None:
            return []
        sheet = self.study["map_sheet"]
        return [{"position_id": p["position_id"], "x": p["x"], "y": p["y"],
                 "label": f"X{p['x']}-Y{int(p['y']):02d}", "donor": p["donor_block_id"], "sample": p["sample_id"],
                 "position_key": position_key(self.study["slide_id"], sheet["sha256"], p["position_id"])}
                for p in sheet["positions"]]

    def labels_by_key(self):
        return {r["position_key"]: r for r in self.records()}

    def say(self, text):
        self.lbl_info.setText(text)
        print(f"[Regions] {text}")

    def sync_enabled(self, editable):
        pd = self.page.ctx.get("project_dir")
        cores_ok = editable and self.page.cores_editable()
        self.lbl_roi.setVisible(bool(pd) and not self.page.cores_editable())
        self.btn_map.setEnabled(bool(pd) and self.study is None and self.page.cores_editable())
        self.btn_find.setEnabled(self.study is not None and not self._detecting and cores_ok)
        self.numbering.setEnabled(cores_ok)

    # ── the overlay ──
    def draw_parts(self):
        """(items, selected ids, handle polygon, order labels) of the pieces."""
        h = self.page.holder
        if h is None:
            return [], set(), None, {}
        d = h.d
        labels = self.labels_by_key()
        items = []
        for pid, p in d["pieces"].items():
            it = {"id": pid, "polygon": p["polygon"]}
            if p["dropped"]:
                it.update(color=COLOR["dropped"], dash=True)
            elif p["owner"] is None:
                it.update(color=COLOR["piece"], dash=True)
            else:
                c = d["cores"][p["owner"]]
                key = c["position_key"]
                it["color"] = COLOR["excluded"] if key and d["positions"][key]["qc"] == "excluded" else COLOR[c["mapping"]]
                if c["pieces"][0] == pid:
                    if key:
                        r = labels.get(key, {})
                        it["label"] = f"{r.get('label', '?')} {r.get('donor', '')}".strip()
                    elif c["custom_label"]:
                        it["label"] = f"{c['custom_label']} (custom)"
            items.append(it)
        sel = set(self.page.selection)        # the unit is one piece, never the whole core (§9)
        handles = None
        if self.page.selected in d["pieces"]:
            handles = d["pieces"][self.page.selected]["polygon"]
        order = {}
        for k, obj in enumerate(self.page.ctl.sequence, start=1):
            order[d["cores"][obj]["pieces"][0] if obj in d["cores"] else obj] = str(k)
        return items, sel, handles, order

    # ── the table ──
    def refresh(self):
        h = self.page.holder
        recs = self.records()
        if h is None or not h.d.get("map_sha256"):
            rows = [{"kind": "position", "id": r["position_key"], "run": None, "position": r["label"],
                     "donor": r["donor"], "sample": r["sample"], "status": "未对应" if r["donor"] else "空位",
                     "pieces": "", "tissue": ""} for r in recs]
            n = sum(1 for r in recs if r["donor"])
            self.table.set_rows(rows, f"With samples {n}. Find cores to start." if rows else "", True)
            if self.candidates is not None and not self._detecting:
                self.lbl_info.setText(f"Candidates v{self.candidates['version']}: {len(self.candidates['candidates'])} found")
            return
        d = h.d
        held = {c["position_key"]: u for u, c in d["cores"].items() if c["position_key"]}
        rows, n = [], {"numbered": 0, "review": 0, "open": 0, "missing": 0, "excluded": 0}
        for r in recs:
            key, occ = r["position_key"], bool(r["donor"])
            st = D.status_text(d, key, occ) if key in d["positions"] else ("未对应" if occ else "空位")
            uid = held.get(key)
            rows.append({"kind": "position", "id": key,
                         "run": d["positions"][key]["run"] if occ and key in d["positions"] else None,
                         "position": r["label"], "donor": r["donor"], "sample": r["sample"], "status": st,
                         "pieces": len(d["cores"][uid]["pieces"]) if uid else "", "tissue": self._tissue(uid)})
            if occ:
                head = st.split("·")[0]
                n["numbered"] += head == "已对应"
                n["review"] += head == "待复核"
                n["open"] += head == "未对应"
                n["missing"] += head == "缺失"
                n["excluded"] += "排除" in st
        for uid, c in d["cores"].items():
            if not c["position_key"]:
                cy, cx = D.core_centre(d, uid)
                rows.append({"kind": "core", "id": uid, "run": None,
                             "position": c["custom_label"] or f"core at ({cy:.0f}, {cx:.0f})", "donor": "",
                             "sample": "", "status": "Custom: information to complete" if c["custom_label"] else "未对应",
                             "pieces": len(c["pieces"]), "tissue": self._tissue(uid)})
        free = dropped = 0
        for pid, p in d["pieces"].items():
            if p["owner"] is not None:
                continue
            dropped += bool(p["dropped"])
            free += not p["dropped"]
            cy, cx = G.centroid(p["polygon"])
            rows.append({"kind": "piece", "id": pid, "run": None, "position": f"piece {pid} ({cy:.0f}, {cx:.0f})",
                         "donor": "", "sample": "",
                         "status": f"Left out: {p['dropped']}" if p["dropped"] else "Unassigned piece",
                         "pieces": 1, "tissue": "" if p.get("tissue") is None else f"{p['tissue']:.2f}"})
        total = sum(1 for r in recs if r["donor"])
        counts = (f"With samples {total}: numbered {n['numbered']}, to confirm {n['review']}, not numbered "
                  f"{n['open']}, missing {n['missing']}, excluded {n['excluded']}. "
                  f"Pieces: unassigned {free}, left out {dropped}.")
        self.table.set_rows(rows, counts, h.read_only or not self.page.cores_editable())

    def _tissue(self, uid):
        if uid is None:
            return ""
        d = self.page.holder.d
        vals = [(G.area(d["pieces"][p]["polygon"]), d["pieces"][p]["tissue"]) for p in d["cores"][uid]["pieces"]
                if d["pieces"][p].get("tissue") is not None]
        if not vals or not sum(w for w, _ in vals):
            return ""
        return f"{sum(w * t for w, t in vals) / sum(w for w, _ in vals):.2f}"

    # ── the numbering preview ──
    def numbering_changed(self):
        h, ctl = self.page.holder, self.page.ctl
        active = h is not None and self.study is not None and ctl.tool in ("line", "click") and bool(ctl.sequence or ctl.line)
        self.numbering.show_plan(self.current_plan() if active else None, active=active)

    def current_plan(self):
        try:
            records = self.target_records()
        except D.RegionsError as exc:
            return {"ok": False, "problems": [str(exc)], "pairs": [], "conflicts": [],
                    "count_text": f"Selected {len(self.page.ctl.sequence)} / target ?"}
        plan = N.plan_binding(self.page.holder.d, self.page.ctl.sequence, records)
        plan["count_text"] = f"Selected {len(self.page.ctl.sequence)} / target {len(records)}"
        if not plan["ok"]:
            plan["pairs"] = []
        return plan

    def target_records(self):
        t = self.numbering.target()
        if t[0] == "custom":
            return N.records_by_ids(self.study, t[1])
        _k, a, b, order = t
        if a is None or b is None:
            raise D.RegionsError("choose the start and the end record")
        return N.target_records(self.study, a, b, order)

    def project_dir(self):
        return self.page.ctx.get("project_dir")

    def basename(self, path):
        return os.path.basename(path or "")
