"""v16_5 R3b plan §3.1-3.2: the core's label reading and planning against
Python's, value for value (no GPU).

  * reading: a synthetic mask with an offset bounding box, odd chunks,
    missing chunk files and ids above 2^24, with its label pyramid built by
    `ensure_pyramid`; every tile of every level (and some outside) read by
    the core equals `read_label_tile` -- ids, world rect, or "unavailable";
  * planning: hundreds of random states -- level, visible tiles, held tiles
    of several levels / generations / kinds in arrival order, which masks
    are shown and have a pyramid, budgets from tight to 256 MB -- given to
    the core's planner and to `Step3LabelBinding._plan` (its thread
    stopped, its state set directly: a test device, not product behaviour)
    give the same wanted tiles in the same order, the same held tiles in
    the same order, the same tiles released, the same job list for the
    reader (with tiles already being read left out) and the same budget note.
"""

import os
import random
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
fvc = pytest.importorskip("fusion_viewer_core")
zarr = pytest.importorskip("zarr")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core import label_pyramid as lp  # noqa: E402
from block01.core import step3_masks as sm  # noqa: E402
from block01.ui.step1_gpu_layer import LabelPlane  # noqa: E402
from block01.ui.step3_core_labels import describe  # noqa: E402
from block01.ui.step3_label_binding import Step3LabelBinding  # noqa: E402

SHAPES = [(2100, 1700), (1050, 850), (525, 425)]
T = 512


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    d = tmp_path_factory.mktemp("r3b_mask")
    rng = np.random.default_rng(3)
    y0, x0, h, w = 37, 91, 1500, 1300
    ids = (rng.integers(0, 40, size=(h // 8 + 1, w // 8 + 1)) * 2 ** 20 + 3).astype(np.uint32)
    ids = np.kron(ids, np.ones((8, 8), np.uint32))[:h, :w]
    ids[rng.random((h, w)) < 0.02] = 0
    path = str(d / "global_mask_ROI_1.zarr")
    z = zarr.open(path, mode="w", shape=ids.shape, chunks=(256, 384), dtype="<u4")
    z[:] = ids
    for name in ("1.2", "4.0"):                      # missing chunks read as the fill (0)
        os.remove(os.path.join(path, name))
    src = sm.MaskSource(kind="cell", mask_path=os.path.realpath(path), bbox=(y0, y0 + h, x0, x0 + w),
                        pyramid_path=lp.pyramid_path_for(os.path.realpath(path)), pyramid_kind="cell",
                        pyramid=None, pyramid_reason="no label pyramid")
    built = sm.ensure_pyramid(src, SHAPES)
    assert built.status == "ready", built
    return built.source


def test_a_reading_matches_python_tile_for_tile(source):
    spec = describe(source)
    level0_only = dict(spec, pyramid=None)
    checked = 0
    for level, (h, w) in enumerate(SHAPES):
        for ty in range(-1, h // T + 2):
            for tx in range(-1, w // T + 2):
                if tx < 0 or ty < 0:
                    continue
                for sp, src in ((spec, source),
                                (level0_only, SimpleNamespace(**{**source.__dict__, "pyramid": None}))):
                    want = sm.read_label_tile(src, level, tx, ty, T, SHAPES)
                    got = fvc.read_label_tile_for_test(sp, SHAPES, level, tx, ty)
                    if isinstance(want, sm.LabelTile):
                        rows, cols, data, rect = got
                        ids = np.frombuffer(data, np.uint32).reshape(rows, cols)
                        np.testing.assert_array_equal(ids, want.labels, err_msg=f"{level} {tx} {ty}")
                        assert tuple(rect) == tuple(want.world_rect), (level, tx, ty)
                        checked += 1
                    else:
                        assert isinstance(got, str), (level, tx, ty, got)
    assert checked > 20


class _Controller(QtCore.QObject):
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)

    def snapshot(self):
        return SimpleNamespace(level=0, visible_tiles=frozenset(), epoch=0)


class _Layer:
    labels_enabled = True

    def set_labels(self, _snapshot):
        return {}


def _grid(level):
    h, w = SHAPES[level]
    return -(-w // T), -(-h // T)


def _rect(level, tx, ty):
    h, w = SHAPES[level]
    ds_y, ds_x = SHAPES[0][0] / h, SHAPES[0][1] / w
    rows, cols = min(T, h - ty * T), min(T, w - tx * T)
    x0, y0 = tx * T * ds_x, ty * T * ds_y
    return (x0, x0 + cols * ds_x, y0, y0 + rows * ds_y), (rows, cols)


def test_b_planning_matches_python_state_for_state(app):
    rng = random.Random(11)
    binding = Step3LabelBinding(controller=_Controller(), layer=_Layer(), level_shapes=SHAPES, tile_size=T)
    binding.dispose(timeout=5.0)                    # the thread ends: nothing takes the jobs
    pyramid = SimpleNamespace(attrs={"levels": []}, where="disk")
    compared = notes = 0
    for case in range(400):
        level = rng.randrange(len(SHAPES))
        nx, ny = _grid(level)
        tx0, ty0 = rng.randrange(-1, nx + 1), rng.randrange(-1, ny + 1)
        visible = {(tx, ty) for tx in range(tx0, tx0 + rng.randrange(0, 4))
                   for ty in range(ty0, ty0 + rng.randrange(0, 4)) if tx >= 0 and ty >= 0}
        gen = rng.randrange(1, 4)
        has = [rng.random() < 0.8, rng.random() < 0.6]
        visible_style = [rng.random() < 0.8, rng.random() < 0.8]
        pyr = [rng.random() < 0.7, rng.random() < 0.7]
        resident = []
        for _ in range(rng.randrange(0, 14)):
            lv = rng.randrange(len(SHAPES))
            gx, gy = _grid(lv)
            key = (rng.choice([gen, gen, gen - 1]), rng.choice(["cell", "nucleus"]), lv,
                   rng.randrange(gx), rng.randrange(gy))
            if key not in [k for k, _ in resident]:
                resident.append((key, _rect(lv, key[3], key[4])))
        budget = rng.choice([1, 2, 3, 6, 12, 256]) * 2 ** 20
        gx, gy = _grid(level)
        reading = {(gen, rng.choice(["cell", "nucleus"]), level, rng.randrange(gx), rng.randrange(gy))
                   for _ in range(rng.randrange(0, 4))}
        # Python
        binding.budget_bytes = budget
        binding._generation = gen
        binding._sources = {kind: (sm.MaskSource(kind=kind, mask_path="", bbox=(0, 1, 0, 1), pyramid_path="",
                                                 pyramid_kind=kind, pyramid=pyramid if pyr[i] else None)
                                   if has[i] else None)
                            for i, kind in enumerate(("cell", "nucleus"))}
        binding._styles = {kind: binding._styles[kind].__class__(visible=visible_style[i])
                           for i, kind in enumerate(("cell", "nucleus"))}
        binding._resident = {key: LabelPlane(identity=key, world_rect=rect,
                                             ids=np.zeros(shape, np.uint32))
                             for key, (rect, shape) in resident}
        binding._reading = set(reading)
        binding._jobs = []
        binding._planned_epoch = None
        binding._plan(SimpleNamespace(level=level, visible_tiles=frozenset(visible), epoch=case))
        py_wanted = sorted(binding._wanted, key=binding._wanted.get)
        py_keep = list(binding._resident)
        py_released = [k for k, _r in resident if k not in binding._resident]
        py_jobs = [j["key"] for j in binding._jobs if j["type"] == "tile"]
        # Rust
        kind_index = {"cell": 0, "nucleus": 1}
        shown = [has[i] and visible_style[i] for i in range(2)]
        as_core = lambda k: (k[0], kind_index[k[1]], k[2], k[3], k[4])   # noqa: E731
        got = fvc.plan_labels_for_test(SHAPES, level, sorted(visible), gen, shown, pyr,
                                       [(as_core(k), rect) for k, (rect, _s) in resident],
                                       [as_core(k) for k in reading], budget)
        assert got is not None
        wanted, keep, released, jobs, note = got
        name = {0: "cell", 1: "nucleus"}
        as_py = lambda keys: [(k[0], name[k[1]], k[2], k[3], k[4]) for k in keys]   # noqa: E731
        assert as_py(wanted) == py_wanted, case
        assert as_py(keep) == py_keep, case
        assert as_py(released) == py_released, case
        assert as_py(jobs) == py_jobs, case
        assert note == binding._budget_note, case
        compared += 1
        notes += note is not None
    assert compared == 400 and notes > 10
