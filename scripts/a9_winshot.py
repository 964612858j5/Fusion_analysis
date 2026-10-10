"""Save a window's current contents as PNG (block A9 §39 helper).
Usage: a9_winshot.py WINDOW_ID OUT.png"""
import ctypes
import sys

import numpy as np
from PIL import Image

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XGetImage.restype = ctypes.c_void_p
x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int)] + [("pad%d" % i, ctypes.c_long) for i in range(30)]


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p),
                ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int)]


win = int(sys.argv[1], 0)
dpy = x11.XOpenDisplay(None)
attr = XWindowAttributes()
x11.XGetWindowAttributes(ctypes.c_void_p(dpy), ctypes.c_ulong(win), ctypes.byref(attr))
img = x11.XGetImage(dpy, win, 0, 0, attr.width, attr.height, 0xFFFFFFFF, 2)
im = XImage.from_address(img)
raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
a = np.frombuffer(raw, np.uint8).reshape(im.height, im.bytes_per_line)[:, :im.width * 4]
a = a.reshape(im.height, im.width, 4)[:, :, [2, 1, 0]]
Image.fromarray(a).save(sys.argv[2])
print(im.width, im.height)
