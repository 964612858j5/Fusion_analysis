"""Step3's label supply for the GPU layer -- block 4b.

Follows the ONE controller of Step3's viewer (its camera events, the level
and the visible tiles of its snapshots) and keeps the GPU layer's mask
layers (`Step1GpuLayer.set_labels`) supplied with the label tiles of
`core.step3_masks` sources:

  * ONE reader thread (the only thread block 4 adds): it first builds the
    pyramid a source lacks (`ensure_pyramid`, cancellable), then reads label
    tiles (`read_label_tile`). While a pyramid is being built no tile is
    read -- level 0 waits as well; the status says so;
  * what is read: the current level's visible tiles and one ring around
    them, centre first. Jumps are served at once, motion every 33 ms, a
    quiet camera catches up once -- the image binding's rhythm;
  * a request that is no longer wanted when the camera moves on is dropped
    from the queue and counted (`stale_dropped`); a result that arrives for
    an older source generation or a tile no longer wanted is dropped and
    counted (`late_dropped`);
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
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt5 import QtCore

from ..core import step3_masks
from .step1_gpu_layer import (LABEL_OUTLINE, LABEL_TEXTURE_BYTES, LabelLayer,
                              LabelPlane, LabelSnapshot, Step1GpuLayerError)

CELL = step3_masks.CELL
NUCLEUS = step3_masks.NUCLEUS
#: Drawn in this order: the nucleus lies on top (user ruling 1).
KINDS = (CELL, NUCLEUS)
MOTION_INTERVAL_MS = 33


@dataclasses.dataclass(frozen=True)
class MaskStyle:
    visible: bool = True
    color: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    alpha: float = 0.75
    width: float = 1.0                  # logical pixels, 0..4
    mode: str = LABEL_OUTLINE


DEFAULT_STYLES = {
    CELL: MaskStyle(),
    NUCLEUS: MaskStyle(color=(0.0, 0.8, 1.0)),
}


class Step3LabelBinding(QtCore.QObject):
    """Label tiles for one viewer's GPU layer."""

    _delivered = QtCore.pyqtSignal(object)
    status_changed = QtCore.pyqtSignal()

    def __init__(self, *, controller, layer, level_shapes, tile_size: int,
                 budget_bytes: int = LABEL_TEXTURE_BYTES,
                 motion_interval_ms: int = MOTION_INTERVAL_MS, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.layer = layer
        self.level_shapes = [tuple(int(v) for v in s) for s in level_shapes]
        self.tile_size = int(tile_size)
        self.budget_bytes = int(budget_bytes)
        self._sources: Dict[str, Optional[step3_masks.MaskSource]] = {CELL: None, NUCLEUS: None}
        self._styles: Dict[str, MaskStyle] = dict(DEFAULT_STYLES)
        self._generation = 0
        self._paused = False
        self._disposed = False
        self._connected = False
        self._latest = None                 # the controller's latest snapshot
        self._planned_epoch = None          # (generation, epoch) last planned
        self._target_level: Optional[int] = None
        self._wanted: Dict[Tuple, int] = {}  # key -> order (lower first)
        self._resident: Dict[Tuple, LabelPlane] = {}
        self._building: set = set()          # kinds whose pyramid is being built
        self._retry_build: set = set()       # kinds cancelled by a pause
        self._reasons: Dict[str, Optional[str]] = {CELL: None, NUCLEUS: None}
        self._budget_note: Optional[str] = None
        self.counts = {"requested": 0, "read": 0, "accepted": 0, "stale_dropped": 0,
                       "late_dropped": 0, "pushes": 0, "pyramids_built": 0}
        self._last_error: Optional[str] = None
        # the reader thread's shared state, under `_cv`
        self._cv = threading.Condition()
        self._jobs: List[dict] = []
        self._pending_bytes = 0              # being read + delivered, not accepted
        self._reading: set = set()           # tile keys taken by the thread, not yet accepted
        self._resident_bytes = 0
        self._stop = False
        self._cancel_generation = -1         # a build of this generation stops
        self._thread = threading.Thread(target=self._run, name="step3-labels", daemon=True)
        self._thread.start()
        self._delivered.connect(self._accept, QtCore.Qt.QueuedConnection)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_motion)
        self._motion_interval_ms = int(motion_interval_ms)
        self._connect()

    # ── public ────────────────────────────────────────────────────────
    def set_sources(self, sources) -> None:
        """New masks (`resolve_masks`' cell / nucleus, None for either):
        a new generation -- everything of the old one is dropped."""
        if self._disposed:
            return
        self._generation += 1
        with self._cv:
            self._cancel_generation = self._generation - 1
            self.counts["stale_dropped"] += len(self._jobs)
            self._jobs = []
            self._resident_bytes = 0
            self._cv.notify_all()
        self._resident.clear()
        self._wanted = {}
        self._building.clear()
        self._retry_build.clear()
        self._planned_epoch = None
        self._sources = {kind: (sources or {}).get(kind) for kind in KINDS}
        self._reasons = {kind: None for kind in KINDS}
        self._budget_note = None
        if not self._paused:
            self._queue_builds(self._sources)
        self._push()
        self.update_viewport()
        self.status_changed.emit()

    def set_style(self, kind: str, **changes) -> None:
        """Visibility, colour, alpha, width, mode of one mask. Drawing only;
        a mask turned on is planned for the current view."""
        before = self._styles[kind]
        self._styles[kind] = dataclasses.replace(before, **changes)
        if self._styles[kind].visible and not before.visible:
            self._planned_epoch = None
            self.update_viewport()
        self._push()

    def style(self, kind: str) -> MaskStyle:
        return self._styles[kind]

    def mask_status(self) -> Dict[str, Any]:
        """Per kind: whether it has a source, the reason coarse levels are
        not drawn (None when they are), whether its pyramid is being built;
        and a note when the budget left part of the view without masks."""
        out = {}
        level = self._target_level
        for kind in KINDS:
            source = self._sources.get(kind)
            coarse = None
            if source is not None and source.pyramid is None:
                coarse = ("the zoomed-out levels are being prepared" if kind in self._building
                          else (self._reasons.get(kind) or source.pyramid_reason
                                or "no label pyramid"))
            out[kind] = {"source": source is not None, "coarse_unavailable": coarse,
                         "building": kind in self._building,
                         "unavailable_here": bool(coarse) and bool(level)}
        out["budget"] = self._budget_note
        out["error"] = self._last_error
        return out

    def stats(self) -> Dict[str, Any]:
        with self._cv:
            pending, jobs = self._pending_bytes, len(self._jobs)
        with self._cv:
            reading = len(self._reading)
        return dict(self.counts, resident=len(self._resident), reading=reading,
                    queued=jobs, resident_bytes=self._resident_bytes,
                    pending_bytes=pending, budget_bytes=self.budget_bytes,
                    target_level=self._target_level, generation=self._generation,
                    thread_alive=self._thread.is_alive())

    def update_viewport(self, snapshot=None) -> None:
        if self._disposed or self._paused:
            return
        snapshot = self.controller.snapshot() if snapshot is None else snapshot
        self._latest = snapshot
        token = (self._generation, int(getattr(snapshot, "epoch", -1)))
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
            self._retry_build |= {j["kind"] for j in self._jobs if j["type"] == "pyramid"}
            self._jobs = []
            self._cancel_generation = self._generation      # a running build stops
            self._cv.notify_all()
        return True

    def resume(self) -> bool:
        if self._disposed or not self._paused:
            return False
        self._paused = False
        with self._cv:
            self._cancel_generation = -1
        retry = {kind: self._sources.get(kind) for kind in self._retry_build}
        self._retry_build.clear()
        self._queue_builds(retry)
        self._connect()
        self._planned_epoch = None
        self.update_viewport()
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def dispose(self, timeout: float = 10.0) -> Dict[str, Any]:
        """End the thread (and wait for it), let the camera go, clear the
        layer's masks. Idempotent."""
        if self._disposed:
            return {"already_disposed": True, "thread_alive": self._thread.is_alive()}
        self._disposed = True
        self._timer.stop()
        self._disconnect()
        with self._cv:
            self._stop = True
            self._cancel_generation = self._generation
            self._jobs = []
            self._cv.notify_all()
        self._thread.join(timeout)
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
        order = lambda t: (t[0] - cx) ** 2 + (t[1] - cy) ** 2   # noqa: E731

        gen = self._generation
        target_vis, target_ring = [], []
        for kind in KINDS:
            source = self._sources.get(kind)
            if source is None or not self._styles[kind].visible:
                continue
            if level > 0 and source.pyramid is None:
                continue                      # not requested, not drawn (status says why)
            target_vis += [(gen, kind, level, tx, ty) for tx, ty in sorted(visible, key=order)]
            target_ring += [(gen, kind, level, tx, ty) for tx, ty in sorted(ring, key=order)]
        target = set(target_vis) | set(target_ring)

        # what stays: the target, and other levels' tiles still under the view
        keep = {}
        for key, plane in self._resident.items():
            if key in target:
                keep[key] = plane
            elif (key[0] == gen and key[2] != level and view is not None
                  and self._styles[key[1]].visible and self._sources.get(key[1]) is not None
                  and self._overlaps(plane.world_rect, view)):
                keep[key] = plane
        # the budget: old levels first, then the ring, then the far visible
        size = lambda key: self._tile_rect(key[2], key[3], key[4])[1]   # noqa: E731
        note = None
        wanted_vis, wanted_ring = list(target_vis), list(target_ring)
        old = sorted((k for k in keep if k[2] != level), key=lambda k: abs(k[2] - level))

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
        with self._cv:
            reading = set(self._reading)
        jobs = []
        for key in wanted_vis + wanted_ring:
            if key in self._resident or key in reading:
                continue
            jobs.append({"type": "tile", "gen": gen, "key": key, "kind": key[1],
                         "epoch": int(getattr(snapshot, "epoch", -1)),
                         "bytes": size(key), "source": self._sources[key[1]]})
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

    def _queue_builds(self, sources):
        builds = []
        for kind in KINDS:
            source = (sources or {}).get(kind)
            if source is None or source.pyramid is not None or len(self.level_shapes) < 2:
                continue
            # ensure_pyramid decides what can be done (it never replaces a
            # pyramid `label_pyramid.read` accepts)
            self._building.add(kind)
            builds.append({"type": "pyramid", "gen": self._generation, "kind": kind,
                           "source": source})
        if builds:
            with self._cv:
                self._jobs = builds + [j for j in self._jobs if j["type"] != "pyramid"]
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
                    gen = job["gen"]
                    result = step3_masks.ensure_pyramid(
                        job["source"], self.level_shapes,
                        cancel_check=lambda: self._stop or self._cancel_generation >= gen)
                else:
                    _gen, _kind, level, tx, ty = job["key"]
                    result = step3_masks.read_label_tile(job["source"], level, tx, ty,
                                                         self.tile_size, self.level_shapes)
            except Exception as exc:  # noqa: BLE001 -- reported, the viewer goes on
                result = exc
            self._delivered.emit((job, result))

    def _next_job_locked(self):
        while self._jobs:
            job = self._jobs[0]
            if job["type"] == "tile":
                if job["gen"] != self._generation:
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
        if isinstance(result, Exception):
            self._last_error = f"label tile {key[2:]} could not be read: {result}"
            print(f"[Step3] {self._last_error}")
            return
        if job["gen"] != self._generation or key not in self._wanted:
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
        kind = job["kind"]
        if job["gen"] != self._generation:
            return
        self._building.discard(kind)
        if isinstance(result, Exception):
            self._reasons[kind] = f"the zoomed-out levels could not be built ({result})"
        elif result.status == "cancelled":
            if self._paused:
                self._retry_build.add(kind)       # restarted by resume()
            else:                                  # resumed before the cancel landed
                self._queue_builds({kind: self._sources.get(kind)})
                self.status_changed.emit()
                return
        elif result.status == "ready":
            self._sources[kind] = result.source
            self._reasons[kind] = None
            self.counts["pyramids_built"] += 1
        else:
            self._sources[kind] = result.source
            self._reasons[kind] = result.reason
        if self._reasons[kind]:
            print(f"[Step3] {kind} mask: zoomed out it cannot be shown: {self._reasons[kind]}")
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
        for kind in KINDS:
            style = self._styles[kind]
            if self._sources.get(kind) is None:
                continue
            mine = [(k, p) for k, p in self._resident.items() if k[1] == kind]
            mine.sort(key=lambda kp: (kp[0][2] == target,
                                      -abs(kp[0][2] - (target or 0)), kp[0][4], kp[0][3]))
            layers.append(LabelLayer(kind, tuple(p for _k, p in mine), color=style.color,
                                     alpha=style.alpha, width=style.width, mode=style.mode,
                                     visible=style.visible))
        try:
            self.layer.set_labels(LabelSnapshot(tuple(layers)))
            self.counts["pushes"] += 1
        except (Step1GpuLayerError, RuntimeError) as exc:
            self._last_error = f"masks could not be drawn: {exc}"
            print(f"[Step3] {self._last_error}")
