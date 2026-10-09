"""Block A9 §35.6/§35.7: the single-pass virtual-texture compositor.

The tile array store (`step1_gpu_layer._TileArrayStore`) holds the pixels;
this module keeps, per drawn channel and pyramid level, a PAGE TABLE from
tile cell to array slot -- only for the planes the current submission
draws -- plus a per-slot metadata texture and a per-row parameter texture,
and composes every channel in one fullscreen pass (`step1_gpu_vt.frag`).

`plan()` decides whether a submission can be drawn this way; anything it
cannot express exactly (a plane off the grid, two planes in one cell, an
order that is not coarse-to-fine, more levels or blocks than the shader
has, ...) returns None and the layer draws that submission per channel,
as before.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

TILE = 512
U_BLOCKS = 6        # defaults; the layer sets the real split per GL context
F_BLOCKS = 4
MAX_LEVELS = 16
MAX_LEVELS_PER_CHANNEL = 8
BLOCK_ROWS = 3
PARAM_TEXELS = 5


def shader_source(template: str, u_blocks: int, f_blocks: int) -> str:
    """The single-pass fragment shader for this many integer / float block
    samplers (GLSL 3.30 indexes samplers only by constants: one name and
    one branch per slot)."""
    samplers = [f"uniform usampler2DArray u_u{i};" for i in range(u_blocks)]
    samplers += [f"uniform sampler2DArray u_f{i};" for i in range(f_blocks)]
    fetch = ["    if (family == 0) {"]
    fetch += [f"        if (block == {i}) return float(texelFetch(u_u{i}, at, 0).r);"
              for i in range(u_blocks)]
    fetch += ["        return 0.0;", "    }"]
    fetch += [f"    if (block == {i}) return texelFetch(u_f{i}, at, 0).r;" for i in range(f_blocks)]
    fetch += ["    return 0.0;"]
    constants = (f"const int BLOCK_ROWS = {BLOCK_ROWS};\n"
                 f"const int U_BLOCKS = {u_blocks};")
    return (template.replace("//@SAMPLERS@", "\n".join(samplers))
            .replace("//@FETCH@", "\n".join(fetch))
            .replace("//@BLOCK_CONSTANTS@", constants))


def sampler_split(max_units: int) -> Tuple[int, int]:
    """(integer blocks, float blocks) the single pass can bind: every unit
    but the page, meta and parameter textures; mostly integer (raw)."""
    available = max(2, min(int(max_units), 32) - 3)
    floats = max(1, available // 6)
    return available - floats, floats


def _family(fmt: str) -> int:
    return 1 if fmt == "f32" else 0


class VtPlan:
    """What one submission needs: rows in draw order and their pages."""

    __slots__ = ("rows", "levels", "cells", "fusion")

    def __init__(self, rows, levels, cells, fusion):
        self.rows = rows          # [(channel, params dict)]
        self.levels = levels      # [(ds_x, ds_y)] level slots used, by index
        self.cells = cells        # {channel: {(level, tx, ty): plane}}
        self.fusion = fusion


class VirtualCompositor:
    def __init__(self):
        self.level_slots: List[Tuple[float, float]] = []   # (ds_x, ds_y)
        self.level_grid: List[Tuple[int, int]] = []        # (grid w, grid h)
        self.level_offset: List[int] = []                  # x offset in the page atlas
        self.extent = (0.0, 0.0)                           # world (w, h) of the slide
        self.channel_slots: Dict[str, int] = {}
        self.pages: Optional[np.ndarray] = None            # (channels, h, w) int32
        self.pages_tex = 0
        self.pages_shape = None
        self.meta = np.zeros((1, 1, 4), np.float32)
        self.meta_tex = 0
        self.meta_dirty = True
        self.params_tex = 0
        self.synced = None                                  # (id(descriptor), store generation)
        self.max_layers = 2048
        self.max_texture = 16384
        self.u_blocks = U_BLOCKS
        self.f_blocks = F_BLOCKS
        self.last_written = 0
        self.last_layout = ""
        #: channel -> (plane signature, cells, levels used, extent): reused
        #: when a publication hands the same planes back
        self._channel_cells = {}
        #: the layer's id(plane) -> plane residency cache (shared)
        self.known_resident = {}
        #: channel -> the cells its page layer was last written from
        self._written = {}
        #: id(plane) -> the (block, layer) its page entry points at
        self._slot_of = {}
        #: id(plane) -> (plane, ds_x, level key, tx, ty, x1, y1): its grid cell
        self._geometry = {}
        self.frames = 0
        self.fallbacks = 0

    # ── planning (pure Python, no GL) ───────────────────────────────────

    def plan(self, store, composed_rows, fusion: bool, legacy=None) -> Optional[VtPlan]:
        """`composed_rows`: [(channel, source, row params)] in draw order.
        `legacy`: the per-plane texture store -- a drawn plane held there
        (an oversized one) cannot be reached by the single pass."""
        if store is None:
            return None
        blocks = {0: 0, 1: 0}
        for block in store.blocks:
            blocks[_family(block.fmt)] += 1
        if blocks[0] > self.u_blocks or blocks[1] > self.f_blocks:
            self.why = "blocks"
            return None
        cells: Dict[str, Dict[Tuple[int, int, int], object]] = {}
        levels: List[Tuple[float, float]] = list(self.level_slots)
        extent_w, extent_h = self.extent
        for channel, source, _params in composed_rows:
            planes = source.selected_planes()
            # the same plane objects as last time -> the same cells (the
            # binding hands its resident planes back unchanged)
            known = self.known_resident
            sig = (tuple(map(id, planes)),
                   sum(1 for plane in planes if known.get(id(plane)) is plane),
                   store.evictions)
            cached = self._channel_cells.get(channel)
            if cached is not None and cached[0] == sig and all(
                    level < len(levels) and levels[level] == ds
                    for level, ds in cached[2]):
                cells[channel] = cached[1]
                extent_w, extent_h = max(extent_w, cached[3][0]), max(extent_h, cached[3][1])
                continue
            mine = cells.setdefault(channel, {})
            last_ds = math.inf
            own_extent = (0.0, 0.0)
            for plane in planes:
                if known.get(id(plane)) is not plane and not store.is_resident(plane):
                    if legacy is not None and legacy.is_resident(plane):
                        self.why = "legacy plane"
                        return None
                    continue
                info = self._geometry.get(id(plane))
                if info is None or info[0] is not plane:
                    values = np.asarray(plane.values)
                    h, w = values.shape[:2]
                    if h > TILE or w > TILE:
                        self.why = "oversized"
                        return None
                    x0, x1, y0, y1 = plane.world_rect
                    ds_x, ds_y = (x1 - x0) / w, (y1 - y0) / h
                    tx, ty = x0 / (TILE * ds_x), y0 / (TILE * ds_y)
                    if (abs(tx - round(tx)) > 1e-6 or abs(ty - round(ty)) > 1e-6
                            or tx < -0.5 or ty < -0.5):
                        self.why = "off grid"
                        return None                 # not on the binding's grid
                    info = (plane, ds_x, (round(ds_x, 9), round(ds_y, 9)),
                            int(round(tx)), int(round(ty)), x1, y1)
                    if len(self._geometry) > 200000:
                        self._geometry.clear()
                    self._geometry[id(plane)] = info
                _p, ds_x, key_ds, tx, ty, x1, y1 = info
                if ds_x > last_ds * (1 + 1e-9):
                    self.why = "order"
                    return None                     # not coarse -> fine
                last_ds = ds_x
                if key_ds not in levels:
                    levels.append(key_ds)
                    if len(levels) > MAX_LEVELS:
                        self.why = "levels"
                        return None
                level = levels.index(key_ds)
                cell = (level, tx, ty)
                held = mine.get(cell)
                if held is not None and held is not plane and held.identity != plane.identity:
                    self.why = "cell conflict %r" % (cell,)
                    return None                     # two planes in one cell
                mine[cell] = plane
                extent_w, extent_h = max(extent_w, x1), max(extent_h, y1)
                own_extent = (max(own_extent[0], x1), max(own_extent[1], y1))
            if len({cell[0] for cell in mine}) > MAX_LEVELS_PER_CHANNEL:
                self.why = "levels per channel"
                return None
            used_levels = tuple((level, levels[level]) for level in {cell[0] for cell in mine})
            self._channel_cells[channel] = (sig, mine, used_levels, own_extent)
        self._extent_next = (extent_w, extent_h)
        return VtPlan(composed_rows, levels, cells, fusion)

    # ── GL ──────────────────────────────────────────────────────────────

    def _layout(self, gl, levels, extent) -> bool:
        """(Re)lay out the page atlas. A new level is APPENDED (the levels
        already laid out keep their place and their entries: only the
        texture grows); a larger slide extent lays everything out again.
        Returns True when the entries had to be dropped."""
        grown = extent[0] > self.extent[0] or extent[1] > self.extent[1]
        if levels == self.level_slots and not grown:
            return False
        appended = (not grown and self.pages is not None
                    and levels[:len(self.level_slots)] == self.level_slots)
        if not appended:
            self.level_slots, self.level_grid, self.level_offset = [], [], []
            self.extent = (max(extent[0], self.extent[0]), max(extent[1], self.extent[1]))
            self.pages = None
        offset = sum(g[0] for g in self.level_grid)
        for ds_x, ds_y in levels[len(self.level_slots):]:
            gw = max(1, int(math.ceil(self.extent[0] / (TILE * ds_x))) + 1)
            gh = max(1, int(math.ceil(self.extent[1] / (TILE * ds_y))) + 1)
            self.level_slots.append((ds_x, ds_y))
            self.level_grid.append((gw, gh))
            self.level_offset.append(offset)
            offset += gw
        if appended:
            layers, height, width = self.pages.shape
            new_height = max(height, max(g[1] for g in self.level_grid))
            grown_pages = np.full((layers, new_height, offset), -1, np.int32)
            grown_pages[:, :height, :width] = self.pages
            self.pages = grown_pages
            return False
        return True

    def sync(self, gl, store, plan: VtPlan, descriptor_key) -> bool:
        """Bring the GL page/meta/param textures to `plan`. False: cannot."""
        if not plan.levels:
            return False                      # nothing to page: the empty case draws per channel
        before = (len(self.level_slots), self.extent)
        relaid = self._layout(gl, plan.levels, self._extent_next)
        self.last_layout = ("reset" if relaid else
                            "append" if before != (len(self.level_slots), self.extent) else "same")
        width = sum(g[0] for g in self.level_grid)
        height = max(g[1] for g in self.level_grid)
        for channel, _source, _params in plan.rows:
            if channel not in self.channel_slots:
                self.channel_slots[channel] = len(self.channel_slots)
        layers = max(1, len(self.channel_slots))
        if width > self.max_texture or height > self.max_texture or layers > self.max_layers:
            return False
        shape = (layers, height, width)
        if relaid or self.pages is None:
            self.pages = np.full(shape, -1, np.int32)
            self._written = {}
        elif self.pages.shape != shape:
            # more channels (or an appended level): the entries are kept
            grown_pages = np.full(shape, -1, np.int32)
            old = self.pages
            grown_pages[:old.shape[0], :old.shape[1], :old.shape[2]] = old
            self.pages = grown_pages
        full = self.pages_shape != shape or not self.pages_tex
        changed_layers = []
        drawn = set(plan.cells)
        for channel, slot in self.channel_slots.items():
            cells = plan.cells.get(channel)
            if cells is None:
                if channel in self._written:      # no longer drawn: its page empties
                    self.pages[slot].fill(-1)
                    del self._written[channel]
                    changed_layers.append((slot, 0, height))
                continue
            if self._written.get(channel) is cells:
                continue                          # the very same cells: nothing to write
            # write only what changed against the cells last written
            previous = self._written.get(channel) or {}
            layer_pages = self.pages[slot]
            touched_rows = []
            for cell in previous.keys() - cells.keys():
                level, tx, ty = cell
                layer_pages[ty, self.level_offset[level] + tx] = -1
                touched_rows.append(ty)
            for cell, plane in cells.items():
                if previous.get(cell) is plane and not self._moved(store, plane):
                    continue
                level, tx, ty = cell
                gw, gh = self.level_grid[level]
                if tx >= gw or ty >= gh:
                    return False
                block, layer = store.slot(plane)
                layer_pages[ty, self.level_offset[level] + tx] = (
                    (_family(block.fmt) << 24) | (block.vt_index << 16) | layer)
                touched_rows.append(ty)
            if touched_rows:
                changed_layers.append((slot, min(touched_rows), max(touched_rows) + 1))
            self._written[channel] = cells
        del drawn
        self.last_written = len(changed_layers)
        if full:
            self._upload_pages(gl, self.pages, full=True)
        elif changed_layers:
            self._upload_pages(gl, self.pages, full=False, changed=changed_layers)
        self._sync_meta(gl, store)
        self.synced = descriptor_key
        return True

    def _moved(self, store, plane) -> bool:
        """Did this plane's slot change since its cell was written (an
        eviction and a re-upload put it elsewhere)?"""
        now = store.slots.get(plane.identity)
        was = self._slot_of.get(id(plane))
        self._slot_of[id(plane)] = now
        return was is not None and was != now

    def _upload_pages(self, gl, pages, full, changed=None) -> None:
        layers, height, width = pages.shape
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        if full or not self.pages_tex:
            if not self.pages_tex:
                self.pages_tex = int(np.asarray(gl.glGenTextures(1)).reshape(-1)[0])
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.pages_tex)
            for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, name, gl.GL_NEAREST)
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_R32I, width, height, layers, 0,
                            gl.GL_RED_INTEGER, gl.GL_INT, np.ascontiguousarray(pages))
            self.pages_shape = pages.shape
        else:
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.pages_tex)
            for layer, row0, row1 in changed:     # the changed rows of one channel
                gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, int(row0), int(layer),
                                   width, int(row1 - row0), 1, gl.GL_RED_INTEGER, gl.GL_INT,
                                   np.ascontiguousarray(pages[layer, row0:row1]))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)

    def _sync_meta(self, gl, store) -> None:
        """Per (family, block) three rows: rect, valid rect, size -- the
        exact float32 numbers the per-plane path hands its uniforms.
        Follows the store's upload/eviction log; rebuilt only when the
        block layout changed."""
        width = max([block.layers for block in store.blocks] + [1])
        height = (self.u_blocks + self.f_blocks) * BLOCK_ROWS
        rebuild = self.meta.shape[:2] != (height, width) or not self.meta_tex
        log = store.meta_log
        start = getattr(self, "_meta_seen", 0)
        if start > len(log):
            rebuild = True                       # the log was reset (store cleared)
        if rebuild:
            self.meta = np.zeros((height, width, 4), np.float32)
            entries = [(block, layer, identity) for identity, (block, layer) in store.slots.items()]
        else:
            entries = log[start:]
        touched = set()
        for block, layer, identity in entries:
            row = (_family(block.fmt) * self.u_blocks + block.vt_index) * BLOCK_ROWS
            if identity is None or identity not in store.slots or store.slots[identity] != (block, layer):
                self.meta[row:row + 3, layer] = 0.0
            else:
                rect, valid, size, _fmt = store.meta[identity]
                self.meta[row, layer] = rect
                self.meta[row + 1, layer] = valid if valid is not None else (0.0, 0.0, 0.0, 0.0)
                self.meta[row + 2, layer] = (size[0], size[1], 0.0, 0.0)
            touched.add(row)
        self._meta_seen = len(log)
        if len(log) > 100000:                    # keep the log bounded
            del log[:]
            self._meta_seen = 0
        if not self.meta_tex:
            self.meta_tex = int(np.asarray(gl.glGenTextures(1)).reshape(-1)[0])
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.meta_tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        if rebuild:
            for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, name, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA32F, width, height, 0,
                            gl.GL_RGBA, gl.GL_FLOAT, self.meta)
        else:
            for row in sorted(touched):          # one block's three rows at a time
                gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, row, width, BLOCK_ROWS,
                                   gl.GL_RGBA, gl.GL_FLOAT,
                                   np.ascontiguousarray(self.meta[row:row + BLOCK_ROWS]))
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def upload_params(self, gl, plan: VtPlan) -> None:
        rows = np.zeros((max(1, len(plan.rows)), PARAM_TEXELS, 4), np.float32)
        for row, (channel, _source, params) in enumerate(plan.rows):
            lo, hi, gamma = params["mapping"]
            rows[row, 0] = (lo, hi, gamma, self.channel_slots[channel])
            r, g, b = params["color"]
            rows[row, 1] = (r, g, b, params["weight"])
            cells = plan.cells.get(channel, {})
            used = sorted({cell[0] for cell in cells},
                          key=lambda level: plan.levels[level][0])        # finest first
            rows[row, 2] = (params.get("group", 0), params.get("group_weight", 0.0),
                            len(used), 0.0)
            padded = (used + [0] * 8)[:8]
            rows[row, 3] = padded[:4]
            rows[row, 4] = padded[4:]
        # A9 §35: allocated once (grown when needed) and then only WRITTEN:
        # re-specifying a texture every frame can make the driver wait for
        # the frame still reading it (measured: the paint -> present tail)
        if rows.tobytes() == getattr(self, "_params_last", None):
            return
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        capacity = getattr(self, "_params_rows", 0)
        if not self.params_tex or rows.shape[0] > capacity:
            if not self.params_tex:
                self.params_tex = int(np.asarray(gl.glGenTextures(1)).reshape(-1)[0])
            capacity = max(64, 1 << (int(rows.shape[0]) - 1).bit_length())
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.params_tex)
            for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, name, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA32F, PARAM_TEXELS, capacity, 0,
                            gl.GL_RGBA, gl.GL_FLOAT, None)
            self._params_rows = capacity
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.params_tex)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, PARAM_TEXELS, rows.shape[0],
                           gl.GL_RGBA, gl.GL_FLOAT, rows)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        self._params_last = rows.tobytes()

    def level_uniforms(self, plan: VtPlan):
        a = np.zeros((MAX_LEVELS, 4), np.float32)
        b = np.zeros((MAX_LEVELS, 2), np.int32)
        for index, (ds_x, ds_y) in enumerate(self.level_slots):
            gw, gh = self.level_grid[index]
            a[index] = (TILE * ds_x, TILE * ds_y, self.level_offset[index], 0.0)
            b[index] = (gw, gh)
        return a, b

    def clear(self, gl) -> None:
        names = [n for n in (self.pages_tex, self.meta_tex, self.params_tex) if n]
        if names and gl is not None:
            gl.glDeleteTextures(names)
        self.__init__()
