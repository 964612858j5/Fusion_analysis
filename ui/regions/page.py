"""The Regions page, "Step 0.5: Regions" (docs/region_qc_module_plan.md §4):
a `StepFrame` page between Step0 and Step1.

* Title bar: the study / slide, `Version` (the draft, the locked versions,
  F3's read-only versions), `Discard draft`, `Lock`.
* Left tabs: `Channels` (the one public channel dock -- Step1's rows, the
  channel state shared with Step1 / Step3 -- and `Intensity…`), `Cores`
  (map sheet, Find cores, numbering, core table).
* Right tab `Viewer`: the tool row over the viewer slot; the viewer is the
  third resident Step1 mount (`mount.RegionsViewerHost`, built by the
  window) with the overlay above it.

One document per slide (`core.regions`), one draft, one undo history.
The window only wires: `set_context`, `jump_box`, `intensity_requested`.
"""

from __future__ import annotations

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QButtonGroup, QComboBox, QHBoxLayout, QLabel, QPushButton, QShortcut, QVBoxLayout, QWidget

from ...core.regions import document as D
from ...core.regions import lock as K
from ..tma.draft import DraftHolder
from .cores_panel import CoresPanel
from .tools import ToolController
from .versions import VersionsMixin

TOOL_BUTTONS = (("Select", "select", "Select; drag the selected shape to move it, its handles to scale it"),
                ("Line", "line", "Number by line: drag along the cores; the wheel sets the band width"),
                ("Clicks", "click", "Number by clicks: click the cores in order; Backspace drops the last, Enter ends"),
                ("Merge", "merge", "Merge: with a core selected, click pieces to add them to it"),
                ("Circle", "circle", "Draw a circle: press at the centre, drag out the radius"))
VIEWER_PENDING_TEXT = "The whole-slide view opens when Regions is entered."


class _NoView:
    """Until the viewer is open: nothing is drawn, no camera."""

    def set_items(self, *a, **k):
        pass

    def set_line(self, *a):
        pass

    def set_sequence(self, *a):
        pass

    def set_circle(self, *a):
        pass

    def screen_px(self):
        return 1.0


class RegionsPage(QWidget, VersionsMixin):
    intensity_requested = pyqtSignal()

    def __init__(self, parent=None, metrics=None, title_bar=None):
        super().__init__(parent)
        self.setObjectName("regions_page")
        self.ctx = {}
        self.holder = None
        self.view_version = None           # None = draft; ("regions", n) / ("f3", n) = read-only
        self.selected = None
        self.view = _NoView()
        self.jump_box = None               # set by the window: callable(bbox) -> bool
        self.ctl = ToolController(self)
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
        self.lbl_study = QLabel("")
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

        chan = QWidget()
        cl = QVBoxLayout(chan)
        cl.setContentsMargins(0, 3, 0, 0)
        box = QtWidgets.QGroupBox("Channels")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        self.btn_intensity = QPushButton("Intensity…")
        head.addWidget(self.btn_intensity)
        head.addStretch()
        bl.addLayout(head)
        self._channels_host = bl
        cl.addWidget(box, 1)
        self._left_panel = chan
        self.frame.left_tabs.addTab(chan, "Channels")
        self.cores = CoresPanel(self)
        self.frame.left_tabs.addTab(self.cores, "Cores")

        right = QWidget()
        rl = QVBoxLayout(right)
        tools = QHBoxLayout()
        self.tool_group = QButtonGroup(self)
        for i, (text, key, tip) in enumerate(TOOL_BUTTONS):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setProperty("tool", key)
            self.tool_group.addButton(b, i)
            tools.addWidget(b)
        self.tool_group.button(0).setChecked(True)
        # user 2026-10-18: the outlines can be hidden to look at the tissue
        self.btn_outlines = QPushButton("Outlines")
        self.btn_outlines.setCheckable(True)
        self.btn_outlines.setChecked(True)
        self.btn_outlines.setToolTip("Show / hide the core outlines")
        tools.addWidget(self.btn_outlines)
        self.btn_undo, self.btn_redo = QPushButton("Undo"), QPushButton("Redo")
        self.btn_prev, self.btn_next = QPushButton("Prev"), QPushButton("Next")
        for b in (self.btn_undo, self.btn_redo, self.btn_prev, self.btn_next):
            tools.addWidget(b)
        tools.addStretch()
        notice = QLabel(VIEWER_PENDING_TEXT)
        notice.setAlignment(Qt.AlignCenter)
        notice.setWordWrap(True)
        notice.setStyleSheet("color:#888;font-size:12px;background:#111;")
        rl.addWidget(notice, 1)
        self._viewer_layout, self._viewer_notice = rl, notice
        self.frame.tool_row(rl, tools)
        self.frame.right_tabs.addTab(right, "Viewer")

        self.btn_intensity.clicked.connect(self.intensity_requested.emit)
        self.btn_lock.clicked.connect(self._lock_or_branch)
        self.btn_discard.clicked.connect(self._discard_draft)
        self.version_box.activated.connect(self._version_chosen)
        self.tool_group.buttonClicked.connect(lambda b: self._set_tool(b.property("tool")))
        self.btn_outlines.toggled.connect(self._outlines_toggled)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_prev.clicked.connect(lambda: self.cores.step_core(-1))
        self.btn_next.clicked.connect(lambda: self.cores.step_core(+1))
        self.frame.left_tabs.currentChanged.connect(lambda _i: self.ctl.cancel())
        for keys, fn in ((QKeySequence.Undo, self.undo), (QKeySequence("Ctrl+Shift+Z"), self.redo),
                         (QKeySequence(Qt.Key_Delete), self._delete_selected),
                         (QKeySequence(Qt.Key_Escape), self._escape),
                         (QKeySequence(Qt.Key_Backspace), lambda: self._key("backspace")),
                         (QKeySequence(Qt.Key_Return), lambda: self._key("enter")),
                         (QKeySequence(Qt.Key_Enter), lambda: self._key("enter"))):
            sc = QShortcut(keys, right)              # the viewer side only: the dock's search box keeps its keys
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self.refresh()

    # ── what the window reads ──
    def channels_host(self):
        return self._channels_host

    def left_panel(self):
        return self._left_panel

    def channel_column_splitter(self):
        return self.frame.splitter

    def viewer_layout(self):
        return self._viewer_layout

    def viewer_notice(self):
        return self._viewer_notice

    def set_viewer_notice(self, text=None):
        self._viewer_notice.setText(VIEWER_PENDING_TEXT if text is None else str(text))

    def set_view(self, view):
        self.view = view if view is not None else _NoView()
        self.refresh_view()

    # ── context ──
    def set_context(self, ctx):
        """`ctx`: project_dir, slide_path, slide_id, shape, workspace_id,
        handoff_path, handoff_sha256, full_wsi, channel_names. The same
        context keeps the open document; another one reloads."""
        key = (ctx.get("project_dir"), ctx.get("slide_id"))
        if key == (self.ctx.get("project_dir"), self.ctx.get("slide_id")) and self.holder is not None:
            moved = (ctx.get("workspace_id"), ctx.get("full_wsi")) != (self.ctx.get("workspace_id"),
                                                                     self.ctx.get("full_wsi"))
            self.ctx = dict(ctx)
            if moved:
                self.cores.reset_jobs()               # a late Find cores result of the old workspace is dropped
            if moved and not self.holder.read_only:
                # another workspace: the undo history stays with the one it was
                # made in (an ROI workspace must not undo whole-slide core edits)
                self.flush_draft()
                self.holder = DraftHolder(self.holder.d, autosave=lambda _d: self._save_timer.start())
            self._apply_session()
            self.refresh()
            return
        self.flush_draft()
        self.ctx = dict(ctx)
        self.holder, self.selected, self.view_version = None, None, None
        self.ctl.cancel()
        self.cores.reset()
        if ctx.get("project_dir") and ctx.get("slide_id"):
            self._load()
        self.refresh()

    def _load(self):
        from ...core.regions import migrate as M
        self.cores.load_study()
        try:
            draft = M.migrate_draft(self.ctx["project_dir"], self.shape(), self.ctx["slide_id"])
        except D.RegionsError as exc:
            self.say(str(exc))
            return
        versions = K.locked_versions(self.ctx["project_dir"])
        if draft is not None:
            self._open_draft(draft)
        elif versions:
            if not self._open_locked(("regions", versions[-1])):
                return
        else:
            d = D.new_document(self.ctx["slide_id"], self.shape())
            if self.cores.study is not None and self.cores.candidates is not None:
                D.attach_tma(d, self.cores.study, self.cores.candidates)
            self._open_draft(d)

    def _apply_session(self):
        if self.holder is not None and not self.holder.read_only:
            D.set_session(self.holder.d, self.ctx.get("workspace_id", ""), self.ctx.get("handoff_path", ""),
                          self.ctx.get("handoff_sha256", ""))

    def _open_draft(self, d):
        self.flush_draft()
        self.view_version = None
        self.holder = DraftHolder(d, autosave=lambda _d: self._save_timer.start())
        self._apply_session()
        K.save_draft(self.ctx["project_dir"], d)

    def _open_locked(self, which) -> bool:
        """Open a locked version read-only; a version this slide / program
        cannot read is refused with a message (False)."""
        from ...core.regions import migrate as M
        kind, n = which
        try:
            if kind == "regions":
                d = K.draft_from_locked(K.load_locked(self.ctx["project_dir"], n, self.ctx["slide_id"]))
            else:
                d = M.load_f3_locked(self.ctx["project_dir"], n, self.shape(), self.ctx["slide_id"])
        except (D.RegionsError, ValueError, KeyError, OSError) as exc:
            self.say(f"Version {n} cannot be opened: {exc}")
            return False
        self.view_version = which
        self.holder = DraftHolder(d, read_only=True)
        return True

    def flush_draft(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_now()

    def _save_now(self):
        if self.holder is not None and not self.holder.read_only and self.ctx.get("project_dir"):
            K.save_draft(self.ctx["project_dir"], self.holder.d)

    # ── helpers the tools and panels use ──
    def shape(self):
        return tuple(self.ctx.get("shape") or (1, 1))

    def cores_editable(self) -> bool:
        """TMA numbering only in a whole-slide workspace (plan §3)."""
        return bool(self.ctx.get("full_wsi", True))

    def core_diameter(self):
        d = self.holder.d if self.holder else None
        return float(d.get("core_diameter_px") or 1000.0) if d else 1000.0

    def numbering_objects(self):
        d = self.holder.d if self.holder else {"cores": {}, "pieces": {}}
        from ...core.regions import geometry as G
        out = [(u, *D.core_centre(d, u)) for u in d["cores"]]
        out += [(p, *G.centroid(v["polygon"])) for p, v in d["pieces"].items()
                if v["owner"] is None and not v["dropped"]]
        return out

    def edit(self, fn, *args, **kw):
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
            self._apply_session()             # the history is the geometry's; the session stays the live one
            self.refresh()

    def redo(self):
        if self.holder is not None and self.holder.redo():
            self._apply_session()
            self.refresh()

    def select(self, pid):
        self.selected = pid
        core = self.selected_core()
        if core is not None:
            self.cores.table.select_ident(self.holder.d["cores"][core]["position_key"] or core)
        elif pid is not None:
            self.cores.table.select_ident(pid)
        self.refresh_view()

    def selected_core(self):
        if self.holder is None or self.selected not in self.holder.d["pieces"]:
            return None
        return self.holder.d["pieces"][self.selected]["owner"]

    def jump_to(self, bbox):
        if self.jump_box is not None:
            self.jump_box(list(bbox))

    def say(self, text):
        self.cores.say(text)

    def piece_menu(self, pid):
        if self.cores_editable():
            self.cores.piece_menu(pid)

    def _set_tool(self, tool):
        if tool in ("line", "click", "merge") and not self.cores_editable():
            self.say("TMA numbering is done in a whole-slide workspace.")
        self.ctl.set_tool(tool)

    def _key(self, name):
        self.ctl.key(name)

    def _escape(self):
        if self.ctl.key("escape"):
            return
        if self.ctl.tool != "select":
            self.tool_group.button(0).setChecked(True)
            self.ctl.set_tool("select")
        else:
            self.select(None)

    def _delete_selected(self):
        if self.selected is not None and self.holder is not None and not self.holder.read_only \
                and self.cores_editable():
            self.edit(D.drop_piece, self.selected, "left out")

    # ── refresh ──
    def refresh(self):
        self.ctl.prune()
        if self.holder is not None and self.selected is not None and self.selected not in self.holder.d["pieces"]:
            self.selected = None
        self._refresh_title()
        self.cores.refresh()
        self.refresh_view()
        self.numbering_changed()
        self._sync_enabled()

    def numbering_changed(self):
        self.cores.numbering_changed()
        self.refresh_view()

    def outlines_visible(self) -> bool:
        return self.btn_outlines.isChecked()

    def _outlines_toggled(self, on):
        if not on:                         # nothing invisible stays selected or editable
            self.selected = None
            self.ctl.cancel()
        self.refresh_view()

    def refresh_view(self):
        if self.btn_outlines.isChecked():
            self.cores.draw(self.view)
        else:
            self.view.set_items([])
        self.view.set_line(self.ctl.line_band())
        self.view.set_sequence(self.ctl.sequence_points() if self.ctl.tool == "click" else [])
        self.view.set_circle(self.ctl.preview_circle())

    def _sync_enabled(self):
        has = self.holder is not None
        editable = has and not self.holder.read_only
        for b in self.tool_group.buttons():
            b.setEnabled(editable or b.property("tool") == "select")
        self.btn_undo.setEnabled(editable and self.holder.can_undo)
        self.btn_redo.setEnabled(editable and self.holder.can_redo)
        self.btn_lock.setEnabled(has)
        self.btn_lock.setText("New draft from this version" if has and self.holder.read_only else "Lock")
        self.btn_discard.setEnabled(editable)
        self.cores.sync_enabled(editable)

    def shutdown(self):
        from ..tma import workers
        self.flush_draft()
        return workers.shutdown()
