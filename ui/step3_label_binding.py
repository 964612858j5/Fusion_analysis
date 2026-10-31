"""Step3's label supply for the GPU layer -- block 4b.

Follows the ONE controller of Step3's viewer (its camera events, the level
and the visible tiles of its snapshots) and keeps the GPU layer's mask
layers (`Step1GpuLayer.set_labels`) supplied with the label tiles of
`core.step3_masks` sources -- one SLOT per drawn mask, in drawing order
(docs/v17_step3_multi_results_plan.md §3.3; the owner names each by a key,
e.g. a result's cell mask):

  * ONE reader thread (the only thread block 4 adds): it first builds the
    pyramid a source lacks (`ensure_pyramid`, cancellable), then reads label
    tiles (`read_label_tile`). While a pyramid is being built no tile is
    read -- level 0 waits as well; the status says so;
  * what is read: the current level's visible tiles and one ring around
    them, centre first, TILE-MAJOR: every shown slot gets a tile before any
    gets the next, so over the budget all slots lose their far tiles
    together. A new slot list keeps the tiles of the slots still in it
    (by stamp, `SlotStamps`): adding a result does not blank the others.
    A slot's pyramid is built once per destination, whichever slots share
    it. Jumps are served at once, motion every 33 ms, a
    quiet camera catches up once -- the image binding's rhythm;
  * a request that is no longer wanted when the camera moves on is dropped
    from the queue and counted (`stale_dropped`); a result that arrives for
    a slot that left or a tile no longer wanted is dropped and counted
    (`late_dropped`);
  * the TARGET level is drawn last: tiles of other levels stay only where
    they still cover the view, drawn first, farthest level first, so both a
    zoom in and a zoom out keep the screen filled until the target arrives;
  * one budget (256 MB, user ruling) for the arrays held here -- drawn,
    delivered-not-yet-accepted and being read -- which equals the layer's
    texture budget. Over it: old levels go first, then the ring, then the
    visible target tiles farthest from the centre; the status says so;
  * a coarse level without a pyramid is not requested and not drawn: the
    status gives the reason (`mask_status`);
  * pause: the camera is let go, queued requests are dropped, a pyramid
    build is cancelled (restarted on resume, user ruling 4); dispose also
    ends the thread and waits for it.

No scheduler, provider or raw cache of the viewer is used, and nothing is
cached off screen.
"""

from __future__ import annotations

import dataclasses
import math
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt5 import QtCore

from ..core import step3_masks
from ..utils import closing_threads
from .step1_gpu_layer import (LABEL_OUTLINE, LABEL_TEXTURE_BYTES, LabelLayer,
                              LabelPlane, LabelSnapshot, Step1GpuLayerError)

CELL = step3_masks.CELL
NUCLEUS = step3_masks.NUCLEUS
#: A result's masks, drawn in this order: the nucleus lies on top (user ruling 1).
KINDS = (CELL, NUCLEUS)
MOTION_INTERVAL_MS = 33


@dataclasses.dataclass(frozen=True)
class MaskStyle:
    visible: bool = True
    color: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    alpha: float = 0.75
    width: float = 1.0                  # logical pixels, 0..4
    mode: str = LABEL_OUTLINE
    dash: bool = False                  # dashed outline (plan §4)


DEFAULT_STYLES = {
    CELL: MaskStyle(),
    NUCLEUS: MaskStyle(color=(0.0, 0.8, 1.0)),
}


def kind_slots(sources, styles=None, prefix=""):
    """One result's masks (`resolve_masks`' cell / nucleus) as slots in
    drawing order -- cells, then the nucleus on top -- keyed `prefix` +
    kind; `styles` per kind (default `DEFAULT_STYLES`)."""
    styles = styles or {}
    return [(prefix + kind, sources[kind], styles.get(kind, DEFAULT_STYLES[kind]))
            for kind in KINDS if (sources or {}).get(kind) is not None]


def same_mask(a, b) -> bool:
    """The same mask (a pyramid built since does not make it another)."""
    return (a is not None and b is not None and a.mask_path == b.mask_path
            and tuple(a.bbox) == tuple(b.bbox) and a.pyramid_path == b.pyramid_path)


def destination(source) -> str:
    """Where `source`'s pyramid is built: one build per destination."""
    return os.path.realpath(source.pyramid_path)


class SlotStamps:
    """Each slot's stamp: a number given anew when a slot's MASK changes --
    never reused, unique across slots -- so a tile, job or error of a mask
    that left is told apart by its stamp alone. A pyramid built for the
    same mask keeps the stamp: its level 0 is the same, and its coarse
    levels were never read before."""

    def __init__(self):
        self._next = 0
        self.of = {}                    # key -> (stamp, source)
        #: every source with a pyramid seen by this binding, for its life:
        #: a mask unticked while it was built and ticked again takes it
        #: (the owner's source, resolved before, still has none)
        self._built = []

    def remember(self, source):
        if source is not None and source.pyramid is not None:
            self._built = [b for b in self._built if not same_mask(b, source)] + [source]

    def assign(self, slots):
        """`slots` = [(key, source)]; returns [(key, stamp, source)]. A
        source without a pyramid takes the one any held slot of the same
        mask carries (built since): a pyramid is never given up."""
        for _stamp, src in self.of.values():
            self.remember(src)
        out, now = [], {}
        for key, source in slots:
            self.remember(source)
            if source.pyramid is None:
                source = next((b for b in self._built if same_mask(b, source)), source)
            held = self.of.get(key)
            if held is not None and same_mask(held[1], source):
                stamp = held[0]
            else:
                self._next += 1
                stamp = self._next
            now[key] = (stamp, source)
            out.append((key, stamp, source))
        self.of = now
        return out

    def take_built(self, slots, dest, built):
        """Every slot of `dest` with the same mask takes `built` -- unless it
        has a pyramid already (never downgraded). Returns the new list."""
        self.remember(built)
        out = [(key, stamp, built if destination(src) == dest and same_mask(src, built)
                and src.pyramid is None else src) for key, stamp, src in slots]
        self.of = {key: (stamp, src) for key, stamp, src in out}
        return out


class Step3LabelBinding(QtCore.QObject):
    """Label tiles for one viewer's GPU layer, one slot per drawn mask."""

    _delivered = QtCore.pyqtSignal(object)
    status_changed = QtCore.pyqtSignal()

    def __init__(self, *, controller, layer, level_shapes, tile_size: int,
                 budget_bytes: int = LABEL_TEXTURE_BYTES,
                 motion_interval_ms: int = MOTION_INTERVAL_MS, builds: bool = True, parent=None):
        super().__init__(parent)
        #: False: the pyramids are built by the owner (`CoreLabelBinding`,
        #: one writer per destination across its L1 switches) and handed in
        #: with the slots
        self._builds = bool(builds)
        self.controller = controller
        self.layer = layer
        self.level_shapes = [tuple(int(v) for v in s) for s in level_shapes]
        self.tile_size = int(tile_size)
        self.budget_bytes = int(budget_bytes)
        self._stamps = SlotStamps()
        self._slots: List[Tuple[str, int, Any]] = []   # (key, stamp, source), drawing order
        self._styles: Dict[str, MaskStyle] = {}
        self._paused = False
        self._disposed = False
        self._connected = False
        self._latest = None                 # the controller's latest snapshot
        self._planned_epoch = None          # (slot list serial, epoch) last planned
        self._serial = 0                    # slot lists handed over (stats: generation)
        self._target_level: Optional[int] = None
        self._wanted: Dict[Tuple, int] = {}  # key -> order (lower first)
        self._resident: Dict[Tuple, LabelPlane] = {}
        self._building: set = set()          # destinations whose pyramid is being built
        self._retry_build: set = set()       # destinations cancelled by a pause
        self._reasons: Dict[str, Optional[str]] = {}   # per destination
        self._errors: Dict[int, str] = {}    # per stamp
        self._budget_note: Optional[str] = None
        self.counts = {"requested": 0, "read": 0, "accepted": 0, "stale_dropped": 0,
                       "late_dropped": 0, "pushes": 0, "pyramids_built": 0}
        self._last_error: Optional[str] = None   # drawing errors (not a slot's)
        # the reader thread's shared state, under `_cv`
        self._cv = threading.Condition()
        self._jobs: List[dict] = []
        self._pending_bytes = 0              # being read + delivered, not accepted
        self._reading: set = set()           # tile keys taken by the thread, not yet accepted
        self._resident_bytes = 0
        self._stop = False
        self._live_stamps: set = set()       # the thread drops tile jobs of other stamps
        self._live_dests: set = set()        # a build of another destination stops
        self._cancel_all = False             # paused: every build stops
        self._thread = threading.Thread(target=self._run, name="step3-labels", daemon=True)
        self._thread.start()
        self._delivered.connect(self._accept, QtCore.Qt.QueuedConnection)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_motion)
        self._motion_interval_ms = int(motion_interval_ms)
        self._connect()

    # ── public ────────────────────────────────────────────────────────
    def set_slots(self, slots) -> None:
        """The masks to draw, in drawing order: [(key, MaskSource, MaskStyle)].
        A slot whose mask is unchanged keeps its tiles (and a pyramid built
        for it); the tiles, jobs and builds of slots that left go."""
        if self._disposed:
            return
        slots = list(slots or ())
        self._slots = self._stamps.assign([(key, src) for key, src, _st in slots])
        self._styles = {key: (st or MaskStyle()) for key, _src, st in slots}
        self._serial += 1
        stamps = {stamp for _k, stamp, _s in self._slots}
        dests = {destination(src) for _k, _st, src in self._slots}
        with self._cv:
            before = sum(1 for j in self._jobs if j["type"] == "tile")
            self._jobs = [j for j in self._jobs
                          if (j["type"] == "tile" and j["stamp"] in stamps)
                          or (j["type"] == "pyramid" and j["dest"] in dests)]
            self.counts["stale_dropped"] += before - sum(1 for j in self._jobs if j["type"] == "tile")
            self._live_stamps = stamps
            self._live_dests = dests
            for key in [k for k in self._resident if k[0] not in stamps]:
                del self._resident[key]
            self._resident_bytes = sum(p.ids.nbytes for p in self._resident.values())
            self._cv.notify_all()
        self._wanted = {k: i for k, i in self._wanted.items() if k[0] in stamps}
        self._building &= dests
        self._retry_build &= dests
        self._errors = {s: e for s, e in self._errors.items() if s in stamps}
        self._reasons = {d: r for d, r in self._reasons.items() if d in dests}
        self._planned_epoch = None
        self._budget_note = None
        if not self._paused:
            self._queue_builds()
        self._push()
        self.update_viewport()
        self.status_changed.emit()

    def set_style(self, key: str, **changes) -> None:
        """Visibility, colour, alpha, width, mode, dash of one slot. Drawing
        only; a slot turned on is planned for the current view."""
        if key not in self._styles:
            return
        before = self._styles[key]
        self._styles[key] = dataclasses.replace(before, **changes)
        if self._styles[key].visible and not before.visible:
            self._planned_epoch = None
            self.update_viewport()
        self._push()

    def style(self, key: str) -> MaskStyle:
        return self._styles[key]

    def mask_status(self) -> Dict[str, Any]:
        """Per slot key: whether it has a source, the reason coarse levels
        are not drawn (None when they are), whether its pyramid is being
        built, its read error; and the viewer-wide budget note and error."""
        out = {}
        level = self._target_level
        for key, stamp, source in self._slots:
            dest = destination(source)
            coarse = None
            if source.pyramid is None:
                coarse = ("the zoomed-out levels are being prepared" if dest in self._building
                          else (self._reasons.get(dest) or source.pyramid_reason
                                or "no label pyramid"))
            out[key] = {"source": True, "coarse_unavailable": coarse,
                        "building": dest in self._building,
                        "unavailable_here": bool(coarse) and bool(level),
                        "error": self._errors.get(stamp)}
        out["budget"] = self._budget_note
        out["error"] = self._last_error
        return out

    def stats(self) -> Dict[str, Any]:
        with self._cv:
            pending, jobs, reading = self._pending_bytes, len(self._jobs), len(self._reading)
        return dict(self.counts, resident=len(self._resident), reading=reading,
                    queued=jobs, resident_bytes=self._resident_bytes,
                    pending_bytes=pending, budget_bytes=self.budget_bytes,
                    target_level=self._target_level, generation=self._serial,
                    thread_alive=self._thread.is_alive())

    def update_viewport(self, snapshot=None) -> None:
        if self._disposed or self._paused:
            return
        snapshot = self.controller.snapshot() if snapshot is None else snapshot
        self._latest = snapshot
        token = (self._serial, int(getattr(snapshot, "epoch", -1)))
        if token == self._planned_epoch:
            return
        self._planned_epoch = token
        self._plan(snapshot)

    def pause(self) -> bool:
        """Not on screen: ask for nothing. Queued reads are dropped, a
        pyramid build is cancelled (and restarted by `resume`)."""
        if self._disposed or self._paused:
            return False
        self._paused = True
        self._timer.stop()
        self._disconnect()
        with self._cv:
            self.counts["stale_dropped"] += sum(1 for j in self._jobs if j["type"] == "tile")
            self._retry_build |= {j["dest"] for j in self._jobs if j["type"] == "pyramid"}
            self._jobs = []
            self._cancel_all = True                         # a running build stops
            self._cv.notify_all()
        return True

    def resume(self) -> bool:
        if self._disposed or not self._paused:
            return False
        self._paused = False
        with self._cv:
            self._cancel_all = False
        # the builds the pause dropped start again (a build still running
        # starts again when its cancelled result lands, `_accept_pyramid`)
        self._building -= self._retry_build
        self._retry_build.clear()
        self._queue_builds()
        self._connect()
        self._planned_epoch = None
        self.update_viewport()
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def sources(self) -> Dict[str, Any]:
        """The masks as this binding knows them now, by slot key: a pyramid
        it built is in them (R3: a fallback's new binding starts from these)."""
        return {key: source for key, _stamp, source in self._slots}

    def dispose(self, timeout: float = 0.0) -> Dict[str, Any]:
        """Tell the thread to end, let the camera go, clear the layer's
        masks. Idempotent. The thread is NOT waited for (R3 plan §2.5:
        closing or a fallback never blocks the GUI on a read under way);
        it is listed in `closing_threads`, which the program's exit waits
        for. `timeout` > 0 waits that long (tests)."""
        if self._disposed:
            return {"already_disposed": True, "thread_alive": self._thread.is_alive()}
        self._disposed = True
        self._timer.stop()
        self._disconnect()
        with self._cv:
            self._stop = True
            self._cancel_all = True
            self._jobs = []
            self._cv.notify_all()
        if timeout > 0:
            self._thread.join(timeout)
        if self._thread.is_alive():
            closing_threads.register(self._thread)
        self._resident.clear()
        try:
            if getattr(self.layer, "labels_enabled", False):
                self.layer.set_labels(LabelSnapshot())
        except (Step1GpuLayerError, RuntimeError):
            pass
        return {"already_disposed": False, "thread_alive": self._thread.is_alive()}

    # ── the camera ────────────────────────────────────────────────────
    def _connect(self):
        if self._connected:
            return
        for name, slot in (("interaction_event", self._interaction_event),
                           ("gesture_quiet", self._gesture_quiet)):
            signal = getattr(self.controller, name, None)
            if signal is not None:
                signal.connect(slot)
        self._connected = True

    def _disconnect(self):
        if not self._connected:
            return
        for name, slot in (("interaction_event", self._interaction_event),
                           ("gesture_quiet", self._gesture_quiet)):
            signal = getattr(self.controller, name, None)
            if signal is not None:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self._connected = False

    def _interaction_event(self, kind, snapshot):
        if self._disposed or self._paused:
            return
        self._latest = snapshot
        if kind == "NAVIGATOR_JUMP":
            self._timer.stop()
            self.update_viewport(snapshot)
            return
        if not self._timer.isActive():
            self.update_viewport(snapshot)
            self._timer.start(self._motion_interval_ms)

    def _gesture_quiet(self, snapshot):
        if not self._disposed and not self._paused:
            self._latest = snapshot
            self._flush_motion()

    def _flush_motion(self):
        if self._latest is not None:
            self.update_viewport(self._latest)

    # ── planning ──────────────────────────────────────────────────────
    def _tile_rect(self, level, tx, ty):
        h, w = self.level_shapes[level]
        T = self.tile_size
        ds_y, ds_x = self.level_shapes[0][0] / h, self.level_shapes[0][1] / w
        rows = min(T, h - ty * T)
        cols = min(T, w - tx * T)
        x0, y0 = tx * T * ds_x, ty * T * ds_y
        return (x0, x0 + cols * ds_x, y0, y0 + rows * ds_y), rows * cols * 4

    @staticmethod
    def _overlaps(a, b):
        return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]

    def _plan(self, snapshot):
        level = int(snapshot.level)
        if not 0 <= level < len(self.level_shapes):
            return
        self._target_level = level
        h, w = self.level_shapes[level]
        T = self.tile_size
        nx, ny = math.ceil(w / T), math.ceil(h / T)
        visible = {(int(tx), int(ty)) for tx, ty in snapshot.visible_tiles
                   if 0 <= int(tx) < nx and 0 <= int(ty) < ny}
        ring = set()
        for tx, ty in visible:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (tx + dx, ty + dy)
                    if 0 <= q[0] < nx and 0 <= q[1] < ny and q not in visible:
                        ring.add(q)
        if visible:
            cx = (min(t[0] for t in visible) + max(t[0] for t in visible)) / 2.0
            cy = (min(t[1] for t in visible) + max(t[1] for t in visible)) / 2.0
            rects = [self._tile_rect(level, tx, ty)[0] for tx, ty in visible]
            view = (min(r[0] for r in rects), max(r[1] for r in rects),
                    min(r[2] for r in rects), max(r[3] for r in rects))
        else:
            cx = cy = 0.0
            view = None
        # nearest the centre first; ties by row, then column (R3b: a fixed
        # order, the same as the Rust core's planner)
        order = lambda t: ((t[0] - cx) ** 2 + (t[1] - cy) ** 2, t[1], t[0])   # noqa: E731

        shown = {stamp for key, stamp, _src in self._slots if self._styles[key].visible}
        # not requested, not drawn (the status says why): hidden, or a coarse
        # level without a pyramid
        asked = [(stamp, src) for key, stamp, src in self._slots
                 if stamp in shown and (level == 0 or src.pyramid is not None)]
        # TILE-MAJOR (plan §3.4): every slot gets a tile before any gets the next
        target_vis = [(stamp, level, tx, ty) for tx, ty in sorted(visible, key=order)
                      for stamp, _src in asked]
        target_ring = [(stamp, level, tx, ty) for tx, ty in sorted(ring, key=order)
                       for stamp, _src in asked]
        target = set(target_vis) | set(target_ring)

        # what stays: the target, and other levels' tiles still under the view
        keep = {}
        for key, plane in self._resident.items():
            if key in target:
                keep[key] = plane
            elif (key[1] != level and view is not None and key[0] in shown
                  and self._overlaps(plane.world_rect, view)):
                keep[key] = plane
        # the budget: old levels first, then the ring, then the far visible
        size = lambda key: self._tile_rect(key[1], key[2], key[3])[1]   # noqa: E731
        note = None
        wanted_vis, wanted_ring = list(target_vis), list(target_ring)
        old = sorted((k for k in keep if k[1] != level), key=lambda k: abs(k[1] - level))

        def total():
            return (sum(size(k) for k in keep)
                    + sum(size(k) for k in wanted_vis + wanted_ring if k not in keep))
        while total() > self.budget_bytes and old:
            keep.pop(old.pop())               # farthest level first
        if total() > self.budget_bytes:
            for key in wanted_ring:
                keep.pop(key, None)
            wanted_ring = []
        if total() > self.budget_bytes:
            fitted, used = [], 0
            for key in wanted_vis:
                if used + size(key) > self.budget_bytes:
                    break
                fitted.append(key)
                used += size(key)
            for key in wanted_vis[len(fitted):]:
                keep.pop(key, None)
            need = sum(size(k) for k in wanted_vis)
            wanted_vis = fitted
            note = (f"mask memory is full: part of the view has no mask "
                    f"(needs {need // 2**20} MB, limit {self.budget_bytes // 2**20} MB)")
            print(f"[Step3] {note}")
        self._budget_note = note
        released = [k for k in self._resident if k not in keep]
        for key in released:
            del self._resident[key]
        self._wanted = {key: i for i, key in enumerate(wanted_vis + wanted_ring)}

        # the queue: exactly the wanted tiles not already here or being read
        source_of = dict(asked)
        with self._cv:
            reading = set(self._reading)
        jobs = []
        for key in wanted_vis + wanted_ring:
            if key in self._resident or key in reading:
                continue
            jobs.append({"type": "tile", "stamp": key[0], "key": key,
                         "epoch": int(getattr(snapshot, "epoch", -1)),
                         "bytes": size(key), "source": source_of[key[0]]})
        with self._cv:
            builds = [j for j in self._jobs if j["type"] == "pyramid"]
            queued = {j["key"] for j in self._jobs if j["type"] == "tile"}
            self.counts["stale_dropped"] += len(queued - set(self._wanted))
            self.counts["requested"] += sum(1 for j in jobs if j["key"] not in queued)
            self._jobs = builds + jobs
            self._resident_bytes = sum(p.ids.nbytes for p in self._resident.values())
            self._cv.notify_all()
        self._push()
        self.status_changed.emit()

    def _queue_builds(self):
        """A build for every destination a slot needs a pyramid from, once
        (ensure_pyramid decides what can be done: it never replaces a
        pyramid `label_pyramid.read` accepts)."""
        if len(self.level_shapes) < 2 or not self._builds:
            return
        builds, seen = [], set()
        for _key, _stamp, source in self._slots:
            dest = destination(source)
            if source.pyramid is not None or dest in seen or dest in self._building:
                continue
            if self._reasons.get(dest):
                continue                      # tried: level 0 only, the reason is shown
            seen.add(dest)
            self._building.add(dest)
            builds.append({"type": "pyramid", "dest": dest, "source": source})
        if builds:
            with self._cv:
                queued = [j for j in self._jobs if j["type"] == "pyramid"]   # never dropped here
                self._jobs = queued + builds + [j for j in self._jobs if j["type"] != "pyramid"]
                self._cv.notify_all()

    # ── the reader thread ─────────────────────────────────────────────
    def _run(self):
        while True:
            with self._cv:
                while not self._stop:
                    job = self._next_job_locked()
                    if job is not None:
                        break
                    self._cv.wait()
                if self._stop:
                    return
                if job["type"] == "tile":
                    self._pending_bytes += job["bytes"]
                    self._reading.add(job["key"])
            try:
                if job["type"] == "pyramid":
                    dest = job["dest"]
                    result = step3_masks.ensure_pyramid(
                        job["source"], self.level_shapes,
                        cancel_check=lambda: (self._stop or self._cancel_all
                                              or dest not in self._live_dests))
                else:
                    _stamp, level, tx, ty = job["key"]
                    result = step3_masks.read_label_tile(job["source"], level, tx, ty,
                                                         self.tile_size, self.level_shapes)
            except Exception as exc:  # noqa: BLE001 -- reported, the viewer goes on
                result = exc
            self._delivered.emit((job, result))

    def _next_job_locked(self):
        while self._jobs:
            job = self._jobs[0]
            if job["type"] == "tile":
                if job["stamp"] not in self._live_stamps:
                    self._jobs.pop(0)
                    self.counts["stale_dropped"] += 1
                    continue
                if self._resident_bytes + self._pending_bytes + job["bytes"] > self.budget_bytes:
                    return None                 # wait until the GUI takes results
            return self._jobs.pop(0)
        return None

    # ── results, on the GUI thread ────────────────────────────────────
    def _accept(self, payload):
        job, result = payload
        if job["type"] == "tile":
            with self._cv:
                self._pending_bytes -= job["bytes"]
                self._reading.discard(job["key"])
                self._cv.notify_all()
        if self._disposed:
            return
        if job["type"] == "pyramid":
            self._accept_pyramid(job, result)
            return
        key = job["key"]
        self.counts["read"] += 1
        # THE SLOT FIRST: a late result of a mask that left -- an error
        # included -- is not this view's news.
        if job["stamp"] not in self._live_stamps:
            self.counts["late_dropped"] += 1
            return
        if isinstance(result, Exception):
            self._errors[job["stamp"]] = f"label tile {key[1:]} could not be read: {result}"
            print(f"[Step3] {self._errors[job['stamp']]}")
            self.status_changed.emit()
            return
        if key not in self._wanted:
            self.counts["late_dropped"] += 1
            return
        if not isinstance(result, step3_masks.LabelTile):
            return                              # Unavailable: nothing to draw
        self._resident[key] = LabelPlane(identity=key, world_rect=tuple(result.world_rect),
                                         ids=result.labels)
        with self._cv:
            self._resident_bytes += result.labels.nbytes
        self.counts["accepted"] += 1
        self._push()

    def _accept_pyramid(self, job, result):
        dest = job["dest"]
        self._building.discard(dest)
        if dest not in self._live_dests:
            if not isinstance(result, Exception) and result.status == "ready":
                self._stamps.remember(result.source)    # for a slot ticked again
            return                              # no slot needs it any more
        if isinstance(result, Exception):
            self._reasons[dest] = f"the zoomed-out levels could not be built ({result})"
        elif result.status == "cancelled":
            if self._paused:
                self._retry_build.add(dest)       # restarted by resume()
            else:                                  # resumed before the cancel landed
                self._queue_builds()
                self.status_changed.emit()
            return
        elif result.status == "ready":
            # every slot of this destination takes the built source (same
            # mask: its stamp stays, `SlotStamps`)
            self._slots = self._stamps.take_built(self._slots, dest, result.source)
            self._reasons[dest] = None
            self.counts["pyramids_built"] += 1
        elif not any(destination(src) == dest and src.pyramid is not None
                     for _k, _s, src in self._slots):
            self._slots = self._stamps.take_built(self._slots, dest, result.source)
            self._reasons[dest] = result.reason or "no label pyramid"
        if self._reasons.get(dest):
            print(f"[Step3] {dest}: zoomed out it cannot be shown: {self._reasons[dest]}")
        self._planned_epoch = None
        if not self._paused:
            self.update_viewport()
        self.status_changed.emit()

    # ── to the layer ──────────────────────────────────────────────────
    def _push(self):
        if not getattr(self.layer, "labels_enabled", False) or self._disposed:
            return
        target = self._target_level
        layers = []
        for key, stamp, _src in self._slots:
            style = self._styles[key]
            mine = [(k, p) for k, p in self._resident.items() if k[0] == stamp]
            mine.sort(key=lambda kp: (kp[0][1] == target,
                                      -abs(kp[0][1] - (target or 0)), kp[0][3], kp[0][2]))
            layers.append(LabelLayer(key, tuple(p for _k, p in mine), color=style.color,
                                     alpha=style.alpha, width=style.width, mode=style.mode,
                                     visible=style.visible, dash=style.dash))
        try:
            self.layer.set_labels(LabelSnapshot(tuple(layers)))
            self.counts["pushes"] += 1
        except (Step1GpuLayerError, RuntimeError) as exc:
            self._last_error = f"masks could not be drawn: {exc}"
            print(f"[Step3] {self._last_error}")
            self.status_changed.emit()
