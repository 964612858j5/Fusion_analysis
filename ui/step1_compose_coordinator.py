"""Step1's multi-channel composition, planned per tile and served by one scheduler.

Block C2 of `docs/step1_rework_plan.md`, on the architecture ruled 2026-09-17
and the review that followed it.

WHAT THIS IS. The one `ExploreController` owns the camera; this coordinator
asks it what is visible, works out which channels the current draft needs, and
submits every missing tile through THE SAME `TileScheduler` the viewer already
uses. Nothing here reads pixels on the GUI thread: a tile is either already in
the shared cache or it is requested, and the composition runs when a tile's
channels are all in hand.

TILE-FIRST, NOT CHANNEL-FIRST. The requests are interleaved by tile -- the
centre tile's CD3, CD8, DAPI, then the next tile's -- so the first complete
picture arrives as early as possible. Asking for every tile of CD3 and then
every tile of CD8 would finish one channel first and show nothing until the
last one landed.

ONE THREAD OWNS THIS OBJECT'S STATE. The scheduler delivers a tile from the
read worker that read it, so the callback it is given does nothing but hand
the result over a queued signal; the cache lookups, the composition cache,
the in-flight table and what has been announced are touched on this object's
own thread, and nowhere else.

NOTHING IS COMPOSED ON THE GUI THREAD. A tile whose channels are in hand is
handed to a compose worker; the GUI thread only plans, looks the frame up in
the composition cache, and publishes finished RGBA. This is not a refinement
for block D: dragging a weight finds every tile of the screen already in the
tile cache, so composing them inline would put a whole screen of CPU work on
the GUI thread -- exactly the stall this viewer exists to avoid. Everything
mutable here (the composition cache, what has been announced, what is in
flight, the missing set) is touched on the GUI thread only; the worker is
handed arrays and gives back pixels.

WHAT IS CACHED WHERE. The tile cache is the scheduler's, keyed by source,
channel, level and coordinates -- no weight, no colour, no mode, no window.
The composition cache is this object's: an LRU BOUNDED BY BYTES, keyed by the
FULL identity of the tiles that went into it (dataset, fingerprint, stage,
corrected artifact, channel, grid, level, coordinates) plus everything the
composition adds. So moving a weight re-composes from tiles already in hand
and reads nothing; a new dataset or a new corrected product cannot hit
another slide's frame at the same coordinates; and panning a whole slide
cannot grow this cache without limit -- an evicted frame is composed again
from the tile cache, still without a read.

EVERY COMPOSITION CARRIES A JOB TOKEN. The same tile, same draft and same
coordinates can be in flight under an old generation while a new one submits
it again; a result retires only the job registered under its own token, so
the old worker cannot clear the new one's place and have it composed twice.

GENERATIONS. Composition has its own namespaced token,
`("step1-compose", source, n)`. It moves -- and the old one is cancelled --
when the draft changes (weights, colours, mode, ticks), when the dataset
changes, when the handoff/source identity changes, and when a window that was
missing arrives. A tile that lands late still enters the tile cache, because
pixels are pixels; what may not happen is an old generation's COMPOSITION
reaching the screen, and that is checked again when the worker's result
arrives back on the GUI thread.

MISSING WINDOWS. A channel with no display window is not composed and not
guessed at: the channels that do have windows make a partial picture, the
channel is named, and a seed is asked of the SHARED service through the port
this object is given. OUTSTANDING IS WHAT THE SERVICE SAYS: it answers False
when it has not started a pass -- usually because the overview pixels are not
in yet, which it goes and asks for -- and that leaves the question open for
the next pass, so a refusal is never mistaken for an answer on the way. What
is outstanding is `(source, channel)`, not a bare name: after a dataset or a
corrected product changes, the same channel is a different array with a
different window and has to be asked for again, while a weight drag reuses
the computation already in flight. When the window arrives the caller says
so, and a new generation composes the same draft again.

The missing set belongs to the generation: a new generation announces its
own, the empty list included, so a stale notice is cleared rather than left
up -- and that includes a frame with nothing to compose at all, because a
draft whose weights have all been wound to zero has no missing windows
either.
"""

from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PyQt5 import QtCore

from ..viewer import step1_compose as compose_core
from ..viewer.caches import LRUByteCache
from ..viewer.tile_types import RawKey, TileAddress, TileRequest

#: What the composition cache may hold. One 512 tile is 1 MiB of RGBA plus
#: 256 KiB of validity, so this is a few hundred composed tiles -- a screen
#: and its neighbours, bounded for a whole-slide pan.
DEFAULT_COMPOSE_CACHE_BYTES = 256 * 1024 * 1024


class ComposedTile:
    """One composed frame, sized so the byte-bounded LRU can hold it."""

    __slots__ = ("rgba", "valid", "missing")

    def __init__(self, rgba, valid, missing):
        self.rgba = rgba
        self.valid = valid
        self.missing = tuple(missing or ())

    @property
    def nbytes(self):
        total = 0 if self.rgba is None else int(np.asarray(self.rgba).nbytes)
        if self.valid is not None:
            total += int(np.asarray(self.valid).nbytes)
        return total


def tile_identity(key):
    """The FULL identity of one underlying tile, as a sortable tuple.

    Everything that decides what those pixels MEAN: the dataset and its
    fingerprint, the stage and the corrected artifact (which carries the ROI
    and the decisions), the channel, the grid, the level and the coordinates.
    A composed frame is keyed on these, so the same coordinates under another
    dataset, another ROI or another product cannot hit it.
    """
    source = key.source
    grid = key.tile.grid
    return (str(getattr(source, "dataset_path", "")),
            str(getattr(source, "dataset_fingerprint", "")),
            str(getattr(source, "stage", "")),
            str(getattr(source, "corrected_artifact", "") or ""),
            str(key.channel),
            str(getattr(grid, "grid_version", "")),
            int(getattr(grid, "tile_size", 0) or 0),
            tuple(int(n) for n in (getattr(grid, "source_chunk_shape", ())
                                   or ())),
            int(key.tile.level), int(key.tile.tx), int(key.tile.ty))


class Step1ComposeCoordinator(QtCore.QObject):
    """Plans the tiles one composed frame needs, and composes them off the GUI.

    Owned by the host. It never builds a controller, a scheduler or a tile
    cache: it is handed the ones that exist. What it does own is the
    composition cache and the compose workers.
    """

    #: A composed tile is ready: `(level, tx, ty, rgba, valid)`.
    tile_composed = QtCore.pyqtSignal(int, int, int, object, object)
    #: The channels the CURRENT generation cannot compose for want of a
    #: window. Empty when there are none left, which clears the notice.
    windows_missing = QtCore.pyqtSignal(object)
    #: Private: a worker finished. Queued, so the payload lands on this
    #: object's thread and is checked against the live generation there.
    _frame_ready = QtCore.pyqtSignal(object)
    #: Private: a read landed. THE SCHEDULER CALLS BACK ON ITS READ WORKER
    #: (`viewer/scheduler.py`), so the callback does nothing but hand the
    #: result over this queued signal; everything after it runs here.
    _tile_landed = QtCore.pyqtSignal(object)

    def __init__(self, host, seed_port=None, executor=None,
                 max_bytes=DEFAULT_COMPOSE_CACHE_BYTES, parent=None):
        super().__init__(parent)
        self._host = host
        #: WHERE A MISSING WINDOW IS ASKED FOR: the shared display service,
        #: injected. This object does not reach for a singleton, and a host
        #: that hands it none simply names the channels it cannot compose.
        self._seed_port = seed_port
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="step1-compose")
        self._generation = 0
        self._compose_cache = LRUByteCache(int(max_bytes))
        #: The token the live frame was planned under, kept so a change can
        #: cancel THAT one -- the property below moves with the source, and a
        #: rebind would otherwise cancel a token nothing was issued under.
        self._issued = None
        #: Tiles already announced for the live frame; a tile becomes
        #: composable when its last channel lands, and its other channels'
        #: callbacks would otherwise announce it again. GUI thread only.
        self._emitted = set()
        #: Compositions handed to a worker and not yet back, `{cache_key:
        #: token}`. A token, not a bare membership: the same key can be
        #: composed again under a NEW generation while the old worker is
        #: still running, and the old result must not retire the new job.
        #: This object's thread only.
        self._inflight = {}
        #: The missing windows of the LIVE generation, and what was last
        #: announced for it (`None` = nothing announced yet, so the first
        #: answer is always published, the empty list included).
        self._missing = set()
        self._announced_missing = None
        #: Seeds asked for and not yet answered, as `(source, channel)`.
        #: The SOURCE is part of it: another dataset's CD3 is another
        #: question, and the answer to the old one cannot stand in for it.
        self._seed_requested = set()
        #: The draft the live frame was planned from, so a window arriving
        #: can compose exactly that frame again.
        self._last_spec = None
        self._frame_ready.connect(self._on_frame_ready,
                                  QtCore.Qt.QueuedConnection)
        self._tile_landed.connect(self._on_tile_landed,
                                  QtCore.Qt.QueuedConnection)
        #: Every submitted composition gets a token, so a result can only
        #: retire the job that is actually registered under its key.
        self._job = 0

    # ── what the frame is made of ─────────────────────────────────────
    @property
    def generation(self):
        stack = self._host.stack
        source = (stack.provider.source_identity() if stack is not None
                  else None)
        return ("step1-compose", source, self._generation)

    def invalidate(self, reason=""):
        """The draft, the source or a window moved: the old frame is void.

        The queued reads are cancelled by generation; anything already read
        stays in the tile cache, because a tile's pixels do not depend on any
        of this. The missing set goes with the generation that found it: a
        new dataset missing the same channel name must say so again, and a
        generation with nothing missing says that too.
        """
        stack = self._host.stack
        stale = self._issued if self._issued is not None else self.generation
        if stack is not None:
            stack.scheduler.cancel_generation(stale)
        self._generation += 1
        self._emitted.clear()
        self._inflight.clear()
        self._missing.clear()
        self._announced_missing = None
        self._issued = None
        return self.generation

    def forget_compositions(self):
        """Drop composed frames. Tiles are untouched."""
        self._compose_cache.clear()

    def cache_stats(self):
        """The composition cache's own numbers: bytes, items, evictions."""
        return self._compose_cache.stats()

    def shutdown(self):
        """Stop the compose workers. Idempotent."""
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=True)
        self._executor = None

    # ── planning ──────────────────────────────────────────────────────
    def visible_tiles(self):
        """The controller's own visible set, centre first.

        The camera has one owner; this asks it rather than forming a second
        opinion of what is on screen.
        """
        stack = self._host.stack
        if stack is None:
            return 0, []
        controller = stack.controller
        level = controller.level
        tiles = list(getattr(controller, "_visible_tiles", ()) or ())
        if not tiles:
            return level, []
        cx = sum(tx for tx, _ty in tiles) / float(len(tiles))
        cy = sum(ty for _tx, ty in tiles) / float(len(tiles))
        tiles.sort(key=lambda t: (t[0] - cx) ** 2 + (t[1] - cy) ** 2)
        return level, tiles

    def channels_for(self, spec):
        """Which channels this draft needs, in a stable order.

        THE COMPOSITION'S OWN ANSWER (`viewer.step1_compose`), not a second
        reading of the draft: a channel at weight 0, a channel inside a group
        at weight 0 and a nucleus at weight 0 contribute no pixels, so they
        are neither read nor mapped -- while the draft goes on saying they
        take part, at the weight the user gave them.
        """
        if spec.get("mode") == compose_core.MODE_FUSION:
            wanted = compose_core.fusion_channels(
                spec.get("groups"), spec.get("group_weights"),
                spec.get("nucleus") or ("", 0.0))
        else:
            wanted = compose_core.overlay_channels(spec.get("weights"))
        return sorted(wanted)

    def _keys(self, level, tx, ty, channels):
        """The underlying tile keys one composed tile is made of."""
        stack = self._host.stack
        source = stack.provider.source_identity()
        address = TileAddress(grid=stack.controller.grid, level=level,
                              tx=tx, ty=ty)
        return [RawKey(source=source, channel=channel, tile=address)
                for channel in channels]

    def _requests(self, level, tiles, channels, generation):
        """Tile-first interleaving: one tile's channels, then the next tile's."""
        plan = []
        for priority, (tx, ty) in enumerate(tiles):
            for key in self._keys(level, tx, ty, channels):
                plan.append(TileRequest(key=key, generation=generation,
                                        priority=priority))
        return plan

    # ── the frame ─────────────────────────────────────────────────────
    def compose_visible(self, spec):
        """Ask for what the visible tiles need, and start what is in hand.

        Returns the number of tiles served straight from the composition
        cache. Everything else is composed off the GUI thread and arrives
        through `tile_composed`.
        """
        stack = self._host.stack
        if stack is None:
            return 0
        level, tiles = self.visible_tiles()
        channels = self.channels_for(spec)
        self._last_spec = self._snapshot(spec)
        if not tiles or not channels:
            # NOTHING TO COMPOSE IS STILL AN ANSWER. A frame with no visible
            # tiles, or one whose every weight has been wound to 0, has no
            # missing windows -- and a notice left over from the frame before
            # it would be a claim about a picture nobody is looking at. This
            # CLEARS; `_note_missing` merges, and merging nothing into
            # {"CD8"} leaves {"CD8"} standing.
            self._clear_missing()
            return 0

        # A SEED THE SERVICE COULD NOT START YET is asked for again here --
        # once per pass, not once per tile. The usual reason for a refusal is
        # that the overview pixels are still being read; the frame after they
        # land is the one that gets the window.
        for channel in sorted(self._missing):
            self._request_seed(channel)

        generation = self.generation
        # A NEW PASS announces its tiles again -- the same generation asked
        # twice is a repaint, not a duplicate. Within the pass the set stops
        # a tile from being announced once per channel that lands.
        self._issued = generation
        self._emitted.clear()
        # THE MISSING SET BELONGS TO THE FRAME, not to the last frame that
        # had one. It is emptied here and filled again as this frame's tiles
        # publish, so a channel that has since got a window, or left the
        # draft, stops being named. Emptying it announces nothing by itself:
        # the announcement follows the first tile, and a set that comes back
        # the same is not a change.
        self._missing.clear()
        for request in self._requests(level, tiles, channels, generation):
            stack.scheduler.request(
                request,
                lambda result, gen=generation: self._on_tile(result, gen, spec))
        hits = 0
        for tx, ty in tiles:
            if self._start_tile(level, tx, ty, channels, spec, generation):
                hits += 1
        return hits

    @staticmethod
    def _snapshot(spec):
        """A draft a worker can hold while the user goes on editing.

        `dict(spec)` is a shallow copy: the weight, colour, window and group
        dictionaries inside it stay the LIVE ones, and the draft that C3
        binds is edited in place. What a worker composes has to be the draft
        as it was when the frame was planned, so every level is copied.
        """
        out = dict(spec or {})
        for field in ("weights", "colors", "mappings", "group_weights"):
            value = out.get(field)
            if isinstance(value, dict):
                out[field] = dict(value)
        groups = out.get("groups")
        if isinstance(groups, dict):
            out["groups"] = {name: (dict(channels)
                                    if isinstance(channels, dict)
                                    else channels)
                             for name, channels in groups.items()}
        nucleus = out.get("nucleus")
        if isinstance(nucleus, (list, tuple)):
            out["nucleus"] = tuple(nucleus)
        return out

    def _tile_pixels(self, key):
        """A tile from the shared cache, or None when it is not in yet."""
        stack = self._host.stack
        cache = stack.scheduler._cache_for(key) if stack is not None else None
        entry = None if cache is None else cache.get(key)
        if entry is None:
            return None
        return getattr(entry, "handle", entry)

    def _start_tile(self, level, tx, ty, channels, spec, generation):
        """Publish one tile from the composition cache, or hand it to a worker.

        Returns True only for a composition-cache hit. What happens on this
        thread is the lookup, the bookkeeping and the hand-off -- never the
        arithmetic.
        """
        stack = self._host.stack
        if stack is None or generation != self.generation:
            return False
        keys = self._keys(level, tx, ty, channels)
        tiles = {}
        for key in keys:
            pixels = self._tile_pixels(key)
            if pixels is None:
                return False
            tiles[key.channel] = pixels

        cache_key = self._composition_key(keys, spec)
        composed = self._compose_cache.get(cache_key)
        if composed is not None:
            self._publish(generation, level, tx, ty, composed)
            return True
        if cache_key in self._inflight:
            # Another channel's callback, or this pass's own sweep, already
            # handed this tile over.
            return False
        self._job += 1
        token = (generation, self._job)
        self._inflight[cache_key] = token
        self._executor.submit(self._compose_in_worker, {
            "generation": generation, "token": token, "level": level,
            "tx": tx, "ty": ty, "cache_key": cache_key,
            "spec": self._snapshot(spec),
            "tiles": {channel: np.asarray(values, np.float32)
                      for channel, values in tiles.items()}})
        return False

    def _composition_key(self, keys, spec):
        """What makes this composed tile what it is: the FULL tile identities
        plus everything the composition adds."""
        return compose_core.composition_key(
            spec.get("mode"), [tile_identity(key) for key in keys],
            weights=spec.get("weights"), colors=spec.get("colors"),
            mappings=spec.get("mappings"), groups=spec.get("groups"),
            group_weights=spec.get("group_weights"),
            nucleus=spec.get("nucleus") or ("", 0.0))

    # ── the worker ────────────────────────────────────────────────────
    def _compose_in_worker(self, payload):
        """THE ARITHMETIC, off the GUI thread.

        It is handed arrays and a draft, and touches nothing of this
        object's: the result goes back through a queued signal, and the GUI
        thread decides whether it is still wanted.
        """
        spec = payload["spec"]
        try:
            tiles = {channel: (values, ~np.isnan(values))
                     for channel, values in payload["tiles"].items()}
            rgba, valid, missing = compose_core.compose(
                spec.get("mode"), tiles, weights=spec.get("weights"),
                colors=spec.get("colors"), mappings=spec.get("mappings"),
                groups=spec.get("groups"),
                group_weights=spec.get("group_weights"),
                nucleus=spec.get("nucleus") or ("", 0.0))
            payload["composed"] = ComposedTile(rgba, valid, missing)
        except Exception as exc:                            # pragma: no cover
            payload["error"] = exc
        finally:
            payload.pop("tiles", None)
            self._frame_ready.emit(payload)

    def _on_frame_ready(self, payload):
        """A worker's result, back on the GUI thread."""
        generation = payload.get("generation")
        cache_key = payload.get("cache_key")
        # RETIRE ONLY THIS JOB. An old generation's worker can finish after a
        # new one has submitted the SAME key -- same tiles, same draft, new
        # generation -- and clearing the key on the strength of the coordinates
        # alone would let that new job be submitted a second time and composed
        # twice over.
        if self._inflight.get(cache_key) == payload.get("token"):
            del self._inflight[cache_key]
        if payload.get("error") is not None:
            return
        if generation != self.generation:
            return
        composed = payload["composed"]
        self._compose_cache.put(cache_key, composed)
        self._publish(generation, payload["level"], payload["tx"],
                      payload["ty"], composed)

    # ── publishing ────────────────────────────────────────────────────
    def _publish(self, generation, level, tx, ty, composed):
        """Announce one composed tile, once per frame. GUI thread only."""
        if generation != self.generation:
            return False
        self._note_missing(composed.missing)
        if composed.rgba is None:
            return False
        if (level, tx, ty) in self._emitted:
            return False
        self._emitted.add((level, tx, ty))
        self.tile_composed.emit(level, tx, ty, composed.rgba, composed.valid)
        return True

    def _note_missing(self, channels):
        """MERGE one tile's missing windows into the frame's.

        One frame is composed tile by tile, and each tile answers for itself,
        so this only ever adds. Emptying the set is a different act, with a
        different method, because merging nothing into `{"CD8"}` leaves
        `{"CD8"}` -- which is how a notice outlived the frame that made it.
        """
        before = set(self._missing)
        self._missing.update(str(channel) for channel in channels or ())
        for channel in sorted(self._missing - before):
            self._request_seed(channel)
        self._announce_missing()

    def _clear_missing(self):
        """This frame has no missing windows. Say so."""
        self._missing.clear()
        self._announce_missing()

    def _announce_missing(self):
        """Publish the frame's missing set when it is not what was published.

        The empty list is an announcement like any other: it is what takes a
        stale notice down.
        """
        current = tuple(sorted(self._missing))
        if current != self._announced_missing:
            self._announced_missing = current
            self.windows_missing.emit(list(current))

    def _seed_key(self, channel):
        """What a seed request is FOR: this source, this channel.

        A channel name alone is not the question. After a dataset or a
        corrected product changes, CD3 is a different array with a different
        window, and an answer outstanding for the old one must not stand in
        for it.
        """
        stack = self._host.stack
        source = (stack.provider.source_identity() if stack is not None
                  else None)
        return (source, str(channel))

    def _request_seed(self, channel):
        """Ask the SHARED service for this channel's window -- once.

        A window is computed by the display service (B.3), not here, and a
        second ask while one is outstanding would be a second computation of
        the same answer. OUTSTANDING IS WHAT THE SERVICE SAYS IT IS: it
        answers False when it has not started one -- typically because the
        overview pixels are not in yet, which it goes and asks for -- and a
        False must leave the question open, or the channel would never get a
        window at all. A pass that finds it already pending counts as
        outstanding too, so a weight drag reuses the computation in flight.
        """
        key = self._seed_key(channel)
        if key in self._seed_requested:
            return False
        port = self._seed_port
        request = getattr(port, "request_mapping_seed", None)
        if request is None:
            return False
        outstanding = bool(request(channel))
        if not outstanding:
            pending = getattr(port, "mapping_seed_pending", None)
            outstanding = bool(pending(channel)) if pending is not None else False
        if outstanding:
            self._seed_requested.add(key)
        return outstanding

    def window_arrived(self, channel=None):
        """A window this frame was waiting for has landed.

        The frame drawn without it is void: a new generation starts and the
        same draft is composed again, now with that channel in it.
        """
        if channel is not None:
            name = str(channel)
            self._seed_requested = {key for key in self._seed_requested
                                    if key[1] != name}
        self.invalidate("window")
        if self._last_spec is not None:
            self.compose_visible(self._last_spec)
        return self.generation

    # ── the scheduler's callback ──────────────────────────────────────
    def _on_tile(self, result, generation, spec):
        """A read landed -- ON THE SCHEDULER'S READ WORKER.

        `TileScheduler` delivers from the thread that did the reading, so
        this is the one method here that runs off this object's thread, and
        it does nothing but hand the result over. The cache lookups, the
        composition cache, `_inflight` and `_emitted` all wait for the
        queued slot below.
        """
        self._tile_landed.emit({"result": result, "generation": generation,
                                "spec": spec})

    def _on_tile_landed(self, payload):
        """The read, now on this object's thread.

        Its pixels are kept whatever generation asked for them; only the
        COMPOSITION is gated.
        """
        result = payload["result"]
        generation = payload["generation"]
        spec = payload["spec"]
        request = getattr(result, "request", None)
        key = getattr(request, "key", None)
        if key is None or getattr(result, "error", None) is not None:
            return
        if generation != self.generation:
            return
        self._start_tile(key.tile.level, key.tile.tx, key.tile.ty,
                         self.channels_for(spec), spec, generation)
