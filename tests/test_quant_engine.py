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

    def __init__(self, labels, channels, nuclei=None, table=None):
        self.labels_arr = labels
        self.data = channels         # list of arrays, one per channel
        self.nuc, self.table = nuclei, table
        self.shape = labels.shape
        self.reads = {"raw": 0, "corrected": 0}

    def nuclei(self, y0, y1, x0, x1):
        return None if self.nuc is None else np.ascontiguousarray(self.nuc[y0:y1, x0:x1])

    def nucleus_table(self):
        return self.table

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
    def __init__(self, labels, channels, n_objects, has_nuclei=False, compartment="cell"):
        self.shape = labels.shape
        self.has_nuclei = has_nuclei
        self.compartment = compartment
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


def run(lab, chans, n_objects, stats=qe.FAST_STATS, nuclei=None, table=None, regions=(),
        features=("morphology",), compartment="cell", tmp=None, distribution=None,
        markers=None, **kw):
    """The engine in float64 (sink_dtype f8): exact comparisons."""
    import tempfile
    job = Job(lab, chans, n_objects, has_nuclei=nuclei is not None, compartment=compartment)
    kw.setdefault("sink_dtype", "f8")
    res, timing = qe.quantify(job, ArrayReader(lab, chans, nuclei, table), stats,
                              regions=list(regions), features=features,
                              distribution=distribution, markers=markers,
                              sink_path=str(tmp) if tmp else tempfile.mkdtemp(),
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
    assert t1["accumulator_bytes_per_group"] == t16["accumulator_bytes_per_group"]


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
    import tempfile
    with pytest.raises(qe.QuantStopped):
        qe.quantify(Job(lab, chans, n), ArrayReader(lab, chans), qe.FAST_STATS,
                    sink_path=tempfile.mkdtemp(), settings=qe.QuantSettings(tile=16),
                    should_stop=stop)


def test_other_backends_are_named_not_implemented():
    for cls in (qe.RustBackend, qe.CUDABackend):
        with pytest.raises(NotImplementedError):
            cls(10, 2, qe.FAST_STATS)



# ── block S4-2: nucleus / cytoplasm, channel groups, the sink ────────────

def make_nuclei(lab, seed=0, corrupt=True):
    """A nucleus inside most cells (two in some), ids of their own; with
    `corrupt`, the defects of runs made before Step2's seam fix: a nucleus
    partly in a neighbour / background, and one whose cell has no pixels."""
    from scipy import ndimage as ndi
    rng = np.random.default_rng(seed)
    nuc = np.zeros(lab.shape, np.uint32)
    table = [0]
    for k in range(1, int(lab.max()) + 1):
        core = ndi.binary_erosion(lab == k, iterations=2)
        yy, xx = np.nonzero(core)
        if yy.size < 4 or rng.random() < 0.15:          # some cells without a nucleus
            continue
        pick = rng.choice(yy.size, size=min(yy.size, 6), replace=False)
        nuc[yy[pick[:3]], xx[pick[:3]]] = len(table)
        table.append(k)
        if yy.size > 12 and k % 4 == 0:                 # a second nucleus
            nuc[yy[pick[3:]], xx[pick[3:]]] = len(table)
            table.append(k)
    table = np.asarray(table, np.uint32)
    if corrupt:
        n = 1
        ys, xs = np.nonzero(nuc == n)
        nuc[min(lab.shape[0] - 1, ys[0] + 30), xs[0]] = n  # a pixel far from its cell
        table = np.concatenate([table, [int(lab.max())]]).astype(np.uint32)
        orphan = table.size - 1                          # a nucleus of a cell with no pixels
        nuc[0:2, 0:2] = orphan
        lab[lab == table[orphan]] = 0
    return nuc, table


def region_reference(lab, nuc, table, img, ids):
    """float64, pixel by pixel: {region: {stat: values}} over `ids`."""
    from scipy import ndimage as ndi
    inside = (lab > 0) & (nuc > 0) & (table[nuc] == lab)
    masks = {"cell": lab, "nucleus": np.where(inside, lab, 0),
             "cytoplasm": np.where((lab > 0) & ~inside, lab, 0)}
    f = img.astype(np.float64)
    out = {}
    for region, m in masks.items():
        cnt = ndi.sum(np.ones_like(f), m, ids)
        with np.errstate(invalid="ignore"):
            ref = {"sum": ndi.sum(f, m, ids), "mean": ndi.mean(f, m, ids),
                   "std": ndi.standard_deviation(f, m, ids),
                   "min": ndi.minimum(f, m, ids), "max": ndi.maximum(f, m, ids)}
        for k in ref:
            ref[k] = np.where(cnt > 0, ref[k], np.nan)
        out[region] = ref
    return out, inside


def layer(res, name, ci):
    return res.sink.read(name)[:, ci].astype(np.float64)


def test_nucleus_and_cytoplasm_equal_the_pixel_reference(tmp_path):
    lab, chans, n = make_case(seed=11)
    nuc, table = make_nuclei(lab)
    res, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  tile=37, tmp=tmp_path / "s")
    ids = res.ids.astype(int)
    for ci, img in enumerate(chans):
        ref, _inside = region_reference(lab, nuc, table, img, ids)
        for region in ("cell", "nucleus", "cytoplasm"):
            for stat in qe.FAST_STATS:
                got = layer(res, f"{region}_{stat}", ci)
                want = ref[region][stat]
                assert np.array_equal(np.isnan(got), np.isnan(want)), (region, stat)
                ok = ~np.isnan(want)
                if stat in ("min", "max"):
                    np.testing.assert_array_equal(got[ok], want[ok], err_msg=f"{region} {stat}")
                else:
                    np.testing.assert_allclose(got[ok], want[ok], rtol=1e-12, atol=1e-9,
                                               err_msg=f"{region} {stat}")


def test_the_cytoplasm_extremes_are_its_own_not_the_nucleus_ones(tmp_path):
    lab = np.zeros((20, 20), np.uint32)
    lab[2:12, 2:12] = 1
    nuc = np.zeros_like(lab)
    nuc[5:8, 5:8] = 1
    table = np.array([0, 1], np.uint32)
    img = np.full((20, 20), 10, np.uint8)
    img[6, 6] = 250                                      # the brightest pixel is nuclear
    img[6, 5] = 1                                        # the darkest too
    res, _t = run(lab, [img], 1, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  tile=8, tmp=tmp_path / "s")
    assert layer(res, "cell_max", 0)[0] == 250 and layer(res, "cytoplasm_max", 0)[0] == 10
    assert layer(res, "cell_min", 0)[0] == 1 and layer(res, "cytoplasm_min", 0)[0] == 10
    assert layer(res, "nucleus_max", 0)[0] == 250


def test_old_run_defects_follow_the_pixel_definition_without_negatives(tmp_path):
    lab, chans, n = make_case(seed=12)
    nuc, table = make_nuclei(lab, corrupt=True)
    res, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  features=("morphology", "nuclear_summary"), tmp=tmp_path / "s")
    _ref, inside = region_reference(lab, nuc, table, chans[0], res.ids.astype(int))
    outside = (nuc > 0) & ~inside
    assert res.nucleus_outside["pixels"] == int(outside.sum())
    assert res.nucleus_outside["nuclei"] == len(np.unique(nuc[outside]))
    assert (res.obs["cytoplasm_area"] >= 0).all()
    np.testing.assert_array_equal(res.obs["nuclear_area"] + res.obs["cytoplasm_area"],
                                  res.obs["area"])


def test_the_nuclear_summary(tmp_path):
    lab, chans, n = make_case(seed=13)
    nuc, table = make_nuclei(lab, corrupt=False)
    res, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=(),
                  features=("nuclear_summary",), tmp=tmp_path / "s")
    ids = res.ids.astype(int)
    inside = (lab > 0) & (nuc > 0) & (table[nuc] == lab)
    for i, c in enumerate(ids):
        mine = [k for k in range(1, table.size) if table[k] == c and (inside & (nuc == k)).any()]
        areas = [int((inside & (nuc == k)).sum()) for k in mine]
        assert res.obs["n_nuclei"][i] == len(mine)
        assert res.obs["nuclear_area"][i] == sum(areas)
        if mine:
            assert res.obs["nuclear_area_max"][i] == max(areas)
            assert res.obs["nuclear_area_mean"][i] == pytest.approx(sum(areas) / len(mine))
        else:
            assert np.isnan(res.obs["nuclear_area_mean"][i])
    assert (res.obs["n_nuclei"] >= 2).any()
    assert "area" not in res.obs                          # morphology not chosen


@pytest.mark.parametrize("budget", [1, 1 << 40])
def test_channel_groups_do_not_change_the_result(tmp_path, budget):
    lab, chans, n = make_case(seed=14)
    nuc, table = make_nuclei(lab)
    base, tb = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                   tmp=tmp_path / "a")
    res, tr = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  accumulator_budget=budget, tmp=tmp_path / "b")
    assert tr["channel_groups"] == (len(chans) if budget == 1 else 1)
    for name in base.layers:
        np.testing.assert_array_equal(res.sink.read(name), base.sink.read(name), err_msg=name)


def test_unchosen_regions_are_not_allocated():
    acc = qe.ChannelAcc.empty(10, 2, ["mean"], ["cell"])
    assert acc.ns is None and acc.ymn is None and acc.ss is None and acc.mn is None
    acc = qe.ChannelAcc.empty(10, 2, ["mean", "max"], ["cell", "cytoplasm"])
    assert acc.ns is not None and acc.nmx is None and acc.ymx is not None and acc.ymn is None


def test_a_nuclei_only_run_is_one_row_per_nucleus(tmp_path):
    lab, chans, n = make_case(seed=15)
    res, _t = run(lab, chans, n, compartment="nucleus", tmp=tmp_path / "s")
    assert res.id_column == "nucleus_id" and res.regions == ["nucleus"]
    assert res.layers[0] == "nucleus_mean" and res.x_layer == "nucleus_mean"
    with pytest.raises(ValueError):
        run(lab, chans, n, compartment="nucleus", regions=("cytoplasm",), tmp=tmp_path / "t")


def test_regions_need_nuclei(tmp_path):
    lab, chans, n = make_case(seed=16)
    with pytest.raises(ValueError, match="no nuclei"):
        run(lab, chans, n, regions=("nucleus",), tmp=tmp_path / "s")
    with pytest.raises(ValueError, match="no nuclei"):
        run(lab, chans, n, features=("nuclear_summary",), tmp=tmp_path / "t")


def test_the_float32_sink_is_within_1e_6(tmp_path):
    lab, chans, n = make_case(seed=17)
    nuc, table = make_nuclei(lab, corrupt=False)
    exact, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                    tmp=tmp_path / "a")
    f32, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  sink_dtype="f4", tmp=tmp_path / "b")
    for name in exact.layers:
        a, b = exact.sink.read(name), f32.sink.read(name).astype(np.float64)
        assert b.dtype == np.float64 and not np.isinf(b).any()
        ok = ~np.isnan(a)
        np.testing.assert_allclose(b[ok], a[ok], rtol=1e-6, atol=0, err_msg=name)


def test_x_is_the_first_chosen_statistic(tmp_path):
    lab, chans, n = make_case(seed=18)
    res, _t = run(lab, chans, n, stats=["max", "std"], tmp=tmp_path / "s")
    assert res.x_layer == "cell_std" and res.layers == ["cell_std", "cell_max"]


# ── block S4-3: Crofton perimeter, circularity, distribution statistics ──

def test_crofton_equals_skimage_across_tiles_edges_and_touching_cells(tmp_path):
    lab, chans, n = make_case(seed=21)
    lab[(lab == n - 1) | (lab == n)] = 0
    lab[100:120, 20:30] = n - 1                          # two touching cells
    lab[100:120, 30:40] = n
    props = {p.label: p for p in regionprops(lab.astype(np.int64))}
    for tile in (7, 33, 64, 4096):
        res, _t = run(lab, chans, n, tile=tile, tmp=tmp_path / f"s{tile}")
        ids = res.ids.astype(int)
        want = np.array([props[i].perimeter_crofton for i in ids])
        np.testing.assert_allclose(res.obs["perimeter_crofton"], want, rtol=0, atol=1e-9,
                                   err_msg=f"tile {tile}")
        area = np.array([props[i].area for i in ids], float)
        np.testing.assert_allclose(res.obs["circularity"], 4 * np.pi * area / want ** 2,
                                   rtol=1e-12)


def test_circularity_is_not_clipped():
    lab = np.zeros((10, 10), np.uint32)
    lab[4, 4] = 1                                        # one pixel: circularity > 1
    res, _t = run(lab, [np.ones((10, 10), np.uint8)], 1, tile=4)
    assert res.obs["circularity"][0] > 1.0
    want = regionprops(lab.astype(np.int64))[0].perimeter_crofton
    assert res.obs["perimeter_crofton"][0] == pytest.approx(want, abs=1e-12)


def dist_reference(lab, nuc, table, img, ids, region):
    inside = (lab > 0) & (nuc > 0) & (table[nuc] == lab) if nuc is not None else \
        np.zeros(lab.shape, bool)
    mask = {"cell": lab > 0, "nucleus": inside, "cytoplasm": (lab > 0) & ~inside}[region]
    out = {s: [] for s in qe.DIST_STATS}
    for i in ids:
        x = np.sort(img[(lab == i) & mask].astype(np.float64))
        m = x.size
        if m == 0:
            for s in out:
                out[s].append(np.nan)
            continue
        out["median"].append(np.median(x))
        out["p90"].append(np.percentile(x, 90))
        out["p95"].append(np.percentile(x, 95))
        tot = x.sum()
        out["gini"].append(np.nan if tot == 0 else
                           2 * np.sum(np.arange(1, m + 1) * x) / (m * tot) - (m + 1) / m)
    return {s: np.array(v) for s, v in out.items()}


def test_distribution_equals_numpy_in_every_region(tmp_path):
    lab, chans, n = make_case(seed=22)
    nuc, table = make_nuclei(lab, corrupt=False)
    res, _t = run(lab, chans, n, nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
                  distribution=qe.DIST_STATS, markers=["ch0", "ch1"], tile=37,
                  tmp=tmp_path / "s")
    assert res.dist_markers == ["ch0", "ch1"]
    ids = res.ids.astype(int)
    for mi, ci in enumerate([0, 1]):                     # uint8 and float32 markers
        for region in ("cell", "nucleus", "cytoplasm"):
            ref = dist_reference(lab, nuc, table, chans[ci], ids, region)
            for stat in qe.DIST_STATS:
                got = res.sink.read(f"dist_{region}_{stat}")[:, mi]
                want = ref[stat]
                assert np.array_equal(np.isnan(got), np.isnan(want)), (region, stat)
                ok = ~np.isnan(want)
                tol = 1e-12 if stat == "gini" else 0
                np.testing.assert_allclose(got[ok], want[ok], rtol=tol, atol=0,
                                           err_msg=f"ch{ci} {region} {stat}")


def test_only_the_chosen_markers_and_statistics(tmp_path):
    lab, chans, n = make_case(seed=23)
    res, _t = run(lab, chans, n, distribution=["p90", "median"], markers=["ch2"],
                  tmp=tmp_path / "s")
    assert res.dist_stats == ["median", "p90"] and res.dist_markers == ["ch2"]
    assert res.dist_layers == ["dist_cell_median", "dist_cell_p90"]
    assert res.sink.read("dist_cell_median").shape == (res.ids.size, 1)
    assert "ch2_median" in res.columns and "ch0_median" not in res.columns
    with pytest.raises(ValueError, match="at least one marker"):
        run(lab, chans, n, distribution=["median"], markers=[], tmp=tmp_path / "t")
    with pytest.raises(ValueError, match="not a channel"):
        run(lab, chans, n, distribution=["median"], markers=["nope"], tmp=tmp_path / "u")
    with pytest.raises(ValueError, match="not a distribution statistic"):
        run(lab, chans, n, distribution=["p99"], markers=["ch0"], tmp=tmp_path / "v")


def test_gini_edge_cases(tmp_path):
    lab = np.zeros((12, 12), np.uint32)
    lab[1, 1] = 1                                        # one non-zero pixel -> 0
    lab[5:7, 5:7] = 2                                    # all zero -> NaN
    img = np.zeros((12, 12), np.float32)
    img[1, 1] = 7.0
    res, _t = run(lab, [img], 2, distribution=["gini", "median"], markers=["ch0"],
                  tmp=tmp_path / "s")
    g = res.sink.read("dist_cell_gini")[:, 0]
    assert g[0] == 0.0 and np.isnan(g[1])
    assert res.sink.read("dist_cell_median")[1, 0] == 0.0


def test_forced_object_blocks_give_the_same_result_within_the_budget(tmp_path):
    lab, chans, n = make_case(seed=24)
    nuc, table = make_nuclei(lab, corrupt=False)
    kw = dict(nuclei=nuc, table=table, regions=("nucleus", "cytoplasm"),
              distribution=qe.DIST_STATS, markers=["ch0", "ch1", "ch2"], tile=40)
    base, _t = run(lab, chans, n, tmp=tmp_path / "a", **kw)
    # a budget smaller than ONE marker's pixel buffer forces object blocks
    one_marker = int((lab > 0).sum()) * 4
    budget = int(lab.max() + 1) * 8 + one_marker // 3
    res, _t = run(lab, chans, n, tmp=tmp_path / "b", accumulator_budget=budget, **kw)
    rec = res.distribution
    assert rec["passes"] > 3                              # more than one block per marker
    assert rec["peak_working_set_bytes"] <= budget
    assert rec["total_tile_reads"] >= rec["unique_tiles_read"] and rec["reread_factor"] >= 1
    for name in base.dist_layers:
        np.testing.assert_array_equal(res.sink.read(name), base.sink.read(name), err_msg=name)
    assert base.distribution["passes"] == 2               # one per dtype group, one block
