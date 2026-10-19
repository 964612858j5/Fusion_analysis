"""The Regions page's shape routing (docs/region_qc_module_plan.md §4, R3):
which shapes the tools work on (the open tab's: pieces in `Cores`,
exclusions in `Exclusions`), what a drawn polygon / circle becomes, the
selection (one or several shapes; the unit is one piece, never a whole
core), Merge, the shape menus, Delete and the jumps of the shared camera. A
mixin of `RegionsPage`.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from ...core.regions import document as D


class ShapesMixin:

    def exclusions_tab_open(self) -> bool:
        return self.frame.left_tabs.currentWidget() is self.exclusions

    def _tab_changed(self, _i):
        """Another tab: the tools work on its shapes; nothing half done stays.
        Selected core masks stay selected (user 2026-10-19)."""
        self.commit_transform()                       # while the old tab's shapes are still the target
        # selected core masks stay selected: in Exclusions a reason makes them exclusions
        self.selection = ([p for p in self.selected_pieces() if not self.holder.d["pieces"][p]["dropped"]]
                          if self._can_convert_cores() else [])
        if self.ctl.busy() or self.ctl.pressed:
            self.ctl.abort_gesture()
        if self.ctl.tool in ("line", "click") and self.exclusions_tab_open():
            self.ctl.set_tool(None)                     # numbering belongs to the Cores tab
            self._show_tool()
        self.ctl.cancel()
        self.refresh()

    def finish_shape(self, polygon):
        """A drawn polygon / circle: an exclusion in `Exclusions`, otherwise
        a new core of its own (yellow, as Find cores'); it is selected."""
        if self.exclusions_tab_open():
            reasons, channels, note = self.exclusions.defaults()
            sid = self.edit(D.add_exclusion, polygon, reasons, channels, note)
        else:
            if not self.cores_editable():
                self.say("TMA cores are edited in a whole-slide workspace.")
                return
            sid = self.edit(D.add_drawn_core, polygon)
        if sid:
            self.select(sid)

    def shape_menu(self, sid):
        if self.holder is not None and sid in self.holder.d["exclusions"]:
            self.exclusions.menu(sid)
        elif self.cores_editable():
            self.cores.piece_menu(sid)

    def jump_to_shapes(self, polys):
        """Centre the viewer on shapes with a margin (a jump of the shared camera)."""
        from ...core.regions import geometry as G
        y0, y1, x0, x1 = G.bbox_of([q for p in polys for q in p], self.shape())
        m = max(y1 - y0, x1 - x0)
        self.jump_to([y0 - m, y1 + m, x0 - m, x1 + m])

    # ── the selection ──
    @property
    def selected(self):
        """The one selected shape, or None when none or several are selected."""
        return self.selection[0] if len(self.selection) == 1 else None

    @selected.setter
    def selected(self, sid):
        self.selection = [] if sid is None else [sid]

    @staticmethod
    def ctrl_held() -> bool:
        return bool(QApplication.keyboardModifiers() & Qt.ControlModifier)

    def select(self, pid):
        if pid != self.selected or len(self.selection) > 1:
            self.commit_transform()                   # another selection applies a pending transform
        self.selected = pid
        self._selection_changed()

    def toggle_selection(self, sid):
        """Ctrl+click: adds the shape, or takes it out."""
        if sid is None:
            return
        self.commit_transform()
        if sid in self.selection:
            self.selection.remove(sid)
        else:
            self.selection.append(sid)
        self._selection_changed()

    def select_many(self, ids, add=False):
        """A marquee's catch: replaces the selection (Ctrl: adds to it)."""
        self.commit_transform()
        out = list(self.selection) if add else []
        out += [i for i in ids if i not in out]
        self.selection = out
        self._selection_changed()

    def _selection_changed(self):
        self.exclusions.refresh()                     # its row and fields follow (or show the defaults)
        pid = self.selected
        if self.holder is not None and pid is not None and pid not in self.holder.d["exclusions"]:
            core = self.selected_core()
            if core is not None:
                self.cores.table.select_ident(self.holder.d["cores"][core]["position_key"] or core)
            else:
                self.cores.table.select_ident(pid)
        self.cores.numbering_changed()                # Select ▸ Click numbers in the clicked order
        self.refresh_view()
        self._sync_enabled()

    def selected_core(self):
        if self.holder is None or self.selected not in self.holder.d["pieces"]:
            return None
        return self.holder.d["pieces"][self.selected]["owner"]

    def jump_to(self, bbox):
        if self.jump_box is not None:
            self.jump_box(list(bbox))

    def selected_pieces(self):
        d = self.holder.d if self.holder is not None else {"pieces": {}}
        return [s for s in self.selection if s in d["pieces"]]

    def _delete_selected(self):
        """Delete removes the selected shapes of the OPEN tab only, in one
        undo step; a deleted piece is gone (user 2026-10-18), a core left
        without pieces goes and frees its number."""
        if not self.selection or self.holder is None or self.holder.read_only:
            return
        if self.exclusions_tab_open():
            uids = [s for s in self.selection if s in self.holder.d["exclusions"]]
            if uids:
                self.edit(D.delete_exclusions, uids)      # in every workspace
        elif self.cores_editable():
            ids = self.selected_pieces()
            if ids:
                self.edit(D.delete_pieces, ids)

    # ── masks <-> exclusions (user 2026-10-18) ──
    def masks_to_exclusions(self, ids, reasons, channels, note):
        """Selected core masks given a reason in the Exclusions tab become
        exclusions (one undo step); the new exclusions are selected."""
        if not self._can_convert_cores():
            return
        uids = self.edit(D.pieces_to_exclusions, ids, reasons, channels, note)
        if uids:
            self.select_many(uids)

    def _can_convert_cores(self) -> bool:
        return self.holder is not None and not self.holder.read_only and self.cores_editable()

    def pieces_to_exclusions(self, ids, reason):
        if not self._can_convert_cores():
            return
        uids = self.edit(D.pieces_to_exclusions, ids, [reason])
        if uids:
            self.say(f"{len(uids)} core mask(s) made {reason} exclusion(s); see the Exclusions tab.")

    def exclusion_to_core(self, uid):
        """An exclusion back to a core mask; it stays selected (here, ticking
        a reason makes it an exclusion again)."""
        if not self._can_convert_cores():
            return
        pids = self.edit(D.exclusions_to_cores, [uid])
        if pids:
            self.select_many(pids)
            self.say("It is a core mask again.")

    # ── Merge ──
    def merge_blocker(self):
        """None when Merge can run, else why not (the button's tooltip)."""
        if self.holder is None or self.holder.read_only:
            return "nothing editable"
        if self.exclusions_tab_open():
            return "only cores merge"
        if not self.cores_editable():
            return "cores are edited in a whole-slide workspace"
        if len(self.selected_pieces()) < 2:
            return "select two or more pieces"
        return None

    def _core_name(self, uid):
        c = self.holder.d["cores"][uid]
        if c["position_key"]:
            rec = self.cores.labels_by_key().get(c["position_key"])
            return rec["label"] if rec else c["position_key"]
        return c["custom_label"] or "an unnumbered core"

    def merge_selected(self, mode="group"):
        """Merge ▸ Group: the selected pieces become one core; Merge ▸ Union:
        they also fuse into one outline (user 2026-10-18). With numbered
        cores among them the smallest number stays; the others are released
        -- asked first."""
        if self.merge_blocker() is not None:
            return
        self.commit_transform()
        ids = self.selected_pieces()
        study = self.cores.study
        try:
            plan = D.merge_plan(self.holder.d, ids, study)
        except D.RegionsError as exc:
            self.say(str(exc))
            return
        if plan["noop"] and mode == "group":
            self.say("These pieces are already one core.")
            return
        if mode == "union":
            from ...core.regions import geometry as G
            outline, why = G.union_outline([self.holder.d["pieces"][p]["polygon"] for p in ids], self.shape())
            if outline is None:
                self.say("These pieces are too far apart to fuse." if why == "too_far" else
                         "These pieces do not touch, so they cannot be fused; use Group to put them in one core.")
                return
        freed = [self._core_name(u) for u in plan["released"] + plan["cleared_labels"]]
        if freed:
            keep = self._core_name(plan["receiver"])
            if QMessageBox.question(self, "Merge",
                                    f"The merged core keeps {keep}. Released: {', '.join(freed)}. "
                                    "Continue?") != QMessageBox.Yes:
                return
        if mode == "union":
            pid = self.edit(D.union_pieces, ids, study)
            if pid:
                self.select(pid)
        else:
            self.edit(D.merge_pieces, ids, study)
