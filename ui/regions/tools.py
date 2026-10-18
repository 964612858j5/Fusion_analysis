"""The Regions page's tools (docs/region_qc_module_plan.md §4): what a mouse
action on the viewer means in each tool. Shapes are polygons (level-0
whole pixels); the overlay delivers level-0 points.

* Select -- click picks a shape (the smallest one under the pointer); a
  drag on the SELECTED shape moves it, a drag on one of its 8 handles
  scales it about its centre (one undo step per drag); any other drag pans.
* Line -- drag a line; the cores in its band (wheel = band width) are
  ordered along it; a click adds / removes a core.
* Clicks -- each click appends a core; Backspace drops the last; Enter ends.
* Merge -- with a core selected, each clicked piece joins it.
* Circle -- press at the centre, drag out the radius: a new piece (64-gon).

`press` returns True only when the tool takes the drag; otherwise the
viewer pans as in Step1 (user 2026-10-18).
"""

from __future__ import annotations

import math
from typing import List, Optional

from ...core.regions import document as D
from ...core.regions import geometry as G
from ...core.regions import numbering as N

TOOLS = ("select", "line", "click", "merge", "circle")
HANDLE_TOL_PX = 8


def handle_points(poly) -> List:
    """The 8 handles of a shape: its box corners and edge midpoints (y, x)."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    return [(y, x) for y in (y0, ym, y1) for x in (x0, xm, x1) if (y, x) != (ym, xm)]


class ToolController:
    def __init__(self, page):
        self.page = page
        self.tool = "select"
        self.line = None              # ((y0, x0), (y1, x1))
        self.band = None
        self.sequence: List[str] = []
        self.removed: set = set()
        self.added: set = set()
        self.finished = False
        self._drag = None
        self._circle = None

    # ── tools ──
    def set_tool(self, tool):
        self.cancel()
        self.tool = tool

    def cancel(self):
        self.line, self.sequence, self.removed, self.added = None, [], set(), set()
        self.finished = False
        self._circle = None
        self.page.numbering_changed()

    @property
    def doc(self):
        h = self.page.holder
        return h.d if h is not None else None

    # ── hit tests ──
    def shape_at(self, y, x) -> Optional[str]:
        d = self.doc
        if d is None:
            return None
        best = None
        for pid, p in d["pieces"].items():
            if G.contains(p["polygon"], y, x):
                a = G.area(p["polygon"])
                if best is None or a < best[0]:
                    best = (a, pid)
        return best[1] if best else None

    def object_at(self, y, x) -> Optional[str]:
        pid = self.shape_at(y, x)
        if pid is None:
            return None
        p = self.doc["pieces"][pid]
        return None if p["dropped"] else (p["owner"] or pid)

    def handle_at(self, y, x) -> bool:
        pid = self.page.selected
        d = self.doc
        if d is None or pid not in d["pieces"]:
            return False
        tol = HANDLE_TOL_PX * self.page.view.screen_px()
        return any(math.hypot(hy - y, hx - x) <= tol for hy, hx in handle_points(d["pieces"][pid]["polygon"]))

    # ── mouse (from the overlay) ──
    def click(self, y, x, modifiers=None):
        page, d = self.page, self.doc
        if d is None:
            return
        if not page.outlines_visible() and self.tool in ("select", "merge"):
            return                         # hidden outlines are not picked
        if self.tool == "select":
            page.select(self.shape_at(y, x))
            return
        if not page.cores_editable() or page.holder.read_only:
            return
        elif self.tool == "line" and self.line is not None:
            obj = self.object_at(y, x)
            if obj is not None:
                if obj in self.sequence:
                    self.removed.add(obj)
                    self.added.discard(obj)
                else:
                    self.added.add(obj)
                    self.removed.discard(obj)
                self._reselect()
        elif self.tool == "click" and not self.finished:
            obj = self.object_at(y, x)
            if obj is not None and obj not in self.sequence:
                self.sequence.append(obj)
                self._show_sequence()
        elif self.tool == "merge":
            pid = self.shape_at(y, x)
            uid = page.selected_core()
            if pid is None or uid is None:
                page.say("Merge: select a core first (Select tool), then click the pieces to add.")
                return
            page.edit(D.merge, uid, pid)

    def press(self, y, x) -> bool:
        """Start of a left drag; True when the tool takes it."""
        page = self.page
        # in an ROI workspace the cores are read-only (plan §3): no shape edit
        editable = page.holder is not None and not page.holder.read_only and page.cores_editable()
        self._drag = None
        if self.tool == "line" and editable:
            if self.line is not None and self.object_at(y, x) is not None:
                return False                      # a click on a core adds / removes it (see `click`)
            self._drag = ("line", (y, x))
            self.removed, self.added = set(), set()
            return True
        if not editable or not page.outlines_visible():
            return False                   # hidden outlines are not edited: the view pans
        if self.tool == "select":
            pid = page.selected
            if pid in self.doc["pieces"]:
                poly = [list(q) for q in self.doc["pieces"][pid]["polygon"]]
                if self.handle_at(y, x):
                    cy, cx = G.centroid(poly)
                    d0 = max(1.0, math.hypot(y - cy, x - cx))
                    self._drag = ("scale", pid, poly, (cy, cx), d0)
                elif G.contains(poly, y, x):
                    self._drag = ("move", pid, poly, (y, x))
            if self._drag is None:
                return False
            page.holder.begin_drag()
            return True
        if self.tool == "circle":
            self._drag = ("circle", (y, x))
            return True
        return False

    def drag(self, y, x, finish=False):
        if self._drag is None:
            return
        page, kind = self.page, self._drag[0]
        shape = page.shape()
        if kind == "move":
            _k, pid, poly, (y0, x0) = self._drag
            self.doc["pieces"][pid]["polygon"] = G.translate(poly, y - y0, x - x0, shape)
        elif kind == "scale":
            _k, pid, poly, centre, d0 = self._drag
            f = max(0.05, math.hypot(y - centre[0], x - centre[1]) / d0)
            self.doc["pieces"][pid]["polygon"] = G.scale(poly, f, shape, about=centre)
        elif kind == "line":
            start = self._drag[1]
            self.line = (start, (y, x))
            if self.band is None:
                self.band = page.core_diameter() / 2
            self._reselect()
        elif kind == "circle":
            (cy, cx) = self._drag[1]
            self._circle = (cy, cx, max(2.0, math.hypot(y - cy, x - cx)))
        if kind in ("move", "scale"):
            page.refresh_view()
        elif kind == "circle":
            page.refresh_view()
        if finish:
            self._drag = None
            if kind in ("move", "scale"):
                page.holder.end_drag()
                page.refresh()
            elif kind == "circle" and self._circle is not None:
                cy, cx, r = self._circle
                self._circle = None
                pid = page.edit(D.add_circle_piece, cy, cx, r)
                if pid:
                    page.select(pid)

    def wheel(self, delta) -> bool:
        if self.tool == "line" and self.line is not None:
            step = self.page.core_diameter() * 0.05
            self.band = max(step, (self.band or 0) + (step if delta > 0 else -step))
            self._reselect()
            return True
        return False

    def context(self, y, x):
        pid = self.shape_at(y, x)
        if pid is not None:
            self.page.piece_menu(pid)

    def key(self, name) -> bool:
        if name == "backspace" and self.tool == "click" and self.sequence and not self.finished:
            self.sequence.pop()
            self._show_sequence()
            return True
        if name == "enter" and self.tool == "click" and self.sequence:
            self.finished = True
            self.page.say("Sequence finished: check the preview, then Confirm.")
            return True
        if name == "escape" and (self.sequence or self.line or self._drag is not None):
            if self._drag is not None and self._drag[0] in ("move", "scale"):
                self.page.holder.cancel_drag()
            self._drag = None
            self.cancel()
            return True
        return False

    def reverse(self):
        self.sequence.reverse()
        if self.line is not None:
            self.line = (self.line[1], self.line[0])
            self._reselect()
        else:
            self._show_sequence()

    def prune(self):
        """Drop numbering objects that no longer exist (an undo, a deletion)."""
        d = self.doc or {"cores": {}, "pieces": {}}

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
                self._show_sequence()

    # ── drawing state for the overlay ──
    def preview_circle(self):
        return self._circle

    def line_band(self):
        if self.line is None:
            return None
        return (self.line[0], self.line[1], self.band)

    # ── helpers ──
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

    def _show_sequence(self):
        self.page.numbering_changed()

    def sequence_points(self):
        centres = {o: (cy, cx) for o, cy, cx in self.page.numbering_objects()}
        return [centres[o] for o in self.sequence if o in centres]
