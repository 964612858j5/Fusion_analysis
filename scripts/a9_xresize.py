"""Move/resize an X11 window (block A9 §39 helper).
Usage: a9_xresize.py WINDOW_ID X Y W H"""
import ctypes
import sys
x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
dpy = ctypes.c_void_p(x11.XOpenDisplay(None))
win, x, y, w, h = int(sys.argv[1], 0), *map(int, sys.argv[2:6])
x11.XMoveResizeWindow(dpy, ctypes.c_ulong(win), x, y, w, h)
x11.XSync(dpy, 0)
