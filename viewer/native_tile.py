"""`NativeTile`: one Step1 tile in its source's own number format.

Block A9 S2d (application §28, contract v2 §28.5). Step1's ordinary tile
read (`Step1TileProvider.read_tile`) returns float32 with NaN where there
are no pixels, and every CPU consumer keeps using it. A GPU consumer that
asks with the separate `step1-native` identity gets this instead:

  * a RAW channel stored as uint8 / uint16 is its pyramid's own integers,
    the tile's full shape, ZERO outside `valid_rect` -- and zero there is
    "absent", never black: validity is the rectangle, not the value;
  * anything else (a corrected channel, a raw channel stored as float, a
    channel with no source) is float32, valid where FINITE, exactly the
    numbers `read_tile` returns.

The values are read-only. Pure data: no Qt, no I/O, no cache.
"""

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np

#: The cache namespace of native tiles (`SourceIdentity.stage`).
NATIVE_STAGE = "step1-native"

#: `valid_rect` value meaning "valid where the float value is finite".
FINITE = "finite"

KIND_RAW = "raw"                  # uint8 / uint16 pyramid integers
KIND_RAW_FLOAT = "raw-float"      # a raw channel not stored as uint8/uint16
KIND_CORRECTED = "corrected"
KIND_MISSING = "missing"

INTEGER_DTYPES = (np.dtype(np.uint8), np.dtype(np.uint16))

Rect = Tuple[int, int, int, int]   # (y0, y1, x0, x1), tile-local


@dataclass(frozen=True)
class NativeTile:
    values: np.ndarray
    #: (y0, y1, x0, x1) of the valid pixels, tile-local; None: none valid;
    #: FINITE: valid where `values` is finite (float kinds only)
    valid_rect: Union[Rect, str, None]
    kind: str

    def __post_init__(self):
        values = np.asarray(self.values)
        if values.ndim != 2:
            raise ValueError(f"a native tile is 2-D, not {values.shape!r}")
        if self.kind == KIND_RAW:
            if values.dtype not in INTEGER_DTYPES:
                raise ValueError(f"a raw native tile is uint8/uint16, not {values.dtype}")
            if self.valid_rect == FINITE:
                raise ValueError("an integer tile cannot be valid where finite")
        elif values.dtype != np.float32 or self.valid_rect not in (FINITE, None):
            raise ValueError("a float native tile is float32, valid where finite")
        values.setflags(write=False)
        object.__setattr__(self, "values", values)

    # what the scheduler and the byte-bounded cache read
    @property
    def dtype(self):
        return self.values.dtype

    @property
    def shape(self):
        return self.values.shape

    @property
    def nbytes(self) -> int:
        return int(self.values.nbytes)

    def valid_mask(self) -> np.ndarray:
        """A boolean mask of the valid pixels (built on demand)."""
        if self.valid_rect == FINITE:
            return np.isfinite(self.values)
        mask = np.zeros(self.values.shape, bool)
        if self.valid_rect is not None:
            y0, y1, x0, x1 = self.valid_rect
            mask[y0:y1, x0:x1] = True
        return mask

    def to_float(self) -> np.ndarray:
        """The float32 tile with NaN where invalid -- the numbers Step1's
        ordinary `read_tile` returns for the same tile."""
        out = np.asarray(self.values, np.float32).copy()
        out[~self.valid_mask()] = np.nan
        return out


def is_native(source) -> bool:
    """Is `source` (a SourceIdentity) the native namespace?"""
    return getattr(source, "stage", None) == NATIVE_STAGE
