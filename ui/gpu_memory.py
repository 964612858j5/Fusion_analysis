"""Block A9 §36 (M4): the image viewers' GPU memory, released on need.

User ruling 2026-10-09: viewing stays within the EDGE tier's VRAM target;
a viewer the user has left (paused / hidden) KEEPS its textures while
memory is not tight -- coming back is then instant -- and gives them back,
down to its complete coarse, only when something needs the memory:

  * a segmentation run is about to start (`release_hidden("segmentation")`,
    called where Step2 and the Step1 pre-segmentation launch their work);
  * a viewer becomes active while the viewers together exceed
    `VIEW_VRAM_TARGET` (`on_layer_active`).

The registry holds the layers weakly and only ever calls them on the GUI
thread (their GL context lives there).
"""

import os
import weakref

from ..core import resource_tiers as _tiers
from ..utils import perf_trace

#: what all image viewers together may hold before hidden ones give back
VIEW_VRAM_TARGET = _tiers.EDGE.vram_target_bytes

_layers = weakref.WeakSet()

#: A9 §38: the "Smooth" display setting (Odon's smooth_pixels, default on),
#: one for every image viewer; the registry above already knows them all
_smooth = os.environ.get("BLOCK01_SMOOTH", "1") != "0"


def smooth_pixels() -> bool:
    return _smooth


def set_smooth(smooth: bool) -> None:
    """Every Step1 GPU layer, now and later, draws smoothed or nearest."""
    global _smooth
    _smooth = bool(smooth)
    for layer in list(_layers):
        try:
            layer.set_smooth(_smooth)
        except Exception:                                    # noqa: BLE001 -- a disposed layer
            continue


def register(layer) -> None:
    _layers.add(layer)


def allocated_bytes() -> int:
    total = 0
    for layer in list(_layers):
        try:
            total += int(layer.gpu_bytes())
        except Exception:                                    # noqa: BLE001 -- a disposed layer
            pass
    return total


def release_hidden(reason: str) -> int:
    """Every hidden viewer gives its textures back down to its coarse.
    Returns the bytes released."""
    released = 0
    for layer in list(_layers):
        if not getattr(layer, "hidden", False):
            continue
        try:
            released += int(layer.release_to_coarse())
        except Exception:                                    # noqa: BLE001 -- never blocks the caller
            continue
    if released:
        perf_trace.mark("gpu.memory_released", reason=reason, bytes=released)
    return released


def on_layer_active(active) -> int:
    """`active` is about to draw again: if the viewers together are over
    the target, the hidden ones release first."""
    if allocated_bytes() <= VIEW_VRAM_TARGET:
        return 0
    return release_hidden("pressure")
