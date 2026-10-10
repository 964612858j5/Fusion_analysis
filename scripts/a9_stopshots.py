"""§41: our window's picture at fixed times after each gesture's last
injection (is the switch to the sharp picture visible?).
Usage: a9_stopshots.py INJECTIONS_JSONL WINDOW_ID OUTDIR NGESTURES [X,Y,W,H]"""
import ctypes
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DELAYS_MS = (20, 60, 100, 160, 250, 400, 800, 1500)


x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XGetImage.restype = ctypes.c_void_p
x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
libc = ctypes.CDLL("libc.so.6")


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p),
                ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int)]


DPY = None


def grab(win, x, y, w, h):
    global DPY
    if DPY is None:
        DPY = x11.XOpenDisplay(None)
    img = x11.XGetImage(DPY, int(win, 0), x, y, w, h, 0xFFFFFFFF, 2)
    im = XImage.from_address(img)
    raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
    libc.free(ctypes.c_void_p(im.data))
    libc.free(ctypes.c_void_p(img))
    a = np.frombuffer(raw, np.uint8).reshape(h, im.bytes_per_line)[:, :w * 4].reshape(h, w, 4)
    return a[:, :, [2, 1, 0]].copy()


def injections(path):
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except OSError:
        return []


def main(inj_path, win, outdir, ngest, region="0,0,1307,788"):
    os.makedirs(outdir, exist_ok=True)
    rx, ry, rw, rh = map(int, region.split(","))
    taken = []
    for g in range(1, int(ngest) + 1):
        # gesture g has started when seq==1 has been seen g times
        while sum(1 for r in injections(inj_path) if r["seq"] == 1) < g:
            time.sleep(0.002)
        # ... and has stopped when no injection came for 50 ms
        while True:
            last = injections(inj_path)[-1]["t"]
            if time.monotonic() - last > 0.05:
                break
            time.sleep(0.003)
        for d in DELAYS_MS:
            target = last + d / 1000.0
            while time.monotonic() < target:
                time.sleep(0.0005)
            t = time.monotonic()
            taken.append((f"g{g}_{d:03d}ms.png", grab(win, rx, ry, rw, rh)))
            print(g, d, round((t - last) * 1000, 1), round((time.monotonic() - t) * 1000, 1), flush=True)
    for name, a in taken:
        Image.fromarray(a).save(os.path.join(outdir, name))


if __name__ == "__main__":
    main(*sys.argv[1:6])
