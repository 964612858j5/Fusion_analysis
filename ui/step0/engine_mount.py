"""Step0's full image drawn by the Rust viewer core (docs/v17_step0_engine_plan.md
§3.1, §10, §11). S0-A: Original and a saved whole-slide correction are drawn
by the engine, the nucleus (DAPI) overlay with them. S0-B: the live TopHat /
cuCIM preview too -- a Python source on the Original core, its tiles
computed by the Python correction code on demand (`python_source`).

The Python stack stays whole underneath: its `ExploreController` is still
the one camera, mouse and selection, and its layers are the picture whenever
the engine is not (a live method, a fallback). Everything the engine draws
is read from that controller and its overlay -- channel, method, params,
colour, mapping, the Marker and DAPI switches -- so there is no second copy
of the display state; `display_changed` on both says when to read again.

Sources (§10.3, §11.5): Original -> the slide's raw pyramid; a method whose
saved whole-slide product matches (`correction_plan.saved_product`, every one
of its checks) -> that product for the marker channel; a live method -> a
Python source registered on the channel's Original core. Original <-> live
and one live selection to another stay on that core: the new rows are
prepared (fetched, not composed) and published in one trial paint once they
cover the view (`Step0GlHost.request_switch`), the old picture following the
camera until then. A core's raw / saved product is fixed when it is built,
so another channel or a saved product is another core: the host prepares it as a candidate and publishes it only
once it covers the view (`Step0GlHost`); the core it replaces is paused as
the standby and resumed when its source is wanted again (§10.5.6); at most
two cores at a time.

Lifecycle (§10.7): `set_running(visible and not released)` from the tab;
`before_selection` from the tab before every `set_selection` (an engine ->
live switch turns the controller's own supply on FIRST, so the floor the
selection asks for is started, §10.5.4); `close` before the stack goes.
Failure (§10.6): every core goes, the Python picture takes over, and this
dataset is not tried again.
"""

import dataclasses
import logging
import time

from PyQt5 import QtCore, QtGui, QtWidgets

from ...core import resource_tiers as _tiers
from ...utils import perf_trace
from ...utils import tile_trace
from .. import step1_core_adapter as adapter

_log = logging.getLogger(__name__)

#: §10.5.5: two cores (current + candidate) share ONE viewer's tile-store
#: allowance, and split Step0's raw cache allowance
CORE_CPU_CACHE_BYTES = _tiers.STEP0_VIEWER_RAW_CACHE_BYTES // 2
CORE_CORRECTED_CACHE_BYTES = _tiers.STEP0_VIEWER_RAW_CACHE_BYTES // 2

ENGINE = "engine"
PYTHON = "python"

#: tests only: merged into every core's config (e.g. `test_read_delay_ms`)
TEST_CONFIG = {}
#: §14.5 2: a guard on the synchronous preparation before a hand-off (the
#: target is the 0.4 s of §15; this only keeps a stall from holding the GUI)
PREPARE_BUDGET_S = 1.5
PREPARE_POLL_S = 0.002


def engine_refusal() -> str:
    """"" when the shared switch (§3.2) asks for the engine and it can be
    imported; else why not ("python" when the switch says so)."""
    if adapter.requested_backend() != adapter.BACKEND_RUST:
        return "python"
    return adapter.core_unavailable_reason()


class Step0EngineMount(QtCore.QObject):
    """The engine on one Step0 full-image stack. `start()` then the tab's
    calls; `refused` says why the engine is not used (then nothing here
    acts again for this stack)."""

    def __init__(self, stack, parent=None):
        super().__init__(parent)
        self.stack = stack
        self.controller = stack.controller
        self.host = None
        self.refused = ""
        self._running = False
        self._closed = False
        self._raw = None
        self._raw_manifest = None
        self._index = {}
        self._rows_sent = {}
        self._display_queued = False
        self._scene_hold = None
        self._connected = False

    # ── what the tab and the tests read ──────────────────────────────
    @property
    def picture(self) -> str:
        """Who draws what is on screen now: `engine` or `python`."""
        host = self.host
        return ENGINE if host is not None and host.isVisible() and host.has_picture else PYTHON

    @property
    def running(self) -> bool:
        return self._running

    def shows_wanted(self) -> bool:
        """The published picture is exactly the controller's selection: its
        core, and -- for a live method -- its Python source, no switch
        still being prepared (measurement, tests)."""
        host = self.host
        if self.picture != ENGINE or host.current is None:
            return False
        slot = host.current
        if slot.identity != self._wanted_identity() or slot.switch is not None:
            return False
        selection = self._selection()
        if not self._is_live(selection):
            return slot.picture == self._index.get(selection[0])
        return (slot.adapter is not None and slot.picture is not None
                and slot.adapter.selection_of(slot.picture) == selection)

    # ── building ─────────────────────────────────────────────────────
    def start(self, view) -> bool:
        """Make the GL host on the view and initialize it without showing
        a frame. False (and `refused`) when the engine cannot be used."""
        from ..step1_core_binding import raw_provider_of
        from .gl_host import Step0GlHost
        try:
            raw = raw_provider_of(self.stack.provider)
            if raw is None:
                raise RuntimeError("the full image's provider is not a raw OME-TIFF pyramid")
            self._raw_manifest = adapter.build_raw_manifest(raw)
            self._raw = raw
            self._index = {name: i for i, name in enumerate(raw.channel_names)}
            host = Step0GlHost()
            host.attach(view)
            if host.width() <= 0 or host.height() <= 0:
                host.resize(1, 1)
            # initialized now (Qt makes the context lazily), then hidden in
            # the same turn: nothing of it reaches the screen before a
            # picture is published (§10.5.3)
            host.show()
            host.grabFramebuffer()
            host.hide()
            if not host.initialized:
                raise host.init_error or RuntimeError("the GL host did not initialize")
        except Exception as exc:                            # noqa: BLE001 -- reported, Python picture
            self.refused = f"{type(exc).__name__}: {exc}"
            _log.warning("Step0 engine not used: %s", self.refused)
            print(f"[explore] engine not used: {self.refused}", flush=True)
            if "host" in locals():
                self._dispose_host(host)
            return False
        host.view_rect = self._view_rect
        host.cannot_draw = self._cannot_draw
        host.still_wanted = lambda slot: slot.identity == self._wanted_identity()
        host.on_published = self._published
        host.on_failure = self.fail
        host.on_smooth_changed = self._push_display_all
        host.expected_channels = lambda: len(self._rows_for_display())
        host.on_switched = self._switched
        host.hidden = True                                  # until the tab says it runs
        self.host = host
        self._connect()
        return True

    def _connect(self) -> None:
        c = self.controller
        c.display_changed.connect(self._display_changed)
        c.interaction_event.connect(self._interaction_event)
        c.gesture_quiet.connect(self._gesture_quiet)
        gains = getattr(c, "gains_ready", None)
        if gains is not None:
            gains.connect(self._gains_ready)
        overlay = getattr(self.stack, "overlay", None)
        if overlay is not None:
            overlay.display_changed.connect(self._display_changed)
        self._connected = True

    def _disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        c = self.controller
        pairs = [(c.display_changed, self._display_changed),
                 (c.interaction_event, self._interaction_event),
                 (c.gesture_quiet, self._gesture_quiet)]
        if getattr(c, "gains_ready", None) is not None:
            pairs.append((c.gains_ready, self._gains_ready))
        overlay = getattr(self.stack, "overlay", None)
        if overlay is not None:
            pairs.append((overlay.display_changed, self._display_changed))
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    # ── the source (§10.3) ───────────────────────────────────────────
    def _saved(self, channel, method, params):
        if method is None or not params:
            return None
        from ...viewer import correction_plan
        source = self.stack.provider.source_identity()
        path = getattr(source, "dataset_path", None)
        if not path:
            return None
        return correction_plan.saved_product(path, channel, method, tuple(params),
                                             self.stack.provider.level_shape(0))

    def identity_for(self, channel, method, params):
        """Which CORE draws this selection: `(channel, method, params, saved
        token)` for a saved product, else the channel's Original core
        `(channel, None, (), None)` -- Original and every live method alike
        (S0-B). `None` only when the engine draws nothing of it."""
        if method is None:
            return (channel, None, (), None)
        saved = self._saved(channel, method, params)
        if saved is None:
            return (channel, None, (), None)
        return (channel, method, tuple(int(p) for p in params), saved.token)

    def _selection(self):
        c = self.controller
        method = c.method
        return (c.channel, method, () if method is None else tuple(int(p) for p in c.params))

    def _is_live(self, selection) -> bool:
        channel, method, params = selection
        return method is not None and self._saved(channel, method, params) is None

    def _wanted_identity(self):
        c = self.controller
        return self.identity_for(c.channel, c.method, tuple(c.params))

    # ── the tab's calls ──────────────────────────────────────────────
    def before_selection(self, channel, method, params) -> None:
        """Called BEFORE `set_selection`: a switch to the Python picture
        turns the controller's own supply on first (§10.5.4)."""
        if self.refused or self._closed or self.host is None:
            return
        if self.identity_for(channel, method, () if method is None else params) is None:
            self._to_python()

    def sync(self) -> None:
        """The selection may have moved: build, keep or drop cores."""
        if self.refused or self._closed or self.host is None or not self._running:
            return
        wanted = self._wanted_identity()
        if wanted is None:
            self._to_python()
            return
        if not self.controller.snapshot().overview_ready:
            return                    # no trustworthy range yet; the record's install calls again
        host = self.host
        current, candidate = host.current, host.candidate
        if current is not None and current.identity == wanted:
            host.drop_candidate()
            self._converge(current, shown=True)
            host.request_frame()
            return
        if candidate is not None and candidate.identity == wanted:
            self._converge(candidate, shown=False)
            host.request_frame()
            return
        self._build_candidate(wanted)

    def _build_candidate(self, identity) -> None:
        import fusion_viewer_core as fvc
        from ..step1_core_binding import core_config
        from ..step1_core_layer import notice_pair
        from ..step1_viewer_mount import demo_budgets
        channel, method, params, _token = identity
        host = self.host
        # the standby already shows it: resumed, no new core (§10.5.6)
        slot = host.take_standby(identity)
        if slot is not None:
            perf_trace.mark("step0.engine_standby", source=str(identity)[:120].replace(" ", "_"))
            self._converge(slot, shown=False)
            self._set_view(slot.core, self.controller.snapshot(), input_=False)
            host.request_frame()
            return
        # the old candidate -- and, with a current core, the standby -- go
        # BEFORE the new core exists: never three
        host.drop_candidate()
        if host.current is not None:
            host.drop_standby()
        sources = None
        if method is not None:
            saved = self._saved(channel, method, params)
            if saved is None:
                self._to_python()
                return
            raw = self._raw
            strides = [max(1, int(round(raw.level_downsample(k)))) for k in range(raw.num_levels)]
            try:
                sources = adapter.build_corrected_manifest(saved.table, raw, strides)
            except Exception as exc:                        # noqa: BLE001 -- e.g. the store went away
                self.fail(f"the saved result cannot be read by the engine: {type(exc).__name__}: {exc}")
                return
        budgets = demo_budgets()
        budgets = dataclasses.replace(budgets, max_raw_texture_bytes=int(budgets.max_raw_texture_bytes) // 2)
        config = core_config(budgets)
        config["cpu_cache_bytes"] = int(CORE_CPU_CACHE_BYTES)
        config["corrected_cache_bytes"] = int(CORE_CORRECTED_CACHE_BYTES)
        config.update(TEST_CONFIG)
        sockets = notice_pair()
        try:
            core = fvc.ViewerCore(self._raw_manifest, config, None, sockets[1].fileno(),
                                  bool(tile_trace.ON), sources)
        except Exception as exc:                            # noqa: BLE001
            for end in sockets:
                end.close()
            self.fail(f"the engine could not open this source: {type(exc).__name__}: {exc}")
            return
        try:
            slot = self.host.add_candidate(core, sockets, identity)
        except Exception as exc:                            # noqa: BLE001
            self.fail(f"the engine could not start drawing: {type(exc).__name__}: {exc}")
            return
        perf_trace.mark("step0.engine_candidate", source=str(identity)[:120].replace(" ", "_"))
        self._rows_sent.pop(id(slot.core), None)
        self._converge(slot, shown=False)
        self._set_view(slot.core, self.controller.snapshot(), input_=False)
        self.host.request_frame()

    def _published(self, slot) -> None:
        """A candidate is the picture now: the Python layers stop reading
        and are no longer composed under it (as Step1's mount)."""
        if self._closed or self.host is None or self.host.current is not slot:
            return
        self._set_controller_requests(False)
        self._set_engine_owns_view(True)
        self._hold_scene_repaint()
        if not self.host.isVisible():
            self.host.show()
            self.host.raise_()

    def _to_python(self) -> None:
        """The Python picture: its supply first, then the engine steps back
        -- the current core paused as the standby (§10.5.6)."""
        self._set_engine_owns_view(False)          # restores the floor (§11.6)
        self._set_controller_requests(True)
        self._release_scene_repaint()
        host = self.host
        if host is not None:
            host.hide()
            host.drop_candidate()
            host.park_current()

    def fail(self, why: str) -> None:
        """§10.6: the Python picture for the rest of this dataset."""
        if self.refused or self._closed:
            return
        self.refused = why
        _log.warning("Step0 engine hands over to the Python picture: %s", why)
        print(f"[explore] engine hands over to the Python picture: {why}", flush=True)
        self._to_python()
        if self.host is not None:
            self.host.drop_all()          # no standby after a failure

    def set_running(self, running: bool) -> None:
        """Visible and not released for a production run (§10.7)."""
        running = bool(running)
        if self.refused or self._closed or self.host is None or running == self._running:
            self._running = running if not self._closed else False
            return
        self._running = running
        host = self.host
        if not running:
            host.drop_candidate()
            host.hidden = True
            for slot in host.live_cores():
                slot.core.pause()
                if slot.adapter is not None:
                    slot.adapter.set_paused(True)
            return
        host.hidden = False
        host.reset_gate_clock()                      # §14.7: a held-back switch waits afresh
        from .. import gpu_memory
        gpu_memory.on_layer_active(host)
        for slot in host.live_cores():
            slot.core.resume()
            if slot.adapter is not None:
                slot.adapter.set_paused(False)
        self.sync()
        snapshot = self.controller.snapshot()
        for slot in host.live_cores():
            self._set_view(slot.core, snapshot, input_=False)
        host.request_frame()

    def release_for_production(self, keep_picture=False) -> bool:
        """A production run takes the GPU: stop, and give the tiles back --
        or, with `keep_picture` (the patch background correction only,
        §13-15), keep them: the wanted picture is first prepared and
        published if it can be (§14.5 2), and whatever is on screen then
        stays through the run. True when kept."""
        if keep_picture and self._can_keep():
            if self._prepare_switch():
                self.host.present_now()
            for slot in self.host.cores():           # current and standby (§13.7 3)
                if slot.adapter is not None:
                    slot.adapter.suspend_requests()
            self.set_running(False)                  # paused; the published picture is presented
            perf_trace.mark("step0.engine_kept")
            return True
        self.set_running(False)
        if self.host is not None:
            # §11.7: every Python source goes (its requests cancelled); the
            # cores keep their published picture and prepare it again after
            for slot in self.host.cores():
                self._retire_python(slot)
            self.host.release_to_coarse()
        return False

    def _can_keep(self) -> bool:
        """§14.5 3: the engine is the picture, healthy, on screen, and its
        current core is the selection's."""
        host = self.host
        ok = not (self.refused or self._closed or host is None or not self._running or host.hidden)
        if ok:
            slot = host.current
            ok = slot is not None and slot.identity == self._wanted_identity()
        if not ok:
            perf_trace.mark("step0.keep_fallback", why="not_running")
        return ok

    def _switch_done(self, slot) -> bool:
        """The selection's picture is the published one on `slot`."""
        if slot.switch is not None or getattr(slot, "pending", None) is not None or slot.picture is None:
            return False
        selection = self._selection()
        return not self._is_live(selection) or (
            slot.adapter is not None and slot.adapter.selection_of(slot.picture) == selection)

    def _prepare_switch(self) -> bool:
        """§14.5 2 / 15.5: before the hand-off, rounds of -- the core's
        demands (cache hits at once), what the quality gate needs computed,
        the results handed over, one paint (uploads, the trial) -- until the
        new picture is published, nothing more can be done, or
        `PREPARE_BUDGET_S` (a guard, not the target) has gone. The old
        picture stays on screen throughout. True when published."""
        host = self.host
        slot = host.current
        if self._switch_done(slot):
            return False                             # nothing to switch to
        adapter = slot.adapter
        deadline = time.monotonic() + PREPARE_BUDGET_S
        peek = self.stack.scheduler.peek_corrected
        if adapter is not None:
            adapter.set_sync(True)
        try:
            while True:
                news = 0
                if adapter is not None:
                    left_ms = max(1.0, (deadline - time.monotonic()) * 1000.0)
                    news = adapter.step_sync(peek, left_ms)["new"]
                if not host.paint_now(present=False):
                    return False
                if self._switch_done(slot):
                    perf_trace.mark("step0.switch_ready")
                    return True
                if time.monotonic() >= deadline:
                    perf_trace.mark("step0.keep_fallback", why="budget")
                    return False
                done = host.last_paint
                progress = (news or done.get("uploads") or done.get("deferred")
                            or done.get("deferred_growth") or done.get("reserving"))
                if not progress and (adapter is None or not adapter.gate_pending()):
                    perf_trace.mark("step0.keep_fallback", why="stalled")
                    return False
                time.sleep(PREPARE_POLL_S)           # the workers run meanwhile
        finally:
            if adapter is not None:
                adapter.set_sync(False)

    def _gate_for(self, slot, sid):
        """§15.6: the tiles a waiting switch (or candidate) of source `sid`
        needs first (at `Step0GlHost.gate_levels`) -- None when it does not
        wait or no level is known yet."""
        host = self.host
        if host is None:
            return None
        waits = ((slot is host.current and getattr(slot, "pending", None) == sid)
                 or (slot is host.candidate and slot.picture == sid))
        view = host.view_rect() if waits else None
        levels = host.gate_levels(slot) if view is not None else ()
        if not levels:
            return None
        rect = tuple(float(v) for v in view)
        return set().union(*(slot.core.gate_tiles(int(sid), rect, int(lv)) for lv in levels))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._disconnect()
        self._release_scene_repaint()
        host, self.host = self.host, None
        if host is not None:
            self._dispose_host(host)

    @staticmethod
    def _dispose_host(host) -> None:
        try:
            host.dispose()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            host.setParent(None)
            host.deleteLater()
        except RuntimeError:
            pass

    # ── S0-B: a core's picture (§11.5) ───────────────────────────────
    def _adapter(self, slot):
        if slot.adapter is None:
            from .python_source import PythonSourceAdapter
            slot.adapter = PythonSourceAdapter(
                slot.core, self.controller, self.stack.scheduler,
                on_progress=lambda: self.host is not None and self.host.request_frame(),
                parent=self)
            slot.adapter.gate_fn = lambda sid, _slot=slot: self._gate_for(_slot, sid)
        return slot.adapter

    def _marker_target(self, slot, selection):
        """The channel the marker is drawn from on `slot` for `selection`:
        the slide channel (Original, a saved product), or the Python source
        registered for this live selection (reused when it already is)."""
        channel, _method, _params = selection
        if not self._is_live(selection):
            return self._index.get(channel)
        adapter = self._adapter(slot)
        for sid in (slot.picture, getattr(slot, "pending", None)):
            if sid is not None and adapter.selection_of(sid) == selection:
                return sid
        pending = getattr(slot, "pending", None)
        if pending is not None and pending != slot.picture and adapter.selection_of(pending) is not None:
            adapter.retire(pending)              # a newer selection replaces the one being prepared
            slot.pending = None
        return adapter.register(*selection)

    def _converge(self, slot, *, shown) -> None:
        """Bring `slot` to the controller's selection. A core not on screen
        takes it at once; the current core prepares it and switches in one
        published trial paint (`Step0GlHost.request_switch`)."""
        selection = self._selection()
        try:
            target = self._marker_target(slot, selection)
        except ValueError as exc:                           # the core refused a source
            self.fail(f"the engine refused the live preview: {exc}")
            return
        if not shown:
            slot.picture, slot.pending, slot.switch = target, None, None
            self._push_display(slot)
            self._retire_python(slot, keep=(target,))
            return
        if target == slot.picture:
            if slot.switch is not None:          # back to the picture on screen: no switch
                slot.pending, slot.switch = None, None
                self._retire_python(slot, keep=(slot.picture,))
            self._push_display(slot)
            return
        if slot.switch is not None and getattr(slot, "pending", None) == target:
            self._push_display(slot)             # the same switch, new display parameters
            return
        self._switch_token = getattr(self, "_switch_token", 0) + 1
        slot.pending = target
        promote, yield_ = self._push_display(slot)
        self.host.request_switch(slot, promote, yield_, self._switch_token)

    def _switched(self, slot, token) -> None:
        """`Step0GlHost.on_switched`, inside the paint that published the
        trial: the prepared rows become the display; the old source goes."""
        if token != getattr(self, "_switch_token", None) or getattr(slot, "pending", None) is None:
            return
        slot.picture, slot.pending = slot.pending, None
        slot.shown_rows = None                   # the final rows, read fresh
        self._push_display(slot)
        self._retire_python(slot, keep=(slot.picture,))

    def _retire_python(self, slot, keep=()) -> None:
        """Every Python source of `slot` but `keep` goes (requests cancelled)."""
        adapter = slot.adapter
        if adapter is None:
            return
        retired = set()
        for sid in list(adapter.sources):
            if sid not in keep:
                adapter.retire(sid)
                retired.add(int(sid))
                self._rows_sent.pop(id(slot.core), None)     # the core dropped its rows
        if retired and getattr(slot, "shown_rows", None) is not None:
            # a retired number never goes back to the core (codex S0-B code 2, P2-1)
            slot.shown_rows = [r for r in slot.shown_rows if int(r[0]) not in retired]
        if slot.picture is not None and slot.picture not in keep and slot.picture >= 0x8000:
            slot.picture = None
        if getattr(slot, "pending", None) is not None and slot.pending not in keep and slot.pending >= 0x8000:
            slot.pending = None
            slot.switch = None

    def _gains_ready(self, _ctx, _gains) -> None:
        """A gain table for the live selection: the rows carry it (no pixel
        is sent again)."""
        if self.host is not None and not self.refused and not self._closed:
            self._push_display_all()

    # ── display rows (§10.4) ─────────────────────────────────────────
    def _marker_row(self, marker):
        c = self.controller
        if marker is None or not getattr(c, "_marker_visible", True) or c.channel not in self._index:
            return None
        lo, hi, gamma = (float(v) for v in c.display_mapping)
        r, g, b = (float(v) for v in (getattr(c, "_tint", None) or (1.0, 1.0, 1.0)))
        return (int(marker), lo, hi, gamma, r, g, b, 1.0, 0.0, 0.0)

    def _overlay_row(self):
        overlay = getattr(self.stack, "overlay", None)
        if (overlay is not None and overlay.effective_enabled and overlay.calibrated
                and overlay.channel in self._index):
            lo, hi, gamma = overlay.display_mapping
            r, g, b = (float(v) for v in (getattr(overlay, "_tint", None) or (1.0, 1.0, 1.0)))
            return (self._index[overlay.channel], float(lo), float(hi), float(gamma or 1.0),
                    r, g, b, 1.0, 0.0, 0.0)
        return None

    def _rows_for_display(self, marker="wanted"):
        """`(index, lo, hi, gamma, r, g, b, weight, group, group_weight)`
        for the marker (drawn from `marker`: a slide channel or a Python
        source; by default the wanted one) and the nucleus."""
        if marker == "wanted":
            selection = self._selection()
            marker = None if self._is_live(selection) else self._index.get(selection[0])
            slot = self.host.current if self.host is not None else None
            if marker is None and slot is not None:
                marker = getattr(slot, "pending", None) or slot.picture
            if marker is None and self._is_live(selection):
                marker = -1                      # a live marker, not registered yet: still a row
        rows = []
        row = self._marker_row(marker)
        if row is not None:
            rows.append(row)
        row = self._overlay_row()
        if row is not None:
            rows.append(row)
        return rows

    def _row_channels(self, marker):
        return [int(r[0]) for r in self._rows_for_display(marker)]

    def _gains_for(self, slot):
        """Per Python source on `slot`: its gain table -- the controller's
        when it is calibrated for that source's selection, else the last one
        it had (so the picture being replaced keeps its brightness while
        the new one is prepared; codex S0-B code review 1, P2-3)."""
        adapter = slot.adapter
        if adapter is None:
            return {}
        kept = getattr(slot, "gains", None)
        if kept is None:
            kept = slot.gains = {}
        current_selection = self._selection()
        getter = getattr(self.controller, "current_gains", None)
        for sid in list(adapter.sources):
            if getter is not None and adapter.selection_of(sid) == current_selection:
                ctx, gains = getter()
                if ctx is not None or int(sid) not in kept:
                    kept[int(sid)] = [float(g) for g in gains]
        for sid in [k for k in kept if k not in adapter.sources]:
            del kept[sid]
        return {sid: list(g) for sid, g in kept.items()}

    def _display_changed(self, *_args) -> None:
        if not self._display_queued:
            self._display_queued = True
            QtCore.QTimer.singleShot(0, self._display_now)

    def _display_now(self) -> None:
        self._display_queued = False
        if self.refused or self._closed or self.host is None:
            return
        self.sync()

    def _push_display_all(self) -> None:
        """The rows as they now read, to the core(s) drawing the wanted
        source -- never to a core still showing the previous one (it keeps
        its own picture until the new one is published)."""
        if self.host is None:
            return
        wanted = self._wanted_identity()
        for slot in self.host.live_cores():
            if slot.identity == wanted:
                self._push_display(slot)
        self.host.request_frame()

    def _push_display(self, slot):
        """`slot`'s rows. No switch: the picture's rows as they read now
        (and they are what the picture shows). A switch being prepared: the
        rows the old picture was published with, unchanged, plus every row
        the switch brings -- the new marker, a nucleus switched on meanwhile
        -- as rows being prepared (codex S0-B code review 1, P2-2). Returns
        `(promote, yield)` for the switch: the prepared rows, and the old
        rows the final picture no longer has."""
        pending = getattr(slot, "pending", None)
        switching = pending is not None and pending != slot.picture
        final = [r for r in self._rows_for_display(pending if switching else slot.picture) if int(r[0]) >= 0]
        promote, yield_ = [], []
        if not switching:
            rows, preparing = final, []
            slot.shown_rows = list(final)
        else:
            old = getattr(slot, "shown_rows", None)
            if old is None:
                old = [r for r in self._rows_for_display(slot.picture) if int(r[0]) >= 0]
                slot.shown_rows = list(old)
            old_ch = [int(r[0]) for r in old]
            final_ch = [int(r[0]) for r in final]
            rows = list(old) + [r for r in final if int(r[0]) not in old_ch]
            preparing = [c for c in final_ch if c not in old_ch]
            promote = list(preparing)
            yield_ = [c for c in old_ch if c not in final_ch]
            if slot.switch is not None:
                slot.switch = (tuple(promote), tuple(yield_), slot.switch[2])
        gains = self._gains_for(slot)
        key = (tuple(rows), tuple(preparing), tuple(sorted((k, tuple(v)) for k, v in gains.items())),
               bool(self.host.smooth))
        if self._rows_sent.get(id(slot.core)) != key:
            self._rows_sent[id(slot.core)] = key
            slot.core.set_display(rows, bool(self.host.smooth), False, preparing, gains)
        return promote, yield_

    def _cannot_draw(self, core) -> str:
        """§10.6: a displayed channel the core will never draw."""
        stats = core.stats()
        if int(stats.get("base_overflow_channels", 0) or 0) > 0:
            return "the coarse level of the drawn channels exceeds the GPU budget"
        unavailable = set(int(i) for i in stats.get("unavailable", ()) or ())
        names = {i: n for n, i in self._index.items()}
        for row in self._rows_for_display():
            if int(row[0]) in unavailable:
                return f"channel {names.get(int(row[0]), row[0])!r} cannot be drawn (coarse budget)"
        return ""

    # ── camera ───────────────────────────────────────────────────────
    def _view_rect(self):
        view_box = getattr(self.controller.view, "view_box", None)
        if view_box is None:
            return None
        (x0, x1), (y0, y1) = view_box.viewRange()
        return (float(x0), float(x1), float(y0), float(y1))

    def _interaction_event(self, _kind, snapshot) -> None:
        self._view_all(snapshot, input_=True)

    def _gesture_quiet(self, snapshot) -> None:
        self._view_all(snapshot, input_=False)

    def _view_all(self, snapshot, *, input_) -> None:
        if self.host is None or not self._running or self.refused:
            return
        for slot in self.host.live_cores():
            self._set_view(slot.core, snapshot, input_=input_)
        self.host.request_frame()

    @staticmethod
    def _set_view(core, snapshot, *, input_) -> None:
        bbox = getattr(snapshot, "bbox_l0", None)
        if bbox is None:
            return
        core.set_view(int(snapshot.level), tuple(int(v) for v in bbox), int(snapshot.epoch), bool(input_))

    # ── the Python layers under the engine (as Step1's mount) ────────
    def _set_controller_requests(self, enabled: bool) -> None:
        setter = getattr(self.controller, "set_viewport_requests_enabled", None)
        if setter is None:
            return
        try:
            setter(bool(enabled))
        except RuntimeError:                                # C++ side gone
            pass

    def _set_engine_owns_view(self, owns: bool) -> None:
        setter = getattr(self.controller, "set_engine_owns_view", None)
        if setter is None:
            return
        try:
            setter(bool(owns))
        except RuntimeError:                                # C++ side gone
            pass

    def _hold_scene_repaint(self) -> None:
        """While the engine is the picture the scene under it is not
        repainted on every camera move (block A9 §40, as Step1) -- nor at
        all (§15.5 3): the GL host covers the viewport, yet every window
        composition repainted the occluded Python image items (40-120 ms
        of `ImageItem.paint` on a parameter change). Their visibility and
        the floor's calibration are untouched; only the painting under the
        host stops; the margin around it still paints, without them
        (codex §15 code 1 P2-5, code 2 P2-2)."""
        graphics = getattr(self.controller.view, "graphics", None)
        if graphics is None or self._scene_hold is not None:
            return
        try:
            viewport = graphics.viewport()
            content = getattr(getattr(self.controller.view, "view_box", None), "childGroup", None)
            no_paint = _NoPaint(viewport, self.host, graphics, content)
            viewport.installEventFilter(no_paint)
            self._scene_hold = (graphics, graphics.viewportUpdateMode(), no_paint)
            graphics.setViewportUpdateMode(QtWidgets.QGraphicsView.NoViewportUpdate)
        except RuntimeError:
            self._scene_hold = None

    def _release_scene_repaint(self) -> None:
        hold, self._scene_hold = self._scene_hold, None
        if hold is None:
            return
        graphics, mode, no_paint = hold
        try:
            graphics.viewport().removeEventFilter(no_paint)
            no_paint.deleteLater()
            graphics.setViewportUpdateMode(mode)
            graphics.viewport().update()
        except RuntimeError:
            pass


class _NoPaint(QtCore.QObject):
    """Keeps the viewport from painting the image items under the GL host
    (which covers the ViewBox only): a paint wholly under it is swallowed;
    one reaching the margin around it (axes, background) is repainted
    there alone, with the ViewBox's content (`content`, clipped to the
    ViewBox, so nothing of it shows in the margin) made fully transparent
    for that paint, which Qt then skips (codex §15 code 1 P2-5, code 2 P2-2)."""

    def __init__(self, parent, host, graphics=None, content=None):
        super().__init__(parent)
        self._host = host
        self._graphics = graphics
        self._content = content

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() != QtCore.QEvent.Paint:
            return False
        host = self._host
        try:
            if host is None or not host.isVisible():
                return False
            region = event.region()
            rest = region.subtracted(QtGui.QRegion(host.geometry()))
            if rest == region:
                return self._paint_margin(event)
            if not rest.isEmpty():
                # the margin, in a paint of its own (an update inside this
                # paint would be cleared with it)
                QtCore.QTimer.singleShot(0, lambda: _update(obj, rest))
            return True
        except RuntimeError:                                # C++ side gone
            return False

    def _paint_margin(self, event) -> bool:
        """The paint, with the ViewBox's content left out; False (the paint
        goes on as is) when there is no view to paint it through."""
        graphics, content = self._graphics, self._content
        if graphics is None or content is None:
            return False
        opacity = content.opacity()
        content.setOpacity(0.0)          # NoViewportUpdate: no repaint asked for
        try:
            graphics.viewportEvent(event)
        finally:
            content.setOpacity(opacity)
        return True


def _update(widget, region) -> None:
    try:
        widget.update(region)
    except RuntimeError:                                    # C++ side gone
        pass
