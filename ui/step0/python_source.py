"""Step0's live correction drawn by the Rust engine (docs/v17_step0_engine_plan.md
§3.3, §11.4): the tiles of a Python source the engine wants and does not
hold are computed here, by the SAME `CorrectionCompute` and the SAME
`TileScheduler` the Python picture uses (same cache, same dedup), and handed
back. One adapter per core.

* A SOURCE is one selection `(channel, method, params)`. It is registered
  under a number of the core's reserved range (`add_python_source`), never
  reused; at most two are live (shown, being prepared).
* DEMANDS come from `core.drain_demands()`: new tiles become requests,
  withdrawn ones are cancelled. Each request has its own generation token
  `("s0b", adapter, source, level, tx, ty, n)` -- unique across cores and
  re-requests -- and asks for a terminal callback even when cancelled
  (`notify_on_stale_completion`), so every request ends exactly once. An
  ended token is forgotten by the scheduler and never cancelled again.
* CREDIT bounds "requested and not ended" plus "ended and not yet handed to
  the engine": `MAX_TILES` tiles and `MAX_BYTES` bytes, reserved before the
  request. A successful result keeps its credit until it is handed over or
  dropped; a cancellation, stale end or error frees it at once. Demands
  beyond the credit wait here.
* HANDING OVER happens on the GUI thread only, at most `COMMIT_MS` per round
  (the rest next round), and never inside another core call: scheduler
  callbacks -- synchronous on a cache hit, on a worker thread otherwise --
  only queue, through a queued signal.

Payloads are `CorrectionCompute`'s float32 tiles as they are (no gain: the
gain is a display parameter, `set_display(gains=...)`).
"""

import collections
import itertools
import time
from typing import Dict

import numpy as np
from PyQt5 import QtCore

from ...utils import perf_trace
from ...viewer.tile_types import CorrectionKey, TileAddress, TileRequest

#: the core's reserved range (`python_source::FIRST`)
FIRST_SOURCE = 0x8000
MAX_TILES = 64
MAX_BYTES = 128 * 1024 * 1024
COMMIT_MS = 4.0
#: §13.2 1c: drains of a cache-only fill, at most
PREFILL_ROUNDS = 4

_ADAPTERS = itertools.count(1)


class _Source:
    __slots__ = ("sid", "channel", "method", "params")

    def __init__(self, sid, channel, method, params):
        self.sid = int(sid)
        self.channel = channel
        self.method = method
        self.params = tuple(int(p) for p in params)

    @property
    def selection(self):
        return (self.channel, self.method, self.params)


class _Request:
    __slots__ = ("token", "sid", "key", "nbytes", "ended")

    def __init__(self, token, sid, key, nbytes):
        self.token = token
        self.sid = sid
        self.key = key                  # (level, tx, ty)
        self.nbytes = nbytes
        self.ended = False


class PythonSourceAdapter(QtCore.QObject):
    """Computes one core's Python sources. `pump()` on every paint and
    every notice; `on_progress()` is asked for a repaint when a tile went in."""

    _ended = QtCore.pyqtSignal(object, object)          # token, TileResult

    def __init__(self, core, controller, scheduler, *, on_progress=None, parent=None):
        super().__init__(parent)
        self.core = core
        self.controller = controller
        self.scheduler = scheduler
        self.on_progress = on_progress or (lambda: None)
        self.uid = next(_ADAPTERS)
        self._next_sid = FIRST_SOURCE
        self.sources = {}                               # sid -> _Source
        self._requests = {}                             # token -> _Request (not ended, or ended and held)
        self._by_tile = {}                              # (sid, level, tx, ty) -> token
        self._waiting = collections.OrderedDict()       # (sid, level, tx, ty) -> None
        self._ready = collections.OrderedDict()         # token -> float32 array
        self._issued = itertools.count()
        self._tiles = 0
        self._bytes = 0
        self._closed = False
        #: S0-B §11.7: Step0 left -- no new demand is taken or requested;
        #: work under way still ends and its result still goes in
        self._paused = False
        #: §14.5 2: a synchronous preparation drives the rounds itself -- an
        #: end only does the bookkeeping, it does not pump
        self._sync = False
        #: §15.6: (sid) -> the tiles `(level, tx, ty)` a waiting switch of
        #: that source needs first, or None (no restriction); its other
        #: tiles wait here, not asked for, until it is published
        self.gate_fn = None
        self.counts = collections.Counter()
        self._ended.connect(self._on_ended, QtCore.Qt.QueuedConnection)
        self._again = QtCore.QTimer(self)
        self._again.setSingleShot(True)
        self._again.timeout.connect(self.pump)

    # ── sources ──────────────────────────────────────────────────────
    def register(self, channel, method, params) -> int:
        """A new source for this selection; its number. Raises ValueError
        when the core refuses (the caller falls back)."""
        sid = self._next_sid
        self.core.add_python_source(sid)
        self._next_sid += 1
        self.sources[sid] = _Source(sid, channel, method, params)
        self.counts["registered"] += 1
        return sid

    def retire(self, sid) -> None:
        """Everything of `sid` goes: the core's state, its queued and held
        tiles (credit freed), its requests cancelled."""
        sid = int(sid)
        if self.sources.pop(sid, None) is None:
            return
        self.core.drop_python_source(sid)
        for tile in [t for t in self._waiting if t[0] == sid]:
            del self._waiting[tile]
        for token, req in list(self._requests.items()):
            if req.sid == sid:
                self._withdraw(token)
        self.counts["retired"] += 1

    def retire_all(self) -> None:
        for sid in list(self.sources):
            self.retire(sid)

    def selection_of(self, sid):
        source = self.sources.get(int(sid))
        return None if source is None else source.selection

    # ── the round ────────────────────────────────────────────────────
    def pump_soon(self) -> None:
        """A pump on the next event-loop turn (outside any core call): a
        picture was just published, the tiles held back for it go out now
        (§15.6) -- no new demand would wake them."""
        if not self._closed and not self._again.isActive():
            self._again.start(0)

    def pump(self) -> bool:
        """Take the core's demands, request what the credit allows, hand
        over what is ready (within `COMMIT_MS`). True when a tile went in."""
        if self._closed:
            return False
        if self._paused:
            went_in = self._hand_over()
            if self._ready and not self._again.isActive():
                self._again.start(0)
            return went_in
        new, withdrawn = self.core.drain_demands()
        self._take_withdrawn(withdrawn)
        for sid, level, tx, ty in new:
            tile = (int(sid), int(level), int(tx), int(ty))
            if tile[0] in self.sources and tile not in self._by_tile:
                self._waiting[tile] = None
        self._request_waiting()
        went_in = self._hand_over()
        if went_in:
            self._request_waiting()                     # the credit just freed
        if self._ready and not self._again.isActive():
            self._again.start(0)                         # the rest next round
        return went_in

    def _take_withdrawn(self, withdrawn) -> None:
        for sid, level, tx, ty in withdrawn:
            tile = (int(sid), int(level), int(tx), int(ty))
            self._waiting.pop(tile, None)
            token = self._by_tile.get(tile)
            if token is not None:
                self._withdraw(token, keep_ready=True)

    def fill_from_cache(self, peek):
        """§13.2 1a-c, before a production hand-off: the core's new demands
        that `peek(key)` (`TileScheduler.peek_corrected`: the corrected
        cache, nothing computed, read or queued) holds go into the core now;
        the others wait here, NOT requested (the hand-off comes next).
        `(hits, misses)`; None when nothing can be taken."""
        if self._closed or self._paused:
            return None
        hits = misses = 0
        for _ in range(PREFILL_ROUNDS):
            new, withdrawn = self.core.drain_demands()
            self._take_withdrawn(withdrawn)
            got = 0
            for sid, level, tx, ty in new:
                tile = (int(sid), int(level), int(tx), int(ty))
                source = self.sources.get(tile[0])
                if source is None or tile in self._by_tile:
                    continue
                arr = peek(self._key(source, *tile[1:]))
                if arr is None:
                    self._waiting[tile] = None
                    misses += 1
                    continue
                # each completion returns before the next drain: never a
                # core call inside a core call
                self.core.complete_python_tile(
                    tile[0], tile[1], tile[2], tile[3],
                    np.ascontiguousarray(arr, dtype=np.float32).tobytes())
                self._waiting.pop(tile, None)
                self.counts["prefilled"] += 1
                hits += 1
                got += 1
            if not got:
                break
        return hits, misses

    def suspend_requests(self) -> None:
        """§13.6-13.7, a hand-off that keeps the picture: every request not
        ended is cancelled (queued work is dropped by the scheduler, running
        work ends stale); a tile still wanted (its token still owns it --
        not withdrawn) waits here and is asked for once the adapter runs
        again, after that end."""
        for token, req in list(self._requests.items()):
            if req.ended:
                continue
            tile = (req.sid,) + req.key
            if self._by_tile.get(tile) == token:
                self._waiting[tile] = None
            self.counts["suspended"] += 1
            self.scheduler.cancel_generation(token)

    def _tile_bytes(self, level, tx, ty) -> int:
        provider = self.controller.provider
        h, w = provider.level_shape(int(level))
        size = int(self.controller.grid.tile_size)
        rows = max(0, min(size, h - int(ty) * size))
        cols = max(0, min(size, w - int(tx) * size))
        return rows * cols * 4

    def _request_waiting(self) -> None:
        provider = self.controller.provider
        levels = int(getattr(provider, "num_levels", 1) or 1)
        gates = {}
        order = list(self._waiting)
        if self.gate_fn is not None:
            for sid in {t[0] for t in order}:
                gate = self.gate_fn(sid)
                if gate is not None:
                    gates[sid] = gate
            if gates:
                # §15.6: what the gate needs first; the rest of a gated
                # source waits until its picture is published
                first = [t for t in order if t[0] in gates and t[1:] in gates[t[0]]]
                order = first + [t for t in order if t[0] not in gates]
        for tile in order:
            if tile in self._by_tile:
                continue            # a request of it still has to end: asked once it has (§13.7)
            sid, level, tx, ty = tile
            nbytes = self._tile_bytes(level, tx, ty)
            if self._tiles + 1 > MAX_TILES or self._bytes + nbytes > MAX_BYTES:
                self.counts["credit_full"] += 1
                return
            del self._waiting[tile]
            source = self.sources.get(sid)
            if source is None:
                continue
            key = self._key(source, level, tx, ty)
            token = ("s0b", self.uid, sid, level, tx, ty, next(self._issued))
            req = _Request(token, sid, (level, tx, ty), nbytes)
            self._requests[token] = req
            self._by_tile[tile] = token
            self._tiles += 1
            self._bytes += nbytes
            self.counts["requested"] += 1
            # coarser first; the scheduler's heap pops the smallest
            priority = max(0, levels - 1 - int(level))
            emit = self._ended.emit
            self.scheduler.request(
                TileRequest(key=key, generation=token, priority=priority,
                            notify_on_stale_completion=True),
                lambda result, _t=token: emit(_t, result))

    def _key(self, source, level, tx, ty) -> CorrectionKey:
        c = self.controller
        params, quality, algorithm = c.correction_identity(
            int(level), source.channel, source.method, source.params)
        return CorrectionKey(
            source=c.provider.source_identity(), channel=source.channel,
            tile=TileAddress(grid=c.grid, level=int(level), tx=int(tx), ty=int(ty)),
            method=source.method, params=params, algorithm_version=algorithm, quality=quality)

    # ── ends (queued to the GUI thread) ──────────────────────────────
    @QtCore.pyqtSlot(object, object)
    def _on_ended(self, token, result) -> None:
        req = self._requests.get(token)
        try:
            self.scheduler.forget_generation(token)
        except Exception:                               # noqa: BLE001 -- a scheduler gone
            pass
        if req is None or req.ended:
            return
        req.ended = True
        error = getattr(result, "error", None)
        pixels = getattr(result, "pixels", None)
        live = req.sid in self.sources and not self._closed
        tile = (req.sid,) + req.key
        # §13.7-13.8: an end that hands the tile's outcome to the core (a
        # result, a failure) takes it off the waiting list; only a
        # cancelled / stale end leaves a still-wanted tile waiting
        owns = self._by_tile.get(tile) == token
        if error is None and pixels is not None and live:
            self._ready[token] = np.ascontiguousarray(pixels.handle, dtype=np.float32)
            if owns:
                self._waiting.pop(tile, None)
            self.counts["computed"] += 1                # keeps its credit until handed over
        else:
            if error in ("cancelled", "stale", "shutdown") or not live:
                self._free(token)
                self.counts["ended_" + str(error or "dropped")] += 1
            else:
                if owns:
                    self._waiting.pop(tile, None)
                self._free(token)
                self.counts["failed"] += 1
                self.core.fail_python_tile(req.sid, *req.key)
                # the paint is where the core's backoff and give-up are read
                self.on_progress()
        if self._closed:
            self._maybe_dispose()
        elif not self._sync:
            self.pump()

    def _withdraw(self, token, keep_ready=False) -> None:
        """No longer wanted: a request still running is cancelled (its end
        frees the credit). A result already computed still goes into the
        core's cache when `keep_ready` and its source lives (a view moved
        away, Step0 left: §11.7 "cached on completion"); else it is dropped
        now (its source retired)."""
        req = self._requests.get(token)
        if req is None:
            return
        if token in self._ready:
            if keep_ready and req.sid in self.sources:
                self.counts["withdrawn_kept"] += 1
                return
            del self._ready[token]
            self._free(token)
            self.counts["withdrawn_ready"] += 1
            return
        if not req.ended:
            tile = (req.sid,) + req.key
            if self._by_tile.get(tile) == token:
                del self._by_tile[tile]
            self.counts["withdrawn"] += 1
            self.scheduler.cancel_generation(token)

    def _free(self, token) -> None:
        """The request's credit goes back, and its tile is free to be asked
        for again (the tile -> token entry lives until the result is handed
        over or dropped, so a withdrawal finds a result still waiting)."""
        req = self._requests.pop(token, None)
        if req is None:
            return
        tile = (req.sid,) + req.key
        if self._by_tile.get(tile) == token:
            del self._by_tile[tile]
        self._tiles -= 1
        self._bytes -= req.nbytes

    def _hand_over(self, limit_ms=None) -> bool:
        started = time.perf_counter()
        limit_ms = COMMIT_MS if limit_ms is None else limit_ms
        went_in = False
        while self._ready:
            if (time.perf_counter() - started) * 1000.0 >= limit_ms:
                break
            token, arr = self._ready.popitem(last=False)
            req = self._requests.get(token)
            self._free(token)
            if req is None or req.sid not in self.sources:
                continue
            level, tx, ty = req.key
            taken = self.core.complete_python_tile(req.sid, level, tx, ty, arr.tobytes())
            self._waiting.pop((req.sid, level, tx, ty), None)       # §13.7 2
            self.counts["handed"] += 1
            went_in = went_in or bool(taken)
        if went_in:
            perf_trace.mark("step0.python_tiles_in", ready=len(self._ready))
            self.on_progress()
        return went_in

    # ── §14.5 2 / 15.5: a synchronous preparation, one round at a time ──
    def set_sync(self, on: bool) -> None:
        self._sync = bool(on)

    def step_sync(self, peek, limit_ms) -> Dict[str, int]:
        """One round of a synchronous preparation (the mount paints after
        it): the core's new demands -- cache hits handed over now, the rest
        waiting; what the gate needs requested within the credit; the ends
        already queued taken now (a targeted delivery of `_ended`); every
        result ready handed over (within `limit_ms`). Nothing here pumps."""
        if self._closed or self._paused:
            return {"hits": 0, "new": 0}
        new, withdrawn = self.core.drain_demands()
        self._take_withdrawn(withdrawn)
        hits = 0
        for sid, level, tx, ty in new:
            tile = (int(sid), int(level), int(tx), int(ty))
            source = self.sources.get(tile[0])
            if source is None or tile in self._by_tile:
                continue
            arr = peek(self._key(source, *tile[1:]))
            if arr is None:
                self._waiting[tile] = None
                continue
            self.core.complete_python_tile(tile[0], tile[1], tile[2], tile[3],
                                           np.ascontiguousarray(arr, dtype=np.float32).tobytes())
            self._waiting.pop(tile, None)
            self.counts["prefilled"] += 1
            hits += 1
        self._request_waiting()
        QtCore.QCoreApplication.sendPostedEvents(self, QtCore.QEvent.MetaCall)
        self._hand_over(limit_ms)
        self._request_waiting()                         # the credit just freed
        return {"hits": hits, "new": len(new)}

    def gate_pending(self) -> bool:
        """Is anything the gate needs still under way here -- requested and
        not ended, ended and not handed over, or waiting to be asked for?"""
        if self._ready or any(not r.ended for r in self._requests.values()):
            return True
        if self.gate_fn is None:
            return bool(self._waiting)
        for tile in self._waiting:
            gate = self.gate_fn(tile[0])
            if gate is None or tile[1:] in gate:
                return True
        return False

    def set_paused(self, paused: bool) -> None:
        """Step0 left / came back (§11.7)."""
        self._paused = bool(paused)
        if not self._paused and not self._closed:
            self.pump()

    # ── accounting (tests, resource record) ──────────────────────────
    def idle(self) -> bool:
        """Nothing waiting, requested or ready here, and nothing the core
        handed over still owed (measurement: a settled view)."""
        if self._closed:
            return True
        if self._waiting or self._ready or any(not r.ended for r in self._requests.values()):
            return False
        try:
            return int(self.core.python_stats()["handed"]) == 0
        except Exception:                               # noqa: BLE001 -- a core gone
            return True

    def credit(self):
        return self._tiles, self._bytes

    def stats(self) -> dict:
        d = dict(self.counts)
        d.update(waiting=len(self._waiting), ready=len(self._ready),
                 open_requests=sum(1 for r in self._requests.values() if not r.ended),
                 credit_tiles=self._tiles, credit_bytes=self._bytes,
                 sources=sorted(self.sources))
        return d

    def close(self) -> None:
        """Every source retired, every request cancelled, nothing held; the
        core, controller and callbacks let go. The object itself goes once
        the last cancelled request has ended (its token is then forgotten)."""
        if self._closed:
            return
        try:
            self.retire_all()
        finally:
            self._closed = True
            self._again.stop()
            self._waiting.clear()
            self._ready.clear()
            self.core = None                 # a retired core is not kept alive here
            self.controller = None
            self.on_progress = lambda: None
            self._maybe_dispose()

    def _maybe_dispose(self) -> None:
        """Closed and nothing still running: detached and deleted."""
        if not self._closed or any(not r.ended for r in self._requests.values()):
            return
        self._requests.clear()
        self._by_tile.clear()
        try:
            self.setParent(None)
            self.deleteLater()
        except RuntimeError:
            pass
