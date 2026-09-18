"""Where the user is looking, shared by the steps that show the same slide.

Block C4.5a of `docs/step1_rework_plan.md`, on the user's ruling: Step0 and
Step1 are two pictures of ONE slide, and walking between them must not move
the user. Pan or zoom in Step0, enter Step1, and Step1 is looking at the same
place at the same magnification; move in Step1 -- by hand, by a patch button,
by a click on the Tissue Preview -- and Step0 is there when the user comes
back.

A CENTRE AND A SCALE, NOT A RECTANGLE. `(cx, cy)` is the level-0 point in the
middle of the view and `scale` is screen pixels per level-0 pixel. The two
numbers a view IS, independent of the widget showing it: Step0's full image,
Step0's three compare panels and Step1's viewer have different widths, heights
and aspect locks, and handing a RECTANGLE between them lets each one reshape
it -- which, round after round, walks the view away from where it started.
`Step0Page._full_image_camera` already says this for the two Step0 views; this
is the same measurement, carried one step further.

IT BELONGS TO A SLIDE. A camera is a position ON a dataset, so it carries the
dataset it was read from and is refused for any other: opening another slide
must not put the user at the old slide's coordinates, which may not even
exist on the new one.

ONLY THE STEP ON SCREEN WRITES IT. A hidden viewer's late range signal is not
a place the user chose to be.
"""


class CameraSnapshot:
    """One shared observing position: `(cx, cy, scale)` on one dataset."""

    __slots__ = ("dataset", "cx", "cy", "scale", "origin")

    def __init__(self, dataset, cx, cy, scale, origin=""):
        self.dataset = str(dataset or "")
        self.cx = float(cx)
        self.cy = float(cy)
        self.scale = float(scale)
        #: WHO last wrote it. Diagnostics only -- nothing branches on it, so
        #: it cannot become a second copy of "which viewer owns the camera".
        self.origin = str(origin or "")

    @property
    def camera(self):
        return (self.cx, self.cy, self.scale)

    def valid_for(self, dataset):
        """Is this a position on `dataset`, and a usable one?"""
        if not self.dataset or self.dataset != str(dataset or ""):
            return False
        return self.usable()

    def usable(self):
        import math

        return all(math.isfinite(v) for v in (self.cx, self.cy, self.scale)) \
            and self.scale > 0.0

    def __eq__(self, other):
        return (isinstance(other, CameraSnapshot)
                and (self.dataset, self.cx, self.cy, self.scale)
                == (other.dataset, other.cx, other.cy, other.scale))

    def __repr__(self):                                     # pragma: no cover
        return (f"CameraSnapshot({self.dataset!r}, {self.cx:.1f}, "
                f"{self.cy:.1f}, {self.scale:.6g}, {self.origin!r})")


def snapshot_from(dataset, camera, origin=""):
    """A snapshot from a `(cx, cy, scale)` a viewer answered with, or None."""
    if not camera or len(camera) != 3:
        return None
    shot = CameraSnapshot(dataset, camera[0], camera[1], camera[2], origin)
    return shot if shot.usable() else None
