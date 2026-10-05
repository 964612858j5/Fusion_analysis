"""Block A8 / A5: one constant configuration of the resource tiers and the
memory limits, read by every module for its own limit -- values unchanged
(plan v2.3 §0.2, §7.3; user ruling 2026-10-05: record the sum, no budget)."""

import importlib

import pytest

from block01.core import resource_tiers as t

READERS = [
    ("block01.core.bg_parallel", "MEM_RESERVE_BYTES", "BG_MEM_RESERVE_BYTES", 1.0e9),
    ("block01.core.bg_parallel", "MEM_PER_TILE_BYTES", "BG_MEM_PER_TILE_BYTES", 0.75e9),
    ("block01.core.bg_parallel", "MEM_PER_TILE_FAST_BYTES", "BG_MEM_PER_TILE_FAST_BYTES", 0.3e9),
    ("block01.ui.step0.step0_explore_tab", "RAW_CACHE_BYTES", "STEP0_VIEWER_RAW_CACHE_BYTES", 512 << 20),
    ("block01.ui.step0.step0_explore_tab", "CORRECTED_CACHE_BYTES", "STEP0_VIEWER_CORRECTED_CACHE_BYTES", 2 << 30),
    ("block01.ui.step1_viewer_host", "RAW_CACHE_BYTES", "STEP1_VIEWER_RAW_CACHE_BYTES", 512 << 20),
    ("block01.ui.step1_viewer_host", "CORRECTED_CACHE_BYTES", "STEP1_VIEWER_CORRECTED_CACHE_BYTES", 2 << 30),
    ("block01.ui.step1_compose_coordinator", "DEFAULT_COMPOSE_CACHE_BYTES", "STEP1_COMPOSE_CACHE_BYTES", 256 << 20),
    ("block01.workers.preload_scheduler", "DEFAULT_MAX_BYTES", "STEP0_PRELOAD_MAX_BYTES", 512 << 20),
    ("block01.core.preview_compose", "DEFAULT_MAX_BYTES", "PREVIEW_COMPOSE_MAX_BYTES", 160 << 20),
    ("block01.viewer.explore_view", "OVERVIEW_CACHE_BYTES", "VIEWER_OVERVIEW_CACHE_BYTES", 256 << 20),
    ("block01.ui.step1_presegmentation.montage_supply", "CHANNEL_CACHE_BYTES", "MONTAGE_CHANNEL_CACHE_BYTES", 1 << 30),
    ("block01.ui.step1_presegmentation.montage_supply", "COMPOSED_CACHE_BYTES", "MONTAGE_COMPOSED_CACHE_BYTES", 256 << 20),
    ("block01.ui.step1_viewer_mount", "DEMO_GPU_RAW_TEXTURE_BYTES", "GPU_RAW_TEXTURE_BYTES", 512 << 20),
    ("block01.ui.step1_viewer_mount", "DEMO_GPU_COARSE_BYTES_PER_CHANNEL", "GPU_COARSE_BYTES_PER_CHANNEL", 32 << 20),
    ("block01.ui.step1_viewer_mount", "DEMO_GPU_FINE_BYTES_PER_CHANNEL", "GPU_FINE_BYTES_PER_CHANNEL", 48 << 20),
    ("block01.ui.step1_gpu_layer", "LABEL_TEXTURE_BYTES", "GPU_LABEL_TEXTURE_BYTES", 256 << 20),
]


@pytest.mark.parametrize("module,local,tier_name,value", READERS)
def test_each_module_reads_its_limit_from_the_one_place(module, local, tier_name, value):
    mod = importlib.import_module(module)
    assert getattr(mod, local) == getattr(t, tier_name) == value
    assert getattr(mod, "_tiers") is t


def test_the_two_tiers_are_the_plans():
    assert t.DEV.ram_bytes == 10 * t.GB and t.DEV.vram_bytes == 6 * t.GB
    assert t.EDGE.ram_bytes == 16 * t.GB
    assert t.EDGE.ram_target_bytes == 12 * t.GB and t.EDGE.ram_hard_bytes == 14 * t.GB
    assert t.EDGE.vram_target_bytes == int(5.0 * t.GB)
    assert t.EDGE.vram_hard_bytes == int(5.5 * t.GB)


def test_the_host_cache_limits_are_listed_for_the_inventory():
    assert set(t.HOST_CACHE_LIMITS) >= {"STEP0_VIEWER_CORRECTED_CACHE_BYTES",
                                        "STEP1_VIEWER_CORRECTED_CACHE_BYTES",
                                        "MONTAGE_CHANNEL_CACHE_BYTES"}
    assert sum(t.HOST_CACHE_LIMITS.values()) > t.DEV.ram_bytes * 0.5      # recorded, not budgeted
