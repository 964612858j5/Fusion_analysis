"""v16_5 S-Raw: Rust admission and tile cover equal the Python binding's.

The REAL `Step1GpuBinding._admit` (raw uint8 channels, array-slot
accounting, the shipped `demo_budgets()` and block slack) and the Rust
`admit` get the same inputs -- Kevin's pyramid geometry, the controller's
level and clamped box, the active channel count -- over random views and
the edge cases (a box on exact tile multiples, the slide's edges, enough
channels to force "resolution limited" and coarse-only). Every field of the
answer must match: level, tiles, coarse-only, fine bytes, carry, rect.
"""

import math
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt5 import QtCore, QtWidgets

fvc = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_gpu_binding import Step1GpuBinding  # noqa: E402
from block01.ui.step1_gpu_layer import MODE_OVERLAY, DisplaySnapshot, ViewportSnapshot  # noqa: E402
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402
from block01.ui.step1_gpu_layer import ARRAY_TILE, _block_layers  # noqa: E402
from block01.viewer import request_planning as planning  # noqa: E402
from block01.viewer.tile_types import SourceIdentity, TileGridSpec  # noqa: E402

#: Kevin TMA3 (OME-TIFF) and TMA4 (qptiff) level shapes, (h, w)
TMA3 = [(50400, 31680), (25200, 15840), (12600, 7920), (6300, 3960), (3150, 1980), (1575, 990)]
TMA4 = [(46800, 26880), (23400, 13440), (11700, 6720), (5850, 3360), (2925, 1680), (1462, 840)]
GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
MAX_GL_LAYERS = 2048


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Table:
    def source_of(self, _channel):
        return "raw"


class _Provider:
    def __init__(self, shapes):
        self.shapes = shapes
        self.num_levels = len(shapes)
        self.source_table = _Table()
        self._dtype = "uint8"

    def source_identity(self):
        return SourceIdentity("kevin", "1:1", "step1", "token")

    def native_source_identity(self):
        return SourceIdentity("kevin", "1:1", "step1-native", "token")

    def level_shape(self, level):
        return self.shapes[level]

    def level_downsample(self, level):
        return round(self.shapes[0][0] / self.shapes[level][0])

    def level_downsample_yx(self, level):
        (h0, w0), (h, w) = self.shapes[0], self.shapes[level]
        return h0 / h, w0 / w

    def missing_products(self):
        return []


class _Controller(QtCore.QObject):
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)

    def __init__(self, provider):
        super().__init__()
        self.grid = GRID
        self.provider = provider
        self.snap = None

    def snapshot(self):
        return self.snap


class _Scheduler:
    def request(self, request, callback):
        pass

    def cancel_generation(self, generation):
        pass


class _Layer:
    tile_arrays_active = True
    upload_budget_ms = None
    use_tile_arrays = True

    def array_slack_bytes(self, itemsizes=(1,)):
        return sum(_block_layers(demo_budgets().max_raw_texture_bytes, MAX_GL_LAYERS, int(i))
                   * ARRAY_TILE * ARRAY_TILE * int(i) for i in set(itemsizes))

    def submit(self, *_a):
        return {"cache": {}, "pass_count": 0}


def _python(app, shapes, channels):
    provider = _Provider(shapes)
    controller = _Controller(provider)
    names = [f"c{i:02d}" for i in range(channels)]
    display = DisplaySnapshot(MODE_OVERLAY, {n: (0.0, 255.0, 1.0) for n in names},
                              {n: 1.0 for n in names}, {n: (1.0, 1.0, 1.0) for n in names})
    h0, w0 = shapes[0]
    controller.snap = SimpleNamespace(source=provider.source_identity(), level=len(shapes) - 1,
                                      visible_tiles=frozenset(), epoch=0, bbox_l0=(0, 0, h0, w0))
    binding = Step1GpuBinding(provider=provider, scheduler=_Scheduler(), controller=controller,
                              layer=_Layer(), build_display_snapshot=lambda: display,
                              build_viewport_snapshot=lambda: ViewportSnapshot((0, w0, 0, h0), (512, 512)),
                              budgets=demo_budgets())
    binding.source_changed()
    binding._active_fine_channels = tuple(sorted(names))
    return binding, provider, names


def _snapshot(provider, level, bbox, epoch):
    visible = planning.visible_tiles_for_viewport(bbox, provider.level_downsample(level), 512)
    return SimpleNamespace(source=provider.source_identity(), level=level,
                           visible_tiles=frozenset(visible), epoch=epoch, bbox_l0=bbox)


def _cases(shapes, rng, n=150):
    h0, w0 = shapes[0]
    out = [(0, (0, 0, h0, w0)), (len(shapes) - 1, (0, 0, h0, w0))]
    for level in range(len(shapes)):                   # boxes on exact tile multiples
        step = 512 * round(h0 / shapes[level][0])
        out.append((level, (step, step, 3 * step, 5 * step)))
        out.append((level, (h0 - step, w0 - step, h0, w0)))
    for _ in range(n):
        level = int(rng.integers(0, len(shapes)))
        ds = round(h0 / shapes[level][0])
        span_y, span_x = int(rng.uniform(0.3, 2.5) * 1080 * ds), int(rng.uniform(0.3, 2.5) * 1920 * ds)
        cy, cx = int(rng.uniform(0, h0)), int(rng.uniform(0, w0))
        y0, x0, y1, x1 = cy - span_y // 2, cx - span_x // 2, cy + span_y // 2, cx + span_x // 2
        out.append((level, planning.clamp_viewport_to_level0(y0, x0, y1, x1, h0, w0)))
    return out


@pytest.mark.parametrize("shapes", [TMA3, TMA4], ids=["tma3", "tma4"])
@pytest.mark.parametrize("channels", [3, 20, 69])
def test_a_admission_matches_the_python_binding(app, shapes, channels):
    binding, provider, names = _python(app, shapes, channels)
    budgets = demo_budgets()
    total = budgets.max_raw_texture_bytes - _Layer().array_slack_bytes({1})
    rng = np.random.default_rng(channels)
    limited = 0
    for epoch, (level, bbox) in enumerate(_cases(shapes, rng), start=1):
        py = binding._admit(_snapshot(provider, level, tuple(bbox), epoch))
        rs = fvc.admit_for_test(shapes, level, list(bbox), channels,
                                budgets.max_fine_tiles_per_viewport,
                                budgets.max_fine_plane_bytes_per_channel, total)
        where = (level, bbox)
        assert rs["level"] == py.level and rs["coarse_only"] == py.coarse_only, where
        keys = py.keys.get(names[0], frozenset())
        assert sorted((k.tile.tx, k.tile.ty) for k in keys) == rs["tiles"], where
        assert rs["fine_bytes"] == py.fine_bytes.get(names[0], 0), where
        assert rs["carry"] == py.carry[names[0]], where
        if py.rect is None:
            assert rs["rect"] is None, where
        else:
            assert rs["rect"] is not None and all(
                math.isclose(a, b, rel_tol=0, abs_tol=1e-9) for a, b in zip(rs["rect"], py.rect)), where
        limited += py.limited
    if channels == 69:
        assert limited, "the 69-channel cases never limited the resolution"


@pytest.mark.parametrize("shapes", [TMA3, TMA4], ids=["tma3", "tma4"])
def test_b_tile_cover_matches_request_planning(shapes):
    rng = np.random.default_rng(5)
    for level, bbox in _cases(shapes, rng, n=300):
        for at in range(level, len(shapes)):
            ds = round(shapes[0][0] / shapes[at][0])
            want = sorted(planning.visible_tiles_for_viewport(tuple(bbox), ds, 512))
            assert fvc.cover_for_test(shapes, list(bbox), at) == want, (level, at, bbox)
