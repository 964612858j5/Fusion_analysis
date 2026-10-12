"""v16_5 R3b: Step3's masks planned and read by the Rust core.

`CoreLabelBinding` is `Step3LabelBinding`'s surface for a Rust-drawn viewer:
the core plans and reads; Python builds missing pyramids, keeps the styles
and the status (R3b plan §2.4). What the core reads of a mask:
`step3_core_label_source`.
"""

import dataclasses
import threading
from typing import Any, Dict, List, Optional

from PyQt5 import QtCore

from ..core import step3_masks
from ..utils import closing_threads
from .step1_gpu_layer import LABEL_FILL, LABEL_TEXTURE_BYTES, label_radius
from .step3_core_label_source import CoreLabelsRefused, check, describe  # noqa: F401 -- re-exported
from .step3_label_binding import DEFAULT_STYLES, KINDS, Step3LabelBinding


class CoreLabelBinding(QtCore.QObject):
    """`Step3LabelBinding`'s surface for a Rust-drawn viewer whose core plans
    and reads the masks (R3b plan §2.4). Python keeps the pyramid build (one
    thread, once per mask, cancellable), the styles and the status text;
    the core does the rest at every camera change. A mask the core does not
    read (pyramid only in memory, another layout) puts this viewer's masks
    on an inner `Step3LabelBinding` (L1: Python plans and reads, the core
    draws), decided when sources are set and when a build ends (§2.6)."""

    status_changed = QtCore.pyqtSignal()
    _built = QtCore.pyqtSignal(object)

    def __init__(self, *, controller, layer, level_shapes, tile_size: int, parent=None):
        super().__init__(parent)
        self.controller, self.layer, self.core = controller, layer, layer.core
        self.level_shapes = [tuple(int(v) for v in s) for s in level_shapes]
        self.tile_size = int(tile_size)
        self.budget_bytes = LABEL_TEXTURE_BYTES
        self._sources: Dict[str, Any] = {kind: None for kind in KINDS}
        self._styles = dict(DEFAULT_STYLES)
        self._generation = 0
        self._building: set = set()
        self._retry_build: set = set()
        self._reasons: Dict[str, Optional[str]] = {kind: None for kind in KINDS}
        self._paused = False
        self._disposed = False
        self._pyramids_built = 0
        self._builders: List = []
        #: per kind: the generation whose build thread is still running --
        #: one writer per pyramid at a time; a build asked for meanwhile
        #: starts when that thread's result is in (codex R3b review)
        self._running: Dict[str, Optional[int]] = {kind: None for kind in KINDS}
        self._restart_after: set = set()
        self._cancel = threading.Event()
        self._l1: Optional[Step3LabelBinding] = None
        self.l1_reason: Optional[str] = None
        self._seen = None
        self._handed = None                              # what the core reads now
        self._built.connect(self._on_built, QtCore.Qt.QueuedConnection)
        layer.frameSwapped.connect(self._after_frame)
        layer.on_label_ratio = self._send_styles

    # ── sources ───────────────────────────────────────────────────────
    def set_sources(self, sources) -> None:
        if self._disposed:
            return
        self._generation += 1
        self._cancel.set()                              # a build of the old sources stops
        self._cancel = threading.Event()
        self._building.clear()
        self._retry_build.clear()
        self._sources = {kind: (sources or {}).get(kind) for kind in KINDS}
        self._reasons = {kind: None for kind in KINDS}
        why = self._readable() if self._l1 is not None else None
        if self._l1 is not None and why is None:
            self._leave_l1()
        if self._l1 is not None:
            self.l1_reason = why                        # these sources' own reason
            print(f"[Step3] masks read by Python: {why}")
            self._l1.set_sources(self._sources)
            self.status_changed.emit()
            return
        self.core.set_label_sources(None)               # nothing is read while pyramids are built
        self._handed = None
        if not self._paused:
            self._queue_builds()
        self._hand_over()
        self.status_changed.emit()

    def _readable(self) -> Optional[str]:
        """Why the core would not read the current sources (None: it does)."""
        import fusion_viewer_core
        for kind in KINDS:
            why = check(fusion_viewer_core, self._sources.get(kind))
            if why:
                return f"{kind} mask: {why}"
        return None

    def _hand_over(self) -> None:
        """The sources to the core once no pyramid is being built -- or to
        Python (L1) when the core cannot read them."""
        if self._building or self._disposed:
            return
        why = self._readable()
        if why:
            self._enter_l1(why)
            return
        kinds = [describe(self._sources.get(kind)) for kind in KINDS]
        if kinds != self._handed:                       # a resume keeps what the core holds
            self.core.set_label_sources(kinds if any(kinds) else None)
            self._handed = kinds
        self._send_styles()
        self.layer.update()

    def _queue_builds(self, only=None) -> None:
        if len(self.level_shapes) < 2:
            return
        gen, cancel = self._generation, self._cancel
        for kind in KINDS:
            source = self._sources.get(kind)
            if (source is None or source.pyramid is not None or kind in self._building
                    or (only is not None and kind not in only)):
                continue
            self._building.add(kind)
            if self._running[kind] is not None:          # the last writer is still at it
                self._restart_after.add(kind)
                continue
            self._running[kind] = gen

            def build(kind=kind, source=source):
                try:
                    result = step3_masks.ensure_pyramid(source, self.level_shapes, cancel_check=cancel.is_set)
                except Exception as exc:                # noqa: BLE001 -- reported, level 0 goes on
                    result = exc
                self._built.emit((gen, kind, result))
            thread = threading.Thread(target=build, name="step3-pyramid", daemon=True)
            self._builders.append(thread)
            thread.start()

    def _on_built(self, payload) -> None:
        gen, kind, result = payload
        if self._running.get(kind) == gen:
            self._running[kind] = None                  # that writer is done
        if kind in self._restart_after and not self._disposed and not self._paused:
            self._restart_after.discard(kind)
            self._building.discard(kind)
            self._queue_builds(only={kind})             # the build asked for meanwhile
        if self._disposed or gen != self._generation:
            return
        self._building.discard(kind)
        if isinstance(result, Exception):
            self._reasons[kind] = f"the zoomed-out levels could not be built ({result})"
        elif result.status == "cancelled":
            if self._paused:
                self._retry_build.add(kind)
                return
            self._queue_builds(only={kind})            # resumed before the cancel landed
            return
        else:
            self._sources[kind] = result.source
            self._reasons[kind] = None if result.status == "ready" else result.reason
            self._pyramids_built += result.status == "ready"
        if self._reasons[kind]:
            print(f"[Step3] {kind} mask: zoomed out it cannot be shown: {self._reasons[kind]}")
        self._hand_over()
        self.status_changed.emit()

    # ── L1: Python plans and reads, the core draws ────────────────────
    def _enter_l1(self, why: str) -> None:
        self.core.set_label_sources(None)
        self.l1_reason = why
        print(f"[Step3] masks read by Python: {why}")
        self._l1 = Step3LabelBinding(controller=self.controller, layer=self.layer,
                                     level_shapes=self.level_shapes, tile_size=self.tile_size, parent=self)
        self._handed = None
        self._l1.status_changed.connect(self.status_changed.emit)
        for kind, style in self._styles.items():
            self._l1.set_style(kind, **dataclasses.asdict(style))
        if self._paused:
            self._l1.pause()
        self._l1.set_sources(self._sources)

    def _leave_l1(self) -> None:
        l1, self._l1, self.l1_reason = self._l1, None, None
        l1.dispose()                                    # its thread is listed, not waited for
        l1.setParent(None)

    # ── styles ────────────────────────────────────────────────────────
    def set_style(self, kind: str, **changes) -> None:
        self._styles[kind] = dataclasses.replace(self._styles[kind], **changes)
        if self._l1 is not None:
            self._l1.set_style(kind, **changes)
            return
        self._send_styles()

    def style(self, kind: str):
        return self._styles[kind]

    def _send_styles(self) -> None:
        if self._l1 is not None or self._disposed:
            return
        ratio = float(self.layer.devicePixelRatioF()) or 1.0
        for i, kind in enumerate(KINDS):
            st = self._styles[kind]
            self.core.set_label_style(i, bool(st.visible), tuple(float(c) for c in st.color), float(st.alpha),
                                      label_radius(st.width, ratio), st.mode == LABEL_FILL)
        self.layer.update()

    # ── status ────────────────────────────────────────────────────────
    def _core_stats(self) -> Dict[str, Any]:
        return dict(self.core.core_label_stats()) if not self._disposed else {}

    def mask_status(self) -> Dict[str, Any]:
        if self._l1 is not None:
            return dict(self._l1.mask_status(), l1=self.l1_reason)
        stats = self._core_stats()
        level = stats.get("target_level")
        out = {}
        for kind in KINDS:
            source = self._sources.get(kind)
            coarse = None
            if source is not None and source.pyramid is None:
                coarse = ("the zoomed-out levels are being prepared" if kind in self._building
                          else (self._reasons.get(kind) or source.pyramid_reason or "no label pyramid"))
            out[kind] = {"source": source is not None, "coarse_unavailable": coarse,
                         "building": kind in self._building, "unavailable_here": bool(coarse) and bool(level)}
        out["budget"] = stats.get("note")
        out["error"] = stats.get("last_error")
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
                   target_level=stats.get("target_level"), generation=self._generation,
                   thread_alive=bool(stats.get("thread_alive")),
                   building=any(t.is_alive() for t in self._builders))
        return out

    def _after_frame(self) -> None:
        """After a frame: the core's level, budget note or error changed ->
        the status (Python emitted this from every plan)."""
        if self._l1 is not None or self._disposed:
            return
        stats = self._core_stats()
        seen = (stats.get("target_level"), stats.get("note"), stats.get("last_error"))
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
            return self._l1.pause()
        self._retry_build |= set(self._building)
        self._cancel.set()                              # the build stops; resume starts it again
        return True

    def resume(self) -> bool:
        if self._disposed or not self._paused:
            return False
        self._paused = False
        if self._l1 is not None:
            return self._l1.resume()
        if self._retry_build:
            self._cancel = threading.Event()
            self._generation += 1                       # the cancelled build's late end is not news
            self._retry_build.clear()
            self._building.clear()
            self._queue_builds()
        self._hand_over()
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def sources(self) -> Dict[str, Any]:
        return dict(self._l1.sources()) if self._l1 is not None else dict(self._sources)

    def dispose(self, timeout: float = 0.0) -> Dict[str, Any]:
        """Stop reading and building, clear the masks; no thread is waited
        for (the builders are listed for the program's exit)."""
        if self._disposed:
            return {"already_disposed": True, "thread_alive": False}
        self._disposed = True
        self._cancel.set()
        try:
            self.layer.frameSwapped.disconnect(self._after_frame)
        except (TypeError, RuntimeError):
            pass
        self.layer.on_label_ratio = None
        if self._l1 is not None:
            self._l1.dispose(timeout)
        try:
            self.core.set_label_sources(None)
        except Exception:                               # noqa: BLE001 -- a closed core
            pass
        for thread in self._builders:
            if timeout > 0:
                thread.join(timeout)
            if thread.is_alive():
                closing_threads.register(thread)
        return {"already_disposed": False, "thread_alive": any(t.is_alive() for t in self._builders)}
