"""The resource tiers and the memory limits every module reads (block A8 / A5).

Plan v2.3 §0.2 (companion item) and §7 (A5, v2.2 §7.3): ONE constant
configuration; each module reads its own limit from here. There is no
dynamic arbitration and nothing here changes a cache's behaviour -- the
values are exactly the ones the modules held before (user ruling
2026-10-05: the sum of the limits is recorded, not budgeted).

Two tiers:
  * DEV  -- the current development machine (WSL2, ~10 GB RAM, RTX 3060
    Laptop 6 GB): bounded, no OOM; peak RSS, commit and page-file growth are
    recorded.
  * EDGE -- the future edge profile (16 GB RAM, 6 GB VRAM, external SSD):
    RAM target <= 12 GB, hard goal < 14 GB; VRAM target <= 5.0 GB, hard goal
    < 5.5 GB. Missing it does not delay TMA if the architecture is bounded
    and the bottleneck is named.
"""

import dataclasses

GB = 1024 ** 3
MB = 1024 ** 2


@dataclasses.dataclass(frozen=True)
class Tier:
    name: str
    ram_bytes: int
    vram_bytes: int
    ram_target_bytes: int = 0
    ram_hard_bytes: int = 0
    vram_target_bytes: int = 0
    vram_hard_bytes: int = 0


DEV = Tier("dev (WSL2, ~10 GB RAM, RTX 3060 Laptop 6 GB)", 10 * GB, 6 * GB)
EDGE = Tier("edge (16 GB RAM, 6 GB VRAM, external SSD)", 16 * GB, 6 * GB,
            ram_target_bytes=12 * GB, ram_hard_bytes=14 * GB,
            vram_target_bytes=int(5.0 * GB), vram_hard_bytes=int(5.5 * GB))
TIERS = (DEV, EDGE)

# ── per-module limits (values unchanged from the modules they came from) ──

# Step0 Save, CPU tiles (core/bg_parallel.py; block CS P3)
BG_MEM_RESERVE_BYTES = 1.0e9
BG_MEM_PER_TILE_BYTES = 0.75e9            # skimage TopHat fallback
BG_MEM_PER_TILE_FAST_BYTES = 0.3e9        # OpenCV TopHat / scipy Gaussian

# Step0's full-image viewer (ui/step0/step0_explore_tab.py)
STEP0_VIEWER_RAW_CACHE_BYTES = 512 * MB
STEP0_VIEWER_CORRECTED_CACHE_BYTES = 2 * GB

# Step1 / Step3 whole-slide viewer (ui/step1_viewer_host.py)
STEP1_VIEWER_RAW_CACHE_BYTES = 512 * MB
STEP1_VIEWER_CORRECTED_CACHE_BYTES = 2 * GB

# the viewer's whole-slide overview record (viewer/explore_view.py)
VIEWER_OVERVIEW_CACHE_BYTES = 256 * MB

# Step1 composition (ui/step1_compose_coordinator.py)
STEP1_COMPOSE_CACHE_BYTES = 256 * MB

# Step0 patch preload (workers/preload_scheduler.py)
STEP0_PRELOAD_MAX_BYTES = 512 * MB

# preview composition (core/preview_compose.py)
PREVIEW_COMPOSE_MAX_BYTES = 160 * MB

# Step1 pre-segmentation montage (ui/step1_presegmentation/montage_supply.py)
MONTAGE_CHANNEL_CACHE_BYTES = 1 * GB
MONTAGE_COMPOSED_CACHE_BYTES = 256 * MB

# GPU textures (ui/step1_viewer_mount.py, ui/step1_gpu_layer.py)
GPU_RAW_TEXTURE_BYTES = 512 * MB
GPU_COARSE_BYTES_PER_CHANNEL = 32 * MB
GPU_FINE_BYTES_PER_CHANNEL = 48 * MB
GPU_LABEL_TEXTURE_BYTES = 256 * MB

#: Host-memory caches, by name -- what the A5 inventory sums (their upper
#: bounds; they are not all full at once, and that is what is measured).
HOST_CACHE_LIMITS = {
    "STEP0_VIEWER_RAW_CACHE_BYTES": STEP0_VIEWER_RAW_CACHE_BYTES,
    "STEP0_VIEWER_CORRECTED_CACHE_BYTES": STEP0_VIEWER_CORRECTED_CACHE_BYTES,
    "STEP1_VIEWER_RAW_CACHE_BYTES": STEP1_VIEWER_RAW_CACHE_BYTES,
    "STEP1_VIEWER_CORRECTED_CACHE_BYTES": STEP1_VIEWER_CORRECTED_CACHE_BYTES,
    "VIEWER_OVERVIEW_CACHE_BYTES": VIEWER_OVERVIEW_CACHE_BYTES,
    "STEP1_COMPOSE_CACHE_BYTES": STEP1_COMPOSE_CACHE_BYTES,
    "STEP0_PRELOAD_MAX_BYTES": STEP0_PRELOAD_MAX_BYTES,
    "PREVIEW_COMPOSE_MAX_BYTES": PREVIEW_COMPOSE_MAX_BYTES,
    "MONTAGE_CHANNEL_CACHE_BYTES": MONTAGE_CHANNEL_CACHE_BYTES,
    "MONTAGE_COMPOSED_CACHE_BYTES": MONTAGE_COMPOSED_CACHE_BYTES,
}
GPU_LIMITS = {
    "GPU_RAW_TEXTURE_BYTES": GPU_RAW_TEXTURE_BYTES,
    "GPU_COARSE_BYTES_PER_CHANNEL": GPU_COARSE_BYTES_PER_CHANNEL,
    "GPU_FINE_BYTES_PER_CHANNEL": GPU_FINE_BYTES_PER_CHANNEL,
    "GPU_LABEL_TEXTURE_BYTES": GPU_LABEL_TEXTURE_BYTES,
}
