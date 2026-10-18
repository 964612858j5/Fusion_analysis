"""The TMA page's draft holder (F3, docs/tma_f3_page_application.md §2.3):
the current draft, undo / redo and the autosave. No Qt.

Every edit goes through `do(fn, ...)`: the draft is copied before, `fn`
(a `core.regions` document function) edits it, one undo step is pushed; a failing
edit leaves the draft as it was. A drag is ONE step (Odon records one
snapshot per drag, app.rs:5292): `begin_drag()` on press, `end_drag()` on
release. A read-only holder (a locked version) refuses every edit.
"""

from __future__ import annotations

import copy
from typing import Callable, Dict, List, Optional

UNDO_LIMIT = 200


class ReadOnly(RuntimeError):
    pass


class DraftHolder:
    def __init__(self, draft: Dict, *, read_only: bool = False,
                 autosave: Optional[Callable[[Dict], None]] = None):
        self.d = draft
        self.read_only = read_only
        self._autosave = autosave
        self._undo: List[Dict] = []
        self._redo: List[Dict] = []
        self._drag: Optional[Dict] = None

    # ── edits ──
    def do(self, fn, *args, **kw):
        self._check()
        if self._drag is not None:
            raise RuntimeError("an edit while a drag is in progress")
        before = copy.deepcopy(self.d)
        try:
            out = fn(self.d, *args, **kw)
        except Exception:
            self.d = before
            raise
        if self.d != before:                      # a no-op edit takes no step and keeps the redo
            self._push(before)
        return out

    def begin_drag(self) -> None:
        self._check()
        self._drag = copy.deepcopy(self.d)

    def end_drag(self, moved: bool = True) -> None:
        if self._drag is None:
            return
        before, self._drag = self._drag, None
        if moved and before != self.d:
            self._push(before)

    def cancel_drag(self) -> None:
        if self._drag is not None:
            self.d, self._drag = self._drag, None

    # ── undo / redo ──
    @property
    def dragging(self) -> bool:
        return self._drag is not None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo) and not self.read_only and self._drag is None

    @property
    def can_redo(self) -> bool:
        return bool(self._redo) and not self.read_only and self._drag is None

    def undo(self) -> bool:
        if not self.can_undo:
            return False
        self._redo.append(self.d)
        self.d = self._undo.pop()
        self._save()
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._undo.append(self.d)
        self.d = self._redo.pop()
        self._save()
        return True

    # ── internals ──
    def _check(self):
        if self.read_only:
            raise ReadOnly("a locked version is read-only; make a draft from it first")

    def _push(self, before):
        self._undo.append(before)
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        self._save()

    def _save(self):
        if self._autosave is not None and not self.read_only:
            self._autosave(self.d)
