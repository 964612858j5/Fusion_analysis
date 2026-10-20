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
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QMenu, QPushButton, QShortcut, QToolButton,
                             QVBoxLayout, QWidget)

from ...core.regions import document as D
from ...core.regions import lock as K
from ..tma.draft import DraftHolder
from .cores_panel import CoresPanel
from .exclusions_panel import ExclusionsPanel
from .tools import ToolController
from .shapes import ShapesMixin
from .versions import VersionsMixin

# the three tool families (docs/regions_tools_redesign.md §1, §9): a click on
# the button opens its menu; choosing an item lights the family
FAMILIES = (("Select", (("Click", "select", "Click a shape; Ctrl+click adds / removes; a drag on blank "
                                            "ground pans. The selected shape has a transform box as Photoshop's "
                                            "Ctrl+T: drag inside to move, a handle to resize (Shift keeps the "
                                            "ratio, Alt about the centre), just outside a corner to rotate "
                                            "(Shift: 15°), Ctrl+corner to distort; Enter applies, Esc cancels"),
                        ("Rectangle", "select_rect", "Drag a rectangle: the shapes whose centre is inside"),
                        ("Ellipse", "select_ellipse", "Drag an ellipse: the shapes whose centre is inside"),
                        ("Polygon", "select_polygon", "Click the corners; Enter / double-click / the first "
                                                      "point closes it: the shapes whose centre is inside"))),
            ("Mask", (("Circle", "circle", "Press on the outline, drag to the opposite side: the press stays on the "
                                          "outline (Shift: a circle on that diameter, Alt: from the centre)"),
                      ("Polygon", "polygon", "Click the vertices; Enter or a click on the first closes it, "
                                             "Backspace drops the last"),
                      ("Vertex", "vertex", "Drag one vertex of the selected shape"))),
            ("Annotate", (("Line", "line", "Drag a line along the cores (the wheel while dragging sets the band "
                                           "width); a click on a core of the line adds / removes it"),
                          ("Clicks", "click", "Click the cores in order; Backspace drops the last, Enter ends"))))
CORE_TOOLS = ("line", "click")
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

    def set_draft_polygon(self, *a):
        pass

    def set_marquee(self, *a):
        pass

    def set_live(self, *a):
        pass

    def screen_px(self):
        return 1.0


class RegionsPage(QWidget, VersionsMixin, ShapesMixin):
    intensity_requested = pyqtSignal()
    saved = pyqtSignal()                       # a Regions Save was written

    def __init__(self, parent=None, metrics=None, title_bar=None):
        super().__init__(parent)
        self.setObjectName("regions_page")
        self.ctx = {}
        self.holder = None
        self._saved_info = None            # the last Save: {"at", "issues"}
        self._saved_at_mark = None
        self.selection = []                # the selected shapes (pieces or exclusions), in click order
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
        self.lbl_saved = QLabel("")
        row.addWidget(self.lbl_saved)
        self.btn_revert = QPushButton("Revert to saved")
        self.btn_save = QPushButton("Save")
        self.btn_save.setToolTip("Save now (replaces the last Save); unfinished work is saved as it is")
        row.addWidget(self.btn_revert)
        row.addWidget(self.btn_save)
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
        # the window styles and aligns this row as Step0's / Step1's (§13.7)
        self.channels_head, self.channels_box_layout = head, bl
        self._channels_host = bl
        cl.addWidget(box, 1)
        self._left_panel = chan
        self.frame.left_tabs.addTab(chan, "Channels")
        self.cores = CoresPanel(self)
        self.frame.left_tabs.addTab(self.cores, "Cores")
        self.exclusions = ExclusionsPanel(self)
        self.frame.left_tabs.addTab(self.exclusions, "Exclusions")

        right = QWidget()
        rl = QVBoxLayout(right)
        tools = QHBoxLayout()
        self.family_buttons = {}
        for fam, items in FAMILIES:
            b = QToolButton()
            b.setText(f"{fam} ▾")
            b.setCheckable(True)
            b.setPopupMode(QToolButton.InstantPopup)       # a click opens the menu (user 2026-10-18)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            menu = QMenu(b)
            for text, key, tip in items:
                act = menu.addAction(text)
                act.setToolTip(tip)
                act.setData(key)
                act.triggered.connect(lambda _c=False, k=key: self._set_tool(k))
            menu.setToolTipsVisible(True)
            b.setMenu(menu)
            b.setProperty("family", fam)
            self.family_buttons[fam] = b
            tools.addWidget(b)
        self.btn_merge = QToolButton()               # user 2026-10-18: two kinds, the user chooses
        self.btn_merge.setText("Merge ▾")
        self.btn_merge.setPopupMode(QToolButton.InstantPopup)
        mmenu = QMenu(self.btn_merge)
        mmenu.setToolTipsVisible(True)
        for text, mode, tip in (("Group (same core)", "group",
                                 "The pieces belong to one core; each keeps its own outline"),
                                ("Union (fuse outlines)", "union",
                                 "The pieces fuse into one outline (e.g. a patch drawn onto a core); "
                                 "they must touch")):
            act = mmenu.addAction(text)
            act.setToolTip(tip)
            act.triggered.connect(lambda _c=False, m=mode: self.merge_selected(m))
        self.btn_merge.setMenu(mmenu)
        tools.addWidget(self.btn_merge)
        self.btn_undo, self.btn_redo = QToolButton(), QToolButton()
        self.btn_undo.setText("↶")
        self.btn_redo.setText("↷")
        self.btn_undo.setToolTip("Undo (Ctrl+Z): first the half-done action, then the last edit")
        self.btn_redo.setToolTip("Redo (Ctrl+Shift+Z)")
        for b in (self.btn_undo, self.btn_redo):
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
        self.btn_save.clicked.connect(self._save)
        self.btn_revert.clicked.connect(self._revert_to_saved)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.frame.left_tabs.currentChanged.connect(self._tab_changed)
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
        self.commit_transform()                   # in the old context, before anything changes
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
                was_saved = self._saved_mark() == self._saved_at_mark
                self.holder = DraftHolder(self.holder.d, autosave=lambda _d: self._save_timer.start())
                if was_saved:                         # the same document in a new holder: still saved
                    self._saved_at_mark = self._saved_mark()
            self._apply_session()
            self.refresh()
            return
        self.flush_draft()
        self.ctx = dict(ctx)
        self.holder, self.selection = None, []
        self._saved_info = self._saved_at_mark = None
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
        if draft is None:
            self._start_document()
            return
        self._open_draft(draft)                   # the working copy: unsaved changes may be in it
        try:
            rec = K.load_saved(self.ctx["project_dir"], self.ctx["slide_id"])
        except (D.RegionsError, ValueError, OSError):
            rec = None
        if rec is not None:                       # unsaved unless the working copy is the Save itself
            same = K.draft_from_saved(rec) == dict(draft, session=None)
            self._remember_saved(rec, None if same else ("differs",))

    def _apply_session(self):
        if self.holder is not None and not self.holder.read_only:
            D.set_session(self.holder.d, self.ctx.get("workspace_id", ""), self.ctx.get("handoff_path", ""),
                          self.ctx.get("handoff_sha256", ""))

    def _open_draft(self, d):
        self.flush_draft()
        self.holder = DraftHolder(d, autosave=lambda _d: self._save_timer.start())
        self._apply_session()
        K.save_draft(self.ctx["project_dir"], d)

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
        self.commit_transform()                   # every edit applies a pending transform first
        try:
            out = self.holder.do(fn, *args, **kw)
        except Exception as exc:                            # noqa: BLE001
            self.say(str(exc))
            return None
        self.refresh()
        return out

    def undo(self):
        """Ctrl+Z steps back one level and never turns the tool off (user
        2026-10-18): a half-done action first, then an unconfirmed
        numbering preview, then the last edit."""
        if self.ctl.busy():
            self.ctl.abort_gesture()
            return
        if self.ctl.pressed:
            self.ctl.abort_gesture()          # the click still under the button: that is the one level
            return
        if self.ctl.transform is not None:    # a pending transform: back one drag (the document untouched)
            if not self.ctl.transform.step_back():
                self.ctl.transform = None
            self.refresh_view()
            return
        if self.cores.select_preview_pending():   # a Select ▸ Click numbering not confirmed yet
            self.select(None)
            return
        if self.ctl.has_preview():
            self.ctl.cancel()
            return
        if self.holder is not None and self.holder.undo():
            self._apply_session()             # the history is the geometry's; the session stays the live one
            self.refresh()

    def redo(self):
        if self.ctl.transform is not None:
            return
        if self.holder is not None and self.holder.redo():
            self._apply_session()
            self.refresh()

    def say(self, text):
        """A message on the open tab (Cores and Exclusions each have a line)."""
        self.cores.say(text)
        self.exclusions.lbl_info.setText(text)

    def piece_menu(self, pid):
        self.shape_menu(pid)

    def _set_tool(self, tool):
        self.commit_transform()
        if tool in CORE_TOOLS and not self.cores_editable():
            self.say("TMA numbering is done in a whole-slide workspace.")
        if tool in CORE_TOOLS and self.exclusions_tab_open():
            self.say("Numbering is done in the Cores tab.")
            tool = None
        self.ctl.set_tool(tool)
        self._show_tool()

    def _show_tool(self):
        """The lit family shows its tool: "Select: Rectangle ▾"."""
        for fam, items in FAMILIES:
            b = self.family_buttons[fam]
            hit = next((text for text, key, _t in items if key == self.ctl.tool), None)
            b.setChecked(hit is not None)
            b.setText(f"{fam}: {hit} ▾" if hit else f"{fam} ▾")

    def _key(self, name):
        if name == "enter" and self.ctl.transform is not None and not self.ctl.busy():
            self.commit_transform()
            return
        self.ctl.key(name)

    # ── the pending free transform (§10.5) ──
    def commit_transform(self):
        """Apply the pending transform: one ordinary edit (one undo step)."""
        s = self.ctl.transform
        self.ctl.transform = None
        if s is not None and self.ctl._drag is not None and self.ctl._drag[0] == "xf":
            self.ctl._drag = None                 # applied mid-drag: the rest of that drag does nothing
            self.ctl._swallow = True
        if s is None:
            return
        if not s.changed or self.holder is None or self.holder.read_only:
            self.refresh_view()                   # back where it was: the shape returns to the cached layer
            return
        if s.drag is not None:
            s.end()
        if s.sid not in self.holder.d["pieces"] and s.sid not in self.holder.d["exclusions"]:
            return
        shape = self.shape()
        poly = [[min(max(int(round(x)), 0), shape[1] - 1), min(max(int(round(y)), 0), shape[0] - 1)]
                for x, y in s.preview()]
        try:
            self.holder.do(D.set_polygon, s.sid, poly)
        except Exception as exc:                            # noqa: BLE001
            self.say(f"The transform was not applied: {exc}")
        self.refresh()

    def cancel_transform(self):
        if self.ctl.transform is not None:
            self.ctl.transform = None
            self.refresh_view()

    def _escape(self):
        """Esc: the half-done action goes and the tool is turned off (the
        view pans again). A pending transform is dropped first, the tool kept."""
        had_transform = self.ctl.transform is not None
        if self.ctl.busy() or self.ctl.pressed:
            self.ctl.abort_gesture()
        if had_transform:
            self.cancel_transform()
            return
        self.ctl.set_tool(None)
        self._show_tool()

    # ── refresh ──
    def refresh(self):
        self.ctl.prune()
        if self.holder is not None:
            self.selection = [s for s in self.selection if s in self.holder.d["pieces"] or s in self.holder.d["exclusions"]]
            if self.exclusions_tab_open() and not self._can_convert_cores():
                # masks are picked here only to become exclusions; not in an ROI / a read-only version
                self.selection = [s for s in self.selection if s in self.holder.d["exclusions"]]
        else:
            self.selection = []
        self._refresh_title()
        self.cores.refresh()
        self.exclusions.refresh()
        self.refresh_view()
        self.numbering_changed()
        self._sync_enabled()

    def numbering_changed(self):
        self.cores.numbering_changed()
        self.refresh_view()

    def refresh_view(self):
        items, _sel, _h, order = self.cores.draw_parts()
        items = items + self.exclusions.draw_items()
        sel = set(self.selection) | self.ctl.marquee_hits()     # the marquee's catch shows live
        xf = self.ctl.transform
        live = None
        if xf is not None:                                       # the pending transform: drawn live, dashed
            live = next((dict(it, polygon=xf.preview(), dash=True) for it in items if it["id"] == xf.sid), None)
            items = [it for it in items if it["id"] != xf.sid]
        single = self.selected
        poly = None
        if self.holder is not None and single is not None and not self.holder.read_only:
            shapes = self.holder.d["exclusions" if self.exclusions_tab_open() else "pieces"]
            poly = shapes[single]["polygon"] if single in shapes else None
        box = self.ctl.box() if self.ctl.tool == "select" and self.ctl.can_edit() else None
        self.view.set_items(items, selected=sel, handles_for=None,
                            order_labels=order, vertices_for=poly if self.ctl.tool == "vertex" else None)
        self.view.set_live(live, None if box is None else [list(q) for q in box])
        self.refresh_preview()
        self.view.set_line(self.ctl.line_band())
        self.view.set_sequence(self.ctl.sequence_points() if self.ctl.tool == "click" else [])

    def refresh_preview(self):
        """Only what follows the mouse (the polygon being clicked, a marquee,
        the circle): cheap enough for every mouse move."""
        self.view.set_draft_polygon(self.ctl.draft_points())
        self.view.set_marquee(self.ctl.marquee_preview())
        self.view.set_circle(self.ctl.preview_circle())
        self._sync_undo()

    def refresh_live(self):
        """A transform drag: only the moving shape and its box."""
        xf = self.ctl.transform
        if xf is None:
            return
        item = getattr(self.view, "live", None)
        item = dict(item, polygon=xf.preview()) if item else None
        self.view.set_live(item, [list(q) for q in xf.quad])
        self._sync_undo()

    def _sync_undo(self):
        h = self.holder                                    # ↶ also steps back a half-done gesture
        editable = h is not None and not h.read_only
        self.btn_undo.setEnabled(editable and (h.can_undo or self.ctl.busy() or self.ctl.has_preview()
                                               or self.ctl.transform is not None
                                               or self.cores.select_preview_pending()))
        self.btn_redo.setEnabled(editable and h.can_redo and self.ctl.transform is None)

    def _sync_enabled(self):
        has = self.holder is not None
        editable = has and not self.holder.read_only
        self.family_buttons["Mask"].setEnabled(editable)
        self.family_buttons["Annotate"].setEnabled(editable and self.cores_editable() and not self.exclusions_tab_open())
        why = self.merge_blocker()
        self.btn_merge.setEnabled(why is None)
        self.btn_merge.setToolTip("Merge the selected pieces into one core" + (f" ({why})" if why else ""))
        self._sync_undo()
        self.btn_save.setEnabled(editable and bool(self.ctx.get("project_dir")))
        self.btn_revert.setEnabled(editable and self._saved_info is not None)
        self.cores.sync_enabled(editable)

    def hideEvent(self, event):  # noqa: N802
        self.commit_transform()                   # leaving Regions applies a pending transform
        super().hideEvent(event)

    def shutdown(self):
        from ..tma import workers
        self.commit_transform()
        self.flush_draft()
        return workers.shutdown()
