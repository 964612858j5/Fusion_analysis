"""Base images for the montage: read, compose, cache (plan block D, 7.6).

The picture of a patch is the whole-slide viewer's picture, made the same
way and ONLY BY CALLING what it already has:

  * the spec: `step1_draft_spec.build_spec` (the caller reads it on the GUI
    thread and hands it over frozen);
  * the channels a spec composes from: `step1_compose.overlay_channels` /
    `fusion_channels`;
  * the pixels: the Step1 provider's `read_region(channel, level, ...)` --
    original / corrected as decided, NaN outside the ROI; it does not go
    through the viewer's scheduler, so the viewer's caches are not touched;
  * the arithmetic: `step1_compose.compose`, pure numpy.

What this module OWNS (authorised for block D, 2026-09-24): two byte-bounded
LRU caches and two worker threads (the second authorised 2026-09-25), the
newest request first. `finished(generation)` says a request is all on
screen, so the page can send the next one at once -- a frame at a time,
never a queue of stale ones (the Intensity drag of 2026-09-25).

    channels   (patch_bbox, level, channel, pixel_key)     -> float32   1 GiB
    composed   (patch_bbox, level, stride, spec_hash)      -> RGBA u8   256 MiB

COMPOSED AT THE SCREEN'S RESOLUTION (user report 2026-09-24: ticking a
channel and moving Intensity stuttered). Pyramid levels are far apart (4x),
so the level the view picks can hold several times the pixels the screen
shows; the channel block is kept at its level and taken every `stride`-th
pixel before composing -- measured, composing a 1024 px patch of 4 channels
costs ~100 ms and a 2048 px one ~400 ms. The patches on screen first.

Moving Intensity, a colour, a tick or the mode re-composes from the channel
cache without reading the disk. `close()` stops the thread and empties both
caches; nothing is reported after it.
"""

import collections
import threading
import time

import numpy as np
from PyQt5 import QtCore

from ...viewer import step1_compose as compose_core
from ...core import config_hash

CHANNEL_CACHE_BYTES = 1 << 30
COMPOSED_CACHE_BYTES = 256 << 20
WORKERS = 2          # authorised: two worker threads (user, 2026-09-25)


class ByteLRU:
    """An LRU bounded by the bytes of its numpy values."""

    def __init__(self, budget):
        self.budget = int(budget)
        self._d = collections.OrderedDict()
        self.nbytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value, size):
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.nbytes -= old[1]
            if size > self.budget:
                return
            self._d[key] = (value, size)
            self.nbytes += size
            while self.nbytes > self.budget and self._d:
                _, (_, s) = self._d.popitem(last=False)
                self.nbytes -= s

    def value(self, key):
        v = self.get(key)
        return None if v is None else v[0]

    def drop(self, predicate):
        with self._lock:
            for k in [k for k in self._d if predicate(k)]:
                self.nbytes -= self._d.pop(k)[1]

    def clear(self):
        with self._lock:
            self._d.clear()
            self.nbytes = 0

    def __len__(self):
        return len(self._d)


def spec_hash(spec):
    return config_hash.config_hash(spec)


def spec_channels(spec):
    if spec.get("mode") == compose_core.MODE_FUSION:
        return compose_core.fusion_channels(spec.get("groups"), spec.get("group_weights"),
                                            spec.get("nucleus") or ("", 0.0))
    return compose_core.overlay_channels(spec.get("weights"))


def level_rect(bbox, ds_yx):
    """A level-0 box in level-k pixels, per axis (the viewer's geometry)."""
    y0, y1, x0, x1 = bbox
    dsy, dsx = ds_yx
    return (int(y0 / dsy), max(int(y0 / dsy) + 1, int(y1 / dsy)),
            int(x0 / dsx), max(int(x0 / dsx) + 1, int(x1 / dsx)))


class MontageSupply(QtCore.QObject):
    """Composes patch images off the GUI thread.

    `composed(bbox, level, rgba, generation)` -- an RGBA uint8 image of the
    patch at `level`; `missing(channels)` -- channels the spec names that
    have no display window yet (the page asks the shared seed service).
    """

    composed = QtCore.pyqtSignal(object, int, object, int)
    missing = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal(int)                # generation, every patch reported
    plane_ready = QtCore.pyqtSignal(int)             # plane generation: one more plane in
    outlines_ready = QtCore.pyqtSignal(object)       # key of a result mask's outlines

    def __init__(self, provider, pixel_key="", parent=None,
                 channel_budget=CHANNEL_CACHE_BYTES, composed_budget=COMPOSED_CACHE_BYTES):
        super().__init__(parent)
        self.provider = provider
        self.pixel_key = str(pixel_key or "")
        self.channels = ByteLRU(channel_budget)
        self.composites = ByteLRU(composed_budget)
        self._pending = collections.OrderedDict()   # bbox -> request, in order of need
        self._plane_pending = collections.OrderedDict()   # identity -> PlaneSpec (GPU path)
        self._outline_pending = collections.OrderedDict()  # key -> mask path (step 2)
        self.outlines = {}                                 # key -> (polygons, count, median d)
        self.plane_generation = 0
        self._cv = threading.Condition()
        self._closed = False
        self.generation = 0
        self.reads = 0                               # disk reads, for tests and the gate
        self.composes = 0
        self._stats = None
        self._stats_lock = threading.Lock()          # two workers count into it
        self._threads = [threading.Thread(target=self._loop, name=f"montage-supply-{i}",
                                          daemon=True) for i in range(WORKERS)]
        for t in self._threads:
            t.start()

    # ── requests (GUI thread) ────────────────────────────────────────
    def request(self, bboxes, level, spec, stride=1, log=True):
        """Compose `bboxes` (in order of need: those on screen first) at
        `level`, every `stride`-th pixel, with `spec`; supersedes every
        request not yet started. Returns the generation results will carry."""
        spec = dict(spec or {})
        with self._cv:
            if self._closed:
                return self.generation
            self.generation += 1
            gen = self.generation
            self._pending.clear()
            self._stats = {"gen": gen, "n": len(bboxes), "done": 0, "reads": 0, "read_ms": 0.0,
                           "compose_ms": 0.0, "hits": 0, "t0": time.perf_counter(),
                           "level": int(level), "stride": max(1, int(stride)), "log": bool(log)}
            h = spec_hash(spec)
            for bbox in bboxes:
                bbox = tuple(int(v) for v in bbox)
                self._pending[bbox] = {"bbox": bbox, "level": int(level), "spec": spec,
                                       "stride": max(1, int(stride)), "spec_hash": h, "gen": gen}
            self._cv.notify_all()
        return gen

    # ── raw planes for the GPU layer (block D, 2026-09-25) ───────────
    def request_planes(self, specs):
        """Read the planes in `specs` that are not in the channel cache yet,
        in order; supersedes the planes not yet started. Each arrival says
        `plane_ready(generation)`."""
        with self._cv:
            if self._closed:
                return self.plane_generation
            self.plane_generation += 1
            self._plane_pending.clear()
            for spec in specs:
                if self.channels.value(self._plane_key(spec)) is None:
                    self._plane_pending[spec.read_key] = spec
            self._cv.notify_all()
            return self.plane_generation

    # ── outlines of result masks (block D step 2) ─────────────────────
    def request_outlines(self, key, path):
        """Extract the outlines of the mask at `path` once; `outlines_ready(key)`."""
        with self._cv:
            if self._closed or key in self.outlines or key in self._outline_pending:
                return False
            self._outline_pending[key] = path
            self._cv.notify_all()
            return True

    def forget_outlines(self):
        """A new run: its outlines are other results."""
        with self._cv:
            self._outline_pending.clear()
            self.outlines = {}

    def plane(self, spec):
        """The values of a plane if it has arrived, else None."""
        return self.channels.value(self._plane_key(spec))

    @staticmethod
    def _plane_key(spec):
        return ("plane",) + tuple(spec.read_key)

    def forget_patches(self, keep):
        """Drop what belongs to patches not in `keep` (a patch unticked)."""
        keep = {tuple(int(v) for v in b) for b in keep}
        # compose entries start with the bbox; plane entries carry it third
        self.channels.drop(lambda k: (k[2] if k[0] == "plane" else k[0]) not in keep)
        self.composites.drop(lambda k: k[0] not in keep)

    def forget_composites(self):
        """Channels / Intensity / mode moved: the channel blocks still hold."""
        self.composites.clear()

    def set_pixel_key(self, key):
        """Other pixels (a dataset, a corrected product, the ROI): empty all."""
        key = str(key or "")
        if key != self.pixel_key:
            self.pixel_key = key
            self.channels.clear()
            self.composites.clear()

    def close(self, timeout=5.0):
        with self._cv:
            self._closed = True
            self._pending.clear()
            self._plane_pending.clear()
            self._outline_pending.clear()
            self.outlines = {}
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout)
        self.channels.clear()
        self.composites.clear()

    def is_alive(self):
        return any(t.is_alive() for t in self._threads)

    # ── the worker ───────────────────────────────────────────────────
    def _loop(self):
        while True:
            with self._cv:
                while (not self._pending and not self._plane_pending
                       and not self._outline_pending and not self._closed):
                    self._cv.wait()
                if self._closed:
                    return
                outline = None
                if self._plane_pending:              # the picture first
                    _, spec = self._plane_pending.popitem(last=False)
                    gen = self.plane_generation
                    req = None
                elif self._outline_pending:          # then the outlines
                    outline = self._outline_pending.popitem(last=False)
                    req = None
                else:
                    _, req = self._pending.popitem(last=False)
            if outline is not None:
                self._extract_outlines(*outline)
                continue
            if req is None:
                self._read_plane(spec, gen)
                continue
            try:
                out = self._compose(req)
            except Exception as exc:  # noqa: BLE001 -- one patch, reported, not fatal
                print(f"[Montage] compose failed for {req['bbox']}: {exc}")
                continue
            with self._cv:
                if self._closed or req["gen"] != self.generation:
                    continue                      # superseded: never reported
            if out is not None:
                rgba, missing = out
                if missing:
                    self.missing.emit(list(missing))
                self.composed.emit(req["bbox"], req["level"], rgba, req["gen"])
            self._note_done(req["gen"])

    def _extract_outlines(self, key, path):
        from . import mask_layers
        try:
            result = mask_layers.outlines(np.load(path))
        except Exception as exc:  # noqa: BLE001 -- one mask, said, not fatal
            print(f"[Montage] could not outline {path}: {exc}")
            result = ([], 0, 0.0)
        with self._cv:
            if self._closed:
                return
            self.outlines[key] = result
        self.outlines_ready.emit(key)

    def _read_plane(self, spec, gen):
        key = self._plane_key(spec)
        if self.channels.value(key) is None:
            try:
                y0, y1, x0, x1 = spec.rect
                values, _origin = self.provider.read_region(spec.channel, spec.level, y0, y1, x0, x1)
            except Exception as exc:  # noqa: BLE001 -- one plane, said, not fatal
                print(f"[Montage] could not read {spec.channel} level {spec.level}: {exc}")
                return
            values = np.ascontiguousarray(values, np.float32)
            self.reads += 1
            self.channels.put(key, values, values.nbytes)
        with self._cv:
            if self._closed:
                return
        self.plane_ready.emit(gen)

    def _note_done(self, gen):
        """One line per finished request -- where the time went (for the
        real-machine report of 2026-09-24)."""
        with self._stats_lock:
            st = self._stats
            if st is None or st["gen"] != gen:
                return
            st["done"] += 1
            last = st["done"] == st["n"]
        if last:
            if not self._closed:
                self.finished.emit(gen)
            total = (time.perf_counter() - st["t0"]) * 1000
            if not st["log"]:
                return
            print(f"[Montage] {st['n']} patches level={st['level']} stride={st['stride']}: "
                  f"{st['reads']} reads {st['read_ms']:.0f} ms, compose {st['compose_ms']:.0f} ms, "
                  f"{st['hits']} from cache, total {total:.0f} ms")

    def _add(self, gen, key, value):
        with self._stats_lock:
            st = self._stats
            if st is not None and st["gen"] == gen:
                st[key] += value

    def _compose(self, req):
        bbox, level, spec, stride = req["bbox"], req["level"], req["spec"], req["stride"]
        ckey = (bbox, level, stride, req["spec_hash"])
        hit = self.composites.value(ckey)
        if hit is not None:
            self._add(req["gen"], "hits", 1)
            return hit, ()
        ds = self.provider.level_downsample_yx(level)
        y0, y1, x0, x1 = level_rect(bbox, ds)
        tiles = {}
        for ch in sorted(spec_channels(spec)):
            key = (bbox, level, ch, self.pixel_key)
            values = self.channels.value(key)
            if values is None:
                if self._closed:
                    return None
                t = time.perf_counter()
                values, _origin = self.provider.read_region(ch, level, y0, y1, x0, x1)
                values = np.asarray(values, np.float32)
                self._add(req["gen"], "read_ms", (time.perf_counter() - t) * 1000)
                self._add(req["gen"], "reads", 1)
                self.reads += 1
                self.channels.put(key, values, values.nbytes)
            if stride > 1:
                values = np.ascontiguousarray(values[::stride, ::stride])
            tiles[ch] = (values, ~np.isnan(values))
        self.composes += 1
        t = time.perf_counter()
        rgba, _valid, missing = compose_core.compose(
            spec.get("mode"), tiles, weights=spec.get("weights"), colors=spec.get("colors"),
            mappings=spec.get("mappings"), groups=spec.get("groups"),
            group_weights=spec.get("group_weights"), nucleus=spec.get("nucleus") or ("", 0.0))
        self._add(req["gen"], "compose_ms", (time.perf_counter() - t) * 1000)
        if rgba is None:
            rgba = np.zeros((-(-(y1 - y0) // stride), -(-(x1 - x0) // stride), 4), np.uint8)
            rgba[..., 3] = 255
        rgba = np.ascontiguousarray(rgba)
        if not missing:
            self.composites.put(ckey, rgba, rgba.nbytes)
        return rgba, tuple(missing)
