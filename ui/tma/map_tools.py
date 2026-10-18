"""The TMA page's map tools (F3, docs/tma_f3_page_application.md §1-§2):
what a mouse action on the slide map means in each tool.

* Select -- click picks a piece (the nearest centre among the circles under
  the pointer); drag moves it, drag on a handle sets its radius (one undo
  step per drag).
* Number by line -- drag a line; the cores in its band (wheel = band width)
  are ordered along it; a click adds / removes a core.
* Number by clicks -- each click appends a core; Backspace drops the last.
* Merge -- with a core selected, each clicked piece joins it.
* Draw circle -- press at the centre, drag out the radius: a new piece.

The numbering itself (target, preview, confirm) is the page's.
"""

from __future__ import annotations

import math
from typing import List, Optional

from ...core.tma import layout as L

TOOLS = ("select", "line", "click", "merge", "draw")


class MapController:
    def __init__(self, page):
        self.page = page
        self.tool = "select"
        self.line = None              # ((y0, x0), (y1, x1))
        self.band = None              # half width, level 0
        self.sequence: List[str] = []  # objects in image order (line or clicks)
        self.removed: set = set()     # objects taken out of the line's band
        self.added: set = set()
        self._drag = None             # ("move", pid, dy, dx) / ("resize", pid, r0, d0) / ("line",) / ("draw",)
        self.finished = False         # Enter ended the click sequence
        self._draw = None

    # ── tools ──
    def set_tool(self, tool):
        self.cancel()
        self.tool = tool

    def cancel(self):
        self.line, self.sequence, self.removed, self.added = None, [], set(), set()
        self.finished = False
        self.page.map.set_line(None)
        self.page.map.set_sequence([])
        self.page.numbering_changed()

    @property
    def numbering(self) -> bool:
        return self.tool in ("line", "click") and bool(self.sequence or self.line)

    # ── hit tests ──
    def piece_at(self, y, x) -> Optional[str]:
        if self.page.holder is None:
            return None
        d = self.page.holder.d
        best = None
        for pid, p in d["pieces"].items():
            dist = math.hypot(p["cy"] - y, p["cx"] - x)
            if dist <= p["r"] and (best is None or dist < best[0]):
                best = (dist, pid)
        return best[1] if best else None

    def object_at(self, y, x) -> Optional[str]:
        """A numbering object: the core of the piece under the pointer, or
        the piece itself while it is unassigned."""
        pid = self.piece_at(y, x)
        if pid is None:
            return None
        p = self.page.holder.d["pieces"][pid]
        if p["dropped"]:
            return None
        return p["owner"] or pid

    def handle_at(self, y, x) -> bool:
        pid = self.page.selected
        if pid is None or pid not in self.page.holder.d["pieces"]:
            return False
        p = self.page.holder.d["pieces"][pid]
        tol = 8 * self.page.map.screen_px()
        from .core_map import handle_points
        return any(math.hypot(hy - y, hx - x) <= tol for hy, hx in handle_points({"cy": p["cy"], "cx": p["cx"], "r": p["r"]}))

    # ── mouse (called by CoreMap) ──
    def map_click(self, y, x, modifiers):
        page = self.page
        if page.holder is None:
            return
        if self.tool == "select":
            page.select(self.piece_at(y, x))
        elif self.tool == "line":
            obj = self.object_at(y, x)
            if obj is not None and self.line is not None:
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
            pid = self.piece_at(y, x)
            uid = page.selected_core()
            if pid is None or uid is None:
                page.say("Merge: select a core first (Select tool), then click the pieces to add.")
                return
            page.edit(L.merge, uid, pid)

    def map_drag(self, phase, start, now) -> bool:
        """A left-button drag. Returns False at its start when the drag is
        not a tool's -- the map then pans, as in Step1 (user 2026-10-18):
        in Select only a drag on the SELECTED piece (or a handle) edits it;
        Line and Circle draw; everything else pans."""
        page = self.page
        editable = page.holder is not None and not page.holder.read_only
        if phase == "start":
            self._drag = None
            if not editable and self.tool != "line":
                return False
            if self.tool == "select":
                if self.handle_at(*start):
                    p = page.holder.d["pieces"][page.selected]
                    d0 = max(1.0, math.hypot(start[0] - p["cy"], start[1] - p["cx"]))
                    self._drag = ("resize", page.selected, p["r"], d0)   # keeps the grab point's offset
                else:
                    pid = self.piece_at(*start)
                    if pid is None or pid != page.selected:
                        return False                          # not on the selected piece: pan
                    p = page.holder.d["pieces"][pid]
                    self._drag = ("move", pid, p["cy"] - start[0], p["cx"] - start[1])
                page.holder.begin_drag()
            elif self.tool == "line" and page.holder is not None:
                self._drag = ("line",)
                self.removed, self.added = set(), set()
            elif self.tool == "draw":
                self._drag = ("draw",)
            else:
                return False
        if self._drag is None:
            return False
        kind = self._drag[0]
        if kind == "move":
            p = page.holder.d["pieces"][self._drag[1]]
            p["cy"], p["cx"] = now[0] + self._drag[2], now[1] + self._drag[3]
        elif kind == "resize":
            p = page.holder.d["pieces"][self._drag[1]]
            r0, d0 = self._drag[2], self._drag[3]
            p["r"] = max(2.0, r0 * math.hypot(now[0] - p["cy"], now[1] - p["cx"]) / d0)
        elif kind == "line":
            self.line = (start, now)
            if self.band is None:
                self.band = page.core_diameter() / 2
            self._reselect()
        elif kind == "draw":
            self._draw = (start, max(2.0, math.hypot(now[0] - start[0], now[1] - start[1])))
            page.map.set_line(None)
        if kind in ("move", "resize"):
            page.refresh_map()
        elif kind == "draw":
            page.refresh_map(extra={"id": "_draw", "cy": start[0], "cx": start[1], "r": self._draw[1],
                                    "color": "#4fc3f7", "dash": True})
        if phase == "finish":
            if kind in ("move", "resize"):
                page.holder.end_drag()
                page.refresh()
            elif kind == "draw" and self._draw is not None:
                (cy, cx), r = self._draw
                self._draw = None
                pid = page.edit(L.add_piece, cy, cx, r)
                page.select(pid)
            self._drag = None
        return True

    def map_wheel(self, delta) -> bool:
        if self.tool == "line" and self.line is not None:
            step = self.page.core_diameter() * 0.05
            self.band = max(step, (self.band or 0) + (step if delta > 0 else -step))
            self._reselect()
            return True
        return False

    def map_context(self, y, x, screen_pos):
        pid = self.piece_at(y, x)
        if pid is not None:
            self.page.piece_menu(pid, screen_pos)

    def prune(self):
        """Drop numbering objects that no longer exist (an undo, a deletion)."""
        d = self.page.holder.d if self.page.holder is not None else {"cores": {}, "pieces": {}}

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

    def key(self, name) -> bool:
        if name == "backspace" and self.tool == "click" and self.sequence:
            self.sequence.pop()
            self._show_sequence()
            return True
        if name == "escape" and (self.sequence or self.line):
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

    # ── helpers ──
    def _reselect(self):
        if self.line is None:
            return
        (ay, ax), (by, bx) = self.line
        if math.hypot(by - ay, bx - ax) < 1.0:            # a press, not yet a line
            return
        page = self.page
        sel = L.band_select(page.numbering_objects(), self.line[0], self.line[1], self.band)
        sel = [o for o in sel if o not in self.removed]
        extra = [o for o in self.added if o not in sel]
        if extra:                                     # added by click: keep the line's order
            (y0, x0), (y1, x1) = self.line
            n = math.hypot(y1 - y0, x1 - x0) or 1.0
            pos = {o: ((cy - y0) * (y1 - y0) + (cx - x0) * (x1 - x0)) / n for o, cy, cx in page.numbering_objects()}
            sel = sorted(sel + extra, key=lambda o: pos.get(o, 0.0))
        self.sequence = sel
        page.map.set_line((self.line[0], self.line[1], self.band))
        page.numbering_changed()

    def _show_sequence(self):
        page = self.page
        centres = {o: (cy, cx) for o, cy, cx in page.numbering_objects()}
        page.map.set_sequence([centres[o] for o in self.sequence if o in centres])
        page.numbering_changed()
