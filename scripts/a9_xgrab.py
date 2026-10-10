"""§44 step 0: a fast window grab for stop shots (MIT-SHM).

A full-canvas XGetImage (1307x788) took 19-42 ms -- longer than the gaps
between the shots that matter (16, 33, 50 ms after the stop). XShmGetImage
copies the window into a shared segment instead of through the X socket;
the copy out of it is one memcpy. Falls back to XGetImage where MIT-SHM is
missing (e.g. a remote display).

    g = Grabber(window_id_str, x, y, w, h)
    rgb = g.grab()          # (h, w, 3) uint8, a private copy
"""
import ctypes

import numpy as np

x11 = ctypes.CDLL("libX11.so.6")
libc = ctypes.CDLL("libc.so.6", use_errno=True)
try:
    xext = ctypes.CDLL("libXext.so.6")
except OSError:                                   # pragma: no cover
    xext = None

x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XGetImage.restype = ctypes.c_void_p
x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
x11.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
libc.shmget.restype = ctypes.c_int
libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
libc.shmat.restype = ctypes.c_void_p
libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
libc.shmdt.argtypes = [ctypes.c_void_p]
libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

ZPIXMAP, ALLPLANES = 2, 0xFFFFFFFF
IPC_PRIVATE, IPC_CREAT, IPC_RMID = 0, 0o1000, 0


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p),
                ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int)]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
                ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong), ("class_", ctypes.c_int),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


class XShmSegmentInfo(ctypes.Structure):
    _fields_ = [("shmseg", ctypes.c_ulong), ("shmid", ctypes.c_int),
                ("shmaddr", ctypes.c_void_p), ("readOnly", ctypes.c_int)]


class Grabber:
    def __init__(self, win, x, y, w, h):
        self.dpy = x11.XOpenDisplay(None)
        self.win = int(win, 0) if isinstance(win, str) else int(win)
        self.x, self.y, self.w, self.h = x, y, w, h
        self.shm = None
        try:
            self._setup_shm()
        except Exception as exc:                  # noqa: BLE001 -- fall back
            print("a9_xgrab: no MIT-SHM, using XGetImage:", exc, flush=True)
            self.shm = None

    def _setup_shm(self):
        if xext is None or not xext.XShmQueryExtension(ctypes.c_void_p(self.dpy)):
            raise RuntimeError("MIT-SHM missing")
        attrs = XWindowAttributes()
        x11.XGetWindowAttributes(self.dpy, self.win, ctypes.byref(attrs))
        info = XShmSegmentInfo()
        xext.XShmCreateImage.restype = ctypes.c_void_p
        xext.XShmCreateImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_uint, ctypes.c_uint]
        img = xext.XShmCreateImage(self.dpy, attrs.visual, attrs.depth, ZPIXMAP, None,
                                   ctypes.byref(info), self.w, self.h)
        if not img:
            raise RuntimeError("XShmCreateImage failed")
        im = XImage.from_address(img)
        size = im.bytes_per_line * im.height
        info.shmid = libc.shmget(IPC_PRIVATE, size, IPC_CREAT | 0o666)   # the X server runs as another user
        if info.shmid < 0:
            raise RuntimeError("shmget failed")
        info.shmaddr = libc.shmat(info.shmid, None, 0)
        im.data = info.shmaddr
        info.readOnly = 0
        xext.XShmAttach.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        xext.XShmAttach(self.dpy, ctypes.byref(info))
        x11.XSync(self.dpy, 0)
        # the segment goes away with the last detach, even if we crash
        libc.shmctl(info.shmid, IPC_RMID, None)
        xext.XShmGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.shm = (img, im, info, size)

    def grab_raw(self):
        """The grabbed pixels as BGRX bytes (a private copy), row stride
        `self.stride`: cheap enough to call between closely spaced shots."""
        if self.shm is not None:
            img, im, info, size = self.shm
            if not xext.XShmGetImage(self.dpy, self.win, img, self.x, self.y, ALLPLANES):
                raise RuntimeError("XShmGetImage failed")
            self.stride = im.bytes_per_line
            return ctypes.string_at(im.data, size)
        img = x11.XGetImage(self.dpy, self.win, self.x, self.y, self.w, self.h, ALLPLANES, ZPIXMAP)
        im = XImage.from_address(img)
        self.stride = im.bytes_per_line
        raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
        libc.free(ctypes.c_void_p(im.data))
        libc.free(ctypes.c_void_p(img))
        return raw

    def to_rgb(self, raw, stride=None):
        stride = stride or self.stride
        a = np.frombuffer(raw, np.uint8).reshape(self.h, stride)[:, :self.w * 4]
        return a.reshape(self.h, self.w, 4)[:, :, [2, 1, 0]].copy()

    def grab(self):
        return self.to_rgb(self.grab_raw())
