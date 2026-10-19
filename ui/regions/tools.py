"""The Regions page's tools (docs/regions_tools_redesign.md §1-§3, §9): what
a mouse action on the viewer means. Shapes are polygons (level-0 whole
pixels); the overlay delivers level-0 points.

Families (one dropdown each): Select -- `select` (click; Ctrl+click adds /
removes), `select_rect`, `select_ellipse`, `select_polygon`; Mask --
`circle` (an ellipse whose outline passes through the press and the
pointer, diagonally opposite -- the press stays put: Shift a circle on that
diameter, Alt the press is the centre),
`polygon` (a vertex at each press), `vertex` (drag a vertex of the one
selected shape); Annotate -- `line` (drag), `click` (click in order). None
lit (`tool is None`) by default. With `select`, the one selected shape has
Photoshop's free-transform box (`core.regions.transform`, §10.5): it is a
preview until Enter (Esc drops it; the page applies it before anything
else happens).

Mouse rules: with no tool, or with `select` on blank ground, a left drag
pans; any other tool takes the whole left drag from the press (the view
does not pan). The wheel zooms unless a drag is in progress (while a Line
is dragged it sets the band width). Space held at the press pans that one
drag whatever the tool. The shapes are the open tab's: tissue pieces in
Cores / Channels, exclusions in Exclusions. Pieces are read-only in an ROI
workspace; exclusions are edited in every workspace.
"""

from __future__ import annotations

import math
from typing import List, Optional

from PyQt5.QtCore import Qt

from ...core.regions import document as D
from ...core.regions import geometry as G
from ...core.regions import numbering as N
from ...core.regions import transform as T

TOOLS = (None, "select", "select_rect", "select_ellipse", "select_polygon",
         "circle", "polygon", "vertex", "line", "click")
SELECT_TOOLS = ("select", "select_rect", "select_ellipse", "select_polygon")
MASK_TOOLS = ("circle", "polygon", "vertex")
ANNOTATE_TOOLS = ("line", "click")
HANDLE_TOL_PX = 8


class ToolController:
    def __init__(self, page):
        self.page = page
        self.tool = None
        self.line = None              # ((y0, x0), (y1, x1))
        self.band = None
        self.sequence: List[str] = []
        self.removed: set = set()
        self.added: set = set()
        self.finished = False
        self.poly_pts: List = []      # a polygon (mask or selection) being clicked, [(y, x)]
        self.pointer = None
        self.marquee = None           # ("rect" | "ellipse", (y0, x0), (y1, x1)) while dragging
        self._drag = None
        self._circle = None
        self._swallow = False         # a gesture cancelled while the button is still down
        self.pressed = False          # the left button is down on the viewer (a press not yet released)
        self.transform = None         # the pending free transform (`T.Session`), never in the document

    # ── tools ──
    def set_tool(self, tool):
        self.tool = tool              # first: the refresh in `cancel` draws this tool's handles
        self.cancel()

    def cancel(self):
        """Everything half done goes (not the selection)."""
        self.line, self.sequence, self.removed, self.added = None, [], set(), set()
        self.finished = False
        self._circle = None
        self.poly_pts = []
        self.marquee = None
        self.page.numbering_changed()

    @property
    def doc(self):
        h = self.page.holder
        return h.d if h is not None else None

    # ── what the tools work on ──
    def target(self) -> str:
        return "exclusions" if self.page.exclusions_tab_open() else "pieces"

    def shapes(self):
        d = self.doc
        return {} if d is None else d[self.target()]

    def can_edit(self) -> bool:
        page = self.page
        if page.holder is None or page.holder.read_only:
            return False
        return self.target() == "exclusions" or page.cores_editable()

    def can_number(self) -> bool:
        page = self.page
        return (page.holder is not None and not page.holder.read_only and page.cores_editable()
                and self.target() == "pieces")

    # ── hit tests ──
    def shown(self, sid, s):
        """A shape's polygon as on screen (the preview under a pending transform)."""
        xf = self.transform
        return xf.preview() if xf is not None and xf.sid == sid else s["polygon"]

    def _smallest_at(self, shapes, y, x) -> Optional[str]:
        """The smallest shape under the point, as shown: a shape under a
        pending transform is hit where its preview is."""
        best = None
        for sid, s in shapes.items():
            poly = self.shown(sid, s)
            if G.contains(poly, y, x):
                a = G.area(poly)
                if best is None or a < best[0]:
                    best = (a, sid)
        return best[1] if best else None

    def shape_at(self, y, x) -> Optional[str]:
        """The shape a click picks: the open tab's -- and in Exclusions a core
        mask too where no exclusion is (it becomes one once a reason is
        ticked, user 2026-10-19)."""
        sid = self._smallest_at(self.shapes(), y, x)
        if sid is None and self.target() == "exclusions" and self.page._can_convert_cores():
            sid = self.piece_at(y, x)
            if sid is not None and self.doc["pieces"][sid]["dropped"]:
                sid = None
        return sid

    def piece_at(self, y, x) -> Optional[str]:
        d = self.doc
        return None if d is None else self._smallest_at(d["pieces"], y, x)

    def object_at(self, y, x) -> Optional[str]:
        pid = self.piece_at(y, x)
        if pid is None:
            return None
        p = self.doc["pieces"][pid]
        return None if p["dropped"] else (p["owner"] or pid)

    def _single(self):
        sid = self.page.selected
        s = self.shapes().get(sid) if sid is not None else None
        return (sid, [list(q) for q in s["polygon"]]) if s is not None else (None, None)

    def box(self):
        """The transform box of the one selected shape ((x, y) quad), or None."""
        sid, poly = self._single()
        if poly is None:
            return None
        if self.transform is not None and self.transform.sid == sid:
            return self.transform.quad
        return T.box_quad(poly)

    def box_hit(self, y, x):
        q = self.box() if self.tool == "select" and self.can_edit() else None
        return None if q is None else T.hit(q, x, y, HANDLE_TOL_PX * self.page.view.screen_px())

    def _other_shape_at(self, y, x) -> bool:
        sid = self.shape_at(y, x)
        return sid is not None and sid != self.page.selected

    def cursor_at(self, y, x):
        """The pointer's shape over the box: "move", "rotate", or a resize
        direction "hor" / "ver" / "fdiag" / "bdiag" for a handle."""
        h = self.box_hit(y, x)
        if h is None or (h[0] == "rotate" and self._other_shape_at(y, x)):
            return None
        kind, i = h
        if kind != "handle":
            return kind
        q = self.box()
        v = T.handle_points(q)[i] - T.centre(q)
        a = math.degrees(math.atan2(v[1], v[0])) % 180.0
        return ("hor", "fdiag", "ver", "bdiag")[int(((a + 22.5) % 180.0) // 45.0)]

    def vertex_at(self, y, x) -> Optional[int]:
        _sid, poly = self._single()
        if poly is None:
            return None
        tol = HANDLE_TOL_PX * self.page.view.screen_px()
        best = None
        for i, (vx, vy) in enumerate(poly):
            dist = math.hypot(vy - y, vx - x)
            if dist <= tol and (best is None or dist < best[0]):
                best = (dist, i)
        return best[1] if best else None

    # ── gestures ──
    def busy(self) -> bool:
        """A gesture in progress (a drag, a polygon being clicked)."""
        return self._drag is not None or bool(self.poly_pts) or self.marquee is not None

    def has_preview(self) -> bool:
        return bool(self.sequence or self.line)

    def abort_gesture(self):
        """Ctrl+Z / Esc during a gesture: geometry rolled back; the release
        that may still come does not commit anything."""
        if self._drag is not None and self._drag[0] == "vertex":
            self.page.holder.cancel_drag()
        if self._drag is not None and self._drag[0] == "xf" and self.transform is not None:
            self.transform.abort_drag()                   # back to where this drag started
            if not self.transform.history and not self.transform.changed:
                self.transform = None
        if self._drag is not None or self.pressed:
            self._swallow = True          # the release still to come commits nothing
        self.pressed = False              # cancelled once: a lost release cannot hold Ctrl+Z here
        if self._drag is not None and self._drag[0] == "line":
            self.line = None
            self.sequence = []
        self._drag = None
        self._circle = None
        self.poly_pts = []
        self.marquee = None
        self.page.refresh()

    def press(self, y, x, ctrl=False) -> bool:
        """A left press. True: the tool takes this whole drag (the view does
        not pan); False: pyqtgraph pans (no tool, or Select on blank ground)."""
        page, tool = self.page, self.tool
        self._drag = None
        self._swallow = False
        self.pressed = True
        if tool is None:
            return False
        if tool == "select":
            h = self.box_hit(y, x)
            if h is not None and h[0] == "rotate" and self._other_shape_at(y, x):
                h = None                          # the rotate zone never hides a neighbour's click
            if h is not None and not (ctrl and not (h[0] == "handle" and h[1] < 4)):
                kind, i = h                       # the free transform (Ctrl only on a corner: distort)
                sid, poly = self._single()
                if self.transform is None or self.transform.sid != sid:
                    if self.transform is not None:
                        page.commit_transform()
                    self.transform = T.Session(sid, poly)
                mode = "distort" if ctrl else ("scale" if kind == "handle" else kind)
                self.transform.begin(mode, i, (x, y))
                self._drag = ("xf",)
                page.refresh_view()               # once: the shape leaves the cached layer for the live one
                return True
            if ctrl:
                return True                       # Ctrl: a selection click
            return self.shape_at(y, x) is not None   # on a shape: a click; blank ground: pan
        if tool in ("select_rect", "select_ellipse"):
            self._drag = ("marquee", (y, x), ctrl)
            return True
        if tool == "vertex":
            i = self.vertex_at(y, x) if self.can_edit() else None
            if i is not None:
                sid, poly = self._single()
                self._drag = ("vertex", sid, poly, i)
                page.holder.begin_drag()
            return True
        if tool == "circle":
            if self.can_edit():
                self._drag = ("circle", (y, x))   # the fixed corner (Alt: the centre)
            return True
        if tool == "line":
            if self.can_number():
                self._drag = ("line", (y, x))
            return True
        if tool in ("polygon", "select_polygon"):
            # the point goes down with the button, however far the mouse
            # moves before the release (as Photoshop's polygonal lasso)
            if tool == "select_polygon" or self.can_edit():
                self._polygon_click(y, x)
            self._swallow = True                  # its release adds nothing more
        return True                               # polygon / select_polygon / click: clicks, no pan

    def drag(self, y, x, finish=False, mods=None):
        """The pointer moved with the button down; `mods`: the keyboard
        modifiers now (Shift / Alt act at any moment of a drag)."""
        if self._drag is None:
            return
        page, kind = self.page, self._drag[0]
        shape = page.shape()
        mods = int(mods or 0)
        shift, alt = bool(mods & int(Qt.ShiftModifier)), bool(mods & int(Qt.AltModifier))
        if kind == "xf":
            if self.transform is None:            # applied meanwhile (an edit during the drag)
                self._drag = None
                return
            self.transform.move((x, y), shift=shift, alt=alt)
            page.refresh_live()                   # only the moving shape and its box are redrawn
            if finish:
                self._finish(kind)
            return
        elif kind == "vertex":
            _k, sid, poly, i = self._drag
            self.shapes()[sid]["polygon"] = G.move_vertex(poly, i, y, x, shape)
        elif kind == "marquee":
            self.marquee = ("rect" if self.tool == "select_rect" else "ellipse", self._drag[1], (y, x))
        elif kind == "circle":
            # the press and the pointer both lie ON the outline, diagonally
            # opposite (user 2026-10-18: the first point stays put): the
            # ellipse through the four corners of their box; Shift: the
            # circle on that diameter; Alt: the press is the centre
            ay, ax = self._drag[1]
            if alt:
                cy, cx, hy, hx = ay, ax, y - ay, x - ax
            else:
                cy, cx, hy, hx = (ay + y) / 2, (ax + x) / 2, (y - ay) / 2, (x - ax) / 2
            if shift:
                ry = rx = math.hypot(hy, hx)
            else:
                ry, rx = abs(hy) * math.sqrt(2), abs(hx) * math.sqrt(2)
            self._circle = (cy - ry, cx - rx, cy + ry, cx + rx)   # the ellipse's own box
        elif kind == "line":
            start = self._drag[1]
            if math.hypot(y - start[0], x - start[1]) >= 4 * page.view.screen_px():   # a drag, not a click
                self.line = (start, (y, x))
                self.removed, self.added = set(), set()
                if self.band is None:
                    self.band = page.core_diameter() / 2
                self._reselect()
        if kind == "circle":
            page.refresh_preview()                # only the preview moves: the outlines stay as drawn
        else:
            page.refresh_view()
        if finish:
            self._finish(kind)

    def _finish(self, kind):
        page = self.page
        drag, self._drag = self._drag, None
        if kind == "xf":
            if self.transform is None:
                return
            self.transform.end()
            if not self.transform.history and not self.transform.changed:
                self.transform = None
            else:
                page.say("Enter applies the transform, Esc cancels it.")
            self._swallow = True                  # the release is no selection click
            page.refresh_view()
        elif kind == "vertex":
            if G.area(self.shapes()[drag[1]]["polygon"]) <= 0:
                page.holder.cancel_drag()
                page.say("That would leave the shape without an area.")
            else:
                if self.shapes()[drag[1]]["polygon"] != drag[2]:
                    D.log(self.doc, {"kind": kind, "shape": drag[1]})
                page.holder.end_drag()
            page.refresh()
        elif kind == "marquee":
            m, self.marquee = self.marquee, None
            if m is not None:
                page.select_many(self._inside_marquee(m), add=drag[2])
            else:
                page.refresh_view()
        elif kind == "circle":
            c, self._circle = self._circle, None
            if c is not None and abs(c[2] - c[0]) > 2.0 and abs(c[3] - c[1]) > 2.0:
                page.finish_shape(G.ellipse_polygon(*c, page.shape()))
            else:
                page.refresh_view()

    def _inside_marquee(self, m):
        kind, (y0, x0), (y1, x1) = m
        out = []
        for sid, s in self.shapes().items():
            cy, cx = G.centroid(self.shown(sid, s))
            if kind == "rect":
                ok = min(y0, y1) <= cy <= max(y0, y1) and min(x0, x1) <= cx <= max(x0, x1)
            else:
                ry, rx = abs(y1 - y0) / 2 or 1e-9, abs(x1 - x0) / 2 or 1e-9
                my, mx = (y0 + y1) / 2, (x0 + x1) / 2
                ok = ((cy - my) / ry) ** 2 + ((cx - mx) / rx) ** 2 <= 1.0
            if ok:
                out.append(sid)
        return out

    def click(self, y, x, ctrl=False):
        """A left press + release without movement."""
        page, d = self.page, self.doc
        if self._swallow:
            self._swallow = False
            return
        if d is None or self.tool is None:
            return
        tool = self.tool
        if tool == "select":
            sid = self.shape_at(y, x)
            if ctrl:
                if sid is not None:
                    page.toggle_selection(sid)
            else:
                page.select(sid)
                if sid is None and self.target() == "exclusions" and self.piece_at(y, x) is not None:
                    page.say("Cores are selected in the Cores tab; this tab selects exclusions.")
        elif tool in ("polygon", "select_polygon"):
            if tool == "select_polygon" or self.can_edit():
                self._polygon_click(y, x)
        elif tool == "line" and self.line is not None and self.can_number():
            obj = self.object_at(y, x)                    # a finished line: add / remove a core
            if obj is not None:
                if obj in self.sequence:
                    self.removed.add(obj)
                    self.added.discard(obj)
                else:
                    self.added.add(obj)
                    self.removed.discard(obj)
                self._reselect()
        elif tool == "click" and self.can_number() and not self.finished:
            obj = self.object_at(y, x)
            if obj is not None and obj not in self.sequence:
                self.sequence.append(obj)
                self.page.numbering_changed()

    def hover(self, y, x):
        self.pointer = (y, x)
        if self.tool in ("polygon", "select_polygon") and self.poly_pts:
            self.page.refresh_preview()

    def wheel(self, delta, dragging=False) -> bool:
        """True = the tool takes the wheel: the band width while a Line is
        dragged; nothing (swallowed) during any other drag; otherwise the
        view zooms."""
        if not dragging:
            return False
        if self._drag is not None and self._drag[0] == "line" and self.line is not None:
            step = self.page.core_diameter() * 0.05
            self.band = max(step, (self.band or 0) + (step if delta > 0 else -step))
            self._reselect()
        return True

    def context(self, y, x):
        sid = self.shape_at(y, x)
        if sid is not None:
            self.page.shape_menu(sid)

    def key(self, name) -> bool:
        if self.tool in ("polygon", "select_polygon") and self.poly_pts:
            if name == "backspace":
                self.poly_pts.pop()
                self.page.refresh_view()
                return True
            if name == "enter":
                return self._close_polygon()
        if name == "backspace" and self.tool == "click" and self.sequence and not self.finished:
            self.sequence.pop()
            self.page.numbering_changed()
            return True
        if name == "enter" and self.tool == "click" and self.sequence:
            self.finished = True
            self.page.say("Sequence finished: check the preview, then Confirm.")
            return True
        return False

    # ── polygons ──
    def _polygon_click(self, y, x):
        if len(self.poly_pts) >= 3:
            fy, fx = self.poly_pts[0]
            if math.hypot(fy - y, fx - x) <= HANDLE_TOL_PX * self.page.view.screen_px():
                self._close_polygon()
                return
        self.poly_pts.append((y, x))
        self.page.refresh_preview()

    def released(self):
        """The left button came up (after `drag(finish)` / `click`)."""
        self.pressed = False
        self._swallow = False

    def double_click(self, y, x) -> bool:
        """True: the double-click closed a polygon (its release does nothing
        more); False: it counts as a plain press."""
        if self.tool in ("polygon", "select_polygon") and len(self.poly_pts) >= 3:
            self._close_polygon()
            self.pressed, self._swallow = True, True
            return True
        return False

    def _close_polygon(self) -> bool:
        if len(self.poly_pts) < 3:
            self.page.say("A polygon needs at least three points.")
            return True
        shape = self.page.shape()
        pts = [[min(max(int(round(x)), 0), shape[1] - 1), min(max(int(round(y)), 0), shape[0] - 1)]
               for y, x in self.poly_pts]
        self.poly_pts = []
        if self.tool == "select_polygon":
            inside = [sid for sid, s in self.shapes().items() if G.contains(pts, *G.centroid(self.shown(sid, s)))]
            self.page.select_many(inside, add=self.page.ctrl_held())
        else:
            self.page.finish_shape(pts)
        return True

    def draft_points(self):
        if self.tool in ("polygon", "select_polygon") and self.poly_pts and self.pointer is not None:
            return list(self.poly_pts) + [self.pointer]
        return list(self.poly_pts)

    # ── numbering ──
    def reverse(self):
        self.sequence.reverse()
        if self.line is not None:
            self.line = (self.line[1], self.line[0])
            self._reselect()
        else:
            self.page.numbering_changed()

    def prune(self):
        d = self.doc or {"cores": {}, "pieces": {}}
        if self.transform is not None and self.transform.sid not in self.shapes():
            self.transform = None                         # its shape went (undo, a version switch)

        def alive(o):
            if o in d["cores"]:
                return True
            p = d["pieces"].get(o)
            return p is not None and p["owner"] is None and not p["dropped"]
        if any(not alive(o) for o in self.sequence) or any(not alive(o) for o in self.added | self.removed):
            self.sequence = [o for o in self.sequence if alive(o)]
            self.added = {o for o in self.added if alive(o)}
            self.removed = {o for o in self.removed if alive(o)}
            if self.line is not None:
                self._reselect()
            else:
                self.page.numbering_changed()

    def preview_circle(self):
        return self._circle

    def line_band(self):
        return None if self.line is None else (self.line[0], self.line[1], self.band)

    def marquee_preview(self):
        return self.marquee

    def marquee_hits(self):
        return set(self._inside_marquee(self.marquee)) if self.marquee is not None else set()

    def _reselect(self):
        if self.line is None:
            return
        (ay, ax), (by, bx) = self.line
        if math.hypot(by - ay, bx - ax) < 1.0:
            return
        page = self.page
        objs = page.numbering_objects()
        sel = N.band_select(objs, self.line[0], self.line[1], self.band)
        sel = [o for o in sel if o not in self.removed]
        extra = [o for o in self.added if o not in sel]
        if extra:
            n = math.hypot(by - ay, bx - ax) or 1.0
            pos = {o: ((cy - ay) * (by - ay) + (cx - ax) * (bx - ax)) / n for o, cy, cx in objs}
            sel = sorted(sel + extra, key=lambda o: pos.get(o, 0.0))
        self.sequence = sel
        page.numbering_changed()

    def sequence_points(self):
        centres = {o: (cy, cx) for o, cy, cx in self.page.numbering_objects()}
        return [centres[o] for o in self.sequence if o in centres]
