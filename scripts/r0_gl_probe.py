"""v16_5 R0: the Rust core drawing inside Qt's own GL context (plan §3).

    r0_gl_probe.py SCENARIO        -> one JSON line on stdout, exit 0/1

ONE scenario per process (the WSL D3D12 driver allows only ~5 GL contexts
per process; keeping the habit now lets these run on the laptop later).
Scenarios: first_show, resize, dpi, screen_move, reparent, hide_show,
close_order, coexist. Each one checks, on real widgets:

* the picture: Rust draws a 2x2 R8 texture over the whole widget; the four
  quadrant centres must have the expected colours, at device-pixel size;
* GL state: a full glGet* snapshot (test-only, synchronous) before and
  after every Rust paint must be equal;
* lifetime: names released in `aboutToBeDestroyed` with the context
  current, rebuilt in the next initializeGL, no GL error.

Display: DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority, xcb.
Measurement/test only; no product code path.
"""

import json
import os
import socket
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets

import fusion_viewer_core as core

TEXTURE = bytes([0, 255, 85, 170])            # rows: [0, 255], [85, 170]
COLOR = (1.0, 0.6, 0.2)
#: quadrant -> texel value (top row of the screen = first texture row)
QUADRANTS = {"top_left": 0, "top_right": 255, "bottom_left": 85, "bottom_right": 170}


# ── a full GL state snapshot (test-only; synchronous glGet*) ─────────────

def _gl():
    import OpenGL
    OpenGL.ERROR_CHECKING = False
    from OpenGL import GL
    return GL


def snapshot():
    GL = _gl()
    s = {}

    def iv(name, n=1):
        v = GL.glGetIntegerv(getattr(GL, name))
        s[name] = [int(x) for x in (v.flatten() if hasattr(v, "flatten") else [v])][:n] if n > 1 else int(v)

    def fv(name, n):
        v = GL.glGetFloatv(getattr(GL, name))
        s[name] = [round(float(x), 6) for x in v.flatten()][:n]

    for name in ("GL_DRAW_FRAMEBUFFER_BINDING", "GL_READ_FRAMEBUFFER_BINDING", "GL_RENDERBUFFER_BINDING",
                 "GL_CURRENT_PROGRAM", "GL_VERTEX_ARRAY_BINDING", "GL_ARRAY_BUFFER_BINDING",
                 "GL_ELEMENT_ARRAY_BUFFER_BINDING", "GL_PIXEL_PACK_BUFFER_BINDING",
                 "GL_PIXEL_UNPACK_BUFFER_BINDING", "GL_UNIFORM_BUFFER_BINDING", "GL_ACTIVE_TEXTURE",
                 "GL_BLEND_SRC_RGB", "GL_BLEND_DST_RGB", "GL_BLEND_SRC_ALPHA", "GL_BLEND_DST_ALPHA",
                 "GL_BLEND_EQUATION_RGB", "GL_BLEND_EQUATION_ALPHA", "GL_STENCIL_FUNC",
                 "GL_STENCIL_REF", "GL_STENCIL_VALUE_MASK", "GL_STENCIL_WRITEMASK", "GL_STENCIL_FAIL",
                 "GL_STENCIL_PASS_DEPTH_FAIL", "GL_STENCIL_PASS_DEPTH_PASS", "GL_DEPTH_FUNC",
                 "GL_DEPTH_WRITEMASK", "GL_CULL_FACE_MODE", "GL_FRONT_FACE", "GL_UNPACK_ALIGNMENT",
                 "GL_UNPACK_ROW_LENGTH", "GL_UNPACK_SKIP_ROWS", "GL_UNPACK_SKIP_PIXELS",
                 "GL_PACK_ALIGNMENT", "GL_PACK_ROW_LENGTH", "GL_DRAW_BUFFER", "GL_READ_BUFFER"):
        iv(name)
    iv("GL_VIEWPORT", 4)
    iv("GL_SCISSOR_BOX", 4)
    iv("GL_COLOR_WRITEMASK", 4)
    fv("GL_COLOR_CLEAR_VALUE", 4)
    fv("GL_BLEND_COLOR", 4)
    for cap in ("GL_BLEND", "GL_SCISSOR_TEST", "GL_STENCIL_TEST", "GL_DEPTH_TEST", "GL_CULL_FACE",
                "GL_FRAMEBUFFER_SRGB", "GL_DITHER", "GL_MULTISAMPLE", "GL_PRIMITIVE_RESTART",
                "GL_RASTERIZER_DISCARD", "GL_POLYGON_OFFSET_FILL"):
        s[cap] = bool(GL.glIsEnabled(getattr(GL, cap)))
    active = s["GL_ACTIVE_TEXTURE"]
    units = min(16, int(GL.glGetIntegerv(GL.GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS)))
    for unit in range(units):
        GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
        for target in ("GL_TEXTURE_BINDING_2D", "GL_TEXTURE_BINDING_2D_ARRAY", "GL_TEXTURE_BINDING_3D",
                       "GL_TEXTURE_BINDING_BUFFER", "GL_SAMPLER_BINDING"):
            s[f"{target}[{unit}]"] = int(GL.glGetIntegerv(getattr(GL, target)))
    GL.glActiveTexture(active)
    return s


def diff(a, b):
    return {k: (a[k], b.get(k)) for k in a if a[k] != b.get(k)}


# ── another painter in the same context (stands in for Step3's labels) ───

class OtherPainter:
    """PyOpenGL: its own program, VAO, VBO and blending; draws a 50 %
    green square over the centre, leaving GL defaults behind."""

    VERT = "#version 330 core\nlayout(location=0) in vec2 p;\nvoid main(){gl_Position=vec4(p*0.25,0.0,1.0);}\n"
    FRAG = "#version 330 core\nout vec4 o;\nvoid main(){o=vec4(0.0,1.0,0.0,0.5);}\n"

    def __init__(self):
        GL = _gl()
        from OpenGL.GL import shaders
        self.program = shaders.compileProgram(shaders.compileShader(self.VERT, GL.GL_VERTEX_SHADER),
                                              shaders.compileShader(self.FRAG, GL.GL_FRAGMENT_SHADER))
        import numpy as np
        self.vao = GL.glGenVertexArrays(1)
        self.vbo = GL.glGenBuffers(1)
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], np.float32)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, quad.nbytes, quad, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 8, None)
        GL.glBindVertexArray(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

    def paint(self):
        GL = _gl()
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glUseProgram(self.program)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ZERO)
        GL.glDisable(GL.GL_BLEND)

    def release(self):
        GL = _gl()
        GL.glDeleteProgram(self.program)
        GL.glDeleteVertexArrays(1, [self.vao])
        GL.glDeleteBuffers(1, [self.vbo])


# ── the widget ───────────────────────────────────────────────────────────

class ProbeWidget(QtWidgets.QOpenGLWidget):
    def __init__(self, parent=None, other=False):
        super().__init__(parent)
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self.core = None
        self.other = None
        self.want_other = other
        self.events = []
        self.state_diffs = []
        self.gl_errors = []
        self.paints = 0
        self.released_with_current = []
        #: negative control only: leave a state dirty and draw a wrong colour
        self.inject_fault = False

    def initializeGL(self):  # noqa: N802
        ctx = self.context()
        ctx.aboutToBeDestroyed.connect(self.cleanup)

        def get_proc(name):
            ptr = ctx.getProcAddress(QtCore.QByteArray(name))
            return int(ptr) if ptr else 0

        self.core = core.GlCore(get_proc)
        self.core.upload_r8(TEXTURE, 2, 2)
        if self.want_other:
            self.other = OtherPainter()
        self.events.append(("init", id(ctx), self.core.live_names))

    def resizeGL(self, w, h):  # noqa: N802
        self.events.append(("resize", w, h, round(self.devicePixelRatioF(), 3)))

    def paintGL(self):  # noqa: N802
        ratio = self.devicePixelRatioF()
        w, h = int(round(self.width() * ratio)), int(round(self.height() * ratio))
        before = snapshot()
        color = [0.2, 0.6, 1.0] if self.inject_fault else list(COLOR)
        self.core.paint(self.defaultFramebufferObject(), w, h, color)
        if self.inject_fault:
            _gl().glEnable(_gl().GL_SCISSOR_TEST)
        after = snapshot()
        d = diff(before, after)
        if d:
            self.state_diffs.append(d)
        if self.other is not None:
            self.other.paint()
            d2 = diff(before, snapshot())
            if d2:
                self.state_diffs.append({"after_other": d2})
        err = self.core.gl_error()
        if err:
            self.gl_errors.append(err)
        self.paints += 1

    def cleanup(self):
        """`aboutToBeDestroyed`: release our names in that context."""
        ctx = self.context()
        self.makeCurrent()
        current = QtGui.QOpenGLContext.currentContext() is ctx and ctx is not None
        if self.core is not None:
            self.core.release()
            self.released_with_current.append(current)
        if self.other is not None:
            self.other.release()
            self.other = None
        self.doneCurrent()
        self.events.append(("released", current))

    def check_picture(self):
        """Grab the framebuffer and test the four quadrant centres."""
        image = self.grabFramebuffer()
        ratio = self.devicePixelRatioF()
        want = (int(round(self.width() * ratio)), int(round(self.height() * ratio)))
        got = (image.width(), image.height())
        bad = {}
        for name, value in QUADRANTS.items():
            x = got[0] // 4 if "left" in name else 3 * got[0] // 4
            y = got[1] // 4 if "top" in name else 3 * got[1] // 4
            if self.other is not None:                     # off the centre square
                x = 2 if "left" in name else got[0] - 3
                y = 2 if "top" in name else got[1] - 3
            c = QtGui.QColor(image.pixel(x, y))
            expect = [round(255 * k * value / 255.0) for k in COLOR]
            have = [c.red(), c.green(), c.blue()]
            if max(abs(a - b) for a, b in zip(have, expect)) > 2:
                bad[name] = {"have": have, "expect": expect}
        if self.other is not None:                         # the centre: green over top-right/left
            c = QtGui.QColor(image.pixel(got[0] // 2 - 2, got[1] // 2 - 2))   # top-left texel 0
            expect = [0, 128, 0]
            have = [c.red(), c.green(), c.blue()]
            if max(abs(a - b) for a, b in zip(have, expect)) > 3:
                bad["centre_other"] = {"have": have, "expect": expect}
        return {"size_ok": got == want, "size": got, "want": want, "bad": bad}


def pump(app, ms=150):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)


def window(app, other=False, size=(200, 150)):
    top = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(top)
    lay.setContentsMargins(0, 0, 0, 0)
    w = ProbeWidget(top, other=other)
    lay.addWidget(w)
    top.resize(*size)
    top.show()
    pump(app)
    return top, w


def common(w):
    return {"state_diffs": w.state_diffs[:3], "n_state_diffs": len(w.state_diffs),
            "gl_errors": w.gl_errors, "paints": w.paints}


# ── scenarios ────────────────────────────────────────────────────────────

def s_first_show(app):
    top, w = window(app)
    pic = w.check_picture()
    return {"picture": pic, "info": w.core.info, **common(w),
            "ok": pic["size_ok"] and not pic["bad"] and not w.state_diffs and not w.gl_errors}


def s_resize(app):
    top, w = window(app)
    checks = []
    for size in ((640, 480), (37, 91), (1200, 700), (201, 151)):
        top.resize(*size)
        pump(app)
        checks.append(w.check_picture())
    ok = all(c["size_ok"] and not c["bad"] for c in checks) and not w.state_diffs and not w.gl_errors
    return {"checks": checks, **common(w), "ok": ok}


def s_dpi(app):
    """Run with QT_SCALE_FACTOR=2 (the test does): device pixels = 2x."""
    top, w = window(app)
    pic = w.check_picture()
    ratio = w.devicePixelRatioF()
    return {"ratio": ratio, "picture": pic, **common(w),
            "ok": pic["size_ok"] and not pic["bad"] and not w.state_diffs and not w.gl_errors}


def s_screen_move(app):
    """Run with QT_SCREEN_SCALE_FACTORS giving each screen its own ratio:
    move the window from the first screen to the second at run time."""
    screens = app.screens()
    if len(screens) < 2:
        return {"ok": None, "skipped": "one screen only"}
    top, w = window(app)
    first = (w.devicePixelRatioF(), w.check_picture())
    target = screens[1].geometry()
    top.move(target.x() + 20, target.y() + 20)
    pump(app, 800)
    second = (w.devicePixelRatioF(), w.check_picture())
    top.move(screens[0].geometry().x() + 20, screens[0].geometry().y() + 20)
    pump(app, 800)
    third = (w.devicePixelRatioF(), w.check_picture())
    pics = [first, second, third]
    ok = (first[0] != second[0] and third[0] == first[0]
          and all(p[1]["size_ok"] and not p[1]["bad"] for p in pics)
          and not w.state_diffs and not w.gl_errors)
    return {"ratios": [p[0] for p in pics], "pictures": [p[1] for p in pics],
            "screen_of_window": top.windowHandle().screen().name(), **common(w), "ok": ok}


def s_reparent(app):
    top, w = window(app)
    inits_before = sum(1 for e in w.events if e[0] == "init")
    other_top = QtWidgets.QWidget()
    other_top.resize(320, 240)
    lay = QtWidgets.QVBoxLayout(other_top)
    lay.setContentsMargins(0, 0, 0, 0)
    w.setParent(other_top)
    lay.addWidget(w)
    other_top.show()
    w.show()
    pump(app, 300)
    pic = w.check_picture()
    inits = [e for e in w.events if e[0] == "init"]
    released = [e for e in w.events if e[0] == "released"]
    ok = (len(inits) == inits_before + 1 and len(released) >= 1 and all(w.released_with_current)
          and inits[-1][1] != inits[0][1] and w.core.live_names == 4
          and pic["size_ok"] and not pic["bad"] and not w.state_diffs and not w.gl_errors)
    return {"events": w.events, "released_with_current": w.released_with_current,
            "picture": pic, **common(w), "ok": ok}


def s_hide_show(app):
    top, w = window(app)
    top.hide()
    pump(app, 300)
    top.show()
    pump(app, 300)
    pic = w.check_picture()
    inits = [e for e in w.events if e[0] == "init"]
    ok = pic["size_ok"] and not pic["bad"] and not w.state_diffs and not w.gl_errors
    return {"inits": len(inits), "context_rebuilt": len(inits) > 1, "events": w.events,
            "picture": pic, **common(w), "ok": ok}


def s_close_order(app):
    """The shutdown order of plan §3 with a live notice socket and a
    notifier storm running: stop notices -> (reader threads: none in R0)
    -> release GL in the current context -> destroy the widget -> close
    the sockets. Nothing may fail, nothing may be left."""
    top, w = window(app)
    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    notifier = core.Notifier(wsock.fileno())
    received = [0]

    def readable():
        try:                                       # drain -> ack -> work
            while rsock.recv(4096):
                received[0] += 1
        except BlockingIOError:
            pass
        notifier.ack()
        w.update()

    sn = QtCore.QSocketNotifier(rsock.fileno(), QtCore.QSocketNotifier.Read)
    sn.activated.connect(readable)
    notifier.storm(4, 2000, 50)
    pump(app, 300)
    steps = []
    notifier.close()
    steps.append("notices stopped")
    sn.setEnabled(False)
    w.makeCurrent()
    w.core.release()
    live = w.core.live_names
    w.doneCurrent()
    steps.append("gl released")
    top.close()
    top.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    pump(app, 100)
    steps.append("widget destroyed")
    rsock.close()
    wsock.close()
    steps.append("sockets closed")
    writes, coalesced = notifier.counts()
    ok = live == 0 and not w.gl_errors and received[0] > 0 and writes > 0
    return {"steps": steps, "live_after_release": live, "notices_written": writes,
            "notices_coalesced": coalesced, "wakeups": received[0], "gl_errors": w.gl_errors, "ok": ok}


def s_coexist(app):
    top, w = window(app, other=True, size=(240, 180))
    pic = w.check_picture()
    return {"picture": pic, **common(w),
            "ok": pic["size_ok"] and not pic["bad"] and not w.state_diffs and not w.gl_errors}


def s_fault_control(app):
    """Negative control: the same checks must FAIL when the paint leaves a
    state dirty and draws the wrong colour. ok = both faults were caught."""
    top, w = window(app)
    w.inject_fault = True
    w.update()
    pump(app)
    pic = w.check_picture()
    caught_state = any("GL_SCISSOR_TEST" in d for d in w.state_diffs)
    caught_picture = bool(pic["bad"])
    return {"caught_state": caught_state, "caught_picture": caught_picture,
            "ok": caught_state and caught_picture}


SCENARIOS = {name[2:]: fn for name, fn in globals().items() if name.startswith("s_")}


def main():
    name = sys.argv[1]
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["r0_gl_probe"])
    try:
        result = SCENARIOS[name](app)
    except Exception as exc:                                # noqa: BLE001 -- reported, exit 1
        import traceback
        result = {"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-1500:]}
    result["scenario"] = name
    print(json.dumps(result, default=str), flush=True)
    os._exit(0 if result.get("ok") in (True, None) else 1)


if __name__ == "__main__":
    main()
