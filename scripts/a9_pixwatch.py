"""App-neutral frame watcher (block A9 §39): records, on CLOCK_MONOTONIC,
every moment a few rows of a window's pixels change. Works the same for
any X11 program (our viewer, Odon): it reads the window's own contents
(XGetImage; under a compositor that is the window's last presented frame).

Usage: a9_pixwatch.py WINDOW_ID X Y W ROWS SECONDS OUT
  X, Y, W: the first row's offset and width inside the window; ROWS rows
  are sampled, spread evenly over the next 400 px. One JSON line per
  change: {"t": monotonic, "n": capture index}; the last line holds the
  capture count and mean capture interval.
"""
import ctypes
import json
import sys
import time

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XGetImage.restype = ctypes.c_void_p
x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
x11.XDestroyImage = None


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p),
                ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int)]


libc = ctypes.CDLL("libc.so.6")
ZPIXMAP, ALLPLANES = 2, 0xFFFFFFFF


def main():
    win = int(sys.argv[1], 0)
    x, y, w, rows = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    seconds, out = float(sys.argv[6]), sys.argv[7]
    dpy = x11.XOpenDisplay(None)
    ys = [y + (400 * k) // max(1, rows - 1) for k in range(rows)]
    last, n, t_start = None, 0, time.monotonic()
    with open(out, "w", buffering=1) as f:
        while time.monotonic() - t_start < seconds:
            parts = []
            for yy in ys:
                img = x11.XGetImage(dpy, win, x, yy, w, 1, ALLPLANES, ZPIXMAP)
                if not img:
                    continue
                im = XImage.from_address(img)
                parts.append(ctypes.string_at(im.data, im.bytes_per_line))
                libc.free(ctypes.c_void_p(im.data))
                libc.free(ctypes.c_void_p(img))
            t = time.monotonic()
            frame = b"".join(parts)
            n += 1
            if last is not None and frame != last:
                f.write(json.dumps({"t": t, "n": n}) + "\n")
            last = frame
        f.write(json.dumps({"captures": n, "interval_ms": (time.monotonic() - t_start) * 1000.0 / max(1, n)}) + "\n")


if __name__ == "__main__":
    main()
