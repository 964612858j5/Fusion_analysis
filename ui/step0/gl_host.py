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
        #: the view and device size the published picture was drawn for
        self.published_view = None
        self.published_count = 0
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

    def drop_standby(self) -> None:
        slot, self.standby = self.standby, None
        if slot is not None:
            self._retire(slot)

    def park_current(self) -> None:
        """The Python picture takes over: the current core is paused and
        kept as the standby (the previous standby goes), the published
        picture is dropped."""
        slot, self.current = self.current, None
        if slot is not None:
            slot.core.pause()
            self.drop_standby()
            self.standby = slot
        self._with_context(self._drop_published)
        self.published_view = None
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
        self._a9_pending = None

    def _drop_published(self) -> None:
        self._published = None

    def _retire(self, slot: CoreSlot) -> None:
        if tile_trace.ON:
            slot.session.merge_trace()
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
        if not self._failed and not self.hidden and view is not None:
            self._paint_current(width, height, view)
            if not self._failed and self.candidate is not None:
                self._advance_candidate(width, height, view)
        self._present(width, height)

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

    def _paint_slot(self, slot: CoreSlot, width, height, view) -> Optional[dict]:
        """One paint of `slot` into its framebuffer; the report, or None
        after a terminal failure."""
        fbo = self._framebuffer(slot.fbo, width, height)
        if fbo is None:
            return None
        slot.fbo = fbo
        session = slot.session
        report = session.paint(fbo.handle(), width, height, view)
        # the core leaves its own target bound; ours is set again by Qt's
        # calls below and by the next paint (codex S0-A round 1)
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

    def _paint_current(self, width, height, view) -> None:
        slot = self.current
        if slot is None:
            return
        report = self._paint_slot(slot, width, height, view)
        if report is None:
            return
        # the pending picture is now complete: it is the published one
        slot.fbo, self._published = self._published, slot.fbo
        self.published_view = (view, width, height)
        self._a9_pending = slot.session.note_frame(report, int(self.expected_channels() or 0),
                                                   getattr(self, "_a9_input_seq", None))

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
        if required and not slot.core.view_covered(tuple(float(v) for v in view)):
            return False
        if not self.still_wanted(slot):
            return False
        old, self.current, self.candidate = self.current, slot, None
        slot.fbo, self._published = self._published, slot.fbo
        if slot.fbo is not None and (slot.fbo.width(), slot.fbo.height()) != (width, height):
            slot.fbo = None
        self.published_view = (view, width, height)
        self.published_count += 1
        self._a9_pending = drawn
        perf_trace.mark("step0.engine_published", count=self.published_count,
                        source=str(slot.identity)[:120].replace(" ", "_"))
        if old is not None:                 # kept, paused, for going back (§10.5.6)
            old.core.pause()
            self.drop_standby()
            self.standby = old
        hook = self.on_published
        if hook is not None:
            QtCore.QTimer.singleShot(0, lambda: hook(slot))
        return True

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
