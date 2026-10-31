"""v16_5 R3b: Step3's masks planned and read by the Rust core.

`CoreLabelBinding` is `Step3LabelBinding`'s surface for a Rust-drawn viewer:
the core plans and reads; Python builds missing pyramids, keeps the styles
and the status (R3b plan §2.4). What the core reads of a mask:
`step3_core_label_source`. One SLOT per drawn mask, in drawing order
(docs/v17_step3_multi_results_plan.md §3.3).
"""

import dataclasses
import threading
from typing import Any, Dict, List, Optional

from PyQt5 import QtCore

from ..core import step3_masks
from ..utils import closing_threads
from .step1_gpu_layer import LABEL_FILL, LABEL_TEXTURE_BYTES, label_radius
from .step3_core_label_source import CoreLabelsRefused, check, describe  # noqa: F401 -- re-exported
from .step3_label_binding import MaskStyle, SlotStamps, Step3LabelBinding, destination


class CoreLabelBinding(QtCore.QObject):
    """`Step3LabelBinding`'s surface for a Rust-drawn viewer whose core plans
    and reads the masks (R3b plan §2.4). Python keeps the pyramid builds
    (one thread per destination, cancellable), the styles and the status
    text; the core does the rest at every camera change. A slot whose
    pyramid is being built is withheld from the core until it is done;
    the others keep drawing. A mask the core does not read (pyramid only in
    memory, another layout) puts this viewer's masks on an inner
    `Step3LabelBinding` (L1: Python plans and reads, the core draws),
    decided when slots are set and when a build ends (§2.6). The builds
    stay HERE in both cases -- one writer per destination across every
    switch -- and the inner binding is handed the built sources."""

    status_changed = QtCore.pyqtSignal()
    _built = QtCore.pyqtSignal(object)

    def __init__(self, *, controller, layer, level_shapes, tile_size: int, parent=None):
        super().__init__(parent)
        self.controller, self.layer, self.core = controller, layer, layer.core
        self.level_shapes = [tuple(int(v) for v in s) for s in level_shapes]
        self.tile_size = int(tile_size)
        self.budget_bytes = LABEL_TEXTURE_BYTES
        self._stamps = SlotStamps()
        self._slots: List = []                          # (key, stamp, source), drawing order
        self._styles: Dict[str, MaskStyle] = {}
        self._building: set = set()                     # destinations being built
        self._retry_build: set = set()                  # destinations cancelled by a pause
        self._reasons: Dict[str, Optional[str]] = {}    # per destination
        self._paused = False
        self._disposed = False
        self._pyramids_built = 0
        self._builders: List = []
        #: per destination: the cancel event of its build thread still
        #: running -- one writer per pyramid at a time; a build asked for
        #: meanwhile starts when that thread's result is in (codex R3b review)
        self._running: Dict[str, threading.Event] = {}
        self._restart_after: set = set()
        self._l1: Optional[Step3LabelBinding] = None
        self.l1_reason: Optional[str] = None
        self._seen = None
        self._handed = None                              # [(stamp, description)] the core reads now
        self._built.connect(self._on_built, QtCore.Qt.QueuedConnection)
        layer.frameSwapped.connect(self._after_frame)
        layer.on_label_ratio = self._send_styles

    # ── slots ─────────────────────────────────────────────────────────
    def set_slots(self, slots) -> None:
        """The masks to draw, in drawing order: [(key, MaskSource, MaskStyle)]."""
        if self._disposed:
            return
        slots = list(slots or ())
        self._slots = self._stamps.assign([(key, src) for key, src, _st in slots])
        self._styles = {key: (st or MaskStyle()) for key, _src, st in slots}
        dests = {destination(src) for _k, _s, src in self._slots}
        for dest in list(self._running):
            if dest not in dests:
                self._running[dest].set()               # no slot needs it: it stops
        self._building &= dests
        self._retry_build &= dests
        self._restart_after &= dests
        self._reasons = {d: r for d, r in self._reasons.items() if d in dests}
        if not self._paused:
            self._queue_builds()
        self._route()
        self.status_changed.emit()

    def _route(self) -> None:
        """The slots to the core, or -- when it cannot read one -- all of
        them to the inner Python binding (L1), which builds nothing."""
        why = self._readable()
        if why is None:
            if self._l1 is not None:
                self._leave_l1()
            self._hand_over()
            return
        if self._l1 is None:
            self._enter_l1(why)
            return
        if why != self.l1_reason:
            self.l1_reason = why                        # these sources' own reason
            print(f"[Step3] masks read by Python: {why}")
        self._l1.set_slots(self._l1_slots())

    def _l1_slots(self):
        return [(key, src, self._styles[key]) for key, _stamp, src in self._slots]

    def _readable(self) -> Optional[str]:
        """Why the core would not read the current slots (None: it does)."""
        import fusion_viewer_core
        for key, _stamp, source in self._slots:
            why = check(fusion_viewer_core, source)
            if why:
                return f"{key}: {why}"
        return None

    def _style_tuple(self, style):
        ratio = float(self.layer.devicePixelRatioF()) or 1.0
        return (bool(style.visible), tuple(float(c) for c in style.color), float(style.alpha),
                label_radius(style.width, ratio), style.mode == LABEL_FILL, bool(style.dash))

    def _hand_over(self) -> None:
        """The slots whose pyramid is not being built, to the core."""
        if self._disposed or self._l1 is not None:
            return
        ready = [(key, stamp, src) for key, stamp, src in self._slots
                 if destination(src) not in self._building]
        handed = [(stamp, describe(src)) for _key, stamp, src in ready]
        if handed != self._handed:                      # a resume keeps what the core holds
            self.core.set_label_slots([(stamp, desc, self._style_tuple(self._styles[key]))
                                       for (key, stamp, _src), (_s, desc) in zip(ready, handed)])
            self._handed = handed
        else:
            self._send_styles()
        self.layer.update()

    def _queue_builds(self, only=None) -> None:
        if len(self.level_shapes) < 2:
            return
        seen = set()
        for _key, _stamp, source in self._slots:
            dest = destination(source)
            if (source.pyramid is not None or dest in seen or dest in self._building
                    or self._reasons.get(dest) or (only is not None and dest not in only)):
                continue
            seen.add(dest)
            self._building.add(dest)
            if dest in self._running:                   # the last writer is still at it
                self._restart_after.add(dest)
                continue
            cancel = threading.Event()
            self._running[dest] = cancel

            def build(dest=dest, source=source, cancel=cancel):
                try:
                    result = step3_masks.ensure_pyramid(source, self.level_shapes,
                                                        cancel_check=cancel.is_set)
                except Exception as exc:                # noqa: BLE001 -- reported, level 0 goes on
                    result = exc
                self._built.emit((dest, cancel, result))
            thread = threading.Thread(target=build, name="step3-pyramid", daemon=True)
            self._builders.append(thread)
            thread.start()

    def _on_built(self, payload) -> None:
        dest, cancel, result = payload
        if self._running.get(dest) is cancel:
            del self._running[dest]                     # that writer is done
        if self._disposed:
            return
        built = not isinstance(result, Exception) and result.status == "ready"
        if dest in self._restart_after:
            self._restart_after.discard(dest)
            if not built:
                self._building.discard(dest)
                if not self._paused:
                    self._queue_builds(only={dest})      # the build asked for meanwhile
                return
            # it was built after all: the build asked for meanwhile is that one
        needed = any(destination(src) == dest for _k, _s, src in self._slots)
        if not needed:
            self._building.discard(dest)
            if built:
                self._stamps.remember(result.source)    # for a slot ticked again
            return
        if isinstance(result, Exception):
            self._building.discard(dest)
            self._reasons[dest] = f"the zoomed-out levels could not be built ({result})"
        elif result.status == "cancelled":
            if self._paused:
                self._retry_build.add(dest)
                return
            self._building.discard(dest)
            self._queue_builds(only={dest})             # resumed before the cancel landed
            return
        elif built:
            self._building.discard(dest)
            self._slots = self._stamps.take_built(self._slots, dest, result.source)
            self._reasons[dest] = None
            self._pyramids_built += 1
        else:
            self._building.discard(dest)
            # level 0 only -- never in place of a pyramid a slot already has
            if not any(destination(src) == dest and src.pyramid is not None for _k, _s, src in self._slots):
                self._slots = self._stamps.take_built(self._slots, dest, result.source)
                self._reasons[dest] = result.reason or "no label pyramid"
        if self._reasons.get(dest):
            print(f"[Step3] {dest}: zoomed out it cannot be shown: {self._reasons[dest]}")
        self._route()
        self.status_changed.emit()

    # ── L1: Python plans and reads, the core draws ────────────────────
    def _enter_l1(self, why: str) -> None:
        self.core.set_label_slots([])
        self.l1_reason = why
        print(f"[Step3] masks read by Python: {why}")
        self._l1 = Step3LabelBinding(controller=self.controller, layer=self.layer,
                                     level_shapes=self.level_shapes, tile_size=self.tile_size,
                                     builds=False, parent=self)
        self._handed = None
        self._l1.status_changed.connect(self.status_changed.emit)
        if self._paused:
            self._l1.pause()
        self._l1.set_slots(self._l1_slots())

    def _leave_l1(self) -> None:
        l1, self._l1, self.l1_reason = self._l1, None, None
        l1.dispose()                                    # its thread is listed, not waited for
        l1.setParent(None)

    # ── styles ────────────────────────────────────────────────────────
    def set_style(self, key: str, **changes) -> None:
        if key not in self._styles:
            return
        self._styles[key] = dataclasses.replace(self._styles[key], **changes)
        if self._l1 is not None:
            self._l1.set_style(key, **changes)
            return
        stamp = next((s for k, s, _src in self._slots if k == key), None)
        if stamp is not None and self._handed is not None and any(s == stamp for s, _d in self._handed):
            self.core.set_label_slot_style(stamp, *self._style_tuple(self._styles[key]))
        self.layer.update()

    def style(self, key: str):
        return self._styles[key]

    def _send_styles(self) -> None:
        if self._l1 is not None or self._disposed or not self._handed:
            return
        handed = {s for s, _d in self._handed}
        for key, stamp, _src in self._slots:
            if stamp in handed:
                self.core.set_label_slot_style(stamp, *self._style_tuple(self._styles[key]))
        self.layer.update()

    # ── status ────────────────────────────────────────────────────────
    def _core_stats(self) -> Dict[str, Any]:
        return dict(self.core.core_label_stats()) if not self._disposed else {}

    def mask_status(self) -> Dict[str, Any]:
        if self._l1 is not None:
            out = dict(self._l1.mask_status(), l1=self.l1_reason)
            for key, _stamp, source in self._slots:      # the builds are this binding's
                if key in out and destination(source) in self._building:
                    out[key] = dict(out[key], building=True,
                                    coarse_unavailable="the zoomed-out levels are being prepared")
            return out
        stats = self._core_stats()
        level = stats.get("target_level")
        errors = stats.get("errors") or {}
        out = {}
        for key, stamp, source in self._slots:
            dest = destination(source)
            coarse = None
            if source.pyramid is None:
                coarse = ("the zoomed-out levels are being prepared" if dest in self._building
                          else (self._reasons.get(dest) or source.pyramid_reason or "no label pyramid"))
            out[key] = {"source": True, "coarse_unavailable": coarse, "building": dest in self._building,
                        "unavailable_here": bool(coarse) and bool(level), "error": errors.get(stamp)}
        out["budget"] = stats.get("note")
        out["error"] = None                              # the per-slot errors are in the slots
        out["l1"] = None                                 # the core reads them
        return out

    def stats(self) -> Dict[str, Any]:
        if self._l1 is not None:
            return dict(self._l1.stats(), l1=self.l1_reason)
        stats = self._core_stats()
        out = {k: stats.get(k, 0) for k in ("requested", "read", "accepted", "stale_dropped", "late_dropped",
                                             "pushes", "resident", "reading", "queued", "resident_bytes",
                                             "pending_bytes")}
        out.update(pyramids_built=self._pyramids_built, budget_bytes=self.budget_bytes,
                   target_level=stats.get("target_level"), generation=stats.get("generation", 0),
                   thread_alive=bool(stats.get("thread_alive")),
                   building=any(t.is_alive() for t in self._builders))
        return out

    def _after_frame(self) -> None:
        """After a frame: the core's level, budget note or errors changed ->
        the status (Python emitted this from every plan)."""
        if self._l1 is not None or self._disposed:
            return
        stats = self._core_stats()
        seen = (stats.get("target_level"), stats.get("note"), tuple(sorted((stats.get("errors") or {}).items())))
        if seen != self._seen:
            self._seen = seen
            self.status_changed.emit()

    # ── lifecycle ─────────────────────────────────────────────────────
    def update_viewport(self, snapshot=None) -> None:
        """The core plans at every camera change; nothing to do here."""
        if self._l1 is not None:
            self._l1.update_viewport(snapshot)

    def pause(self) -> bool:
        if self._disposed or self._paused:
            return False
        self._paused = True
        if self._l1 is not None:
            self._l1.pause()
        self._retry_build |= set(self._building)
        for cancel in self._running.values():
            cancel.set()                                # the build stops; resume starts it again
        return True

    def resume(self) -> bool:
        if self._disposed or not self._paused:
            return False
        self._paused = False
        if self._l1 is not None:
            self._l1.resume()
        # the builds the pause cancelled, AND the slots added while paused
        # (`set_slots` builds nothing then; codex review 2)
        retry, self._retry_build = set(self._retry_build), set()
        self._building -= retry
        self._queue_builds()
        self._route()
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def sources(self) -> Dict[str, Any]:
        """By slot key, a pyramid built here included."""
        return {key: src for key, _stamp, src in self._slots}

    def dispose(self, timeout: float = 0.0) -> Dict[str, Any]:
        """Stop reading and building, clear the masks; no thread is waited
        for (the builders are listed for the program's exit)."""
        if self._disposed:
            return {"already_disposed": True, "thread_alive": False}
        self._disposed = True
        for cancel in self._running.values():
            cancel.set()
        try:
            self.layer.frameSwapped.disconnect(self._after_frame)
        except (TypeError, RuntimeError):
            pass
        self.layer.on_label_ratio = None
        if self._l1 is not None:
            self._l1.dispose(timeout)
        try:
            self.core.set_label_slots([])
        except Exception:                               # noqa: BLE001 -- a closed core
            pass
        for thread in self._builders:
            if timeout > 0:
                thread.join(timeout)
            if thread.is_alive():
                closing_threads.register(thread)
        return {"already_disposed": False, "thread_alive": any(t.is_alive() for t in self._builders)}
