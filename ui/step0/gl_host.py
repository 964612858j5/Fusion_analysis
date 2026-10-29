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
        self.current: Optional[CoreSlot] = None
        self.candidate: Optional[CoreSlot] = None
        #: the previous source's core, paused (§10.5.6)
        self.standby: Optional[CoreSlot] = None
        self._published: Optional[QtGui.QOpenGLFramebufferObject] = None
        #: `paint_now`'s synchronous repaint: present what is published, no core paint
        self._present_only = False
        #: present-only paints done (tests: `paint_now` put its picture on screen)
        self.presented_now = 0
        #: the view and device size the published picture was drawn for
        self.published_view = None
        self.published_count = 0
        #: §14.6: the level the published picture was drawn at (None: none) --
        #: a switch is never published coarser than this
        self.published_level: Optional[int] = None
        #: §14.6: since when a switch / candidate is held back only by a
        #: coarser transition admission, and the one timer that comes back
        #: per waiting picture (`_owner_of`): codex §15 code 2, P2-1
        self._gate_since: Dict[Any, float] = {}
        self._gate_timer = QtCore.QTimer(self)
        self._gate_timer.setSingleShot(True)
        self._gate_timer.timeout.connect(self.request_frame)
        #: §15: what the last `paint_now` did (uploads, deferred, published)
        self.last_paint: Dict[str, Any] = {}
        self._collect: Optional[list] = None
        #: `gpu_memory`'s switches: `hidden` means "not running" (left the
        #: page, compare mode, a production run) -- never "nothing published"
        self.hidden = False
        from .. import gpu_memory
        self.smooth = gpu_memory.smooth_pixels()
        # ── set by the mount ──
        #: () -> (x0, x1, y0, y1) the ViewBox shows now, or None
        self.view_rect: Callable[[], Any] = lambda: None
        #: (core) -> "" or why it can never draw (unavailable / overflow)
        self.cannot_draw: Callable[[Any], str] = lambda _core: ""
        #: (slot) -> is this candidate's source still the one wanted?
        self.still_wanted: Callable[[CoreSlot], bool] = lambda _slot: True
        #: (slot) after a candidate became the published picture
        self.on_published: Optional[Callable[[CoreSlot], None]] = None
        #: (why) once, on the next event-loop turn after a terminal failure
        self.on_failure: Optional[Callable[[str], None]] = None
        self.on_smooth_changed: Optional[Callable[[], None]] = None
        #: () -> rows the current picture must draw (A9 records only)
        self.expected_channels: Callable[[], int] = lambda: 0
        #: S0-B: (slot, token) right after a switch's trial frame was
        #: published, inside the same paint -- the mount makes it the display
        self.on_switched: Optional[Callable[[CoreSlot, Any], None]] = None
        self._a9_pending = None
        self._drive_queued = False
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
        return self._published is not None

    def cores(self):
        """Every core held: current, candidate and standby."""
        return [s for s in (self.current, self.candidate, self.standby) if s is not None]

    def live_cores(self):
        """The cores that follow the camera and paint: current and candidate."""
        return [s for s in (self.current, self.candidate) if s is not None]

    # ── on the ViewBox (as `Step1CoreLayer.attach`) ───────────────────
    def attach(self, view_adapter) -> None:
        if self._initialized:
            raise Step0GlHostError("attach must happen before the widget is realised")
        self._attached_view = view_adapter
        self._attached_viewport = view_adapter.graphics.viewport()
        if self.parent() is not self._attached_viewport:
            self.setParent(self._attached_viewport)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._attached_viewport.installEventFilter(self)
        view_adapter.view_box.sigRangeChanged.connect(self._range_changed)
        view_adapter.view_box.sigResized.connect(self._sync_geometry)
        self._sync_geometry()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._attached_viewport and event.type() in (
                QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self._sync_geometry()
        return False

    def _sync_geometry(self, *_args) -> None:
        """Cover exactly the ViewBox, as `Step1CoreLayer._sync_geometry`."""
        if self._attached_viewport is None:
            return
        rect = self._attached_viewport.rect()
        view = self._attached_view
        view_box = getattr(view, "view_box", None)
        graphics = getattr(view, "graphics", None)
        if view_box is not None and graphics is not None:
            scene_rect = view_box.mapRectToScene(view_box.rect())
            top_left = graphics.mapFromScene(scene_rect.topLeft())
            width, height = int(round(scene_rect.width())), int(round(scene_rect.height()))
            if width > 0 and height > 0:
                rect = QtCore.QRect(top_left.x(), top_left.y(), width, height)
        self.setGeometry(rect)
        self.raise_()

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
            for slot in self.cores():                       # a reparent's new context
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
        if self._disposed or not self._initialized:
            return
        self.makeCurrent()
        try:
            for slot in self.cores():
                slot.session.release_gl()
                slot.attached = False
                slot.fbo = None
            self._published = None
        finally:
            self.doneCurrent()
        self._initialized = False

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        del width, height                                   # each paint reads the size

    # ── cores ─────────────────────────────────────────────────────────
    def add_candidate(self, core, sockets, identity) -> CoreSlot:
        """A new source's core; the previous candidate (if any) goes. Its GL
        names are made now, in this context. Raises on failure (the core
        is closed and released first)."""
        self.drop_candidate()
        slot = CoreSlot(core, None, identity)
        slot.session = CoreGlSession(core, sockets, owner=self, on_notice=self.request_frame,
                                     tag=self.a9_tag, channel_name=str, parent=self)
        if not self._initialized:
            slot.session.close()
            slot.session.forget_gl()
            slot.session.close_sockets()
            raise Step0GlHostError("the GL host is not initialized")
        self.makeCurrent()
        try:
            slot.session.attach_gl(self.context())
            slot.attached = True
        except Exception:
            slot.session.close()
            slot.session.forget_gl()
            slot.session.close_sockets()
            raise
        finally:
            self.doneCurrent()
        self.candidate = slot
        return slot

    def drop_candidate(self) -> None:
        slot, self.candidate = self.candidate, None
        if slot is not None:
            self._retire(slot)
        self._gate_prune()                       # its stall clock goes with it

    def drop_standby(self) -> None:
        slot, self.standby = self.standby, None
        if slot is not None:
            self._retire(slot)

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
        self._with_context(self._drop_published)
        self.published_view = None
        self.published_level = None
        self._a9_pending = None

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
            self._retire(slot)
        self._with_context(self._drop_published)
        self.published_view = None
        self.published_level = None
        self._a9_pending = None

    def _drop_published(self) -> None:
        self._published = None

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
        elif self.candidate is not None and not self._drive_queued:
            self._drive_queued = True
            QtCore.QTimer.singleShot(0, self._drive)

    def _drive(self) -> None:
        """The candidate's off-screen paint while nothing is published (the
        widget is hidden, so Qt does not call `paintGL`)."""
        self._drive_queued = False
        if (self._disposed or self._failed or self.hidden or self.isVisible()
                or self.candidate is None or not self._initialized):
            return
        view = self.view_rect()
        if view is None:
            return
        width, height = self._device_size()
        self.makeCurrent()
        try:
            self._advance_candidate(width, height, view)
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
        width, height = self._device_size()
        view = self.view_rect()
        if not self._failed and not self.hidden and not self._present_only and view is not None:
            self._paint_current(width, height, view)
            if not self._failed and self.candidate is not None:
                self._advance_candidate(width, height, view)
        self._present(width, height)
        if self._present_only:
            self.presented_now += 1

    def paint_now(self, present=True) -> bool:
        """§13.2 / 13.5: one paint of the current core, now, without asking
        Python for anything (what the cache held is already in); a prepared
        switch is published by it when it covers the view (§11.5) and is put
        on screen at once by a synchronous present-only repaint -- before the
        production run's thread starts and competes with this thread (§13.10).
        False when nothing could be painted."""
        if (not self._initialized or self._disposed or self._failed or self.hidden
                or self.current is None):
            return False
        view = self.view_rect()
        if view is None:
            return False
        width, height = self._device_size()
        count = self.published_count
        self._collect = []
        self.makeCurrent()
        try:
            self._paint_current(width, height, view, pump=False)
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

    def _paint_slot(self, slot: CoreSlot, width, height, view, trial=None, pump=True) -> Optional[dict]:
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
            why = self.cannot_draw(slot.core)
        if why:
            self._fail(why)
            return None
        again = session.again_ms(report)
        if again is not None and not self._again.isActive():
            self._again.start(again)
        session.note_reads()
        return report

    def request_switch(self, slot: CoreSlot, promote, yield_, token) -> None:
        """S0-B (§11.5): on `slot` (the current core), publish the picture
        with `promote` instead of `yield_` as soon as one paint of it covers
        the view; until then every paint shows the old rows at the current
        view (the old picture follows the camera)."""
        slot.switch = (tuple(promote), tuple(yield_), token)
        self._gate_prune()                       # a new switch waits from now (§14.7)
        self.request_frame()

    def _paint_current(self, width, height, view, pump=True) -> None:
        slot = self.current
        if slot is None:
            return
        if slot.switch is not None and self._try_switch(slot, width, height, view, pump):
            return
        if slot.switch is not None and slot.picture is None:
            # nothing of the old picture is left to draw (its source was
            # retired for a production run): the published one stays
            return
        if (slot.switch is not None and self._published is not None
                and self.published_view == (view, width, height)):
            # §14.6: a switch waits and the camera has not moved -- the old
            # picture stays exactly as published (no repaint of its rows,
            # which the planner may be thinning out meanwhile)
            return
        report = self._paint_slot(slot, width, height, view, pump=pump)
        if report is None:
            return
        # the pending picture is now complete: it is the published one
        slot.fbo, self._published = self._published, slot.fbo
        self.published_view = (view, width, height)
        self.published_level = _level_of(report, slot.core)
        self._a9_pending = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                                   getattr(self, "_a9_input_seq", None))

    def _try_switch(self, slot: CoreSlot, width, height, view, pump=True) -> bool:
        """One trial paint of the prepared switch: published when it drew,
        covered the view over the rows it composed (or composed none: the
        empty-picture exception) and is still wanted. False: the caller
        paints the old rows normally, in this same paint."""
        promote, yield_, token = slot.switch
        gate = self.published_level
        # the core gates at the finer of `gate` and this frame's admission
        # level (255: nothing on screen, the admission level alone)
        trial = (promote, yield_, NO_GATE if gate is None else gate)
        report = self._paint_slot(slot, width, height, view, trial, pump)
        if report is None:
            return True                          # a terminal failure: nothing more this paint
        level = _level_of(report, slot.core)
        # the final picture has no row at all (Marker and DAPI both off):
        # nothing to cover -- not "no tile drawn yet", which is not covered
        empty = int(self.expected_channels() or 0) == 0
        # a switch no longer wanted (a candidate replaces it) leaves the
        # gate's clocks alone (codex §15 code 2, P2-1)
        if not self.still_wanted(slot) or not (
                self._gate_open(report.get("trial_exact"), report.get("trial_admitted"), level, gate,
                                owner=_owner_of(slot)) or empty):
            self.switch_trials_missed = getattr(self, "switch_trials_missed", 0) + 1
            return False
        self._gate_done(_owner_of(slot))
        slot.switch = None
        slot.fbo, self._published = self._published, slot.fbo
        self.published_view = (view, width, height)
        self.published_level = level
        self.published_count += 1
        self._a9_pending = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                                   getattr(self, "_a9_input_seq", None))
        perf_trace.mark("step0.engine_switched", count=self.published_count,
                        token=str(token)[:120].replace(" ", "_"))
        if slot.adapter is not None:
            slot.adapter.pump_soon()             # the deferred tiles (§15.6)
        hook = self.on_switched
        if hook is not None:
            hook(slot, token)                    # the display follows now, inside this paint
        return True

    def _advance_candidate(self, width, height, view) -> bool:
        """Paint the candidate; publish it if that paint covered `view` at
        this size. True when it was published."""
        slot = self.candidate
        report = self._paint_slot(slot, width, height, view)
        if report is None:
            return False
        # every successful paint's drawing records and growth check; the
        # presentation records only if this picture is published
        drawn = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                        getattr(self, "_a9_input_seq", None))
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
            return False
        if required and not self._gate_open(exact, admitted, level, gate, owner=_owner_of(slot)):
            return False
        self._gate_done(_owner_of(slot))
        old, self.current, self.candidate = self.current, slot, None
        slot.fbo, self._published = self._published, slot.fbo
        if slot.fbo is not None and (slot.fbo.width(), slot.fbo.height()) != (width, height):
            slot.fbo = None
        self.published_view = (view, width, height)
        self.published_level = level
        self.published_count += 1
        self._a9_pending = drawn
        perf_trace.mark("step0.engine_published", count=self.published_count,
                        source=str(slot.identity)[:120].replace(" ", "_"))
        if slot.adapter is not None:
            slot.adapter.pump_soon()             # the deferred tiles (§15.6)
        if old is not None:                 # kept, paused, for going back (§10.5.6)
            old.core.pause()
            if old.adapter is not None:
                old.adapter.set_paused(True)
            self.drop_standby()
            self.standby = old
        hook = self.on_published
        if hook is not None:
            QtCore.QTimer.singleShot(0, lambda: hook(slot))
        return True

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
            self._gate_since.pop(owner, None)
            return False
        now = time.monotonic()
        waited = now - self._gate_since.setdefault(owner, now)
        if waited >= GATE_STALL_S:
            if not admitted:
                return False                     # the next tile's paint asks again
            perf_trace.mark("step0.gate_budget_limited", level=level, gate=gate)
            return True
        due = int((GATE_STALL_S - waited) * 1000) + 1
        if not self._gate_timer.isActive() or self._gate_timer.remainingTime() > due:
            self._gate_timer.start(due)
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
        since = self._gate_since.get(_owner_of(slot))
        stalled = since is not None and time.monotonic() - since >= GATE_STALL_S
        if stalled and slot.admitted is not None and slot.admitted != gate:
            return (gate, slot.admitted)
        return (gate,)

    def _gate_prune(self) -> None:
        """Only the pictures still waiting keep a clock: the current core's
        switch and the candidate."""
        live = {_owner_of(s) for s in (self.current, self.candidate) if s is not None}
        for owner in [o for o in self._gate_since if o not in live]:
            del self._gate_since[owner]
        if not self._gate_since:
            self._gate_timer.stop()

    def _gate_done(self, owner) -> None:
        self._gate_since.pop(owner, None)
        self._gate_prune()

    def _gate_clear(self) -> None:
        self._gate_since.clear()
        self._gate_timer.stop()

    def reset_gate_clock(self) -> None:
        """Running again after a pause (§14.7): a held-back picture's wait
        starts over."""
        self._gate_clear()

    def _present(self, width, height) -> None:
        """The published picture into the widget's own framebuffer (Qt
        redirects the default one there during `paintGL`); scaled when it
        was drawn for another size, until a paint at this size succeeds."""
        published = self._published
        if published is None:
            return
        QtGui.QOpenGLFramebufferObject.blitFramebuffer(
            None, QtCore.QRect(0, 0, width, height),
            published, QtCore.QRect(0, 0, published.width(), published.height()),
            GL_COLOR_BUFFER_BIT, GL_LINEAR)

    def _fail(self, why: str) -> None:
        """No publication after this, from any core; the mount's fallback
        on the next event-loop turn (never inside a paint)."""
        if self._failed:
            return
        self._failed = why
        perf_trace.mark("gpu.core_failed", why=why[:160].replace(" ", "_"))
        hook = self.on_failure
        if hook is not None:
            QtCore.QTimer.singleShot(0, lambda: hook(why))

    # ── memory (`gpu_memory`) ─────────────────────────────────────────
    def gpu_bytes(self) -> int:
        if self._disposed:
            return 0
        total = sum(int(s.core.gpu_bytes()) for s in self.cores())
        for fbo in [self._published] + [s.fbo for s in self.cores()]:
            if fbo is not None:
                total += int(fbo.width()) * int(fbo.height()) * 4
        return total

    def release_to_coarse(self) -> int:
        if self._disposed or not self._initialized:
            return 0
        got = []
        self._with_context(lambda: got.extend(int(s.core.release_to_coarse()) for s in self.cores()))
        return sum(got)

    def set_smooth(self, smooth: bool) -> None:
        if bool(smooth) == self.smooth:
            return
        self.smooth = bool(smooth)
        hook = self.on_smooth_changed
        if hook is not None:
            hook()
        self.request_frame()

    # ── measurement ───────────────────────────────────────────────────
    def a9_tag(self) -> str:
        return f"{id(self):x}"

    def _presented(self) -> None:
        if tile_trace.ON:
            tile_trace.stamp("present", frame=self.published_count, viewer=self.a9_tag())
            for slot in self.cores():
                slot.session.merge_trace()
        pending, self._a9_pending = self._a9_pending, None
        if pending is None:
            return
        frame, result, seq, _report = pending
        perf_trace.mark("gpu.present", frame=frame, seq=seq, viewer=self.a9_tag(), step=0)
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
        if self._attached_view is not None:
            for signal, slot in ((self._attached_view.view_box.sigRangeChanged, self._range_changed),
                                 (self._attached_view.view_box.sigResized, self._sync_geometry)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        if self._attached_viewport is not None:
            try:
                self._attached_viewport.removeEventFilter(self)
            except RuntimeError:
                pass
        self._disposed = True
        self._initialized = False
        return {"already_disposed": False}
