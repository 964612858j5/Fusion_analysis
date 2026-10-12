"""v16_5 R2 (plan §2.6, §5.2): Rust admission equals the Python binding's
with channels of DIFFERENT texel sizes -- raw channels at the slide's own
integer size, corrected ones (and raw float slides) at 4 bytes -- and the
slack of one block per format in use.

The REAL `Step1GpuBinding._admit` (array-slot accounting, `demo_budgets()`)
against `admit_for_test(..., texels=[...])` over random views and the edge
cases of the S-Raw test: level, tiles, coarse-only, rect, and per channel
the fine bytes and the stand-in allowance.
"""

import numpy as np
import pytest
from PyQt5 import QtWidgets

fvc = pytest.importorskip("fusion_viewer_core")

import test_v16_5_sraw_admission as base  # noqa: E402  -- the S-Raw rig
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402

KINDS = {"uint8": 1, "uint16": 2, "float32": 4}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _MixedTable:
    def __init__(self, corrected):
        self.corrected = set(corrected)

    def source_of(self, channel):
        return "corrected" if channel in self.corrected else "raw"


def _python(app, shapes, channels, dtype, corrected):
    binding, provider, names = base._python(app, shapes, channels)
    provider._dtype = dtype
    provider.source_table = _MixedTable(names[i] for i in corrected)
    binding._native_pixel_bytes = KINDS[dtype] if dtype != "float32" else 4
    binding._admission = None
    return binding, provider, names


@pytest.mark.parametrize("dtype,channels,corrected", [
    ("uint16", 20, ()),
    ("uint16", 20, (1, 4, 7)),
    ("uint8", 12, tuple(range(0, 12, 2))),
    ("uint8", 3, (0, 1, 2)),
    ("float32", 6, ()),
])
def test_a_mixed_texel_admission_matches(app, dtype, channels, corrected):
    binding, provider, names = _python(app, base.TMA3, channels, dtype, corrected)
    budgets = demo_budgets()
    native = KINDS[dtype]
    itemsizes = {native} | ({4} if corrected else set())
    total = budgets.max_raw_texture_bytes - base._Layer().array_slack_bytes(itemsizes)
    texels = [4 if i in corrected else native for i in range(channels)]
    rng = np.random.default_rng(channels + len(corrected))
    for epoch, (level, bbox) in enumerate(base._cases(base.TMA3, rng, n=80), start=1):
        py = binding._admit(base._snapshot(provider, level, tuple(bbox), epoch))
        rs = fvc.admit_for_test(base.TMA3, level, list(bbox), channels,
                                budgets.max_fine_tiles_per_viewport,
                                budgets.max_fine_plane_bytes_per_channel, total, texels)
        where = (dtype, corrected, level, bbox)
        assert rs["level"] == py.level and rs["coarse_only"] == py.coarse_only, where
        keys = py.keys.get(names[0], frozenset())
        assert sorted((k.tile.tx, k.tile.ty) for k in keys) == rs["tiles"], where
        for i, name in enumerate(names):
            assert rs["fine_bytes_per_channel"][i] == py.fine_bytes.get(name, 0), (where, name)
            assert rs["carry_per_channel"][i] == py.carry[name], (where, name)
