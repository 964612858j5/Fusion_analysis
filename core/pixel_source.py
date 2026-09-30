"""The `PixelSource` read contract (block A2a; plan v2.2 §5.1, v2.3 E1).

ONE explicit contract for image data. Consumers (viewer, Step1 fusion,
Step4) are migrated onto it one at a time in A2c; A2a only defines it and
provides adapters for today's sources (`sources/`), with no consumer using
them yet.

This module depends on numpy only -- never on `viewer/`, `ui/` or Qt
(`tests/test_v16_pixel_source.py` checks it): the data layer's contract
must not carry a viewer type.

Semantics every source keeps:

  * Coordinates are the level's own pixels, half-open ``[y0, y1) x [x0, x1)``;
    level 0 is the slide's global pixel grid.
  * ``valid_bounds(level)`` is the range the source really OWNS (a whole
    level for a slide, one ROI's bbox for a saved corrected product).
  * ``read_region`` returns the INTERSECTION of the request with
    ``valid_bounds`` and the intersection's actual origin; an empty
    intersection raises `OutOfBounds`. No source ever fills a pixel it does
    not own from another source.
  * Pixels come in the source's NATIVE dtype; no float conversion.
  * Region, tile and scan access return bitwise the same pixels for the same
    (channel, level, region).
  * Every read method may be called from several threads at once on one
    instance; a backend that cannot do that serialises internally. One
    `scan` iterator serves one caller; several iterators may coexist.
  * ``close()`` ends the source; any read afterwards raises. ``with source:``
    closes on exit.
  * Only ``level_downsample`` -- exact, per axis -- is a scale. A rounded
    factor is not part of this contract (see the OME-TIFF adapter's
    ``legacy_level_downsample_rounded``, a cache-key compatibility hint).
  * Native tiles (block A2c, plan v2.4 P1): ``native_tile_shape(level)`` is
    the source's own storage block -- a TIFF tile (or strip), a zarr chunk --
    and ``read_native_tile(channel, level, tile_y, tile_x)`` returns exactly
    one such block, clipped to what the source owns: bitwise
    ``read_region`` of that rectangle. The grid is anchored at the origin of
    the source's storage (level pixel 0 for a slide; the product's bbox
    origin for a saved corrected product, see ``native_tile_origin``).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np


class OutOfBounds(ValueError):
    """A request that does not intersect what the source owns."""


class SourceClosed(RuntimeError):
    """A read on a source after `close()`."""


@dataclass(frozen=True)
class PixelSourceIdentity:
    """RUNTIME / STORAGE identity: is the local file still the one that was
    opened? Used for caches and staleness only.

    It is NOT the scientific identity of the slide: that is `slide_id`
    (A3 / A8), for which the field is reserved here. The fingerprint is
    ``size:mtime_ns``, not a content hash -- a copied or touched file gets a
    new one.
    """

    dataset_path: str
    dataset_fingerprint: str
    stage: str                      # "raw" | "corrected_saved"
    product: Optional[str] = None   # a derived product's own identity
    slide_id: Optional[str] = None  # reserved (A3 / A8); never inferred here


Bounds = Tuple[int, int, int, int]


def intersect(request: Bounds, owned: Bounds) -> Bounds:
    """The part of `request` inside `owned` (both ``(y0, y1, x0, x1)``,
    half-open); `OutOfBounds` when they do not overlap."""
    y0, y1, x0, x1 = (int(v) for v in request)
    oy0, oy1, ox0, ox1 = (int(v) for v in owned)
    cy0, cy1 = max(y0, oy0), min(y1, oy1)
    cx0, cx1 = max(x0, ox0), min(x1, ox1)
    if cy0 >= cy1 or cx0 >= cx1:
        raise OutOfBounds(f"request {[y0, y1, x0, x1]} does not intersect "
                          f"{[oy0, oy1, ox0, ox1]}")
    return cy0, cy1, cx0, cx1


class PixelSource(ABC):
    """One image source: region, multiscale and sequential-scan access over
    the same pixels (module docstring gives the semantics)."""

    # ── identity and metadata ────────────────────────────────────────
    @abstractmethod
    def source_identity(self) -> PixelSourceIdentity: ...

    @abstractmethod
    def channel_names(self) -> List[str]: ...

    @abstractmethod
    def dtype(self, channel) -> np.dtype: ...

    @abstractmethod
    def level_count(self) -> int: ...

    @abstractmethod
    def level_shape(self, level: int) -> Tuple[int, int]: ...

    def level_downsample(self, level: int) -> Tuple[float, float]:
        """``(H0 / H_L, W0 / W_L)``: exact, per axis, never rounded."""
        h0, w0 = self.level_shape(0)
        hl, wl = self.level_shape(level)
        return (h0 / hl if hl else 1.0, w0 / wl if wl else 1.0)

    @abstractmethod
    def valid_bounds(self, level: int) -> Bounds: ...

    # ── reads ────────────────────────────────────────────────────────
    @abstractmethod
    def read_region(self, channel, level: int, y0: int, y1: int, x0: int,
                    x1: int) -> Tuple[np.ndarray, Tuple[int, int]]: ...

    def read_tile(self, channel, level: int, ty: int, tx: int,
                  tile_size: int) -> Tuple[np.ndarray, Tuple[int, int]]:
        """The tile ``(ty, tx)`` of a ``tile_size`` grid anchored at the
        level's origin: exactly `read_region` of that rectangle."""
        ts = int(tile_size)
        y0, x0 = int(ty) * ts, int(tx) * ts
        return self.read_region(channel, level, y0, y0 + ts, x0, x0 + ts)

    def read_regions(self, channels: Sequence, level: int, y0: int, y1: int,
                     x0: int, x1: int) -> Tuple[np.ndarray, Tuple[int, int]]:
        """``(len(channels), h, w)``: bitwise the per-channel `read_region`s."""
        parts, origin = [], None
        for ch in channels:
            arr, origin = self.read_region(ch, level, y0, y1, x0, x1)
            parts.append(arr)
        if not parts:
            raise ValueError("no channels")
        return np.stack(parts), origin

    def scan(self, channels: Sequence, level: int,
             tile_size: int) -> Iterator[Tuple[int, int, np.ndarray]]:
        """Row-major ``(y0, x0, block)`` over `valid_bounds(level)` in a
        ``tile_size`` grid; `block` is ``(len(channels), h, w)`` in the
        requested channel order, bitwise `read_regions` of the same window.
        How far a backend reads ahead is its own business."""
        by0, by1, bx0, bx1 = self.valid_bounds(level)
        ts = int(tile_size)
        for y0 in range(by0, by1, ts):
            for x0 in range(bx0, bx1, ts):
                block, _ = self.read_regions(channels, level, y0, min(y0 + ts, by1),
                                             x0, min(x0 + ts, bx1))
                yield y0, x0, block

    # ── native tiles (block A2c) ─────────────────────────────────────
    @abstractmethod
    def native_tile_shape(self, level: int) -> Tuple[int, int]:
        """``(h, w)`` of the storage block at `level`."""

    def native_tile_origin(self, level: int) -> Tuple[int, int]:
        """Where the storage grid of `level` starts, in level pixels."""
        return 0, 0

    def read_native_tile(self, channel, level: int, tile_y: int,
                         tile_x: int) -> Tuple[np.ndarray, Tuple[int, int]]:
        """One storage block, clipped to `valid_bounds`: exactly
        `read_region` of that block's rectangle."""
        th, tw = self.native_tile_shape(level)
        oy, ox = self.native_tile_origin(level)
        y0, x0 = oy + int(tile_y) * th, ox + int(tile_x) * tw
        return self.read_region(channel, level, y0, y0 + th, x0, x0 + tw)

    # ── optional hints ───────────────────────────────────────────────

    def preferred_threads(self) -> Optional[int]:
        return None

    def physical_size(self) -> Optional[Tuple[float, float]]:
        """``(dy_um, dx_um)`` of level 0 in micrometres, or None when the
        source does not record it (never a guessed default)."""
        return None

    # ── lifecycle ────────────────────────────────────────────────────
    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
