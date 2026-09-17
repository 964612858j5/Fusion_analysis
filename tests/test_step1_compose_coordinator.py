"""Step1's multi-channel frame: planned per tile, served by the one scheduler.

Block C2 of `docs/step1_rework_plan.md`, on the 2026-09-17 architecture and
the review that followed it: the coordinator asks the ONE controller what is
visible, submits every missing tile through the SAME `TileScheduler`, composes
OFF the GUI thread, and publishes finished RGBA.

Gates here: tile-first interleaving; the two cache boundaries (moving a weight
re-composes and reads nothing) with the composition cache keyed on the FULL
tile identity and bounded by bytes; the composition running on a worker rather
than the GUI thread; generation cancellation in each of the four cases the
plan names; and a missing window making a partial picture, asking the shared
service once, and clearing its own notice.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.ui.step1_compose_coordinator import (  # noqa: E402
    ComposedTile, Step1ComposeCoordinator, tile_identity,
)
from block01.viewer import step1_compose as compose_core  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    RawKey, SourceIdentity, TileAddress, TileGridSpec,
)

GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
WINDOW = (0.0, 100.0, 1.0)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Cache:
    def __init__(self):
        self._items = {}

    def get(self, key):
        return self._items.get(key)

    def put(self, key, value):
        self._items[key] = value
        return value


class _Scheduler:
    """The real scheduler's contract, with its reads counted.

    `request(req, cb)` serves a cached tile synchronously and otherwise
    "reads" through the provider -- the two paths the coordinator has to tell
    apart -- and remembers the order it was asked in. `deliver` runs what was
    queued WHATEVER was cancelled, so an old generation's result can be
    injected: cancellation is a courtesy, the generation check is the
    guarantee.
    """

    def __init__(self, provider):
        self.provider = provider
        self.cache = _Cache()
        self.asked = []
        self.reads = 0
        self.cancelled = []
        self._deferred = []

    def _cache_for(self, _key):
        return self.cache

    def cancel_generation(self, generation):
        self.cancelled.append(generation)

    def request(self, req, callback):
        self.asked.append((req.key.channel, req.key.tile.tx, req.key.tile.ty,
                           req.priority))
        cached = self.cache.get(req.key)
        if cached is not None:
            callback(SimpleNamespace(request=req, pixels=cached, error=None))
            return
        self._deferred.append((req, callback))

    def deliver(self):
        """Run the reads that were queued, as a worker thread would."""
        queued, self._deferred = self._deferred, []
        for req, callback in queued:
            values, _io = self.provider.read_tile(req.key.channel,
                                                  req.key.tile)
            self.reads += 1
            self.cache.put(req.key, values)
            callback(SimpleNamespace(request=req,
                                     pixels=SimpleNamespace(handle=values),
                                     error=None))


class _Executor:
    """The compose worker, run when the test says so.

    It keeps the arithmetic off the caller's stack the way the real pool
    does, and it records the thread each job ran on.
    """

    def __init__(self):
        self.jobs = []
        self.threads = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))
        return None

    def run(self):
        jobs, self.jobs = self.jobs, []
        for fn, args, kwargs in jobs:
            self.threads.append(threading.current_thread())
            fn(*args, **kwargs)
        return len(jobs)


class _Provider:
    def __init__(self, path="/x/c2.ome.tif"):
        self._identity = SourceIdentity(dataset_path=path,
                                        dataset_fingerprint="1:1",
                                        stage="step1",
                                        corrected_artifact="token-1")

    def source_identity(self):
        return self._identity

    def rebind(self, token):
        self._identity = SourceIdentity(
            dataset_path=self._identity.dataset_path,
            dataset_fingerprint="1:1", stage="step1",
            corrected_artifact=token)

    def switch_dataset(self, path):
        self._identity = SourceIdentity(
            dataset_path=path, dataset_fingerprint="2:2", stage="step1",
            corrected_artifact="token-1")

    def read_tile(self, channel, tile):
        """Pixels that depend on the SOURCE as well as the coordinates -- a
        new product or a new dataset means different numbers here, which is
        what makes a stale composed frame visible."""
        base = {"CD3": 10.0, "CD8": 40.0, "DAPI": 70.0}.get(channel, 5.0)
        bump = 0.0 if self._identity.corrected_artifact == "token-1" else 25.0
        bump += 0.0 if self._identity.dataset_path == "/x/c2.ome.tif" else 7.0
        values = np.full((8, 8), base + bump + tile.tx + tile.ty, np.float32)
        return values, 0.0


class _Controller:
    def __init__(self, tiles, level=0):
        self.level = level
        self.grid = GRID
        self._visible_tiles = set(tiles)


class _Host:
    def __init__(self, tiles=((1, 1), (2, 1), (1, 2))):
        self.provider = _Provider()
        self.scheduler = _Scheduler(self.provider)
        self.controller = _Controller(tiles)
        self.stack = SimpleNamespace(provider=self.provider,
                                     scheduler=self.scheduler,
                                     controller=self.controller)


class _SeedPort:
    """The shared display service's one question, as this object asks it.

    The real service answers False when it has NOT started a pass -- the
    overview pixels are not in yet, and it goes and asks for them
    (`Block01DisplayServices.request_mapping_seed`). `answers` lets a test
    play that refusal and then the acceptance that follows it.
    """

    def __init__(self, answers=None):
        self.asked = []
        self.answers = list(answers or [])
        self.pending = False

    def request_mapping_seed(self, channel):
        self.asked.append(channel)
        if self.answers:
            return self.answers.pop(0)
        return True

    def mapping_seed_pending(self, channel=None):
        return self.pending


def _spec(mode=compose_core.MODE_OVERLAY, weights=None, colors=None,
          mappings=None, **extra):
    spec = {"mode": mode,
            "weights": weights or {"CD3": 1.0, "CD8": 0.5},
            "colors": colors or {"CD3": (0.0, 1.0, 0.0),
                                 "CD8": (1.0, 0.0, 0.0)},
            "mappings": mappings or {"CD3": WINDOW, "CD8": WINDOW}}
    spec.update(extra)
    return spec


def _coordinator(app, host=None, seed_port=None, **kwargs):
    host = host or _Host()
    executor = _Executor()
    coordinator = Step1ComposeCoordinator(host, seed_port=seed_port,
                                          executor=executor, **kwargs)
    coordinator.executor = executor
    return coordinator, host


def _settle(app, coordinator, rounds=8):
    """Let the queued deliveries land and run the compose workers.

    Two queues now: the reads the scheduler hands over (its callback runs on
    a read worker, so it only emits) and the compositions the workers finish.
    """
    for _ in range(rounds):
        app.processEvents()
        ran = coordinator.executor.run()
        app.processEvents()
        if not ran and not coordinator.executor.jobs:
            break


# ── 1. planning: one scheduler, tile-first ────────────────────────────

def test_every_tile_is_asked_for_through_the_scheduler(app):
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())

    assert host.scheduler.asked, "nothing was submitted"
    channels = {channel for channel, *_rest in host.scheduler.asked}
    assert channels == {"CD3", "CD8"}
    assert len(host.scheduler.asked) == 3 * 2


def test_the_requests_are_interleaved_by_tile_not_by_channel(app):
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())

    order = [(tx, ty) for _ch, tx, ty, _p in host.scheduler.asked]
    # Tile-first: each tile's channels are adjacent, so the tile only
    # changes every len(channels) requests.
    assert order[0] == order[1], f"channel-first ordering: {order}"
    assert order[2] == order[3]
    assert order[0] != order[2]


def test_the_centre_tile_is_asked_for_first(app):
    host = _Host(tiles=((5, 5), (1, 1), (5, 6)))
    coordinator, host = _coordinator(app, host)
    coordinator.compose_visible(_spec())

    first = host.scheduler.asked[0]
    assert (first[1], first[2]) != (1, 1), (
        "the far tile was asked for before the centre")


def test_the_coordinator_reads_no_pixels_itself(app):
    """Everything goes through the scheduler -- the GUI thread reads nothing."""
    import ast
    import inspect

    import block01.ui.step1_compose_coordinator as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("read_tile", "read_region"), (
                "the coordinator reads pixels itself")


def test_a_zero_weight_channel_is_not_even_asked_for(app):
    """C.3 gate 8, in the planner: what cannot contribute is not read."""
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec(weights={"CD3": 1.0, "CD8": 0.0}))

    assert {channel for channel, *_rest in host.scheduler.asked} == {"CD3"}


def test_a_zero_weight_group_and_nucleus_are_not_asked_for(app):
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec(
        mode=compose_core.MODE_FUSION,
        groups={"markers": {"CD3": 1.0}, "muted": {"CD8": 1.0}},
        group_weights={"markers": 1.0, "muted": 0.0}, nucleus=("DAPI", 0.0),
        mappings={"CD3": WINDOW, "CD8": WINDOW, "DAPI": WINDOW}))

    assert {channel for channel, *_rest in host.scheduler.asked} == {"CD3"}


# ── 2. composing when the tiles are in hand ───────────────────────────

def test_a_tile_is_composed_when_its_channels_have_landed(app):
    coordinator, host = _coordinator(app)
    frames = []
    coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append((level, tx, ty, rgba)))

    coordinator.compose_visible(_spec())
    assert frames == []                      # nothing is in the cache yet
    host.scheduler.deliver()
    _settle(app, coordinator)

    assert len(frames) == 3, frames
    level, tx, ty, rgba = frames[0]
    assert rgba.shape == (8, 8, 4)
    assert (rgba[..., 3] == 255).all()


def test_the_composed_pixels_are_the_compose_core_s(app):
    coordinator, host = _coordinator(app)
    frames = []
    coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append((tx, ty, rgba)))
    spec = _spec()

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)

    tx, ty, rgba = frames[0]
    tiles = {}
    for channel in ("CD3", "CD8"):
        values, _io = host.provider.read_tile(
            channel, TileAddress(grid=GRID, level=0, tx=tx, ty=ty))
        tiles[channel] = (values, np.ones(values.shape, bool))
    expected, _valid, _missing = compose_core.compose(
        spec["mode"], tiles, weights=spec["weights"], colors=spec["colors"],
        mappings=spec["mappings"])
    assert np.array_equal(rgba, expected)


# ── 3. the composition runs off the GUI thread ────────────────────────

def test_compose_core_never_runs_on_the_qt_gui_thread(app):
    """C2, on the review of 2026-09-17: a weight drag finds every visible
    tile already cached, so composing inline would stall the GUI thread. The
    REAL executor is used here -- this gate is about threads, not about the
    test double."""
    host = _Host()
    seen = []
    real = compose_core.compose

    def _spy(*args, **kwargs):
        seen.append(QtCore.QThread.currentThread())
        return real(*args, **kwargs)

    compose_core.compose = _spy
    coordinator = Step1ComposeCoordinator(host)        # its own thread pool
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))
    try:
        coordinator.compose_visible(_spec())
        host.scheduler.deliver()
        deadline = time.time() + 10.0
        while len(frames) < 3 and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        compose_core.compose = real
        coordinator.shutdown()

    assert len(frames) == 3, "the worker never delivered a frame"
    assert seen, "compose_core.compose was never called"
    gui = app.thread()
    assert all(thread is not gui for thread in seen), (
        "the composition ran on the Qt GUI thread")


def test_a_cache_hit_is_published_without_composing_again(app):
    """The GUI thread may look a frame up; it may not recompute one."""
    coordinator, host = _coordinator(app)
    spec = _spec()
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)

    calls = []
    real = compose_core.compose
    compose_core.compose = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        hits = coordinator.compose_visible(spec)
        assert coordinator.executor.jobs == [], "a cached frame was recomposed"
    finally:
        compose_core.compose = real

    assert hits == 3
    assert calls == []


# ── 4. the two caches ─────────────────────────────────────────────────

def test_moving_a_weight_recomposes_and_reads_nothing(app):
    """C.3 gate 3: the tile cache is not keyed on the composition."""
    coordinator, host = _coordinator(app)
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))

    coordinator.compose_visible(_spec())
    host.scheduler.deliver()
    _settle(app, coordinator)
    reads = host.scheduler.reads
    assert reads == 6
    frames.clear()

    coordinator.invalidate("weight")
    coordinator.compose_visible(_spec(weights={"CD3": 0.25, "CD8": 0.5}))
    _settle(app, coordinator)

    assert host.scheduler.reads == reads, "a weight change read from disk"
    assert len(frames) == 3, "the frame was not re-composed"


def test_a_colour_or_a_mode_change_reads_nothing_either(app):
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())
    host.scheduler.deliver()
    _settle(app, coordinator)
    reads = host.scheduler.reads

    coordinator.invalidate("colour")
    coordinator.compose_visible(_spec(colors={"CD3": (1.0, 1.0, 0.0),
                                              "CD8": (0.0, 0.0, 1.0)}))
    _settle(app, coordinator)
    assert host.scheduler.reads == reads

    coordinator.invalidate("mode")
    coordinator.compose_visible(_spec(
        mode=compose_core.MODE_FUSION,
        groups={"markers": {"CD3": 1.0, "CD8": 1.0}},
        group_weights={"markers": 1.0}, nucleus=("DAPI", 1.0),
        mappings={"CD3": WINDOW, "CD8": WINDOW, "DAPI": WINDOW}))
    # Fusion needs DAPI, which nobody has read yet: exactly three new asks.
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert host.scheduler.reads == reads + 3


def test_an_unchanged_frame_is_served_from_the_composition_cache(app):
    coordinator, host = _coordinator(app)
    spec = _spec()
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)

    composed = coordinator.compose_visible(spec)
    assert composed == 3, "the composed frames were not reused"
    assert host.scheduler.reads == 6


# ── 4b. the composition cache is keyed on the SOURCE ──────────────────

def _frame_at(app, coordinator, host, spec, tile=(1, 1)):
    frames = {}
    handle = coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.__setitem__((tx, ty), rgba))
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    coordinator.tile_composed.disconnect(handle)
    return frames.get(tile)


@pytest.mark.parametrize("move", ["product", "dataset"])
def test_a_new_source_cannot_hit_the_old_frame(app, move):
    """The composed frame is keyed on the FULL tile identity, so the same
    coordinates under another corrected product or another dataset compose
    again rather than showing the old slide's pixels."""
    coordinator, host = _coordinator(app)
    spec = _spec()
    first = _frame_at(app, coordinator, host, spec)
    assert first is not None

    if move == "product":
        host.provider.rebind("token-2")
    else:
        host.provider.switch_dataset("/x/other.ome.tif")
    coordinator.invalidate(move)
    second = _frame_at(app, coordinator, host, spec)

    assert second is not None, "the new source composed nothing"
    assert not np.array_equal(first, second), (
        "a stale composed frame was served for a new source")


def test_the_tile_identity_carries_the_whole_source(app):
    key = RawKey(source=SourceIdentity(dataset_path="/a", 
                                       dataset_fingerprint="f",
                                       stage="step1",
                                       corrected_artifact="tok"),
                 channel="CD3",
                 tile=TileAddress(grid=GRID, level=2, tx=3, ty=4))
    identity = tile_identity(key)

    for part in ("/a", "f", "step1", "tok", "CD3", "v1"):
        assert part in identity, f"{part} is missing from the tile identity"
    assert 512 in identity and 2 in identity and 3 in identity and 4 in identity


# ── 4c. the composition cache is bounded ──────────────────────────────

def test_the_composition_cache_is_bounded_and_evicts(app):
    """A whole-slide pan may not grow this cache without limit."""
    one_tile = ComposedTile(np.zeros((8, 8, 4), np.uint8),
                            np.ones((8, 8), bool), ()).nbytes
    coordinator, host = _coordinator(app, max_bytes=2 * one_tile)

    coordinator.compose_visible(_spec())          # three tiles
    host.scheduler.deliver()
    _settle(app, coordinator)

    stats = coordinator.cache_stats()
    assert stats["bytes"] <= 2 * one_tile, stats
    assert stats["items"] == 2, stats
    assert stats["evictions"] >= 1, "nothing was evicted at the ceiling"


def test_an_evicted_frame_is_composed_again_without_a_read(app):
    one_tile = ComposedTile(np.zeros((8, 8, 4), np.uint8),
                            np.ones((8, 8), bool), ()).nbytes
    coordinator, host = _coordinator(app, max_bytes=2 * one_tile)
    spec = _spec()
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    reads = host.scheduler.reads

    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))
    coordinator.compose_visible(spec)
    _settle(app, coordinator)

    assert host.scheduler.reads == reads, (
        "an evicted composition re-read its tiles")
    assert len(frames) == 3, "the evicted frames were not composed again"


def test_evicting_a_composition_leaves_the_tile_cache_alone(app):
    one_tile = ComposedTile(np.zeros((8, 8, 4), np.uint8),
                            np.ones((8, 8), bool), ()).nbytes
    coordinator, host = _coordinator(app, max_bytes=one_tile)
    coordinator.compose_visible(_spec())
    host.scheduler.deliver()
    _settle(app, coordinator)

    for tx, ty in ((1, 1), (2, 1), (1, 2)):
        key = RawKey(source=host.provider.source_identity(), channel="CD3",
                     tile=TileAddress(grid=GRID, level=0, tx=tx, ty=ty))
        assert host.scheduler.cache.get(key) is not None, (
            "a tile left the tile cache when a composition was evicted")


# ── 5. generations ────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["weights", "dataset", "source", "window"])
def test_each_kind_of_change_cancels_the_old_composition(app, reason):
    """C.3 gate 4: the four cases the plan names."""
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())
    before = coordinator.generation

    if reason == "source":
        host.provider.rebind("token-2")
    if reason == "dataset":
        host.provider.switch_dataset("/x/other.ome.tif")
    coordinator.invalidate(reason)

    assert coordinator.generation != before
    assert before in host.scheduler.cancelled, (
        "the old generation's queued reads were not cancelled")


@pytest.mark.parametrize("reason", ["weights", "dataset", "source", "window"])
def test_a_late_result_from_an_old_generation_composes_nothing(app, reason):
    """C.3 gate 4: an old generation's result is INJECTED, not merely dropped
    by a cancelled queue -- cancellation is a courtesy, the generation check
    is the guarantee. `_Scheduler.deliver` runs what was queued whatever was
    cancelled, so what these four cases exercise is the coordinator's gate."""
    coordinator, host = _coordinator(app)
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))

    coordinator.compose_visible(_spec())
    assert host.scheduler._deferred, "nothing was queued to arrive late"
    if reason == "dataset":
        host.provider.switch_dataset("/x/other.ome.tif")
    if reason == "source":
        host.provider.rebind("token-2")
    coordinator.invalidate(reason)
    frames.clear()

    host.scheduler.deliver()          # the old generation's reads land now
    _settle(app, coordinator)

    assert frames == [], "an old generation's composition reached the screen"


def test_a_worker_result_is_checked_again_when_it_lands(app):
    """The generation moved WHILE the worker was composing: the picture it
    made belongs to a frame nobody is looking at any more."""
    coordinator, host = _coordinator(app)
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))

    coordinator.compose_visible(_spec())
    host.scheduler.deliver()                     # the reads land...
    app.processEvents()                          # ...and are picked up here
    assert coordinator.executor.jobs, "nothing was handed to a worker"
    coordinator.invalidate("the user moved a weight")

    _settle(app, coordinator)                    # the workers finish now

    assert frames == [], "a result composed under the old draft reached the screen"


def test_a_late_tile_still_enters_the_tile_cache(app):
    """Pixels are pixels: only the composition is gated."""
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())
    coordinator.invalidate("weights")
    host.scheduler.deliver()
    _settle(app, coordinator)

    key = RawKey(source=host.provider.source_identity(), channel="CD3",
                 tile=TileAddress(grid=GRID, level=0, tx=1, ty=1))
    assert host.scheduler.cache.get(key) is not None

    reads = host.scheduler.reads
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))
    coordinator.compose_visible(_spec(weights={"CD3": 0.4, "CD8": 0.5}))
    _settle(app, coordinator)
    assert host.scheduler.reads == reads, "the cached tiles were read again"
    assert len(frames) == 3


# ── 6. a missing window ───────────────────────────────────────────────

def test_a_missing_window_names_the_channel_and_composes_the_rest(app):
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)

    assert named and named[-1] == ["CD8"], named
    assert len(frames) == 3, "the channels that had windows composed nothing"


def test_a_missing_window_asks_the_shared_service_once(app):
    """The window is computed by the shared service, and one outstanding
    request is one computation."""
    port = _SeedPort()
    coordinator, host = _coordinator(app, seed_port=port)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    # The same frame again, and then a NEW GENERATION of it: a weight drag
    # while a seed is being computed must not start that computation again.
    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    _settle(app, coordinator)
    coordinator.invalidate("weight")
    coordinator.compose_visible(_spec(weights={"CD3": 0.3, "CD8": 0.5},
                                      mappings={"CD3": WINDOW}))
    _settle(app, coordinator)

    assert port.asked == ["CD8"], port.asked


def test_the_arriving_window_starts_a_new_generation_and_recomposes(app):
    port = _SeedPort()
    coordinator, host = _coordinator(app, seed_port=port)
    frames = []
    coordinator.tile_composed.connect(lambda *a: frames.append(a[:3]))
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    before = coordinator.generation
    frames.clear()

    # The service answered: the host hands the window over and says so.
    coordinator._last_spec["mappings"] = {"CD3": WINDOW, "CD8": WINDOW}
    coordinator.window_arrived("CD8")
    host.scheduler.deliver()
    _settle(app, coordinator)

    assert coordinator.generation != before, "the window did not start a frame"
    assert len(frames) == 3, "the frame was not composed with the new window"
    assert named[-1] == [], "the missing notice was not cleared"


def test_the_missing_set_belongs_to_the_generation(app):
    """A NEW SOURCE missing the same channel name must say so again -- the
    notice is not a memory of what some earlier frame lacked."""
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)
    spec = _spec(mappings={"CD3": WINDOW})

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"]

    host.provider.switch_dataset("/x/other.ome.tif")
    coordinator.invalidate("dataset")
    named.clear()
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)

    assert named and named[-1] == ["CD8"], (
        "the new dataset's missing window was never announced")


# ── 7. C2.1: the thread the scheduler calls back on ───────────────────

def test_a_read_delivered_on_a_worker_thread_is_handled_on_this_one(app):
    """`TileScheduler` delivers from the thread that did the reading
    (`viewer/scheduler.py`), so the callback may not touch the caches, the
    in-flight table or the controller. It hands the result over, and the work
    happens on this object's thread."""
    coordinator, host = _coordinator(app)
    threads = []
    real = coordinator._start_tile
    coordinator._start_tile = lambda *a, **k: (
        threads.append(QtCore.QThread.currentThread()), real(*a, **k))[1]

    coordinator.compose_visible(_spec())
    threads.clear()
    queued = list(host.scheduler._deferred)
    host.scheduler._deferred = []

    def _deliver_from_a_worker():
        for req, callback in queued:
            values, _io = host.provider.read_tile(req.key.channel,
                                                  req.key.tile)
            host.scheduler.reads += 1
            host.scheduler.cache.put(req.key, values)
            callback(SimpleNamespace(request=req,
                                     pixels=SimpleNamespace(handle=values),
                                     error=None))

    worker = threading.Thread(target=_deliver_from_a_worker)
    worker.start()
    worker.join()
    assert threads == [], "the read worker went straight into the coordinator"

    _settle(app, coordinator)
    assert threads, "the delivered reads were never picked up"
    mine = coordinator.thread()
    assert all(thread is mine for thread in threads), (
        "the coordinator's state was touched from the read worker")


# ── 8. C2.1: an old worker may not retire a new job ───────────────────

def test_an_old_generation_s_result_does_not_retire_the_new_job(app):
    """The SAME key can be in flight twice: an old generation's worker is
    still composing it when a new generation submits it again. The old
    result must retire its own job and nothing else."""
    coordinator, host = _coordinator(app)
    spec = _spec()
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    app.processEvents()
    assert len(coordinator.executor.jobs) == 3
    old_jobs = coordinator.executor.jobs
    coordinator.executor.jobs = []

    # A window arrived, say: a new generation, the same draft, the same tiles.
    coordinator.invalidate("window")
    coordinator.compose_visible(spec)
    new_inflight = dict(coordinator._inflight)
    assert len(new_inflight) == 3, new_inflight
    assert len(coordinator.executor.jobs) == 3

    # Now the OLD workers finish.
    coordinator.executor.jobs = old_jobs + coordinator.executor.jobs
    first = coordinator.executor.jobs[:3]
    coordinator.executor.jobs = coordinator.executor.jobs[3:]
    for fn, args, kwargs in first:
        fn(*args, **kwargs)
    app.processEvents()

    assert coordinator._inflight == new_inflight, (
        "an old generation's result retired the new generation's job")


def test_a_tile_in_flight_is_not_submitted_twice(app):
    coordinator, host = _coordinator(app)
    coordinator.compose_visible(_spec())
    host.scheduler.deliver()
    app.processEvents()
    submitted = len(coordinator.executor.jobs)

    coordinator.compose_visible(_spec())          # the same frame again
    app.processEvents()

    assert len(coordinator.executor.jobs) == submitted, (
        "a composition already in flight was submitted again")


# ── 9. C2.1: a refused seed is asked for again ────────────────────────

def test_a_refused_seed_is_asked_for_again_when_the_pixels_arrive(app):
    """The service answers False while the overview pixels are still being
    read. That is not an answer, and the channel would otherwise never get a
    window."""
    port = _SeedPort(answers=[False, True])
    coordinator, host = _coordinator(app, seed_port=port)
    spec = _spec(mappings={"CD3": WINDOW})

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert port.asked == ["CD8"], port.asked

    coordinator.compose_visible(spec)             # the next frame retries
    _settle(app, coordinator)
    assert port.asked == ["CD8", "CD8"], port.asked

    coordinator.compose_visible(spec)             # now it is outstanding
    _settle(app, coordinator)
    assert port.asked == ["CD8", "CD8"], "an outstanding seed was asked again"


def test_a_seed_already_pending_counts_as_outstanding(app):
    """A refusal from a service that is already computing it is not a reason
    to ask a third party for the same number."""
    port = _SeedPort(answers=[False])
    port.pending = True
    coordinator, host = _coordinator(app, seed_port=port)
    spec = _spec(mappings={"CD3": WINDOW})

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    coordinator.compose_visible(spec)
    _settle(app, coordinator)

    assert port.asked == ["CD8"], port.asked


def test_a_new_source_asks_for_the_same_channel_again(app):
    """Another dataset's CD8 is another array with another window."""
    port = _SeedPort()
    coordinator, host = _coordinator(app, seed_port=port)
    spec = _spec(mappings={"CD3": WINDOW})

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert port.asked == ["CD8"]

    host.provider.switch_dataset("/x/other.ome.tif")
    coordinator.invalidate("dataset")
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)

    assert port.asked == ["CD8", "CD8"], (
        "the new dataset reused the old dataset's outstanding seed")


# ── 10. C2.1: a frame with nothing in it still owns its notice ────────

def test_winding_every_weight_to_zero_clears_the_missing_notice(app):
    """A draft that composes nothing has no missing windows -- and the notice
    from the frame before it is about a picture nobody is looking at."""
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"]

    coordinator.invalidate("weights")
    coordinator.compose_visible(_spec(weights={"CD3": 0.0, "CD8": 0.0},
                                      mappings={"CD3": WINDOW}))
    _settle(app, coordinator)

    assert named[-1] == [], "the missing notice outlived the draft that made it"


def test_a_frame_with_no_visible_tiles_clears_it_too(app):
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"]

    host.controller._visible_tiles = set()
    coordinator.invalidate("the camera moved off the slide")
    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    _settle(app, coordinator)

    assert named[-1] == []


# ── 11. C2.1: the worker's draft is the draft it was planned with ─────

def test_the_worker_holds_its_own_copy_of_the_draft(app):
    """C3 binds a draft that is edited in place; a job planned a moment ago
    must not compose with weights the user has since moved."""
    coordinator, host = _coordinator(app)
    weights = {"CD3": 1.0, "CD8": 0.5}
    mappings = {"CD3": WINDOW, "CD8": WINDOW}
    spec = _spec(weights=weights, mappings=mappings)
    frames = []
    coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append(rgba))

    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    app.processEvents()
    weights["CD8"] = 0.0            # the user moves it while a worker waits
    mappings.pop("CD8")
    _settle(app, coordinator)

    tiles = {}
    for channel in ("CD3", "CD8"):
        values, _io = host.provider.read_tile(
            channel, TileAddress(grid=GRID, level=0, tx=1, ty=1))
        tiles[channel] = (values, np.ones(values.shape, bool))
    expected, _valid, _missing = compose_core.compose(
        compose_core.MODE_OVERLAY, tiles, weights={"CD3": 1.0, "CD8": 0.5},
        colors=spec["colors"], mappings={"CD3": WINDOW, "CD8": WINDOW})
    assert any(np.array_equal(rgba, expected) for rgba in frames), (
        "the worker composed with a draft that moved under it")


def test_the_tile_identity_carries_the_grids_chunk_shape(app):
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(1, 256, 256),
                        grid_version="v1")
    key = RawKey(source=SourceIdentity(dataset_path="/a",
                                       dataset_fingerprint="f",
                                       stage="step1",
                                       corrected_artifact="tok"),
                 channel="CD3",
                 tile=TileAddress(grid=grid, level=0, tx=0, ty=0))
    other = RawKey(source=key.source, channel="CD3",
                   tile=TileAddress(grid=GRID, level=0, tx=0, ty=0))

    assert (1, 256, 256) in tile_identity(key)
    assert tile_identity(key) != tile_identity(other)


# ── 12. C2.2: the notice goes down without an invalidate ──────────────

def test_an_empty_viewport_clears_the_notice_without_an_invalidate(app):
    """NO `invalidate()` here. The camera moved off the slide inside the
    SAME generation, and the notice about a picture that is no longer on
    screen has to go with it -- merging an empty set into {"CD8"} would
    leave "CD8" standing."""
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"], named

    host.controller._visible_tiles = set()
    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    _settle(app, coordinator)

    assert named[-1] == [], named


def test_winding_the_weights_to_zero_clears_it_without_an_invalidate(app):
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"], named

    coordinator.compose_visible(_spec(weights={"CD3": 0.0, "CD8": 0.0},
                                      mappings={"CD3": WINDOW}))
    _settle(app, coordinator)

    assert named[-1] == [], named


def test_a_window_that_arrives_stops_the_channel_being_named(app):
    """The same class of clearing, one step further in: the frame composes,
    nothing is missing any more, and the notice comes down -- again without
    an `invalidate()` of the test's own."""
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    coordinator.compose_visible(_spec(mappings={"CD3": WINDOW}))
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named[-1] == ["CD8"]

    coordinator.compose_visible(_spec())          # both windows now
    _settle(app, coordinator)

    assert named[-1] == [], named


def test_a_frame_that_is_still_missing_a_window_does_not_flicker(app):
    """Clearing the set per frame may not announce an empty list the frame
    is about to contradict."""
    coordinator, host = _coordinator(app)
    named = []
    coordinator.windows_missing.connect(named.append)

    spec = _spec(mappings={"CD3": WINDOW})
    coordinator.compose_visible(spec)
    host.scheduler.deliver()
    _settle(app, coordinator)
    assert named == [["CD8"]], named

    coordinator.compose_visible(spec)
    _settle(app, coordinator)

    assert named == [["CD8"]], f"the notice flickered: {named}"
