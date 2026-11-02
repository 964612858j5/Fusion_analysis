"""Step4's SpatialData export (docs/v17_spatialdata_export_plan.md §3): the
zarr written through another environment's spatialdata, opened again THERE
and compared with the h5ad and the label stores.

Needs a Python with spatialdata (`BLOCK01_SPATIALDATA_PYTHON`, or the
server's Step5 environment); skipped without one. Synthetic projects in the
test's temporary directory only.
"""

import json
import os
import subprocess
import sys

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from test_quant_sources import build_project  # noqa: E402
from test_f4_step4 import _add_regions, _excl, _facts, _snapshot  # noqa: E402

from block01.core import quant_engine as qe  # noqa: E402
from block01.utils import spatialdata_python as sdp  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _python():
    for cand in (os.environ.get(sdp.ENV, ""), "/root/micromamba/envs/fusion_test2/bin/python"):
        if cand and sdp.check(cand)[0]:
            return cand
    return None


PY = _python()
pytestmark = pytest.mark.skipif(PY is None, reason="no Python with spatialdata")

# Runs in the spatialdata environment: what the zarr holds, next to the h5ad
# read by THAT environment's anndata (Step5's side of the hand-over).
INSPECT = r'''
import json, sys
import numpy as np, zarr, anndata as ad, spatialdata as sd
from spatialdata.transformations import get_transformation
zpath, h5, labels = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
s = sd.read_zarr(zpath)
t = s.tables["table"]
a = ad.read_h5ad(h5)
def same(x, y):
    x, y = np.asarray(x), np.asarray(y)
    return bool(x.shape == y.shape and (np.array_equal(x, y, equal_nan=True) if x.dtype.kind == "f" else np.array_equal(x, y)))
out = {"labels": sorted(s.labels), "shapes": sorted(s.shapes), "n_obs": int(t.n_obs),
       "attrs": {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in t.uns.get("spatialdata_attrs", {}).items()},
       "x_statistic": t.uns.get("x_statistic"), "has_X_statistic": "X_statistic" in t.uns,
       "ome_tiff": t.uns.get("ome_tiff"), "source_labels": dict(t.uns.get("source_labels", {})),
       "source_images": {"slide": dict(t.uns["source_images"]["slide"]),
                         "channels": {k: dict(v) for k, v in t.uns["source_images"]["channels"].items()}},
       "X": same(t.X, a.X), "layers": all(same(t.layers[k], a.layers[k]) for k in a.layers) and sorted(t.layers) == sorted(a.layers),
       "obs_cols": all(c in t.obs.columns and t.obs[c].astype(str).tolist() == a.obs[c].astype(str).tolist() for c in a.obs.columns),
       "obs_index": t.obs_names.tolist() == a.obs_names.tolist(),
       "var": t.var.equals(a.var), "obsm": sorted(k for k in t.obsm if k != "spatial") == sorted(a.obsm),
       "obsm_equal": all(same(np.asarray(t.obsm[k]), np.asarray(a.obsm[k])) for k in a.obsm),
       "spatial": np.asarray(t.obsm["spatial"]).tolist(),
       "region": sorted(set(t.obs["region"].astype(str))) if "region" in t.obs else None}
for name, path in labels.items():
    el = s.labels[name]
    tree = hasattr(el, "children")          # multiscale: a DataTree; one level: a DataArray
    lvl0 = el["scale0"]["image"] if tree else el
    out[name] = {"equal": same(np.asarray(lvl0), zarr.open(path, mode="r")[:]),
                 "levels": len(el.children) if tree else 1,
                 "global": [float(v) for v in get_transformation(el, "global").translation],
                 "systems": sorted(get_transformation(el, get_all=True))}
if "regions" in s.shapes:
    g = s.shapes["regions"]
    out["regions"] = {"kinds": sorted(g["kind"].tolist()), "valid": bool(g.geometry.is_valid.all()),
                      "types": sorted(set(g.geometry.geom_type))}
print(json.dumps(out, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
'''


def _inspect(res, labels):
    p = subprocess.run([PY, "-I", "-c", INSPECT, res["spatialdata"], res["h5ad"], json.dumps(labels)],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def _project(tmp_path, name, tma=False, exclusions=None, **kw):
    (tmp_path / name).mkdir()
    p = build_project(tmp_path / name, **kw)
    if tma:
        _add_regions(p, _snapshot(exclusions=exclusions), _facts(p["labels"]["Full WSI"], 40))
    return p


def _run(p, out, **kw):
    kw.setdefault("spatialdata", True)
    kw.setdefault("spatialdata_python", PY)
    return few.run_extraction(p["run_dir"], str(out), **kw)


def test_a_tma_cell_run_with_nuclei(tmp_path):
    p = _project(tmp_path, "p", tma=True, nuclei=True,
                 exclusions=[_excl("e1", ["CD8"])])
    res = _run(p, tmp_path / "o", regions=["nucleus"], distribution=["median"], markers=["CD3"])
    job = res["job"]
    got = _inspect(res, {"cell_labels": job.label_path, "nucleus_labels": job.nucleus_path})
    assert got["labels"] == ["cell_labels", "nucleus_labels"] and got["shapes"] == ["regions"]
    assert got["attrs"] == {"region": "cell_labels", "region_key": "region", "instance_key": "cell_id"}
    assert got["region"] == ["cell_labels"]
    assert got["x_statistic"] == "mean" and not got["has_X_statistic"]
    assert got["ome_tiff"] == job.slide
    assert got["source_images"]["slide"]["path"] == job.slide
    ch = got["source_images"]["channels"]
    assert [ch[str(k)]["name"] for k in range(len(ch))] == [c.name for c in job.channels]
    assert got["source_labels"] == {"cell": job.label_path, "nucleus": job.nucleus_path}
    for key in ("X", "layers", "obs_cols", "obs_index", "var", "obsm", "obsm_equal"):
        assert got[key], key
    y0, _y1, x0, _x1 = job.bbox
    for name in ("cell_labels", "nucleus_labels"):
        assert got[name]["equal"], name
        assert got[name]["global"] == [float(y0), float(x0)]
    assert got["regions"]["kinds"] == ["core", "core", "core", "exclusion"]
    assert got["regions"]["valid"]
    prov = json.load(open(res["provenance"]))
    assert prov["spatialdata"]["spatialdata_version"] and prov["outputs"]["spatialdata"].endswith(".zarr")
    assert not [f for f in os.listdir(res["output_dir"]) if f.endswith(".partial") or f.endswith("_job.json")]


def test_a_plain_run_has_no_shapes_and_centroids_match_step4(tmp_path):
    p = _project(tmp_path, "p")
    a = _run(p, tmp_path / "with", features=("morphology",))
    b = _run(p, tmp_path / "without", features=())
    ga = _inspect(a, {"cell_labels": a["job"].label_path})
    gb = _inspect(b, {"cell_labels": b["job"].label_path})
    assert ga["shapes"] == [] and ga["n_obs"] == gb["n_obs"] > 0
    np.testing.assert_allclose(np.array(gb["spatial"]), np.array(ga["spatial"]), rtol=0, atol=1e-9)
    assert json.load(open(b["provenance"]))["spatialdata"]["centroids"] == "labels"


def test_a_nuclei_only_run_annotates_the_nucleus_labels(tmp_path):
    p = _project(tmp_path, "p", nuclei_only=True)
    res = _run(p, tmp_path / "o")
    got = _inspect(res, {"nucleus_labels": res["job"].label_path})
    assert got["labels"] == ["nucleus_labels"]
    assert got["attrs"]["region"] == "nucleus_labels" and got["attrs"]["instance_key"] == "nucleus_id"
    assert got["nucleus_labels"]["equal"]


def test_zero_objects_write_a_plain_table(tmp_path):
    p = _project(tmp_path, "p", n_cells=0)
    res = _run(p, tmp_path / "o")
    got = _inspect(res, {"cell_labels": res["job"].label_path})
    assert got["n_obs"] == 0 and got["attrs"] == {} and got["cell_labels"]["equal"]


def test_a_stop_during_the_export_publishes_nothing(tmp_path):
    p = _project(tmp_path, "p")
    out = tmp_path / "o"
    seen = []

    def stop():                                # True once the export has started
        if any(f.endswith("_spatialdata_job.json") for _r, _d, fs in os.walk(tmp_path) for f in fs):
            seen.append(1)
        return bool(seen)
    with pytest.raises(qe.QuantStopped):
        _run(p, out, should_stop=stop)
    assert seen
    leftovers = [os.path.join(r, f) for r, ds, fs in os.walk(tmp_path / "p") for f in fs + ds
                 if f.endswith(".partial") or f.endswith("_job.json") or f.endswith(".spatialdata.zarr")]
    assert leftovers == []


def test_an_unusable_python_is_refused_before_any_output(tmp_path):
    p = _project(tmp_path, "p")
    out = tmp_path / "o"
    with pytest.raises(ValueError, match="spatialdata"):
        _run(p, out, spatialdata_python=str(tmp_path / "no_such_python"))
    assert not out.exists() or not os.listdir(out)


def test_the_multiscale_factors():
    from importlib import util
    spec = util.spec_from_file_location("export_spatialdata", few.SPATIALDATA_SCRIPT)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.scale_factors(46800, 26880, 256) == [2, 2, 2, 2]
    assert mod.scale_factors(1000, 600, 256) == [2]
    assert mod.scale_factors(130, 130, 256) == []


def test_a_known_pixel_size_adds_micrometers(tmp_path, monkeypatch):
    monkeypatch.setattr(few.qa, "_pixel_size", lambda job: (0.5, 0.25))
    p = _project(tmp_path, "p")
    res = _run(p, tmp_path / "o")
    got = _inspect(res, {"cell_labels": res["job"].label_path})
    assert got["cell_labels"]["systems"] == ["global", "micrometers"]


def test_the_lazy_pyramid_has_the_librarys_pixels():
    """`strided_multiscale` replaces spatialdata's labels pyramid (memory);
    every level must hold the library's own pixels, odd sizes included."""
    code = r'''
import importlib.util, json, sys
import numpy as np, dask.array as da
from spatialdata.models import Labels2DModel
spec = importlib.util.spec_from_file_location("ex", sys.argv[1])
ex = importlib.util.module_from_spec(spec); spec.loader.exec_module(ex)
from spatialdata.models import models as M
rng = np.random.default_rng(0)
ok = []
for shape in [(1000, 600), (1001, 603), (517, 999), (300, 2049)]:
    a = da.from_array(rng.integers(0, 50, size=shape).astype(np.uint32), chunks=128)
    lib = Labels2DModel.parse(a, dims=("y", "x"), scale_factors=[2, 2, 2])
    real = M.to_multiscale
    M.to_multiscale = ex.strided_multiscale
    try:
        mine = Labels2DModel.parse(a, dims=("y", "x"), scale_factors=[2, 2, 2])
    finally:
        M.to_multiscale = real
    for k in lib.keys():
        x, y = np.asarray(lib[k]["image"]), np.asarray(mine[k]["image"])
        ok.append(bool(x.shape == y.shape and np.array_equal(x, y)))
    from spatialdata.transformations import get_transformation
    ok.append(all(str(get_transformation(lib[k]["image"])) == str(get_transformation(mine[k]["image"]))
                  for k in lib.keys()))
print(json.dumps(ok))
'''
    p = subprocess.run([PY, "-I", "-c", code, few.SPATIALDATA_SCRIPT], capture_output=True, text=True,
                       timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got and all(got)


def test_overlapping_core_pieces_keep_their_union():
    """Regions "Group" allows overlapping pieces of one core: the shape is
    their union (area 28 for [0,4]^2 and [2,6]^2), not a repair that drops
    the overlap (codex review)."""
    code = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("ex", sys.argv[1])
ex = importlib.util.module_from_spec(spec); spec.loader.exec_module(ex)
sq = lambda a, b: [[a, a], [b, a], [b, b], [a, b]]
bow = [[0, 0], [4, 4], [4, 0], [0, 4]]                 # self-crossing: repaired
job = {"bbox": [0, 10, 0, 10], "pixel_um": None,
       "shapes": [{"kind": "core", "uid": "c0", "position_key": "k0", "name": "", "run": True,
                   "rings": [sq(0, 4), sq(2, 6)]},
                  {"kind": "exclusion", "uid": "e1", "position_key": "", "name": "", "run": True,
                   "rings": [bow]}]}
g, repaired = ex.shapes_element(job)
print(json.dumps({"areas": [round(a, 6) for a in g.geometry.area.tolist()], "repaired": repaired,
                  "valid": bool(g.geometry.is_valid.all())}))
'''
    p = subprocess.run([PY, "-I", "-c", code, few.SPATIALDATA_SCRIPT], capture_output=True, text=True,
                       timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["areas"] == [28.0, 8.0] and got["repaired"] == 1 and got["valid"]


def test_a_channel_name_with_a_slash_survives(tmp_path):
    """Channel names may hold "/": uns["source_images"] is keyed by position,
    the name a value (codex review)."""
    import types
    job = types.SimpleNamespace(slide=str(tmp_path / "s.tif"), channels=[
        types.SimpleNamespace(name="CD3/PD1", index=0, kind="raw", path="s.tif", array="", identity={}),
        types.SimpleNamespace(name="CD3", index=1, kind="corrected", path="c.zarr", array="g/CD3",
                              identity={"method": "tophat"})])
    src = few._source_images(job)
    code = r'''
import json, sys, anndata as ad, numpy as np
a = ad.AnnData(np.zeros((1, 1), np.float32)); a.uns["source_images"] = json.loads(sys.argv[1])
a.write_zarr(sys.argv[2]); b = ad.read_zarr(sys.argv[2])
ch = b.uns["source_images"]["channels"]
print(json.dumps([str(ch[str(k)]["name"]) for k in range(len(ch))]))
'''
    p = subprocess.run([PY, "-I", "-c", code, json.dumps(src), str(tmp_path / "t.zarr")],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-2000:]
    assert json.loads(p.stdout.strip().splitlines()[-1]) == ["CD3/PD1", "CD3"]
