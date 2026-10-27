"""Step0's full image drawn by the Rust viewer core (docs/v17_step0_engine_plan.md
§3.1, §10). S0-A: Original and a saved whole-slide correction are drawn by
the engine, the nucleus (DAPI) overlay with them; the live TopHat / cuCIM
preview stays on the Python picture.

The Python stack stays whole underneath: its `ExploreController` is still
the one camera, mouse and selection, and its layers are the picture whenever
the engine is not (a live method, a fallback). Everything the engine draws
is read from that controller and its overlay -- channel, method, params,
colour, mapping, the Marker and DAPI switches -- so there is no second copy
of the display state; `display_changed` on both says when to read again.

Sources (§10.3): Original -> the slide's raw pyramid; a method whose saved
whole-slide product matches (`correction_plan.saved_product`, every one of
its checks) -> that product for the marker channel; anything else -> the
Python picture. A source is fixed when a core is built, so another source
is another core: the host prepares it as a candidate and publishes it only
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

from PyQt5 import QtCore, QtWidgets

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
        host.hidden = True                                  # until the tab says it runs
        self.host = host
        self._connect()
        return True

    def _connect(self) -> None:
        c = self.controller
        c.display_changed.connect(self._display_changed)
        c.interaction_event.connect(self._interaction_event)
        c.gesture_quiet.connect(self._gesture_quiet)
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
        """What the engine would draw for this selection: `None` for the
        Python picture, else `(channel, method, params, saved token)`."""
        if method is None:
            return (channel, None, (), None)
        saved = self._saved(channel, method, params)
        if saved is None:
            return None
        return (channel, method, tuple(int(p) for p in params), saved.token)

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
            self._push_display_all()
            return
        if candidate is not None and candidate.identity == wanted:
            self._push_display_all()
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
            self._push_display(slot.core)
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
        self._push_display(slot.core)
        self._set_view(slot.core, self.controller.snapshot(), input_=False)
        self.host.request_frame()

    def _published(self, slot) -> None:
        """A candidate is the picture now: the Python layers stop reading
        and are no longer composed under it (as Step1's mount)."""
        if self._closed or self.host is None or self.host.current is not slot:
            return
        self._set_controller_requests(False)
        self._hold_scene_repaint()
        if not self.host.isVisible():
            self.host.show()
            self.host.raise_()

    def _to_python(self) -> None:
        """The Python picture: its supply first, then the engine steps back
        -- the current core paused as the standby (§10.5.6)."""
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
            return
        host.hidden = False
        from .. import gpu_memory
        gpu_memory.on_layer_active(host)
        for slot in host.live_cores():
            slot.core.resume()
        self.sync()
        snapshot = self.controller.snapshot()
        for slot in host.live_cores():
            self._set_view(slot.core, snapshot, input_=False)
        host.request_frame()

    def release_for_production(self) -> None:
        """A production run takes the GPU: stop, and give the tiles back."""
        self.set_running(False)
        if self.host is not None:
            self.host.release_to_coarse()

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

    # ── display rows (§10.4) ─────────────────────────────────────────
    def _rows_for_display(self):
        """`(index, lo, hi, gamma, r, g, b, weight, group, group_weight)`
        for the marker and the nucleus, read from the controller and its
        overlay as they stand."""
        rows = []
        c = self.controller
        # the user's Marker switch (`set_marker_visible`); no public getter
        if getattr(c, "_marker_visible", True) and c.channel in self._index:
            lo, hi, gamma = (float(v) for v in c.display_mapping)
            r, g, b = (float(v) for v in (getattr(c, "_tint", None) or (1.0, 1.0, 1.0)))
            rows.append((self._index[c.channel], lo, hi, gamma, r, g, b, 1.0, 0.0, 0.0))
        overlay = getattr(self.stack, "overlay", None)
        if (overlay is not None and overlay.effective_enabled and overlay.calibrated
                and overlay.channel in self._index):
            lo, hi, gamma = overlay.display_mapping
            r, g, b = (float(v) for v in (getattr(overlay, "_tint", None) or (1.0, 1.0, 1.0)))
            rows.append((self._index[overlay.channel], float(lo), float(hi), float(gamma or 1.0),
                         r, g, b, 1.0, 0.0, 0.0))
        return rows

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
                self._push_display(slot.core)
        self.host.request_frame()

    def _push_display(self, core) -> None:
        rows = self._rows_for_display()
        key = (tuple(rows), bool(self.host.smooth))
        if self._rows_sent.get(id(core)) == key:
            return
        self._rows_sent[id(core)] = key
        core.set_display(rows, bool(self.host.smooth), False)

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

    def _hold_scene_repaint(self) -> None:
        """While the engine is the picture the scene under it is not
        repainted on every camera move (block A9 §40, as Step1)."""
        graphics = getattr(self.controller.view, "graphics", None)
        if graphics is None or self._scene_hold is not None:
            return
        try:
            self._scene_hold = (graphics, graphics.viewportUpdateMode())
            graphics.setViewportUpdateMode(QtWidgets.QGraphicsView.NoViewportUpdate)
        except RuntimeError:
            self._scene_hold = None

    def _release_scene_repaint(self) -> None:
        hold, self._scene_hold = self._scene_hold, None
        if hold is None:
            return
        graphics, mode = hold
        try:
            graphics.setViewportUpdateMode(mode)
            graphics.viewport().update()
        except RuntimeError:
            pass
