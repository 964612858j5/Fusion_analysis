"""The TMA page (F3, docs/tma_f3_page_application.md): one TMA slide's
study, candidate cores, numbering, correction and locked versions.

Built on `StepFrame` like the other step pages: the title bar (study,
slide, version, Lock), the left column "Cores" (map sheet, Find cores,
numbering panel, core table) sharing the channel column's width, the right
column "Map" (tool row + slide map). The window only wires it
(`set_context`, `open_in_step1`, `shutdown`, `channel_column_splitter`).
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QButtonGroup, QComboBox, QHBoxLayout, QLabel, QPushButton, QShortcut,
                             QVBoxLayout, QWidget)

from ...core.tma import layout as L
from ...core.tma import layout_lock as K
from . import workers
from .core_map import CoreMap
from .core_table import CoreTable
from .draft import DraftHolder
from .map_tools import MapController
from .numbering_panel import NumberingPanel
from .page_view import PageView

TOOL_BUTTONS = (("Select", "select", "Select, move and resize circles"),
                ("Line", "line", "Number by line: drag along the cores; the wheel sets the band width"),
                ("Clicks", "click", "Number by clicks: click the cores in order; Backspace drops the last"),
                ("Merge", "merge", "Merge: with a core selected, click pieces to add them to it"),
                ("Circle", "draw", "Draw a circle: press at the centre, drag out the radius"))


class TmaPage(QWidget, PageView):
    open_in_step1 = pyqtSignal(list)      # core bbox [y0, y1, x0, x1], level 0

    def __init__(self, parent=None, metrics=None, title_bar=None):
        super().__init__(parent)
        self.setObjectName("tma_page")
        self.project_dir = self.slide_path = None
        self.shape = None
        self.study = self.candidates = None
        self.holder = None
        self.view_version = None          # None = the draft, n = locked version n (read-only)
        self.selected = None              # piece id
        self._gen = 0
        self._detecting = False
        self.study_mismatch = False
        self.ctl = MapController(self)
        from PyQt5.QtCore import QTimer
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_now)
        self._build(metrics, title_bar)

    # ── building ──
    def _build(self, metrics, title_bar):
        from ..step_frame import FRAME_TAB_QSS, StepFrame, StepFrameMetrics, free_tab_bar
        if metrics is None:
            metrics = StepFrameMetrics(title_height=34, bottom_height=8, tab_qss=FRAME_TAB_QSS)
        self.frame = StepFrame(metrics, free_tab_bar)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        bar = title_bar if title_bar is not None else QWidget()
        row = bar.layout() if bar.layout() is not None else QHBoxLayout(bar)
        self.lbl_study = QLabel("No project")
        row.addWidget(self.lbl_study)
        row.addWidget(QLabel("Version:"))
        self.version_box = QComboBox()
        self.version_box.setMinimumWidth(170)
        row.addWidget(self.version_box)
        self.btn_discard = QPushButton("Discard draft")
        self.btn_lock = QPushButton("Lock")
        row.addWidget(self.btn_discard)
        row.addWidget(self.btn_lock)
        self.frame.set_title(bar)

        left = QWidget()
        ll = QVBoxLayout(left)
        top = QHBoxLayout()
        self.btn_map = QPushButton("Choose map sheet…")
        self.btn_find = QPushButton("Find cores")
        top.addWidget(self.btn_map)
        top.addWidget(self.btn_find)
        ll.addLayout(top)
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        ll.addWidget(self.lbl_info)
        self.numbering = NumberingPanel()
        ll.addWidget(self.numbering)
        self.table = CoreTable()
        ll.addWidget(self.table, 1)
        self.frame.left_tabs.addTab(left, "Cores")

        right = QWidget()
        rl = QVBoxLayout(right)
        tools = QHBoxLayout()
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        for i, (text, key, tip) in enumerate(TOOL_BUTTONS):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setProperty("tool", key)
            self.tool_group.addButton(b, i)
            tools.addWidget(b)
        self.tool_group.button(0).setChecked(True)
        self.btn_undo = QPushButton("Undo")
        self.btn_redo = QPushButton("Redo")
        self.btn_prev = QPushButton("Prev")
        self.btn_next = QPushButton("Next")
        self.btn_step1 = QPushButton("Step1")
        self.btn_prev.setToolTip("Previous core (by position)")
        self.btn_next.setToolTip("Next core (by position)")
        self.btn_step1.setToolTip("Open the selected core in Step1's viewer")
        for b in (self.btn_undo, self.btn_redo, self.btn_prev, self.btn_next, self.btn_step1):
            tools.addWidget(b)
        tools.addStretch()
        self.map = CoreMap()
        self.map.controller = self.ctl
        rl.addWidget(self.map, 1)
        self.frame.tool_row(rl, tools)
        self.frame.right_tabs.addTab(right, "Map")
        # the tab row is kept blank (plan §2.1): each column's one tab is laid
        # out like every other tab, so the row has every page's height
        for tabs in (self.frame.left_tabs, self.frame.right_tabs):
            tabs.setTabText(0, "")
            tabs.tabBar().setStyleSheet(StepFrame._BLANK_TAB_QSS)

        self.btn_map.clicked.connect(self._choose_map)
        self.btn_find.clicked.connect(self._find_cores)
        self.btn_lock.clicked.connect(self._lock_or_branch)
        self.btn_discard.clicked.connect(self._discard_draft)
        self.version_box.activated.connect(self._version_chosen)
        self.tool_group.buttonClicked.connect(lambda b: self._set_tool(b.property("tool")))
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_prev.clicked.connect(lambda: self.step_core(-1))
        self.btn_next.clicked.connect(lambda: self.step_core(+1))
        self.btn_step1.clicked.connect(self._open_step1)
        self.numbering.target_changed.connect(self.numbering_changed)
        self.numbering.confirm.connect(self._confirm_numbering)
        self.numbering.reverse.connect(self.ctl.reverse)
        self.numbering.cancel.connect(self.ctl.cancel)
        self.numbering.bind_single.connect(self._bind_single)
        self.numbering.custom_single.connect(self._custom_single)
        self.table.row_clicked.connect(self._row_clicked)
        self.table.run_toggled.connect(self._run_toggled)
        self.table.context_requested.connect(self._row_menu)
        for keys, fn in ((QKeySequence.Undo, self.undo), (QKeySequence("Ctrl+Shift+Z"), self.redo),
                         (QKeySequence(Qt.Key_Delete), self._delete_selected),
                         (QKeySequence(Qt.Key_Escape), self._escape),
                         (QKeySequence(Qt.Key_Backspace), lambda: self.ctl.key("backspace")),
                         (QKeySequence(Qt.Key_Return), self._enter_key),
                         (QKeySequence(Qt.Key_Enter), self._enter_key)):
            sc = QShortcut(keys, self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self._sync_enabled()

    def channel_column_splitter(self):
        return self.frame.splitter

    # ── context ──
    def set_context(self, project_dir, slide_path):
        """The committed dataset (window, plan §2.5). A new slide drops
        every result still on its way for the old one."""
        same = (project_dir, slide_path) == (self.project_dir, self.slide_path)
        if same and self.holder is not None:
            return
        self.flush_draft()
        self._gen += 1
        self._detecting = False                           # a job of the old slide no longer counts
        self.project_dir, self.slide_path = project_dir, slide_path
        self.study = self.candidates = self.holder = None
        self.selected, self.view_version = None, None
        self.ctl.cancel()
        if not project_dir or not slide_path:
            self.lbl_study.setText("No project: load a dataset in Step0 first")
            self.shape = None
            self.map.clear()
            self.refresh()
            return
        from ...sources.ome_tiff import OmeTiffSource
        try:
            src = OmeTiffSource(slide_path)
            try:
                self.shape = tuple(src.level_shape(0))
                names = list(src.channel_names())
                self.pixel_um = src.physical_size()
            finally:
                src.close()
        except Exception as exc:                            # noqa: BLE001
            self.say(f"Cannot open the slide: {exc}")
            self.shape = None
            self.map.clear()
            self.refresh()
            return
        self.nuc_channel = next((n for n in names if n.strip().lower() == "dapi"), None)
        self.map.set_slide(self.shape, None)
        if self.nuc_channel is not None:
            w = workers.keep(workers.ThumbWorker(self._gen, slide_path, self.nuc_channel))
            w.done.connect(self._thumb_done)
            w.failed.connect(lambda g, m: g == self._gen and self.say(f"Thumbnail failed: {m}"))
            w.start()
        self._load_project()

    def _thumb_done(self, gen, arr):
        if gen == self._gen and self.shape is not None:
            self.map.set_slide(self.shape, arr)
            self.refresh_map()

    def _load_project(self):
        from ...core.tma import dearray as da
        from ...core.tma.study import StudyError, load_study
        try:
            self.study = load_study(self.project_dir)
        except StudyError as exc:
            self.say(str(exc))
            self.study = None
        self.study_mismatch = False
        if self.study is not None:
            # the project's study must be THIS slide's: another slide opened in
            # the same project is refused, nothing is loaded or detected
            from ...core import provenance as prov
            try:
                sid = prov.ensure_raw_slide(self.project_dir, self.slide_path)
            except Exception as exc:                        # noqa: BLE001
                sid = None
                self.say(f"Cannot identify the slide: {exc}")
            if sid != self.study["slide_id"]:
                self.study_mismatch = True
                self.say(f"This project's TMA study is for another slide ({self.study.get('slide_path')}); "
                         "open that slide, or use another project for this one.")
                self.study = self.candidates = self.holder = None
                self.refresh()
                return
        self.candidates = None
        if self.study is not None:
            n = da.next_version(self.project_dir) - 1
            if n >= 1:
                self.candidates = da.load_candidates(self.project_dir, n)
            self.numbering.set_records([r for r in self._all_records() if r["donor"]])
        draft = K.load_draft(self.project_dir)
        versions = K.locked_versions(self.project_dir)
        if draft is not None:
            self._open_draft(draft)
        elif versions:
            self._open_locked(versions[-1])
        elif self.study is not None and self.candidates is not None:
            self._open_draft(L.new_draft(self.study, self.candidates))
        self.refresh()

    def _open_draft(self, d):
        self.flush_draft()
        self.view_version = None
        self.holder = DraftHolder(d, autosave=self._schedule_save)
        K.save_draft(self.project_dir, d)

    def _schedule_save(self, _d):
        """Autosave after a short pause: one write per burst of edits (each
        write syncs the file to disk, ~0.25 s on the HDD)."""
        self._save_timer.start()

    def flush_draft(self):
        """Write the pending autosave now (context switch, lock, close)."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_now()

    def _save_now(self):
        if self.holder is not None and not self.holder.read_only and self.project_dir:
            K.save_draft(self.project_dir, self.holder.d)

    def _open_locked(self, version):
        rec = K.load_locked(self.project_dir, version)
        self.view_version = version
        self.holder = DraftHolder(K.draft_from_locked(rec), read_only=True)

    # ── edits ──
    def edit(self, fn, *args, **kw):
        """One undoable edit through the holder; errors are shown."""
        if self.holder is None:
            return None
        try:
            out = self.holder.do(fn, *args, **kw)
        except Exception as exc:                            # noqa: BLE001
            self.say(str(exc))
            return None
        self.refresh()
        return out

    def undo(self):
        if self.holder is not None and self.holder.undo():
            self.refresh()

    def redo(self):
        if self.holder is not None and self.holder.redo():
            self.refresh()

    def select(self, pid):
        self.selected = pid
        core = self.selected_core()
        if core is not None:
            key = self.holder.d["cores"][core]["position_key"]
            self.table.select_ident(key or core)
        elif pid is not None:
            self.table.select_ident(pid)
        self.refresh_map()

    def selected_core(self):
        if self.holder is None or self.selected not in (self.holder.d["pieces"] if self.holder else {}):
            return None
        return self.holder.d["pieces"][self.selected]["owner"]

    def core_diameter(self):
        if self.holder is not None:
            return float(self.holder.d["core_diameter_px"])
        return 1000.0

    def numbering_objects(self):
        d = self.holder.d if self.holder else {"cores": {}, "pieces": {}}
        out = [(u, *L.core_centre(d, u)) for u in d["cores"]]
        out += [(p, v["cy"], v["cx"]) for p, v in d["pieces"].items() if v["owner"] is None and not v["dropped"]]
        return out

    def _set_tool(self, tool):
        self.ctl.set_tool(tool)
        if tool == "merge" and self.selected_core() is None:
            self.say("Merge: select a core first, then click the pieces to add (Esc ends).")

    def _escape(self):
        if self.ctl.key("escape"):
            return
        if self.ctl.tool != "select":
            self.tool_group.button(0).setChecked(True)
            self.ctl.set_tool("select")
        else:
            self.select(None)

    def _enter_key(self):
        """Enter ends a click sequence (no more clicks are taken); Confirm
        still commits it (ruling, codex_tma_f3_p34_1.out)."""
        if self.ctl.tool == "click" and self.ctl.sequence:
            self.ctl.finished = True
            self.say("Sequence finished: check the preview, then Confirm.")

    def _delete_selected(self):
        if self.selected is not None and self.holder is not None and not self.holder.read_only:
            self.edit(L.drop_piece, self.selected, "left out")

    def say(self, text):
        self.lbl_info.setText(text)
        print(f"[TMA] {text}")

    def shutdown(self):
        """Wait for the page's jobs (window close). The page is left as it
        is: a refused close goes on with the job still counted."""
        self.flush_draft()
        return workers.shutdown()
