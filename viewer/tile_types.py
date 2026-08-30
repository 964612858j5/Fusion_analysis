"""Typed identity/request/result contracts for the v15 viewer foundation.

See docs/v15_viewer_foundation_interfaces.md for the authoritative design.
All *identity* types (SourceIdentity, TileGridSpec, TileAddress, RawKey,
CorrectionKey) are frozen + hashable so they can be used directly as cache
keys and as dedup keys in the scheduler. TileRequest is also frozen (its
`generation` field is a delivery token only — it is NOT part of dedup or
cache identity). PixelBuffer/TileResult carry live payloads (numpy arrays,
error strings) and are plain (non-frozen) dataclasses.
"""

from dataclasses import dataclass
from typing import Optional, Set, Tuple, Union


# ── Quality levels ───────────────────────────────────────────────────────────

class QualityLevel:
    """String constants for TileResult.quality / CorrectionKey.quality.

    INTERACTIVE: computed at the displayed (possibly downsampled) level with
        scale-adjusted params; labeled as an approximation in the UI.
    NATIVE: level-0-local; numerically aligned with PRODUCTION within a
        stated tolerance (golden-tested elsewhere, not enforced here).
    PRODUCTION: full deterministic tiled run that writes artifacts; NOT
        served by this scheduler (a separate engine owns it).
    """

    INTERACTIVE = "interactive"
    NATIVE = "native"
    PRODUCTION = "production"


# ── Identity types ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceIdentity:
    """Identifies WHAT pixels mean. Any field change invalidates every cache."""

    dataset_path: str
    dataset_fingerprint: str
    stage: str  # "raw" | "corrected_saved"
    corrected_artifact: Optional[str] = None


@dataclass(frozen=True)
class TileGridSpec:
    """Canonical tiling grid for a session. `tile_size` is a session param."""

    tile_size: int = 512
    source_chunk_shape: Tuple[int, ...] = ()
    grid_version: str = "v1"


@dataclass(frozen=True)
class TileAddress:
    """One tile's position in a TileGridSpec at a given pyramid level."""

    grid: TileGridSpec
    level: int
    tx: int
    ty: int


@dataclass(frozen=True)
class RawKey:
    """Identity/cache key for an uncorrected (raw) tile."""

    source: SourceIdentity
    channel: str
    tile: TileAddress


@dataclass(frozen=True)
class CorrectionKey:
    """Identity/cache key for a corrected tile. Equality == reusable result."""

    source: SourceIdentity
    channel: str
    tile: TileAddress
    method: str  # "tophat" | "cucim"
    params: Tuple[int, ...]  # canonical-order method params, e.g. (radius,)
    algorithm_version: str
    boundary_mode: str = "halo_crop_reflect"
    quality: str = QualityLevel.INTERACTIVE


TileKey = Union[RawKey, CorrectionKey]


def effective_param(base: int, level: int, downsample: float) -> int:
    """Scale a base method param down for a coarser pyramid level.

    Used for QualityLevel.INTERACTIVE at level > 0: the correction is run at
    the displayed (downsampled) resolution, so a radius/sigma tuned for
    level-0 pixels must shrink by the level's downsample factor to keep the
    same *physical* feature size. `downsample` is the ratio
    level0_size / levelN_size (>= 1 for level > 0, == 1 for level 0).
    Always at least 1 (a param of 0 is meaningless to the kernels).
    """
    if level <= 0 or downsample <= 1:
        return int(base)
    return max(1, int(round(base / downsample)))


def tiles_covering(bbox: Tuple[int, int, int, int], tile_size: int) -> Set[Tuple[int, int]]:
    """Return the set of (tx, ty) tile coords covering a pixel bbox.

    `bbox` is (y0, x0, y1, x1), possibly unaligned to the tile grid (e.g. a
    center-anchored viewport). Uses floor/ceil division so the returned set
    is exactly the tiles needed to cover [y0, y1) x [x0, x1) — the same
    convention used for the initial viewport fill, so pan-step coverage is
    computed identically to the fill phase.
    """
    y0, x0, y1, x1 = bbox
    if y1 <= y0 or x1 <= x0:
        return set()
    tx0, tx1 = x0 // tile_size, (x1 - 1) // tile_size
    ty0, ty1 = y0 // tile_size, (y1 - 1) // tile_size
    return {(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}


# ── Request / result ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TileRequest:
    """A single ask for a tile. `generation` gates delivery only, never dedup.

    `generation` is an OPAQUE, hashable token -- not necessarily an int. The
    scheduler only ever compares generations by equality / set membership
    (`cancel_generation` adds a token to a stale-set; delivery checks
    membership), so callers are free to use namespaced tokens such as
    `("raw", n)` / `("precise", n)` to keep independent generation counters
    from colliding when both happen to reach the same small integer (e.g. a
    view-generation counter and a settled/precise-generation counter).
    """

    key: TileKey
    generation: object
    priority: int  # 0 = visible-center ... higher = prefetch ring
    deadline_ms: Optional[int] = None


@dataclass
class PixelBuffer:
    """A pixel payload plus enough metadata to interpret / upload it.

    `residency="cpu"` with a numpy ndarray handle in this prototype; the
    shape leaves room for "cuda"/"gl" residency later without an interface
    change (CUDA-GL interop stays possible).
    """

    residency: str  # "cpu" | "cuda" | "gl"
    dtype: str
    shape: Tuple[int, ...]
    handle: object


@dataclass
class TileResult:
    """Outcome of a TileRequest. `pixels` is None on error."""

    request: TileRequest
    pixels: Optional[PixelBuffer]
    quality: str
    provisional: bool
    timing: dict
    error: Optional[str] = None
