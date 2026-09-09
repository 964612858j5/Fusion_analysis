"""Inject real X11 pointer events on the running display via XTEST (ctypes, no deps)."""
import ctypes, ctypes.util, time, sys

x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]

class Injector:
    def __init__(self, display=b":1"):
        self.d = x11.XOpenDisplay(display)
        if not self.d:
            raise RuntimeError("cannot open display %r" % display)

    def flush(self):
        x11.XFlush(ctypes.c_void_p(self.d))

    def move_abs(self, x, y):
        xtst.XTestFakeMotionEvent(ctypes.c_void_p(self.d), -1, int(x), int(y), 0)
        self.flush()

    def button(self, btn, down):
        xtst.XTestFakeButtonEvent(ctypes.c_void_p(self.d), int(btn), bool(down), 0)
        self.flush()

    def drag(self, x0, y0, dx, dy, steps=20, btn=2, step_sleep=0.016, hold_before_move=0.0):
        self.move_abs(x0, y0)
        time.sleep(0.15)
        self.button(btn, True)
        if hold_before_move:
            time.sleep(hold_before_move)
        for i in range(1, steps + 1):
            self.move_abs(x0 + dx * i / steps, y0 + dy * i / steps)
            time.sleep(step_sleep)
        self.button(btn, False)
        self.flush()

if __name__ == "__main__":
    inj = Injector(sys.argv[1].encode() if len(sys.argv) > 1 else b":1")
    print("display opened OK")


def window_under_pointer(display=b":1"):
    """(root_x, root_y, child_window_id) — for checking whose window we are
    about to click before injecting anything."""
    d = x11.XOpenDisplay(display)
    root = ctypes.c_ulong(x11.XDefaultRootWindow(ctypes.c_void_p(d)))
    child = ctypes.c_ulong(0); rroot = ctypes.c_ulong(0)
    rx = ctypes.c_int(); ry = ctypes.c_int(); wx = ctypes.c_int(); wy = ctypes.c_int()
    mask = ctypes.c_uint()
    x11.XQueryPointer(ctypes.c_void_p(d), root,
                      ctypes.byref(rroot), ctypes.byref(child),
                      ctypes.byref(rx), ctypes.byref(ry),
                      ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
    x11.XCloseDisplay(ctypes.c_void_p(d))
    return rx.value, ry.value, child.value


def ancestors(wid, display=b":1"):
    """wid plus every parent up to the root — a reparenting WM puts its frame
    between the root and our window, so the window under the pointer is the
    frame, not us."""
    d = x11.XOpenDisplay(display)
    chain = [wid]
    cur = wid
    for _ in range(32):
        root = ctypes.c_ulong(0); parent = ctypes.c_ulong(0)
        kids = ctypes.POINTER(ctypes.c_ulong)()
        n = ctypes.c_uint(0)
        ok = x11.XQueryTree(ctypes.c_void_p(d), ctypes.c_ulong(cur),
                            ctypes.byref(root), ctypes.byref(parent),
                            ctypes.byref(kids), ctypes.byref(n))
        if kids:
            x11.XFree(kids)
        if not ok or parent.value == 0:
            break
        chain.append(parent.value)
        if parent.value == root.value:
            break
        cur = parent.value
    x11.XCloseDisplay(ctypes.c_void_p(d))
    return chain
