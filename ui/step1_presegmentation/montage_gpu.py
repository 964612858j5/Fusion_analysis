"""The montage drawn by Step1's own GPU layer (block D, user ruling 2026-09-25).

WHY: composing on the CPU and switching between a quick half-resolution and
a full frame while Intensity moved made the picture shimmer; the viewer does
not, because `Step1GpuLayer` keeps the raw planes on the card and re-renders
every frame from them -- a new window, colour, tick or mode is a new
uniform, not a new image. The montage now uses THAT layer, only calling it:

  * `Step1GpuLayer` -- the shaders and the Overlay / Fusion composition;
  * `build_spec` -> `DisplaySnapshot` -- the same copy the viewer's mount
    makes (`display_snapshot`, held equal to it by a test);
  * the canvas IS the layer's world: a patch is a set of raw planes placed
    on its own canvas rectangle (canvas units = level-0 pixels).

What the montage adds is the SUPPLY of planes, as the viewer's binding does
for the whole slide (whose tile planner fits one slide, not a canvas of
patches):

  * per patch and channel, one COARSE plane -- the whole patch at a level
    whose longest side is at most `COARSE_MAX_SIDE`, so every patch is
    drawn at once;
  * per patch on screen and channel, one FINE plane -- only the part of the
    patch in view, at the level the zoom needs, aligned to `FINE_BLOCK`
    pixels so a small pan reuses it.

THE BUDGET (user question, 2026-09-25: what if a patch is huge?). Planes
are read at the resolution the screen needs, and a fine plane covers only
what is in view, so the bytes follow the screen and the channel count, not
the patch size. Before every submission the active set is counted: coarse
planes shrink (halving `COARSE_MAX_SIDE`) to fit `COARSE_BUDGET`, and fine
planes step to a coarser level to fit `FINE_BUDGET`; together they stay
under the layer's own `TEXTURE_BUDGET`, which refuses rather than
overflows anyway.
"""

import math

from ..step1_gpu_layer import (ChannelSource, DisplaySnapshot, MODE_OVERLAY, RawPlane,
                               SourceDescriptor, Step1GpuLayer, ViewportSnapshot)

TEXTURE_BUDGET = 512 * 1024 * 1024            # the montage's own GPU texture cache
COARSE_BUDGET = TEXTURE_BUDGET // 4
FINE_BUDGET = TEXTURE_BUDGET // 2
COARSE_MAX_SIDE = 1024
FINE_BLOCK = 256
BYTES_PER_PIXEL = 4                           # R32F, as the layer uploads


def display_snapshot(spec):
    """`build_spec()`'s answer as a `DisplaySnapshot` -- the same copy as the
    viewer's `Step1WholeSlideMount._gpu_display_snapshot`, no number or rule
    changed."""
    return DisplaySnapshot(
        mode=str(spec.get("mode") or MODE_OVERLAY),
        mappings={str(ch): tuple(float(v) for v in value)
                  for ch, value in (spec.get("mappings") or {}).items()},
        weights={str(ch): float(w or 0.0) for ch, w in (spec.get("weights") or {}).items()},
        colors={str(ch): tuple(float(v) for v in value)
                for ch, value in (spec.get("colors") or {}).items()},
        groups={str(g): {str(ch): float(w or 0.0) for ch, w in (members or {}).items()}
                for g, members in (spec.get("groups") or {}).items()},
        group_weights={str(g): float(w or 0.0)
                       for g, w in (spec.get("group_weights") or {}).items()},
        nucleus=(str((spec.get("nucleus") or ("", 0.0))[0]),
                 float((spec.get("nucleus") or ("", 0.0))[1] or 0.0)),
    )


class PlaneSpec:
    """One raw plane to read and place: `rect` in level-k pixels, `world`
    = (x0, x1, y0, y1) on the canvas.

    `read_key` names the PIXELS (what is read and cached); `identity` names
    the plane on the card, which is the pixels AND where they are drawn --
    the layer refuses one identity at two places, and a patch moves on the
    canvas when another is unticked. Moving never re-reads the disk."""

    __slots__ = ("read_key", "identity", "channel", "level", "rect", "world", "kind")

    def __init__(self, read_key, channel, level, rect, world, kind):
        self.read_key = tuple(read_key)
        self.identity = self.read_key + (tuple(float(v) for v in world),)
        self.channel, self.level = channel, level
        self.rect, self.world, self.kind = rect, world, kind

    def nbytes(self):
        y0, y1, x0, x1 = self.rect
        return (y1 - y0) * (x1 - x0) * BYTES_PER_PIXEL


def _level_for_side(downsamples, side, max_side):
    """The finest level at which `side` level-0 pixels are <= `max_side`."""
    for level, ds in enumerate(downsamples):
        if side / ds <= max_side:
            return level
    return len(downsamples) - 1


def _to_level(y0, y1, x0, x1, ds_yx):
    dsy, dsx = ds_yx
    ly0, lx0 = int(y0 / dsy), int(x0 / dsx)
    return (ly0, max(ly0 + 1, int(math.ceil(y1 / dsy))), lx0, max(lx0 + 1, int(math.ceil(x1 / dsx))))


def _world_of(rect, ds_yx, bbox, canvas_rect):
    """Where a level-k rect's pixels really lie on the canvas: its exact
    level-0 extent, moved by the patch's canvas offset. Never stretched to
    the patch frame -- a stretch by a hair is the sub-pixel shift that made
    the CPU montage shimmer."""
    ly0, ly1, lx0, lx1 = rect
    dsy, dsx = ds_yx
    by0, _, bx0, _ = bbox
    cy, cx, _, _ = canvas_rect
    return (cx + lx0 * dsx - bx0, cx + lx1 * dsx - bx0, cy + ly0 * dsy - by0, cy + ly1 * dsy - by0)


def plan_planes(patches, rects, view_rect, fine_level, channels, pixel_key, downsamples,
                ds_yx, coarse_max_side=COARSE_MAX_SIDE):
    """Every plane the current view needs.

    `patches` [{bbox}] and `rects` [(y, x, h, w)] -- the layout; `view_rect`
    (x0, x1, y0, y1) on the canvas; `fine_level` the level the zoom asks
    for; `ds_yx(level)` the per-axis downsample. Returns (coarse, fine) as
    lists of PlaneSpec within the budgets.
    """
    channels = sorted(channels)
    vx0, vx1, vy0, vy1 = view_rect
    # ── coarse: every patch, whole; shrink until the set fits ──
    side = coarse_max_side
    while True:
        coarse, levels = [], []
        for p, (cy, cx, ch_, cw) in zip(patches, rects):
            y0, y1, x0, x1 = (int(v) for v in p["bbox"])
            level = _level_for_side(downsamples, max(y1 - y0, x1 - x0), side)
            levels.append(level)
            rect = _to_level(y0, y1, x0, x1, ds_yx(level))
            world = _world_of(rect, ds_yx(level), (y0, y1, x0, x1), (cy, cx, ch_, cw))
            for c in channels:
                coarse.append(PlaneSpec(("coarse", (y0, y1, x0, x1), level, c, pixel_key), c, level,
                                        rect, world, "coarse"))
        if sum(s.nbytes() for s in coarse) <= COARSE_BUDGET or side <= 64:
            break
        side //= 2
    # ── fine: only what is in view, and only where finer than the coarse ──
    fine_level = int(fine_level)
    while True:
        fine = []
        for p, (cy, cx, ch_, cw), clevel in zip(patches, rects, levels):
            if fine_level >= clevel:
                continue
            # the visible part of this patch, in its level-0 pixels
            ix0, ix1 = max(vx0, cx), min(vx1, cx + cw)
            iy0, iy1 = max(vy0, cy), min(vy1, cy + ch_)
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            by0, by1, bx0, bx1 = (int(v) for v in p["bbox"])
            ds = ds_yx(fine_level)
            block_y, block_x = FINE_BLOCK * ds[0], FINE_BLOCK * ds[1]
            # aligned outward to whole blocks of the fine level, kept inside the patch
            oy0 = max(0, int((iy0 - cy) // block_y * block_y))
            oy1 = min(by1 - by0, int(math.ceil((iy1 - cy) / block_y) * block_y))
            ox0 = max(0, int((ix0 - cx) // block_x * block_x))
            ox1 = min(bx1 - bx0, int(math.ceil((ix1 - cx) / block_x) * block_x))
            rect = _to_level(by0 + oy0, by0 + oy1, bx0 + ox0, bx0 + ox1, ds)
            world = _world_of(rect, ds, (by0, by1, bx0, bx1), (cy, cx, ch_, cw))
            for c in channels:
                fine.append(PlaneSpec(("fine", (by0, by1, bx0, bx1), fine_level, rect, c, pixel_key),
                                      c, fine_level, rect, world, "fine"))
        if sum(s.nbytes() for s in fine) <= FINE_BUDGET or fine_level >= len(downsamples) - 1:
            break
        fine_level += 1                        # this frame a coarser fine plane
    return coarse, fine


def source_descriptor(coarse, fine, planes):
    """The layer's input from the planes that have ARRIVED (`planes`:
    identity -> float32 values). A channel with fine planes draws them over
    its coarse ones (the layer's own rule)."""
    by_channel = {}
    for kind, specs in (("coarse", coarse), ("fine", fine)):
        for s in specs:
            values = planes.get(s.identity)
            if values is None:
                continue
            raw = RawPlane(identity=s.identity, world_rect=tuple(float(v) for v in s.world),
                           values=values)
            by_channel.setdefault(s.channel, {"coarse": [], "fine": []})[kind].append(raw)
    return SourceDescriptor(channels=tuple(
        ChannelSource(channel=c, coarse=tuple(v["coarse"]), fine=tuple(v["fine"]),
                      selected_level="fine" if v["fine"] else "coarse")
        for c, v in sorted(by_channel.items())))


class _ViewAdapter:
    """What `Step1GpuLayer.attach` expects of a view: its QGraphicsView and
    its ViewBox."""

    def __init__(self, graphics, view_box):
        self.graphics, self.view_box = graphics, view_box


def build_layer(view, factory=None):
    """A GPU layer attached over the montage's canvas, or (None, reason).
    Like the viewer's mount: forced to initialise now, so the backend is
    decided on what actually happened."""
    layer = None
    try:
        layer = (factory or (lambda: Step1GpuLayer(max_raw_texture_bytes=TEXTURE_BUDGET,
                                                   require_hardware=True)))()
        layer.attach(_ViewAdapter(view.gv, view.vb))
        if layer.width() <= 0 or layer.height() <= 0:
            layer.resize(1, 1)
        layer.show()
        layer.grabFramebuffer()
        if not layer.initialized:
            raise layer.init_error or RuntimeError("the montage GPU layer did not initialize")
        return layer, ""
    except Exception as exc:  # noqa: BLE001 -- said, and the CPU picture is used
        reason = f"{type(exc).__name__}: {exc}"
        if layer is not None:
            try:
                layer.dispose()
                layer.setParent(None)
                layer.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        return None, reason


def viewport_snapshot(view, layer):
    (x0, x1), (y0, y1) = view.vb.viewRange()
    try:
        ratio = float(layer.devicePixelRatioF())
    except AttributeError:
        ratio = 1.0
    return ViewportSnapshot((float(x0), float(x1), float(y0), float(y1)),
                            (max(1, int(layer.width())), max(1, int(layer.height()))),
                            ratio if ratio > 0 else 1.0)
