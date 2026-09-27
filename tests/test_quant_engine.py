"""Block S4-1: Step4's streaming quantification (`core/quant_engine.py`).

Against independent float64 references on synthetic labels:
  * scipy.ndimage per-label count / sum / mean / std / min / max;
  * skimage `regionprops`: centroid, bbox, axis lengths, eccentricity,
    orientation, equivalent diameter, extent;
  * `boundary_pixel_count` against a whole-image label-aware 3x3 reference
    (max / min filter, background outside);
  * results do not depend on the tile size (including sizes that do not
    divide the region), the channel batch or the thread count;
  * empty labels are not output and are listed; a label above the store's
    object count is refused; only the chosen statistics are output;
  * one accumulator set whatever the thread count; Stop ends the job.
"""

import numpy as np
import pytest
from scipy import ndimage as ndi
from skimage.measure import regionprops

from block01.core import quant_engine as qe
from block01.core.quant_sources import ChannelSource

ORIGIN = (1000, 20000)       # region origin in slide pixels


class ArrayReader:
    """A QuantReader over in-memory arrays (region coordinates)."""

    def __init__(self, labels, channels):
        self.labels_arr = labels
        self.data = channels         # list of arrays, one per channel
        self.shape = labels.shape
        self.reads = {"raw": 0, "corrected": 0}

    def channel_dtype(self, ch):
        return self.data[ch.index].dtype

    def labels(self, y0, y1, x0, x1):
        H, W = self.shape
        out = np.zeros((y1 - y0 + 2, x1 - x0 + 2), np.uint32)
        ry0, ry1, rx0, rx1 = max(0, y0 - 1), min(H, y1 + 1), max(0, x0 - 1), min(W, x1 + 1)
        out[ry0 - y0 + 1:ry1 - y0 + 1, rx0 - x0 + 1:rx1 - x0 + 1] = self.labels_arr[ry0:ry1, rx0:rx1]
        return out

    def channels(self, sources, y0, y1, x0, x1):
        return np.stack([self.data[s.index][y0:y1, x0:x1] for s in sources])


class Job:
    def __init__(self, labels, channels, n_objects):
        self.shape = labels.shape
        self.bbox = (ORIGIN[0], ORIGIN[0] + labels.shape[0], ORIGIN[1], ORIGIN[1] + labels.shape[1])
        self.n_objects = n_objects
        self.channels = tuple(
            ChannelSource(name=f"ch{i}", index=i, decision="original",
                          kind="corrected" if a.dtype == np.float32 else "raw", path="")
            for i, a in enumerate(channels))


def make_case(seed=0, shape=(173, 211), n=60):
    rng = np.random.default_rng(seed)
    lab = np.zeros(shape, np.uint32)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for k in range(1, n + 1):
        cy, cx = rng.integers(0, shape[0]), rng.integers(0, shape[1])
        a, b = rng.integers(2, 14, size=2)
        t = rng.random() * np.pi
        u = (yy - cy) * np.cos(t) + (xx - cx) * np.sin(t)
        v = -(yy - cy) * np.sin(t) + (xx - cx) * np.cos(t)
        lab[(u / a) ** 2 + (v / b) ** 2 <= 1] = k
    lab[5, 5] = n + 1                      # a one-pixel cell
    lab[0:3, 100:140] = n + 2              # a line on the region edge
    chans = [rng.integers(0, 256, size=shape, dtype=np.uint8),
             (rng.random(shape, dtype=np.float32) * 1000 - 20).astype(np.float32),
             rng.integers(0, 256, size=shape, dtype=np.uint8)]
    return lab, chans, n + 2


def run(lab, chans, n_objects, stats=qe.FAST_STATS, **kw):
    job = Job(lab, chans, n_objects)
    res, timing = qe.quantify(job, ArrayReader(lab, chans), stats,
                              settings=qe.QuantSettings(**kw))
    return res, timing


def col(res, name):
    return res.values[:, res.columns.index(name)]


def boundary_reference(lab):
    mx = ndi.maximum_filter(lab, size=3, mode="constant", cval=0)
    mn = ndi.minimum_filter(lab, size=3, mode="constant", cval=0)
    edge = (lab > 0) & ((mx != lab) | (mn != lab))
    return np.bincount(lab[edge], minlength=int(lab.max()) + 1)


def test_statistics_equal_scipy_per_label():
    lab, chans, n = make_case()
    res, _t = run(lab, chans, n, tile=64)
    ids = res.cell_ids.astype(int)
    for ci, img in enumerate(chans):
        f = img.astype(np.float64)
        ref = {"sum": ndi.sum(f, lab, ids), "mean": ndi.mean(f, lab, ids),
               "std": ndi.standard_deviation(f, lab, ids),
               "min": ndi.minimum(f, lab, ids), "max": ndi.maximum(f, lab, ids)}
        for stat, want in ref.items():
            got = col(res, f"ch{ci}_{stat}")
            if stat in ("min", "max"):
                np.testing.assert_array_equal(got, want)
            else:
                np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-9)


def test_morphology_equals_skimage_regionprops():
    lab, chans, n = make_case(seed=1)
    res, _t = run(lab, chans, n, tile=50)
    props = {p.label: p for p in regionprops(lab.astype(np.int64))}
    ids = res.cell_ids.astype(int)
    assert sorted(props) == list(ids)
    oy, ox = ORIGIN

    def ref(fn):
        return np.array([fn(props[i]) for i in ids], np.float64)
    np.testing.assert_array_equal(col(res, "area"), ref(lambda p: p.area))
    np.testing.assert_allclose(col(res, "centroid_y"), ref(lambda p: p.centroid[0] + oy),
                               rtol=0, atol=1e-9)
    np.testing.assert_allclose(col(res, "centroid_x"), ref(lambda p: p.centroid[1] + ox),
                               rtol=0, atol=1e-9)
    for k, name in enumerate(["bbox_min_y", "bbox_min_x", "bbox_max_y", "bbox_max_x"]):
        np.testing.assert_array_equal(col(res, name),
                                      ref(lambda p, k=k: p.bbox[k] + (oy if k % 2 == 0 else ox)))
    for name, fn in [("major_axis", lambda p: p.axis_major_length),
                     ("minor_axis", lambda p: p.axis_minor_length),
                     ("orientation", lambda p: p.orientation),
                     ("equivalent_diameter", lambda p: p.equivalent_diameter_area),
                     ("extent", lambda p: p.extent)]:
        np.testing.assert_allclose(col(res, name), ref(fn), rtol=0, atol=1e-8, err_msg=name)
    # eccentricity = sqrt(1 - l2/l1): for a near-circular cell the square
    # root turns 1e-14 of rounding into ~1e-7, so the square is compared
    # tightly and the value itself at the real-data gate (1e-6)
    ecc = col(res, "eccentricity")
    want_ecc = ref(lambda p: p.eccentricity)
    np.testing.assert_allclose(ecc ** 2, want_ecc ** 2, rtol=0, atol=1e-12)
    np.testing.assert_allclose(ecc, want_ecc, rtol=0, atol=1e-6)
    minor = ref(lambda p: p.axis_minor_length)
    major = ref(lambda p: p.axis_major_length)
    want = np.where(minor > 0, major / np.where(minor > 0, minor, 1), np.nan)
    np.testing.assert_allclose(col(res, "aspect_ratio"), want, rtol=1e-12)


def test_orientation_sign_on_a_known_diagonal():
    lab = np.zeros((40, 40), np.uint32)
    for i in range(30):
        lab[5 + i, 5 + i] = 1               # rows grow with columns
        lab[5 + i, 6 + i] = 1
    res, _t = run(lab, [np.ones((40, 40), np.uint8)], 1, tile=16)
    want = regionprops(lab.astype(np.int64))[0].orientation
    assert col(res, "orientation")[0] == pytest.approx(want, abs=1e-12)
    assert want == pytest.approx(np.pi / 4, abs=0.01)   # skimage: rows growing with columns


def test_boundary_pixels_equal_the_label_aware_reference():
    lab, chans, n = make_case(seed=2)
    # touching cells: two halves of one block
    lab[(lab == n - 1) | (lab == n)] = 0
    lab[100:120, 20:30] = n - 1
    lab[100:120, 30:40] = n
    ref = boundary_reference(lab)
    for tile in (17, 64, 1000):
        res, _t = run(lab, chans, n, tile=tile)
        np.testing.assert_array_equal(col(res, "boundary_pixel_count"),
                                      ref[res.cell_ids.astype(int)], err_msg=f"tile {tile}")
    # between the touching halves every edge pixel counts: a 20x10 half has
    # its whole rim, the shared side included
    i = list(res.cell_ids).index(n - 1)
    assert col(res, "boundary_pixel_count")[i] == 2 * 20 + 2 * 10 - 4


@pytest.mark.parametrize("tile", [7, 33, 64, 173, 4096])
def test_results_do_not_depend_on_the_tile_size(tile):
    lab, chans, n = make_case(seed=3)
    base, _t = run(lab, chans, n, tile=211)
    res, _t = run(lab, chans, n, tile=tile)
    np.testing.assert_array_equal(res.cell_ids, base.cell_ids)
    for c, name in enumerate(res.columns):
        if name.startswith("ch1_") and name.split("_", 1)[1] in ("sum", "mean", "std"):
            # float32 channel: the summation order follows the tiles
            np.testing.assert_allclose(res.values[:, c], base.values[:, c], rtol=1e-12)
        else:
            np.testing.assert_array_equal(res.values[:, c], base.values[:, c], err_msg=name)


@pytest.mark.parametrize("batch_bytes,threads", [(1, 1), (64 * 64, 2), (1 << 30, 16)])
def test_results_do_not_depend_on_batches_or_threads(batch_bytes, threads):
    lab, chans, n = make_case(seed=4)
    base, _t = run(lab, chans, n, tile=64)
    res, _t = run(lab, chans, n, tile=64, batch_bytes=batch_bytes, compute_threads=threads)
    np.testing.assert_array_equal(res.values, base.values)


def test_accumulators_do_not_grow_with_threads():
    lab, chans, n = make_case(seed=5)
    _r, t1 = run(lab, chans, n, compute_threads=1)
    _r, t16 = run(lab, chans, n, compute_threads=16)
    assert t1["accumulator_bytes"] == t16["accumulator_bytes"]


def test_empty_labels_are_listed_not_output():
    lab, chans, n = make_case(seed=6)
    lab[lab == 7] = 0
    res, _t = run(lab, chans, n + 3)       # 3 more labels than exist
    assert 7 not in res.cell_ids
    want = [k for k in range(1, n + 4) if not (lab == k).any()]
    assert 7 in want and n + 3 in want
    np.testing.assert_array_equal(res.empty_label_ids, want)
    assert res.max_label_id == n + 3
    assert np.isfinite(res.values).all() or np.isnan(col(res, "aspect_ratio")).any()
    assert np.isfinite(col(res, "ch0_min")).all() and np.isfinite(col(res, "ch1_mean")).all()


def test_a_label_above_the_store_count_is_refused():
    lab, chans, n = make_case(seed=7)
    with pytest.raises(qe.QuantLabelError):
        run(lab, chans, n - 1)


def test_only_the_chosen_statistics_are_output():
    lab, chans, n = make_case(seed=8)
    res, _t = run(lab, chans, n, stats=["max", "mean"])
    stat_cols = [c for c in res.columns if c.startswith("ch")]
    assert stat_cols == ["ch0_mean", "ch0_max", "ch1_mean", "ch1_max", "ch2_mean", "ch2_max"]
    assert res.columns[:len(qe.MORPHOLOGY_COLUMNS) + 1] == ["cell_id"] + list(qe.MORPHOLOGY_COLUMNS)
    with pytest.raises(ValueError):
        qe.normalize_statistics(["median"])


def test_stop_ends_the_job():
    lab, chans, n = make_case(seed=9)
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 2
    with pytest.raises(qe.QuantStopped):
        qe.quantify(Job(lab, chans, n), ArrayReader(lab, chans), qe.FAST_STATS,
                    settings=qe.QuantSettings(tile=16), should_stop=stop)


def test_other_backends_are_named_not_implemented():
    for cls in (qe.RustBackend, qe.CUDABackend):
        with pytest.raises(NotImplementedError):
            cls(10, 2, qe.FAST_STATS)
