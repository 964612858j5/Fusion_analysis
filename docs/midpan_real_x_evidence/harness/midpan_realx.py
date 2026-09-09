"""Real-X11 middle-drag evidence harness.

Runs the REAL TissueNavigatorPopup + OverviewPanel on the running X server,
and drives the middle button with XTEST (real X pointer events through the
platform plugin), not QApplication.sendEvent.  Everything it prints is a
measurement; it fixes nothing.

Usage:  DISPLAY=:1 python3 midpan_realx.py <group> [outfile]
  groups: cold  warm  raise-cold  raise-warm
"""
import ctypes, os, sys, time, threading

os.environ["BLOCK01_MIDPAN_DEBUG"] = "1"
sys.path.insert(0, os.environ.get("BLOCK01_PARENT",
                               "/sda1/Fusion/analysis_pipline"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from block01_v14.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
from xinject import Injector, window_under_pointer, ancestors

GROUP = sys.argv[1] if len(sys.argv) > 1 else "cold"
WIN_X = int(os.environ.get("HARNESS_X", 2300))
WIN_Y = int(os.environ.get("HARNESS_Y", 120))
WIN_W, WIN_H = 560, 620

T0 = time.monotonic()
def ms(): return (time.monotonic() - T0) * 1000.0
def log(kind, msg): print("%9.2f %-14s %s" % (ms(), kind, msg), flush=True)

SLIDE_H = int(os.environ.get("HARNESS_SLIDE_H", 2048))
SLIDE_W = int(os.environ.get("HARNESS_SLIDE_W", 1024))
DS = int(os.environ.get("HARNESS_DS", 32))

class Loader:
    _CHANNELS = ["DAPI", "CD3", "CD20"]
    shape = (SLIDE_H, SLIDE_W)
    def channel_names(self): return list(self._CHANNELS)
    @property
    def ch_map(self): return {c: i for i, c in enumerate(self._CHANNELS)}
    def overview_downsample(self): return DS
    def read_region(self, ch, y0, y1, x0, x1, **kw):
        return np.zeros((y1 - y0, x1 - x0), np.float32)
    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        h, w = SLIDE_H // ds, SLIDE_W // ds
        return np.linspace(0, 1000, h * w, dtype=np.float32).reshape(h, w)

# ── instrumentation ──────────────────────────────────────────────────────
class Spy(QtCore.QObject):
    """Every mouse/activation/paint event the application sees, with the
    object it was delivered to and Qt's own event timestamp."""
    NAMES = {QtCore.QEvent.MouseButtonPress: "PRESS",
             QtCore.QEvent.MouseMove: "MOVE",
             QtCore.QEvent.MouseButtonRelease: "REL",
             QtCore.QEvent.WindowActivate: "WIN-ACT",
             QtCore.QEvent.WindowDeactivate: "WIN-DEACT",
             QtCore.QEvent.Paint: "PAINT"}
    def __init__(self, viewport):
        super().__init__()
        self._vp = viewport
    def eventFilter(self, obj, e):
        t = e.type()
        n = self.NAMES.get(t)
        if n is None:
            return False
        if t == QtCore.QEvent.Paint:
            if obj is not self._vp:
                return False       # only the plot's own viewport repaints matter
            t0 = time.monotonic()
            QtWidgets.QApplication.sendEvent(obj, e) if False else None
            log("EV/PAINT", "obj=%s (start)" % type(obj).__name__)
            return False
        extra = ""
        if t in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseMove,
                 QtCore.QEvent.MouseButtonRelease):
            try:
                extra = " btn=%d buttons=%d qt_ts=%d global=%s" % (
                    int(e.button()), int(e.buttons()), e.timestamp(),
                    (e.globalX(), e.globalY()))
            except Exception as exc:
                extra = " <%s>" % exc
        log("EV/" + n, "obj=%s%s" % (type(obj).__name__, extra))
        return False

def instrument_move(panel):
    """Time the move handler and record whether the camera actually moved."""
    orig = type(panel)._middle_pan_move
    def wrapped(self, event):
        before = self.vb.viewRange()
        t0 = time.monotonic()
        out = orig(self, event)
        dt = (time.monotonic() - t0) * 1000.0
        after = self.vb.viewRange()
        log("MOVE-HANDLER",
            "took=%.2fms moved=%s before_x=(%.2f,%.2f) after_x=(%.2f,%.2f) ret=%s"
            % (dt, before != after, before[0][0], before[0][1],
               after[0][0], after[0][1], out))
        return out
    type(panel)._middle_pan_move = wrapped

class GuiCall(QtCore.QObject):
    """The injector runs on its own thread; a widget call must be made on the
    GUI thread, so it is emitted as a signal and executed in the receiver."""
    fire = QtCore.pyqtSignal()
    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.fire.connect(self._run, QtCore.Qt.QueuedConnection)
    @QtCore.pyqtSlot()
    def _run(self):
        self._fn()


class LoopGap(QtCore.QObject):
    """A 5 ms heartbeat: a gap means the GUI thread was busy, not the mouse."""
    def __init__(self, threshold_ms=25.0):
        super().__init__()
        self._last = time.monotonic()
        self._thr = threshold_ms
        self._t = QtCore.QTimer(self)
        self._t.setInterval(5)
        self._t.timeout.connect(self._tick)
        self._t.start()
    def _tick(self):
        now = time.monotonic()
        gap = (now - self._last) * 1000.0
        self._last = now
        if gap > self._thr:
            log("LOOP-GAP", "gui thread unavailable for %.1fms" % gap)

# ── build ────────────────────────────────────────────────────────────────
app = QtWidgets.QApplication(sys.argv)
loader = Loader()
popup = TissueNavigatorPopup(loader, "DAPI")
popup.setGeometry(WIN_X, WIN_Y, WIN_W, WIN_H)
popup.set_overview_context(loader=loader, nuc_ch="DAPI", rois=[], patches=[])
popup.show()
panel = popup.overview
app.processEvents()
time.sleep(0.4)
app.processEvents()

spy = Spy(panel.gview.viewport())
app.installEventFilter(spy)
instrument_move(panel)
gaps = LoopGap()

log("SETUP", "group=%s popup=%s panel=%s viewport=%s"
    % (GROUP, hex(id(popup)), hex(id(panel)), hex(id(panel.gview.viewport()))))
log("SETUP", "viewRange=%s active=%s" % (panel.vb.viewRange(), popup.isActiveWindow()))
try:
    img = panel.img_item.image
    log("SETUP", "thumbnail array shape=%s dtype=%s" % (getattr(img, "shape", None), getattr(img, "dtype", None)))
except Exception as exc:
    log("SETUP", "thumbnail unavailable: %s" % exc)

# ── the gestures ─────────────────────────────────────────────────────────
def centre_of_viewport():
    vp = panel.gview.viewport()
    g = vp.mapToGlobal(vp.rect().center())
    return g.x(), g.y()

def guard(x, y):
    """Never inject unless the window under the cursor is ours."""
    inj_x, inj_y, child = window_under_pointer()
    wid = int(popup.winId())
    chain = ancestors(wid)
    ours = child in chain
    log("GUARD", "pointer=(%d,%d) child=%s our_chain=%s ours=%s"
        % (inj_x, inj_y, hex(child), [hex(c) for c in chain], ours))
    return ours

def gesture(inj, label, hold_before_move):
    x, y = centre_of_viewport()
    inj.move_abs(x, y)
    time.sleep(0.25)
    if not guard(x, y):
        log("ABORT", "%s: cursor is not over our window; injecting nothing" % label)
        return
    log("GESTURE", "%s start at (%d,%d) hold_before_move=%.2fs range=%s"
        % (label, x, y, hold_before_move, panel.vb.viewRange()[0]))
    r0 = [list(r) for r in panel.vb.viewRange()]
    inj.button(2, True)
    if hold_before_move:
        time.sleep(hold_before_move)
    for i in range(1, 13):
        inj.move_abs(x + 8 * i, y + 4 * i)
        time.sleep(0.016)
    inj.button(2, False)
    time.sleep(0.35)
    r1 = [list(r) for r in panel.vb.viewRange()]
    log("GESTURE", "%s end range=%s panned=%s" % (label, r1[0], r0 != r1))

def run():
    time.sleep(1.2)
    inj = Injector(b":1")
    if GROUP in ("raise-cold", "raise-warm"):
        # exactly what Step1 does: MainWindow._show_tissue_navigator ->
        # step0_page._bring_to_front
        log("STEP1", "bring_to_front: setWindowState|WindowActive, show, raise_, activateWindow")
        bring.fire.emit()
        time.sleep(0.6)
    if GROUP in ("warm", "raise-warm"):
        x, y = centre_of_viewport()
        inj.move_abs(x, y); time.sleep(0.2)
        if guard(x, y):
            log("WARMUP", "left click inside the preview")
            inj.button(1, True); time.sleep(0.08); inj.button(1, False)
            time.sleep(0.5)
    gesture(inj, "%s/immediate" % GROUP, 0.0)
    time.sleep(0.6)
    gesture(inj, "%s/held-1s" % GROUP, 1.0)
    time.sleep(0.5)
    log("DONE", "quitting")
    QtCore.QMetaObject.invokeMethod(app, "quit", QtCore.Qt.QueuedConnection)

def _bring_to_front():
    # verbatim ui/step0/step0_page.py::_bring_to_front
    popup.setWindowState(
        (popup.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    popup.show()
    popup.raise_()
    popup.activateWindow()
    log("STEP1", "bring_to_front done active=%s" % popup.isActiveWindow())

bring = GuiCall(_bring_to_front)

threading.Thread(target=run, daemon=True).start()
app.exec_()
