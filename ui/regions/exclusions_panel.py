"""The Regions page's `Exclusions` tab (docs/region_qc_module_plan.md §3,
§4, R3): blur / fold exclusion annotation -- marks only, pixels never
change, stored as drawn (never clipped), one polygon each.

* The list: number, reasons, channels, area, the core it lies in (live,
  the same rule as the lock: the core it overlaps most).
* The reasons (Blur / Fold / Other), the channels (all, or chosen ones) and
  a note: for the selected exclusion (each change one undo step), and the
  defaults for the next one drawn.
* Drawing happens on the viewer with `Polygon` / `Circle` while this tab is
  open; `Select` / `Vertex` then pick and edit exclusions, Delete removes.
* In an ROI workspace exclusions are drawn as everywhere (only the cores
  are read-only there).
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...core.regions import document as D
from ...core.regions import geometry as G

EXCL_COLOR = "#e040fb"
COLUMNS = ("#", "Reasons", "Channels", "Area", "Core")


class ExclusionsPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, page):
        super().__init__()
        self.page = page
        self._filling = False
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Draw on the viewer with Polygon or Circle; Select / Vertex edit, Delete removes."))
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("regions_exclusions_table")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        self.lbl_target = QLabel("For the next exclusion:")
        lay.addWidget(self.lbl_target)
        row = QHBoxLayout()
        self.cb = {}
        for key, text in (("blur", "Blur"), ("fold", "Fold"), ("other", "Other")):
            c = QCheckBox(text)
            self.cb[key] = c
            row.addWidget(c)
        row.addStretch()
        lay.addLayout(row)
        self.cb["blur"].setChecked(True)
        crow = QHBoxLayout()
        self.cb_all = QCheckBox("All channels")
        self.cb_all.setChecked(True)
        self.btn_channels = QPushButton("Channels…")
        self.lbl_channels = QLabel("")
        crow.addWidget(self.cb_all)
        crow.addWidget(self.btn_channels)
        crow.addWidget(self.lbl_channels, 1)
        lay.addLayout(crow)
        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("Note:"))
        self.note = QLineEdit()
        nrow.addWidget(self.note, 1)
        lay.addLayout(nrow)
        self._channels = []
        self._defaults = (["blur"], [], "")     # the next exclusion's reasons / channels / note
        self._rows = []
        self.table.cellClicked.connect(self._row_clicked)
        for c in self.cb.values():
            c.toggled.connect(self._fields_edited)
        self.cb_all.toggled.connect(self._fields_edited)
        self.btn_channels.clicked.connect(self._choose_channels)
        self.note.editingFinished.connect(self._fields_edited)

    # ── fields ──
    def reasons(self):
        return [k for k, c in self.cb.items() if c.isChecked()]

    def channels(self):
        return "all" if self.cb_all.isChecked() or not self._channels else list(self._channels)

    def defaults(self):
        """What a new exclusion gets: the ticked reasons (blur when none)."""
        return (self.reasons() or ["blur"]), self.channels(), self.note.text().strip()

    def _selected_uid(self):
        h = self.page.holder
        sel = self.page.selected
        return sel if h is not None and sel in h.d["exclusions"] else None

    def _selected_masks(self):
        """Core masks selected here (no exclusion selected): a reason ticked
        makes them exclusions (user 2026-10-19)."""
        h = self.page.holder
        if h is None or not self.page.exclusions_tab_open() \
                or any(s in h.d["exclusions"] for s in self.page.selection):
            return []
        return [s for s in self.page.selection if s in h.d["pieces"]]

    def _fields_edited(self, *_a):
        if self._filling:
            return
        self.btn_channels.setEnabled(not self.cb_all.isChecked())
        masks = self._selected_masks()
        if masks:
            reasons = self.reasons()
            if reasons:
                self.page.masks_to_exclusions(masks, reasons, self.channels(), self.note.text().strip())
            return
        uid = self._selected_uid()
        if uid is None:                                      # the next exclusion's defaults
            self._defaults = (self.reasons(), list(self._channels) if not self.cb_all.isChecked() else [],
                              self.note.text().strip())
            return
        if self.page.holder.read_only:
            self._show_fields()
            return
        reasons = self.reasons()
        if not reasons:                                      # no reason left: a core mask again (user 2026-10-19)
            if self.page._can_convert_cores():
                self.page.exclusion_to_core(uid)
                return
            self.page.say("An exclusion needs at least one reason (cores are not edited in this workspace).")
            self._show_fields()
            return
        e = self.page.holder.d["exclusions"][uid]
        new = (sorted(reasons), self.channels(), self.note.text().strip())
        if new != (e["reasons"], e["channels"], e["note"]):
            self.page.edit(D.update_exclusion, uid, reasons=new[0], channels=new[1], note=new[2])

    def _choose_channels(self):
        names = self.page.ctx.get("channel_names") or []
        dlg = QDialog(self)
        dlg.setWindowTitle("Channels")
        lay = QVBoxLayout(dlg)
        lst = QListWidget()
        for n in names:
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if n in self._channels else Qt.Unchecked)
            lst.addItem(it)
        lay.addWidget(lst)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        lay.addWidget(box)
        if dlg.exec_() != QDialog.Accepted:
            return
        chosen = [lst.item(i).text() for i in range(lst.count()) if lst.item(i).checkState() == Qt.Checked]
        self.set_channel_choice(chosen)

    def set_channel_choice(self, chosen):
        self._channels = list(chosen)
        self._filling = True
        self.cb_all.setChecked(not chosen)
        self._filling = False
        self.lbl_channels.setText(", ".join(chosen))
        self._fields_edited()

    def _show_fields(self):
        """The selected exclusion's fields (or the defaults, untouched)."""
        uid = self._selected_uid()
        masks = self._selected_masks()
        self.lbl_target.setText("Selected exclusion:" if uid else
                                "Several exclusions selected." if self._several() else
                                f"{len(masks)} core mask(s) selected: tick a reason to make "
                                f"{'it an exclusion' if len(masks) == 1 else 'them exclusions'}" if masks else
                                "For the next exclusion:")
        if masks:                                            # no reason yet: ticking one converts
            e = {"reasons": [], "channels": self._defaults[1] or "all", "note": self._defaults[2]}
        elif uid is None:
            reasons, chans, note = self._defaults
            e = {"reasons": reasons, "channels": chans or "all", "note": note}
        else:
            e = self.page.holder.d["exclusions"][uid]
        self._filling = True
        for k, c in self.cb.items():
            c.setChecked(k in e["reasons"])
        self._channels = [] if e["channels"] == "all" else list(e["channels"])
        self.cb_all.setChecked(e["channels"] == "all")
        self.lbl_channels.setText("" if e["channels"] == "all" else ", ".join(e["channels"]))
        self.note.setText(e["note"])
        h = self.page.holder
        self.btn_channels.setEnabled(e["channels"] != "all" and h is not None and not h.read_only
                                     and not self._several())
        self._filling = False

    # ── list ──
    def _several(self):
        h = self.page.holder
        return h is not None and sum(s in h.d["exclusions"] for s in self.page.selection) > 1

    def refresh(self):
        h = self.page.holder
        editable = h is not None and not h.read_only and not self._several()   # several: no shared fields
        for w in list(self.cb.values()) + [self.cb_all, self.note]:
            w.setEnabled(editable)
        self.btn_channels.setEnabled(editable and not self.cb_all.isChecked())
        self._filling = True
        self.table.setRowCount(0)
        self._rows = []
        if h is not None:
            d = h.d
            links = self._links(d)
            px = self.page.ctx.get("pixel_um")
            um2 = (px[0] * px[1]) if px else None
            for n, (uid, e) in enumerate(d["exclusions"].items(), start=1):
                a = G.area(e["polygon"])
                area = f"{a * um2 / 1e6:.3f} mm²" if um2 else f"{a:.0f} px²"
                core = links.get(uid)
                cells = (str(n), ", ".join(e["reasons"]),
                         "all" if e["channels"] == "all" else ", ".join(e["channels"]), area,
                         self._core_label(d, core))
                r = self.table.rowCount()
                self.table.insertRow(r)
                for j, v in enumerate(cells):
                    it = QTableWidgetItem(v)
                    if j == 0:
                        it.setForeground(QColor(EXCL_COLOR))
                    self.table.setItem(r, j, it)
                self._rows.append(uid)
        self.table.resizeColumnsToContents()
        self._filling = False
        uid = self._selected_uid()
        self.table.clearSelection()
        if uid in self._rows:
            self.table.selectRow(self._rows.index(uid))
        self._show_fields()

    def _links(self, d):
        from ...core.regions import lock as K
        if not d["exclusions"] or not d.get("map_sha256"):
            return {}
        try:
            return K.exclusion_cores(d, K.exclusion_regions(d), K.core_regions(d, include_excluded=True))
        except Exception:                                   # noqa: BLE001
            return {}

    def _core_label(self, d, core):
        if not core or core not in d["cores"]:
            return ""
        key = d["cores"][core]["position_key"]
        rec = self.page.cores.labels_by_key().get(key) if key else None
        return rec["label"] if rec else ""

    def _row_clicked(self, row, _col):
        if not (0 <= row < len(self._rows)):
            return
        uid = self._rows[row]
        self.page.jump_to_shapes([self.page.holder.d["exclusions"][uid]["polygon"]])
        self.page.select(uid)

    # ── the overlay ──
    def draw_items(self):
        h = self.page.holder
        if h is None:
            return []
        out = []
        for n, (uid, e) in enumerate(h.d["exclusions"].items(), start=1):
            out.append({"id": uid, "polygon": e["polygon"], "color": EXCL_COLOR, "hatch": True,
                        "label": f"E{n} {'/'.join(e['reasons'])}"})
        return out

    def menu(self, uid):
        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QMenu
        h = self.page.holder
        if h is None or h.read_only:
            return
        m = QMenu(self.page)
        m.addAction("Delete exclusion", lambda: self.page.edit(D.delete_exclusions, [uid]))
        if self.page.cores_editable():
            m.addAction("Make core mask", lambda: self.page.exclusion_to_core(uid))
        m.exec_(QCursor.pos())
