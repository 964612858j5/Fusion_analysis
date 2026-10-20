"""The Regions page's Save (plan §3; user 2026-10-18: Save instead of Lock,
one result overwritten each time, never refused for unfinished work): the
title line, the saved / unsaved line, `Revert to saved` and `Save`. A mixin
of `RegionsPage`: one document per slide.
"""

from __future__ import annotations

import itertools
import os

from PyQt5.QtWidgets import QMessageBox

from ...core.regions import document as D
from ...core.regions import lock as K

LIST_MAX = 25
_SERIALS = itertools.count(1)


class VersionsMixin:

    def _refresh_title(self):
        st = self.cores.study
        slide = os.path.basename(self.ctx.get("slide_path") or "")
        name = (st["map_sheet"].get("block_name") or "TMA") if st else "no map sheet"
        self.lbl_study.setText(f"Slide: {slide}   Study: {name}" if slide else "No dataset: Save in Step0 first")
        self._refresh_saved()

    def _saved_mark(self):
        """The open document's state: the holder and its revision (every
        edit a new one, undo / redo the old one back) and the workspaces it
        was edited in (`set_session` is no edit). Anything else than at the
        last Save shows "unsaved changes"."""
        if self.holder is None:
            return None
        import json
        if not hasattr(self.holder, "mark_serial"):        # a number never reused (an id() can be)
            self.holder.mark_serial = next(_SERIALS)
        return self.holder.mark_serial, self.holder.revision, json.dumps(self.holder.d["handoffs"], sort_keys=True)

    def _refresh_saved(self):
        if self.holder is None or not self.ctx.get("project_dir"):
            self.lbl_saved.setText("")
            return
        info = self.__dict__.get("_saved_info")
        if info is None:
            self.lbl_saved.setText("Not saved yet")
            self.lbl_saved.setToolTip("")
            return
        text = f"Saved {info['at'][11:16]}"
        if info["issues"]:
            text += f" · {len(info['issues'])} open"
        if self._saved_mark() != self.__dict__.get("_saved_at_mark"):
            text += " · unsaved changes"
        self.lbl_saved.setText(text)
        more = f"\n… and {len(info['issues']) - LIST_MAX} more" if len(info["issues"]) > LIST_MAX else ""
        self.lbl_saved.setToolTip("Still open:\n" + "\n".join(info["issues"][:LIST_MAX]) + more
                                  if info["issues"] else "Complete")

    def _remember_saved(self, rec, mark=None):
        """`mark`: the saved document's own mark when a working copy that
        may differ from it is open."""
        if rec is not None and not K.table_matches(self.ctx["project_dir"], rec):
            self.say("The last Save was interrupted (its core table does not match): Save again.")
        self._saved_info = None if rec is None else {"at": rec["saved_at"], "issues": list(rec["open_issues"])}
        self._saved_at_mark = mark if mark is not None else self._saved_mark()

    # ── save, revert ──
    def _save(self):
        """Save at any time: the result replaces the last one; what is still
        open is listed, never blocking."""
        if self.holder is None or not self.ctx.get("project_dir"):
            return
        self.commit_transform()
        self.flush_draft()
        try:
            rec = K.save(self.ctx["project_dir"], self.holder.d, self.cores.study, self.ctx.get("channel_names"))
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.warning(self, "Save", str(exc))
            return
        self._remember_saved(rec)
        self.saved.emit()                          # the Save's cores are drawn elsewhere too
        n = len(rec["open_issues"])
        self.say("Saved." + (f" Still open: {n} (see the line next to Save)." if n else " Complete."))
        self.refresh()

    def _revert_to_saved(self):
        pd = self.ctx.get("project_dir")
        if not pd or QMessageBox.question(self, "Revert to saved", "Drop every change since the last Save?"
                                          ) != QMessageBox.Yes:
            return
        self.cancel_transform()
        if self.ctl.busy() or self.ctl.pressed:
            self.ctl.abort_gesture()               # nothing half drawn survives into the saved document
        self.ctl.cancel()
        self._save_timer.stop()
        K.discard_draft(pd)
        self.holder = None
        self.selection = []
        self._load()
        self.refresh()

    def _start_document(self):
        """No working copy: the last Save, else the newest version an
        earlier build locked, else a new document."""
        pd, sid = self.ctx["project_dir"], self.ctx["slide_id"]
        try:
            rec = K.load_saved(pd, sid)
        except (D.RegionsError, ValueError, OSError) as exc:
            self.say(f"The saved file cannot be read: {exc}")
            rec = None
        if rec is not None:
            self._remember_saved(None)
            self._open_draft(K.draft_from_saved(rec))         # the open workspace may add its handoff
            same = K.draft_from_saved(rec) == dict(self.holder.d, session=None)
            self._remember_saved(rec, None if same else ("differs",))
            return
        from ...core.regions import migrate as M
        for v in reversed(K.locked_versions(pd)):
            try:
                d = K.draft_from_locked(K.load_locked(pd, v, sid))
            except (D.RegionsError, ValueError, KeyError, OSError):
                continue
            d["based_on"] = None
            self._open_draft(d)
            return
        for v in reversed(M.f3_locked_versions(pd)):          # an F3 layout of an even earlier build
            try:
                d = M.load_f3_locked(pd, v, self.shape(), sid)
            except (D.RegionsError, ValueError, KeyError, OSError):
                continue
            d["based_on"] = None
            self._open_draft(d)
            return
        d = D.new_document(sid, self.shape())
        if self.cores.study is not None and self.cores.candidates is not None:
            D.attach_tma(d, self.cores.study, self.cores.candidates)
        self._open_draft(d)
