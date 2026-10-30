"""Step0's one GL container for the Rust viewer core (docs/v17_step0_engine_plan.md
§3.4, §10.5). S0-A: the full image -- one current core, and at most one
candidate while a new source is being prepared.

A `QOpenGLWidget` laid over the full image's ViewBox exactly as
`Step1CoreLayer` is (mouse passes through, geometry follows the ViewBox).
What reaches the screen is ALWAYS the published framebuffer:

* the current core paints into its pending framebuffer; only a paint with
  no failure is swapped in as the published one (a failed one leaves the
  last published picture on screen);
* a candidate paints into its own framebuffer and replaces the published
  picture only when that paint succeeded AND the core says the view it
  drew is wholly covered for every display channel (`view_covered`,
  §10.5.2) at the current device size; the old core is then paused as
  the standby (below);
* the published framebuffer is copied into the widget's own.

The core a published candidate replaces, and the current one when the
Python picture takes over, is not closed but PAUSED and kept as the
standby (user ruling 2026-10-27, plan §10.5.6): going back to its source
resumes it instead of building a new core and warming new GPU blocks.
Never more than two cores: the mount drops the standby before building a
new core while a current one exists.

A terminal failure stops every later publication at once; the mount's
fallback runs on the next event-loop turn, never inside a paint. While no
picture is published the widget stays hidden (the Python picture shows) and
a candidate is drawn off screen by `_drive`, with the context made current
by hand.

The mount decides sources, display rows and camera; this file only owns
the context, the framebuffers and the cores' sessions.

S0-C (plan §16.4-16.5): everything above is one `Panel` -- its cores, its
published picture, its quality gate. The host holds the context and the
panels of its layout: the full image is one panel; the compare strip three,
each presented into its own rectangle. With more than one panel a paint
publishes the panels' new pictures together or not at all (§16.5). The old
host attributes (`current`, `published_level`, `request_switch`, ...) are
the full-image panel's.
"""

import time
from typing import Any, Callable, Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ...utils import perf_trace
from ...utils import tile_trace
from ...viewer import coverage_probe
from ..core_gl_session import CoreGlSession

GL_COLOR_BUFFER_BIT = 0x00004000
GL_LINEAR = 0x2601
GL_FRAMEBUFFER = 0x8D40
GL_SCISSOR_TEST = 0x0C11


class _Gl:
    """The few GL calls the host makes itself (PyQt5 here has no
    `versionFunctions` module): the GL 1.x ones from the system library,
    `glBindFramebuffer` through the context."""

    def __init__(self, context):
        import ctypes
        lib = _libgl()
        self.enable = lib.glEnable
        self.enable.argtypes = [ctypes.c_uint]
        self.disable = lib.glDisable
        self.disable.argtypes = [ctypes.c_uint]
        self.scissor = lib.glScissor
        self.scissor.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.clear_color = lib.glClearColor
        self.clear_color.argtypes = [ctypes.c_float] * 4
        self.clear = lib.glClear
        self.clear.argtypes = [ctypes.c_uint]
        address = int(context.getProcAddress(b"glBindFramebuffer") or 0)
        if not address:
            raise RuntimeError("glBindFramebuffer is not available")
        self.bind_framebuffer = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(address)


_LIBGL = []


def _libgl():
    """The system GL library, looked up once: `find_library` runs a
    subprocess, which once per frame stalled every drag of the compare
    panels (user acceptance, 2026-10-29)."""
    if not _LIBGL:
        import ctypes
        import ctypes.util
        _LIBGL.append(ctypes.CDLL(ctypes.util.find_library("GL") or "libGL.so.1"))
    return _LIBGL[0]


def _gl_functions(host):
    """`_Gl` for `host`'s current context (made once per context, kept on
    the host until the context goes), or None. Not keyed on `id()` of
    `context()`: that is a new Python wrapper on every call."""
    context = host.context()
    if context is None:
        return None
    if host._gl is None:
        try:
            host._gl = _Gl(context)
        except Exception:                                   # noqa: BLE001 -- gaps left as they are
            host._gl = False
    return host._gl or None


class Step0GlHostError(RuntimeError):
    pass


class CoreSlot:
    """One core in the host: its session, the source it shows, and the
    framebuffer it paints into (the candidate's own, or the current one's
    pending one)."""

    def __init__(self, core, session: CoreGlSession, identity):
        self.core = core
        self.session = session
        self.identity = identity
        self.fbo: Optional[QtGui.QOpenGLFramebufferObject] = None
        self.attached = False
        #: S0-B: the core's Python-source adapter (`python_source`), if any
        self.adapter = None
        #: S0-B (§11.5): what this core's picture is -- the marker's drawn
        #: channel (a slide channel or a Python source) -- apart from
        #: `identity`, which says what the CORE is
        self.picture = None
        #: S0-B: a switch being prepared on this (current) core:
        #: `(promote, yield, token)`, tried on every paint until published
        self.switch = None
        #: S0-B: the marker channel the prepared switch brings (the mount's)
        self.pending = None
        #: S0-B: the rows the published picture was drawn with, and each
        #: Python source's last gain table (the mount's)
        self.shown_rows = None
        self.gains = {}
        #: §14.6: the level this core's last paint admitted (normalised)
        self.admitted = None


#: §14.6-14.7: a switch held back only because the transition's admission
#: is coarser than the picture on screen is let through after this long
GATE_STALL_S = 3.0
#: a trial's gate with nothing on screen: the core gates at its admission level
NO_GATE = 255


def _owner_of(slot):
    """Whose stall clock (§14.7): the current core's switch by its token,
    a candidate by its slot."""
    switch = getattr(slot, "switch", None)
    if switch is not None:
        return ("switch", switch[2])
    return ("candidate", id(slot))


def _level_of(report, core) -> int:
    """A report's drawn level; -1 (coarse only, frame.rs) is the coarsest
    level (§14.7)."""
    level = int(report.get("level", -1))
    return int(core.coarsest_level()) if level < 0 else level


class Panel:
    """One picture of the host (S0-C §16.4): its cores (current, candidate,
    standby), its published framebuffer, the view and level that picture
    was drawn at, its quality-gate clocks, and the mount's hooks. Painted
    and presented by the host, into `rect` (device pixels in the host;
    None: the whole widget)."""

    def __init__(self, host, name="full"):
        self.host = host
        self.name = name
        # every panel is the host's until forgotten: its cores and framebuffer
        # are counted, released and rebuilt with the context even while it is
        # not laid out (codex S0-C code 1, P2-3)
        registry = getattr(host, "_panels", None)
        if registry is not None:
            registry.append(self)
        self.current: Optional[CoreSlot] = None
        self.candidate: Optional[CoreSlot] = None
        #: the previous source's core, paused (§10.5.6)
        self.standby: Optional[CoreSlot] = None
        self.published: Optional[QtGui.QOpenGLFramebufferObject] = None
        #: the view and device size the published picture was drawn for
        self.published_view = None
        #: §14.6: the level the published picture was drawn at (None: none) --
        #: a switch is never published coarser than this
        self.published_level: Optional[int] = None
        #: §14.6-15.9: since when each waiting picture (`_owner_of`) is held
        #: back only by a coarser transition admission
        self.gate_since: Dict[Any, float] = {}
        self.switch_trials_missed = 0
        #: not running (left the page, the other mode, a production run):
        #: not painted, its cores paused by its mount
        self.paused = False
        #: keep the core a published candidate replaces, paused, as the
        #: standby (the full image, §10.5.6); a compare panel closes it (§16.6)
        self.keep_standby = True
        #: device rectangle in the host (None: the whole widget)
        self.rect: Optional[QtCore.QRect] = None
        self.a9_pending = None
        # ── set by the mount ──
        #: () -> (x0, x1, y0, y1) the ViewBox shows now, or None
        self.view_rect: Callable[[], Any] = lambda: None
        #: (core) -> "" or why it can never draw (unavailable / overflow)
        self.cannot_draw: Callable[[Any], str] = lambda _core: ""
        #: (slot) -> is this slot's source still the one wanted?
        self.still_wanted: Callable[[CoreSlot], bool] = lambda _slot: True
        #: (slot) after a candidate became the published picture
        self.on_published: Optional[Callable[[CoreSlot], None]] = None
        #: () -> rows the current picture must draw
        self.expected_channels: Callable[[], int] = lambda: 0
        #: S0-B: (slot, token) right after a switch's trial frame was
        #: published, inside the same paint -- the mount makes it the display
        self.on_switched: Optional[Callable[[CoreSlot, Any], None]] = None

    # ── cores ─────────────────────────────────────────────────────────
    def cores(self):
        """Every core held: current, candidate and standby."""
        return [s for s in (self.current, self.candidate, self.standby) if s is not None]

    def live_cores(self):
        """The cores that follow the camera and paint: current and candidate."""
        return [s for s in (self.current, self.candidate) if s is not None]

    def add_candidate(self, core, sockets, identity) -> CoreSlot:
        """A new source's core; the previous candidate (if any) goes. Its GL
        names are made now, in the host's context. Raises on failure (the
        core is closed and released first)."""
        host = self.host
        self.drop_candidate()
        slot = CoreSlot(core, None, identity)
        slot.session = CoreGlSession(core, sockets, owner=host, on_notice=host.request_frame,
                                     tag=host.a9_tag, channel_name=str, parent=host)
        if not host._initialized:
            slot.session.close()
            slot.session.forget_gl()
            slot.session.close_sockets()
            raise Step0GlHostError("the GL host is not initialized")
        host.makeCurrent()
        try:
            slot.session.attach_gl(host.context())
            slot.attached = True
        except Exception:
            slot.session.close()
            slot.session.forget_gl()
            slot.session.close_sockets()
            raise
        finally:
            host.doneCurrent()
        self.candidate = slot
        return slot

    def drop_candidate(self) -> None:
        slot, self.candidate = self.candidate, None
        if slot is not None:
            self.host._retire(slot)
        self._gate_prune()                       # its stall clock goes with it

    def drop_standby(self) -> None:
        slot, self.standby = self.standby, None
        if slot is not None:
            self.host._retire(slot)

    def park_current(self) -> None:
        """The Python picture takes over: the current core is paused and
        kept as the standby (the previous standby goes), the published
        picture is dropped."""
        slot, self.current = self.current, None
        self._gate_prune()
        if slot is not None:
            slot.core.pause()
            if slot.adapter is not None:
                slot.adapter.set_paused(True)
            self.drop_standby()
            self.standby = slot
        self.host._with_context(self._drop_published)
        self.published_view = None
        self.published_level = None
        self.a9_pending = None

    def take_standby(self, identity) -> Optional[CoreSlot]:
        """The standby showing `identity`, resumed as the candidate (the
        candidate in hand goes), or None."""
        slot = self.standby
        if slot is None or slot.identity != identity:
            return None
        self.drop_candidate()
        self.standby = None
        slot.core.resume()
        if slot.adapter is not None:
            slot.adapter.set_paused(False)
        self.candidate = slot
        return slot

    def drop_all(self) -> None:
        """Every core and the published picture go."""
        slots = self.cores()
        self.current = self.candidate = self.standby = None
        for slot in slots:
            self.host._retire(slot)
        self.host._with_context(self._drop_published)
        self.published_view = None
        self.published_level = None
        self.a9_pending = None
        self.gate_since.clear()

    def _drop_published(self) -> None:
        self.published = None

    def request_switch(self, slot: CoreSlot, promote, yield_, token) -> None:
        """S0-B (§11.5): on `slot` (the current core), publish the picture
        with `promote` instead of `yield_` as soon as one paint of it covers
        the view; until then every paint shows the old rows at the current
        view (the old picture follows the camera)."""
        slot.switch = (tuple(promote), tuple(yield_), token)
        self._gate_prune()                       # a new switch waits from now (§14.7)
        self.host.request_frame()

    # ── one paint (§16.5: tried, then committed by the host) ──────────
    def trial_switch(self, width, height, view, pump=True):
        """One trial paint of the prepared switch. Returns its commit (a
        callable that publishes it) when it drew, covered the view over the
        rows it composed (or composed none: the empty-picture exception)
        and is still wanted; None when not (the old rows are then painted,
        `paint_old`, in this same paint); `FAILED` after a terminal
        failure."""
        slot = self.current
        promote, yield_, token = slot.switch
        gate = self.published_level
        # the core gates at the finer of `gate` and this frame's admission
        # level (255: nothing on screen, the admission level alone)
        trial = (promote, yield_, NO_GATE if gate is None else gate)
        report = self.host._paint_slot(self, slot, width, height, view, trial, pump)
        if report is None:
            return FAILED                        # a terminal failure: nothing more this paint
        level = _level_of(report, slot.core)
        # the final picture has no row at all (Marker and DAPI both off):
        # nothing to cover -- not "no tile drawn yet", which is not covered
        empty = int(self.expected_channels() or 0) == 0
        # a switch no longer wanted (a candidate replaces it) leaves the
        # gate's clocks alone (codex §15 code 2, P2-1)
        if not self.still_wanted(slot) or not (
                self._gate_open(report.get("trial_exact"), report.get("trial_admitted"), level, gate,
                                owner=_owner_of(slot)) or empty):
            self.switch_trials_missed += 1
            return None

        def commit():
            self._gate_done(_owner_of(slot))
            slot.switch = None
            slot.fbo, self.published = self.published, slot.fbo
            self.published_view = (view, width, height)
            self.published_level = level
            host = self.host
            host.published_count += 1
            self.a9_pending = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                                      getattr(host, "_a9_input_seq", None))
            perf_trace.mark("step0.engine_switched", count=host.published_count, panel=self.name,
                            token=str(token)[:120].replace(" ", "_"))
            if slot.adapter is not None:
                slot.adapter.pump_soon()         # the deferred tiles (§15.6)
            hook = self.on_switched
            if hook is not None:
                hook(slot, token)                # the display follows now, inside this paint
        return commit

    def paint_old(self, width, height, view, pump=True) -> None:
        """The current core's picture as it is (a switch waiting or none):
        painted and published -- or, while a switch waits and the camera has
        not moved, left exactly as published (§14.6)."""
        slot = self.current
        if slot is None:
            return
        if slot.switch is not None and slot.picture is None:
            # nothing of the old picture is left to draw (its source was
            # retired for a production run): the published one stays
            return
        if (slot.switch is not None and self.published is not None
                and self.published_view == (view, width, height)):
            # §14.6: a switch waits and the camera has not moved -- the old
            # picture stays exactly as published (no repaint of its rows,
            # which the planner may be thinning out meanwhile)
            return
        report = self.host._paint_slot(self, slot, width, height, view, pump=pump)
        if report is None:
            return
        # the pending picture is now complete: it is the published one
        slot.fbo, self.published = self.published, slot.fbo
        self.published_view = (view, width, height)
        self.published_level = _level_of(report, slot.core)
        self.a9_pending = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                                  getattr(self.host, "_a9_input_seq", None))

    def trial_candidate(self, width, height, view):
        """Paint the candidate. Its commit when that paint covered `view` at
        this size (the quality gate, §14.6) and it is still wanted; None
        when not; `FAILED` after a terminal failure."""
        slot = self.candidate
        report = self.host._paint_slot(self, slot, width, height, view)
        if report is None:
            return FAILED
        # every successful paint's drawing records and growth check; the
        # presentation records only if this picture is published
        drawn = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                        getattr(self.host, "_a9_input_seq", None))
        # nothing to draw (Marker and DAPI both switched off): nothing to
        # cover, the successful paint is the picture
        required = int(self.expected_channels() or 0)
        rect = tuple(float(v) for v in view)
        gate = self.published_level
        level = _level_of(report, slot.core)
        if required:
            # §14.6: the same quality gate as a switch -- never coarser than
            # the picture on screen, nor than this frame's admission level
            target = level if gate is None else min(level, gate)
            exact = slot.core.view_covered_at(rect, target)
            admitted = exact if target == level else slot.core.view_covered_at(rect, level)
        if not self.still_wanted(slot):
            return None
        if required and not self._gate_open(exact, admitted, level, gate, owner=_owner_of(slot)):
            return None

        def commit():
            self._gate_done(_owner_of(slot))
            old, self.current, self.candidate = self.current, slot, None
            slot.fbo, self.published = self.published, slot.fbo
            if slot.fbo is not None and (slot.fbo.width(), slot.fbo.height()) != (width, height):
                slot.fbo = None
            self.published_view = (view, width, height)
            self.published_level = level
            host = self.host
            host.published_count += 1
            self.a9_pending = drawn
            perf_trace.mark("step0.engine_published", count=host.published_count, panel=self.name,
                            source=str(slot.identity)[:120].replace(" ", "_"))
            if slot.adapter is not None:
                slot.adapter.pump_soon()         # the deferred tiles (§15.6)
            if old is not None and not self.keep_standby:
                self.host._retire(old)
            elif old is not None:                # kept, paused, for going back (§10.5.6)
                old.core.pause()
                if old.adapter is not None:
                    old.adapter.set_paused(True)
                self.drop_standby()
                self.standby = old
            hook = self.on_published
            if hook is not None:
                QtCore.QTimer.singleShot(0, lambda: hook(slot))
        return commit

    def waiting(self) -> bool:
        """A new picture is being prepared here: a candidate, or a switch
        still wanted (§16.5: what a group publication waits for)."""
        slot = self.current
        return self.candidate is not None or (
            slot is not None and slot.switch is not None and self.still_wanted(slot))

    # ── §14.6: the quality gate ───────────────────────────────────────
    def _gate_open(self, exact, admitted, level, gate, owner) -> bool:
        """May the new picture be published? When it covers the view at the
        finer of `gate` (the level on screen; None: nothing is) and this
        frame's admission `level` (`exact`) -- or, when the admission itself
        is coarser than the gate (old and new rows budgeted together) and
        has been for `GATE_STALL_S` for this `owner` (each waiting picture
        has its own clock), when it covers the view at the admission level
        (`admitted`); that exception is recorded."""
        if exact:
            return True
        self._gate_prune()
        if gate is None or level <= gate:
            self.gate_since.pop(owner, None)
            return False
        now = time.monotonic()
        waited = now - self.gate_since.setdefault(owner, now)
        if waited >= GATE_STALL_S:
            if not admitted:
                return False                     # the next tile's paint asks again
            perf_trace.mark("step0.gate_budget_limited", level=level, gate=gate)
            return True
        self.host._gate_wake(int((GATE_STALL_S - waited) * 1000) + 1)
        return False

    def gate_levels(self, slot) -> tuple:
        """§14.6 / 15.6: the levels a waiting picture of `slot` is asked at
        first -- its gate (the finer of the level on screen and its last
        admission), and once held back past `GATE_STALL_S` its admission
        level too (what the budget-limited exception publishes at)."""
        known = [lv for lv in (self.published_level, slot.admitted) if lv is not None]
        if not known:
            return ()
        gate = min(known)
        since = self.gate_since.get(_owner_of(slot))
        stalled = since is not None and time.monotonic() - since >= GATE_STALL_S
        if stalled and slot.admitted is not None and slot.admitted != gate:
            return (gate, slot.admitted)
        return (gate,)

    def _gate_prune(self) -> None:
        """Only the pictures still waiting keep a clock: the current core's
        switch and the candidate."""
        live = {_owner_of(s) for s in (self.current, self.candidate) if s is not None}
        for owner in [o for o in self.gate_since if o not in live]:
            del self.gate_since[owner]
        self.host._gate_idle()

    def _gate_done(self, owner) -> None:
        self.gate_since.pop(owner, None)
        self._gate_prune()

    def reset_gate_clock(self) -> None:
        """Running again after a pause (§14.7): a held-back picture's wait
        starts over."""
        self.gate_since.clear()
        self.host._gate_idle()

    def release_to_coarse(self) -> int:
        """This panel's cores give their GPU tiles back down to the coarse level."""
        host = self.host
        if host._disposed or not host._initialized:
            return 0
        got = []
        host._with_context(lambda: got.extend(int(s.core.release_to_coarse()) for s in self.cores()))
        return sum(got)


#: a trial's verdict after a terminal failure (nothing more this paint)
FAILED = object()


class Step0GlHost(QtWidgets.QOpenGLWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initialized = False
        self._ever_initialized = False
        self._init_error: Optional[Exception] = None
        self._disposed = False
        self._failed = ""
        self._attached_view = None
        self._attached_viewport = None
        #: S0-C: the full image's panel, and the panels painted and presented
        #: now (the full image's alone, or the compare strip's three)
        self._panels = []
        self.full = Panel(self, "full")
        self.layout = [self.full]
        #: `paint_now`'s synchronous repaint: present what is published, no core paint
        self._present_only = False
        #: `_Gl` of the current context (None: not made yet, False: unusable)
        self._gl = None
        #: present-only paints done (tests: `paint_now` put its picture on screen)
        self.presented_now = 0
        #: publications, every panel together
        self.published_count = 0
        #: §14.7: the one timer that comes back for the panels' held-back
        #: pictures (each panel keeps its own clocks)
        self._gate_timer = QtCore.QTimer(self)
        self._gate_timer.setSingleShot(True)
        self._gate_timer.timeout.connect(self.request_frame)
        #: §15: what the last `paint_now` did (uploads, deferred, published)
        self.last_paint: Dict[str, Any] = {}
        self._collect: Optional[list] = None
        #: S0-C: "full" or "compare" (§16.4), the compare strip's widgets
        self._mode = "full"
        self._compare = None
        #: (logical region in this widget, colour): what shows between the
        #: compare panels (§16.4 3)
        self._gaps = []
        from .. import gpu_memory
        self.smooth = gpu_memory.smooth_pixels()
        # ── set by the mount ──
        #: (why) once, on the next event-loop turn after a terminal failure --
        #: the owner's, then every borrower's (§16.13 3: all go to Python)
        self.on_failure: Optional[Callable[[str], None]] = None
        self.failure_hooks = []
        self.on_smooth_changed: Optional[Callable[[], None]] = None
        self.smooth_hooks = []
        #: () before the owner disposes the host: the borrowers close their panels
        self.closing_hooks = []
        #: (panels) inside the paint that published new pictures on them
        self.commit_hooks = []
        self._drive_queued = False
        #: the owner mount's raw manifest (`Step0EngineMount.manifest_offer`),
        #: offered to the compare panels' mounts of the same file
        self.manifest_offer = None
        self._again = QtCore.QTimer(self)
        self._again.setSingleShot(True)
        self._again.timeout.connect(self.request_frame)
        self.frameSwapped.connect(self._presented)
        gpu_memory.register(self)
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        fmt.setSwapInterval(QtGui.QSurfaceFormat.defaultFormat().swapInterval())
        self.setFormat(fmt)

    # ── state ─────────────────────────────────────────────────────────
    @property
    def initialized(self) -> bool:
        return bool(self._initialized)

    @property
    def init_error(self) -> Optional[Exception]:
        return self._init_error

    @property
    def failed(self) -> str:
        return self._failed

    @property
    def has_picture(self) -> bool:
        return self.full.published is not None

    def cores(self):
        """Every core of the full-image panel: current, candidate, standby."""
        return self.full.cores()

    def live_cores(self):
        """The full-image panel's cores that follow the camera and paint."""
        return self.full.live_cores()

    def panels(self):
        """Every panel the host holds, laid out or not (the full image's
        first)."""
        return list(self._panels)

    def forget_panel(self, panel) -> None:
        """A borrower's panel goes (its cores already closed)."""
        if panel is not self.full and panel in self._panels:
            self._panels.remove(panel)
            if panel in self.layout:
                self.layout = [p for p in self.layout if p is not panel] or [self.full]

    def _all_cores(self):
        return [slot for panel in self.panels() for slot in panel.cores()]

    # ── `hidden`: the layout is not running (§16.13 3) ────────────────
    @property
    def hidden(self) -> bool:
        """`gpu_memory`'s switch and the paint gate: every panel of the
        layout is paused ("not running": left the page, the other mode, a
        production run) -- never "nothing published"."""
        return all(p.paused for p in self.layout)

    @hidden.setter
    def hidden(self, value) -> None:
        # the full-image mount's switch (before S0-C: the host's own)
        self.full.paused = bool(value)

    @property
    def mode(self) -> str:
        return self._mode

    # ── on the ViewBox (as `Step1CoreLayer.attach`) ───────────────────
    def attach(self, view_adapter) -> None:
        if self._initialized:
            raise Step0GlHostError("attach must happen before the widget is realised")
        self._attached_view = view_adapter
        self._attached_viewport = view_adapter.graphics.viewport()
        if self.parent() is not self._attached_viewport:
            self.setParent(self._attached_viewport)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._follow([view_adapter], [self._attached_viewport], True)
        self._sync_geometry()

    def _follow(self, views, widgets, on) -> None:
        """Repaint on the views' range changes; place again on their and
        `widgets`' size changes (or stop)."""
        for view in views:
            pairs = ((view.view_box.sigRangeChanged, self._range_changed),
                     (view.view_box.sigResized, self._sync_geometry))
            for signal, slot in pairs:
                try:
                    signal.connect(slot) if on else signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        for widget in widgets:
            try:
                widget.installEventFilter(self) if on else widget.removeEventFilter(self)
            except RuntimeError:
                pass

    def _followed(self):
        if self._mode == "compare" and self._compare is not None:
            panes, views = self._compare
            return list(views), [panes] + [v.graphics.viewport() for v in views]
        if self._attached_view is None:
            return [], []
        return [self._attached_view], [self._attached_viewport]

    def use_full(self) -> None:
        """§16.4: back on the full image's ViewBox, its one panel."""
        if self._mode == "full":
            return
        self._follow(*self._followed(), False)
        self._mode, self._compare, self._gaps = "full", None, []
        self.layout = [self.full]
        self._reparent(self._attached_viewport)
        self._follow(*self._followed(), True)
        self._sync_geometry()

    def use_compare(self, panes, views, panels) -> None:
        """§16.4: over the compare strip's three ViewBoxes (`views`, laid
        out in `panes`), one panel each. Re-parenting inside the window
        keeps this context (measured); were it rebuilt, `initializeGL`
        attaches every core again."""
        if self._mode == "compare" and self._compare == (panes, tuple(views)) and self.layout == list(panels):
            return
        self._follow(*self._followed(), False)
        self._mode, self._compare = "compare", (panes, tuple(views))
        self.layout = list(panels)
        self._reparent(panes)
        self._follow(*self._followed(), True)
        self._sync_geometry()

    def _reparent(self, parent) -> None:
        if parent is not None and self.parent() is not parent:
            self.setParent(parent)               # hidden by Qt until shown again
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self._sync_geometry()
        return False

    @staticmethod
    def _view_box_rect(view, into):
        """`view`'s ViewBox as a rectangle of widget `into`, or None."""
        view_box = getattr(view, "view_box", None)
        graphics = getattr(view, "graphics", None)
        if view_box is None or graphics is None:
            return None
        scene_rect = view_box.mapRectToScene(view_box.rect())
        top_left = graphics.mapFromScene(scene_rect.topLeft())
        width, height = int(round(scene_rect.width())), int(round(scene_rect.height()))
        if width <= 0 or height <= 0:
            return None
        viewport = graphics.viewport()
        if into is not viewport:
            top_left = viewport.mapTo(into, top_left)
        return QtCore.QRect(top_left.x(), top_left.y(), width, height)

    def _sync_geometry(self, *_args) -> None:
        """Cover exactly the ViewBox, as `Step1CoreLayer._sync_geometry` --
        in the compare layout the three ViewBoxes' bounding rectangle, each
        panel its own ViewBox's part of it (device pixels)."""
        if self._mode == "compare" and self._compare is not None:
            self._sync_compare()
            return
        if self._attached_viewport is None:
            return
        rect = self._view_box_rect(self._attached_view, self._attached_viewport)
        if rect is None:
            rect = self._attached_viewport.rect()
        self.setGeometry(rect)
        self.raise_()

    def _sync_compare(self) -> None:
        panes, views = self._compare
        rects = [self._view_box_rect(v, panes) for v in views]
        if any(r is None for r in rects):
            return
        bound = rects[0]
        for r in rects[1:]:
            bound = bound.united(r)
        self.setGeometry(bound)
        self.raise_()
        ratio = float(self.devicePixelRatioF()) or 1.0
        local = [r.translated(-bound.topLeft()) for r in rects]
        for panel, r in zip(self.layout, local):
            panel.rect = QtCore.QRect(int(round(r.x() * ratio)), int(round(r.y() * ratio)),
                                      max(1, int(round(r.width() * ratio))),
                                      max(1, int(round(r.height() * ratio))))
        # what shows between them: each view's margin in its background,
        # the strip's spacing in the strip's
        rest = QtGui.QRegion(QtCore.QRect(0, 0, bound.width(), bound.height()))
        for r in local:
            rest = rest.subtracted(QtGui.QRegion(r))
        gaps = []
        for view in views:
            graphics = view.graphics
            viewport = graphics.viewport()
            area = QtCore.QRect(viewport.mapTo(panes, QtCore.QPoint(0, 0)), viewport.size())
            area = QtGui.QRegion(area.translated(-bound.topLeft())).intersected(rest)
            if not area.isEmpty():
                gaps.append((area, QtGui.QColor(graphics.backgroundBrush().color())))
                rest = rest.subtracted(area)
        if not rest.isEmpty():
            gaps.append((rest, QtGui.QColor(panes.palette().color(panes.backgroundRole()))))
        self._gaps = gaps
        self.request_frame()

    def _range_changed(self, *_args) -> None:
        self.request_frame()

    def _device_size(self):
        ratio = float(self.devicePixelRatioF()) or 1.0
        return max(1, int(round(self.width() * ratio))), max(1, int(round(self.height() * ratio)))

    # ── Qt lifecycle ──────────────────────────────────────────────────
    @perf_trace.timed("gpu.init")
    def initializeGL(self) -> None:  # noqa: N802
        context = self.context()
        if context is None or not context.isValid():
            self._init_error = Step0GlHostError("Qt did not create a valid OpenGL context")
            return
        try:
            context.aboutToBeDestroyed.connect(self._context_going)
            for slot in self._all_cores():                  # a reparent's new context
                slot.session.attach_gl(context)
                slot.attached = True
            self._ever_initialized = True
            self._initialized = True
        except Exception as exc:                            # noqa: BLE001 -- reported, fallback
            self._init_error = exc
            if self._ever_initialized:
                self._fail(f"GL re-initialisation failed: {exc}")

    def _context_going(self) -> None:
        """`aboutToBeDestroyed`: every core's names and the framebuffers go
        with this context; a reparent builds them again."""
        self._gl = None
        if self._disposed or not self._initialized:
            return
        self.makeCurrent()
        try:
            for panel in self.panels():
                for slot in panel.cores():
                    slot.session.release_gl()
                    slot.attached = False
                    slot.fbo = None
                panel.published = None
        finally:
            self.doneCurrent()
        self._initialized = False

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        del width, height                                   # each paint reads the size

    # ── cores (the full-image panel's; S0-C: each panel has its own) ──
    def add_candidate(self, core, sockets, identity) -> CoreSlot:
        return self.full.add_candidate(core, sockets, identity)

    def drop_candidate(self) -> None:
        self.full.drop_candidate()

    def drop_standby(self) -> None:
        self.full.drop_standby()

    def park_current(self) -> None:
        self.full.park_current()

    def take_standby(self, identity) -> Optional[CoreSlot]:
        return self.full.take_standby(identity)

    def drop_all(self) -> None:
        """Every core of every panel and the published pictures go."""
        for panel in self.panels():
            panel.drop_all()

    def _retire(self, slot: CoreSlot) -> None:
        if tile_trace.ON:
            slot.session.merge_trace()
        if slot.adapter is not None:
            slot.adapter.close()                 # its sources retired, requests cancelled
            slot.adapter = None
        slot.session.close()

        def release():
            if slot.attached:
                slot.session.release_gl()
            slot.fbo = None
        if slot.attached and not self._with_context(release):
            slot.session.forget_gl()
        slot.attached = False
        slot.session.close_sockets()
        slot.session.setParent(None)

    def _with_context(self, work) -> bool:
        """Run `work` with this widget's context current (made current here
        unless it already is). False when there is no live context."""
        context = self.context()
        if not self._initialized or context is None or not context.isValid():
            return False
        own = QtGui.QOpenGLContext.currentContext() is not context
        if own:
            self.makeCurrent()
        try:
            work()
        finally:
            if own:
                self.doneCurrent()
        return True

    # ── frames ────────────────────────────────────────────────────────
    def request_frame(self, *_args) -> None:
        """Something changed: paint again -- through Qt while the picture is
        on screen, else (a candidate prepared off screen) by hand."""
        if self._disposed or self._failed or self.hidden:
            return
        if self.isVisible():
            self.update()
        elif self._off_screen_work() and not self._drive_queued:
            self._drive_queued = True
            QtCore.QTimer.singleShot(0, self._drive)

    def _off_screen_work(self) -> bool:
        """A candidate is prepared off screen -- or, on the compare strip, a
        switch the host waits for before it is shown (`CompareEngine`)."""
        return any(p.candidate is not None or (self._mode == "compare" and p.current is not None
                                               and p.current.switch is not None)
                   for p in self.layout)

    def _drive(self) -> None:
        """The candidates' off-screen paint while nothing is published (the
        widget is hidden, so Qt does not call `paintGL`)."""
        self._drive_queued = False
        if (self._disposed or self._failed or self.hidden or self.isVisible()
                or not self._off_screen_work() or not self._initialized):
            return
        self.makeCurrent()
        try:
            self._paint_panels(currents=self._mode == "compare")
        finally:
            self.doneCurrent()
        # shown by the mount's `on_published`, together with turning the
        # Python supply off -- one owner of "who is the picture"

    @perf_trace.timed("gpu.paint")
    def paintGL(self) -> None:  # noqa: N802
        if tile_trace.ON:
            tile_trace.stamp("paint")
        if not self._initialized or self._disposed:
            return
        if not self._failed and not self.hidden and not self._present_only:
            self._paint_panels()
        self._present(*self._device_size())
        if self._present_only:
            self.presented_now += 1

    def _panel_size(self, panel):
        """`panel`'s device size: its rectangle's, or the widget's."""
        rect = panel.rect
        if rect is None:
            return self._device_size()
        return max(1, int(rect.width())), max(1, int(rect.height()))

    def _paint_panels(self, pump=True, currents=True, candidates=True) -> None:
        """One paint of the layout's panels (§16.5). Every panel's new
        picture (a switch, a candidate) is tried; with one panel what passed
        is published, with several only when EVERY panel waiting for one
        passed in this same paint -- else none is, and each keeps its old
        picture. The current pictures not replaced are painted as they are
        (following the camera, or frozen, §14.6) -- all of them before
        anything is published, so nothing is published in a paint where a
        current core failed."""
        work = []
        for panel in self.layout:
            view = panel.view_rect()
            if view is None:
                continue
            width, height = self._panel_size(panel)
            work.append((panel, width, height, view))
        commits, waiting, switching = [], 0, []
        if currents:
            for panel, width, height, view in work:
                slot = panel.current
                if slot is None:
                    continue
                if slot.switch is None:
                    panel.paint_old(width, height, view, pump)
                else:
                    verdict = panel.trial_switch(width, height, view, pump)
                    if verdict is FAILED:
                        return
                    switching.append((panel, width, height, view))
                    if panel.candidate is None and panel.waiting():
                        waiting += 1
                    if verdict is not None:
                        commits.append((panel, "switch", verdict))
                if self._failed:
                    return
        if candidates:
            for panel, width, height, view in work:
                if panel.candidate is None:
                    continue
                verdict = panel.trial_candidate(width, height, view)
                if verdict is FAILED or self._failed:
                    return
                waiting += 1
                if verdict is not None:
                    commits.append((panel, "candidate", verdict))
        if len(self.layout) > 1 and waiting and len(commits) < waiting:
            commits = []                         # §16.5: all together or none
        switched = {id(panel) for panel, kind, _commit in commits if kind == "switch"}
        for panel, width, height, view in switching:
            if id(panel) not in switched:
                panel.paint_old(width, height, view, pump)
            if self._failed:
                return
        for _panel, _kind, commit in commits:
            commit()
        if commits:
            done = [panel for panel, _kind, _commit in commits]
            for hook in list(self.commit_hooks):
                hook(done)

    def paint_now(self, present=True, candidates=False) -> bool:
        """§13.2 / 13.5: one paint of the current core, now, without asking
        Python for anything (what the cache held is already in); a prepared
        switch is published by it when it covers the view (§11.5) and is put
        on screen at once by a synchronous present-only repaint -- before the
        production run's thread starts and competes with this thread (§13.10).
        False when nothing could be painted."""
        if (not self._initialized or self._disposed or self._failed or self.hidden
                or not any(p.current is not None for p in self.layout)):
            return False
        if not any(p.view_rect() is not None for p in self.layout):
            return False
        count = self.published_count
        self._collect = []
        self.makeCurrent()
        try:
            self._paint_panels(pump=False, candidates=candidates)
        finally:
            self.doneCurrent()
            reports, self._collect = self._collect, None
        self.last_paint = {
            "published": self.published_count != count,
            "uploads": sum(int(r.get("uploads", 0) or 0) for r in reports),
            "deferred": sum(int(r.get("deferred", 0) or 0) for r in reports),
            "deferred_growth": sum(int(r.get("deferred_growth", 0) or 0) for r in reports),
            "reserving": any(bool(r.get("reserving")) for r in reports),
            "paints": len(reports),
        }
        if present:
            self.present_now()
        return not self._failed

    def paint_ahead(self, panel, width, height) -> Optional[Dict[str, Any]]:
        """S0-C (codex review 2026-10-30, a trial the user approved): one
        off-screen paint of `panel` -- a compare panel, while the full image
        is the layout -- at `width` x `height`, so its cores reserve their
        GPU blocks (one per paint, `frame.rs` top_up) and take their first
        tiles before the strip is entered. Nothing is presented. What the
        paint did, or None when nothing was painted."""
        if (not self._initialized or self._disposed or self._failed or self.hidden
                or panel in self.layout or not panel.live_cores() or panel.view_rect() is None):
            return None
        layout, rect = self.layout, panel.rect
        self.layout, panel.rect = [panel], QtCore.QRect(0, 0, max(1, int(width)), max(1, int(height)))
        self._collect = []
        self.makeCurrent()
        try:
            self._paint_panels()
        finally:
            self.doneCurrent()
            self.layout, panel.rect = layout, rect
            reports, self._collect = self._collect, None
        return {"reserving": any(bool(r.get("reserving")) for r in reports),
                "uploads": sum(int(r.get("uploads", 0) or 0) for r in reports),
                "paints": len(reports)}

    def present_now(self) -> None:
        """What is published, on screen now: a synchronous present-only
        repaint (13.10), before a production run's thread starts."""
        if not self.isVisible():
            return
        before = self.presented_now
        self._present_only = True
        try:
            self.repaint()
            if self.presented_now == before:
                # Qt composes an OpenGL window at most once a refresh: a
                # repaint() that soon after the last one is posted as an
                # update request instead (codex S13 code review 2) --
                # delivered here, now
                QtCore.QCoreApplication.sendPostedEvents(self.window(), QtCore.QEvent.UpdateRequest)
        finally:
            self._present_only = False
        if self.presented_now == before:
            perf_trace.mark("step0.present_deferred")

    def _framebuffer(self, fbo, width, height):
        """`fbo` if it has this size, else a new one (None when it cannot
        be made: a failure)."""
        if fbo is not None and fbo.width() == width and fbo.height() == height:
            return fbo
        fmt = QtGui.QOpenGLFramebufferObjectFormat()
        made = QtGui.QOpenGLFramebufferObject(width, height, fmt)
        if not made.isValid():
            self._fail(f"a {width}x{height} framebuffer could not be made")
            return None
        return made

    def _paint_slot(self, panel, slot: CoreSlot, width, height, view, trial=None, pump=True) -> Optional[dict]:
        """One paint of `slot` into its framebuffer; the report, or None
        after a terminal failure. S0-B: the core's Python tiles are taken
        first (`PythonSourceAdapter.pump`, never inside a core call) --
        not with `pump=False` (`paint_now`: nothing is asked of Python)."""
        fbo = self._framebuffer(slot.fbo, width, height)
        if fbo is None:
            return None
        slot.fbo = fbo
        if pump and slot.adapter is not None:
            slot.adapter.pump()
        session = slot.session
        report = session.paint(fbo.handle(), width, height, view, trial)
        if self._collect is not None:
            self._collect.append(report)
        # the core leaves its own target bound; ours is set again by Qt's
        # calls below and by the next paint (codex S0-A round 1)
        slot.admitted = _level_of(report, slot.core)
        why = session.failure_of(report)
        if not why and report.get("capacity_exceeded"):
            why = "the drawn channels need more GPU blocks of one format than one pass can bind"
        if not why:
            why = panel.cannot_draw(slot.core)
        if why:
            self._fail(why)
            return None
        again = session.again_ms(report)
        if again is not None and not self._again.isActive():
            self._again.start(again)
        session.note_reads()
        return report

    def request_switch(self, slot: CoreSlot, promote, yield_, token) -> None:
        self.full.request_switch(slot, promote, yield_, token)

    # ── §14.6: the quality gate (each panel's own; the host's one timer) ──
    def _gate_open(self, exact, admitted, level, gate, owner) -> bool:
        return self.full._gate_open(exact, admitted, level, gate, owner)

    def gate_levels(self, slot) -> tuple:
        return self.full.gate_levels(slot)

    def _gate_wake(self, due_ms: int) -> None:
        """A held-back picture is due in `due_ms`: paint then."""
        if not self._gate_timer.isActive() or self._gate_timer.remainingTime() > due_ms:
            self._gate_timer.start(due_ms)

    def _gate_idle(self) -> None:
        if not any(p.gate_since for p in self.panels()):
            self._gate_timer.stop()

    def _gate_clear(self) -> None:
        for panel in self.panels():
            panel.gate_since.clear()
        self._gate_timer.stop()

    def reset_gate_clock(self) -> None:
        """Running again after a pause (§14.7): a held-back picture's wait
        starts over."""
        self._gate_clear()

    def _present(self, width, height) -> None:
        """Each panel's published picture into its rectangle of the
        widget's own framebuffer (`width` x `height`; Qt redirects the
        default one there during `paintGL`); scaled when it was drawn for
        another size, until a paint at this size succeeds."""
        for panel in self.layout:
            published = panel.published
            if published is None:
                continue
            rect = panel.rect
            if rect is None:
                rect = QtCore.QRect(0, 0, width, height)
            else:
                # framebuffer rows run bottom-up
                rect = QtCore.QRect(rect.x(), height - rect.y() - rect.height(), rect.width(), rect.height())
            QtGui.QOpenGLFramebufferObject.blitFramebuffer(
                None, rect,
                published, QtCore.QRect(0, 0, published.width(), published.height()),
                GL_COLOR_BUFFER_BIT, GL_LINEAR)
        if self._gaps:
            self._fill_gaps(height)

    def _fill_gaps(self, height) -> None:
        """What shows between the compare panels (§16.4 3): each gap cleared
        to its colour, scissored, in the widget's own framebuffer -- plain GL
        clears, not a QPainter over the cores' GL state."""
        gl = _gl_functions(self)
        if gl is None:
            return
        ratio = float(self.devicePixelRatioF()) or 1.0
        gl.bind_framebuffer(GL_FRAMEBUFFER, int(self.defaultFramebufferObject()))
        gl.enable(GL_SCISSOR_TEST)
        try:
            for region, colour in self._gaps:
                gl.clear_color(colour.redF(), colour.greenF(), colour.blueF(), 1.0)
                for r in region.rects():
                    x, y = int(round(r.x() * ratio)), int(round(r.y() * ratio))
                    w, h = int(round(r.width() * ratio)), int(round(r.height() * ratio))
                    gl.scissor(x, height - y - h, w, h)
                    gl.clear(GL_COLOR_BUFFER_BIT)
        finally:
            gl.disable(GL_SCISSOR_TEST)

    def _fail(self, why: str) -> None:
        """No publication after this, from any core; the mount's fallback
        on the next event-loop turn (never inside a paint)."""
        if self._failed:
            return
        self._failed = why
        perf_trace.mark("gpu.core_failed", why=why[:160].replace(" ", "_"))
        for hook in [self.on_failure] + list(self.failure_hooks):
            if hook is not None:
                QtCore.QTimer.singleShot(0, lambda hook=hook: hook(why))

    # ── memory (`gpu_memory`) ─────────────────────────────────────────
    def gpu_bytes(self) -> int:
        if self._disposed:
            return 0
        total = sum(int(s.core.gpu_bytes()) for s in self._all_cores())
        for fbo in [p.published for p in self.panels()] + [s.fbo for s in self._all_cores()]:
            if fbo is not None:
                total += int(fbo.width()) * int(fbo.height()) * 4
        return total

    def release_to_coarse(self) -> int:
        if self._disposed or not self._initialized:
            return 0
        got = []
        self._with_context(lambda: got.extend(int(s.core.release_to_coarse()) for s in self._all_cores()))
        return sum(got)

    def set_smooth(self, smooth: bool) -> None:
        if bool(smooth) == self.smooth:
            return
        self.smooth = bool(smooth)
        for hook in [self.on_smooth_changed] + list(self.smooth_hooks):
            if hook is not None:
                hook()
        self.request_frame()

    # ── measurement ───────────────────────────────────────────────────
    def a9_tag(self) -> str:
        return f"{id(self):x}"

    def _presented(self) -> None:
        if tile_trace.ON:
            tile_trace.stamp("present", frame=self.published_count, viewer=self.a9_tag())
            for slot in self._all_cores():
                slot.session.merge_trace()
        for panel in self.layout:
            pending, panel.a9_pending = panel.a9_pending, None
            if pending is None:
                continue
            frame, result, seq, _report = pending
            perf_trace.mark("gpu.present", frame=frame, seq=seq, viewer=self.a9_tag(), step=0,
                            panel=panel.name)
            coverage_probe.publish(result, "gpu", frame)

    # ── tests ─────────────────────────────────────────────────────────
    def presented_rgba_for_test(self):
        """What the widget shows now (`grabFramebuffer`), as an RGBA array."""
        import numpy as np
        image = self.grabFramebuffer().convertToFormat(QtGui.QImage.Format_RGBA8888)
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        return np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)[
            :, :image.width()].copy()

    # ── end of life ───────────────────────────────────────────────────
    def dispose(self) -> Dict[str, Any]:
        """Every core closed and released in this context, then the
        framebuffers. Safe twice."""
        if self._disposed:
            return {"already_disposed": True}
        self._again.stop()
        self.drop_all()
        self._follow(*self._followed(), False)
        self._disposed = True
        self._initialized = False
        return {"already_disposed": False}


def _forward(name, attr=None):
    attr = attr or name
    return property(lambda self: getattr(self.full, attr),
                    lambda self, value: setattr(self.full, attr, value))


# the host's single-picture attributes, from before S0-C: the full image's panel
for _name, _attr in (("current", None), ("candidate", None), ("standby", None),
                     ("_published", "published"), ("published_view", None),
                     ("published_level", None), ("_gate_since", "gate_since"),
                     ("_a9_pending", "a9_pending"), ("switch_trials_missed", None),
                     ("view_rect", None), ("cannot_draw", None), ("still_wanted", None),
                     ("on_published", None), ("expected_channels", None), ("on_switched", None)):
    setattr(Step0GlHost, _name, _forward(_name, _attr))
