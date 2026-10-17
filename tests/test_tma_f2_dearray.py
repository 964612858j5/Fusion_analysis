"""TMA F2 (docs/tma_foundation_application.md §4 F2, as narrowed 2026-10-17):
core detection only -- foreground by texture, candidate cores, measures --
and the saved candidate list.

A synthetic slide (tmp_path) with known cores: bright and dim grainy discs,
two touching discs, one cut by the image edge, and a soft glow with no
grain that is not tissue.
"""

import json

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

from block01.core import provenance as prov  # noqa: E402
from block01.core.tma import dearray as da  # noqa: E402

D = 100                                   # core diameter, level-0 pixels
H, W = 900, 1200


def _slide(tmp_path):
    rng = np.random.default_rng(7)
    img = np.zeros((H, W), np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    cores = [(150, 150, 1.0), (150, 330, 1.0), (150, 510, 0.25),      # bright, bright, dim
             (420, 150, 1.0), (420, 330, 0.25),
             (420, 600, 1.0), (420, 690, 1.0),                         # touching pair (90 apart)
             (700, 1180, 1.0)]                                         # cut by the right edge
    for cy, cx, gain in cores:
        disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= (D / 2) ** 2
        img[disc] = gain * rng.uniform(20, 200, disc.sum())             # grainy: nuclei
    # a soft glow (fades out, like the glow between scan fields): no grain, not tissue
    img += 60.0 * np.exp(-((yy - 700) ** 2 + (xx - 400) ** 2) / (2 * (D / 2) ** 2))
    path = tmp_path / "tma.ome.tif"
    tifffile.imwrite(str(path), np.stack([np.clip(img, 0, 255).astype(np.uint8),
                                          np.zeros((H, W), np.uint8)]), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    return str(path), cores


def _near(cands, cy, cx, tol=8):
    return [c for c in cands if abs(c["cy"] - cy) <= tol and abs(c["cx"] - cx) <= tol]


def test_cores_are_found_measured_and_the_glow_is_not(tmp_path):
    path, cores = _slide(tmp_path)
    r = da.detect_cores(path, core_diameter_px=D, channel="DAPI", params={"min_core_px": 20})
    cands = r["candidates"]
    assert r["level"] == 0 and r["channel"] == "DAPI"
    for cy, cx, _gain in cores[:-1]:                                   # every whole core, the dim ones too
        hit = _near(cands, cy, cx)
        assert len(hit) == 1, (cy, cx, hit)
        assert abs(hit[0]["radius"] - D / 2) <= 0.12 * D / 2, hit[0]
        assert not hit[0]["touches_edge"] and hit[0]["tissue_fraction"] > 0.8
    assert not _near(cands, 700, 400, tol=40), "a smooth glow is not tissue"
    pair = _near(cands, 420, 600) + _near(cands, 420, 690)
    assert len(pair) == 2 and all(c["split"] for c in pair), "touching cores are split"
    edge = [c for c in cands if c["cx"] > 1100]
    assert len(edge) == 1 and edge[0]["touches_edge"]
    assert len(cands) == len(cores)


def test_the_expected_size_needs_a_diameter(tmp_path):
    path, _ = _slide(tmp_path)
    with pytest.raises(ValueError):
        da.detect_cores(path, channel=0)                               # no size, no pixel size


def test_choose_level_keeps_cores_wide_enough():
    shapes = [(8000, 4000), (4000, 2000), (2000, 1000), (1000, 500)]
    assert da.choose_level(shapes, 1000, 80) == 3                      # 125 px at level 3
    assert da.choose_level(shapes, 1000, 200) == 2
    assert da.choose_level(shapes, 50, 80) == 0                        # never coarser than needed


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    return str(d)


def test_candidates_are_saved_versioned_and_registered(project, tmp_path):
    path, _ = _slide(tmp_path)
    sid = prov.ensure_raw_slide(project, path)
    r = da.detect_cores(path, core_diameter_px=D, channel="DAPI", params={"min_core_px": 20})
    one = da.save_candidates(project, r, sid)
    two = da.save_candidates(project, r, sid)
    assert (one["version"], two["version"]) == (1, 2)                  # counts up, nothing overwritten
    back = da.load_candidates(project, 1)
    assert back["candidates"] == r["candidates"] and back["slide_id"] == sid
    entries = [e for e in prov.load_entries(project) if e["kind"] == "tma_dearray"]
    assert len(entries) == 2                                           # two files, two entries
    assert all(e["depends_on"] == [sid] and e["parameters"]["candidates"] == len(r["candidates"])
               for e in entries)


def _pyramid_slide(tmp_path, d=400, levels=3):
    """A 3200 x 2400 slide with a 2x mean-pooled pyramid (pixel centres as
    A3's SlideFrames has them), cores far apart, one close to the top-left corner."""
    rng = np.random.default_rng(11)
    h, w = 3200, 2400
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w), np.float32)
    centres = [(800.0, 700.0), (800.0, 1700.0), (2000.0, 700.0), (2000.0, 1700.0), (260.0, 260.0)]
    for cy, cx in centres:
        disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= (d / 2) ** 2
        img[disc] = rng.uniform(20, 200, disc.sum())
    base = np.stack([np.clip(img, 0, 255).astype(np.uint8), np.zeros((h, w), np.uint8)])
    path = tmp_path / "pyr.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(base, subifds=levels, tile=(256, 256), metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
        cur = base.astype(np.float32)
        for _ in range(levels):
            cur = cur.reshape(2, cur.shape[1] // 2, 2, cur.shape[2] // 2, 2).mean(axis=(2, 4))
            tw.write(np.round(cur).astype(np.uint8), subfiletype=1, tile=(256, 256))
    return str(path), centres, d


def test_centres_from_a_coarse_level_land_on_level_0(tmp_path):
    path, centres, d = _pyramid_slide(tmp_path)
    r = da.detect_cores(path, core_diameter_px=d, channel="DAPI", params={"min_core_px": 40})
    assert r["level"] == 3 and r["scale_yx"] == [8.0, 8.0]           # 50 px cores at 1/8
    errs, corner = [], None
    for cy, cx in centres:
        hit = _near(r["candidates"], cy, cx, tol=20)
        assert len(hit) == 1, (cy, cx)
        errs.append(np.hypot(hit[0]["cy"] - cy, hit[0]["cx"] - cx))
        if (cy, cx) == (260.0, 260.0):
            corner = hit[0]
    # a bare multiply would put every centre 3.5 px up-left at this scale
    assert np.mean(errs) < 1.5, errs
    whole = [c for c in r["candidates"] if c is not corner]
    assert all(abs(c["radius"] - d / 2) <= 0.1 * d / 2 for c in whole)
    assert all(0.8 < c["circularity"] <= 1.05 and c["solidity"] > 0.9 for c in whole)
    assert all(abs(c["area"] - np.pi * (d / 2) ** 2) <= 0.2 * np.pi * (d / 2) ** 2 for c in whole)
    assert not corner["touches_edge"]                                 # 60 px clear of both edges: inside


def test_an_empty_slide_has_no_candidates(tmp_path):
    path = tmp_path / "blank.ome.tif"
    tifffile.imwrite(str(path), np.zeros((2, 400, 300), np.uint8), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    assert da.detect_cores(str(path), core_diameter_px=60, channel=0, params={"min_core_px": 10})["candidates"] == []


@pytest.mark.parametrize("kw", [{"core_diameter_px": 0}, {"core_diameter_px": -5},
                                {"core_diameter_px": float("nan")}, {"core_diameter_px": float("inf")},
                                {"core_diameter_um": -1.0}, {"core_diameter_px": 80, "params": {"thr_frac": 0}}])
def test_bad_sizes_and_parameters_are_refused(tmp_path, kw):
    path, _ = _slide(tmp_path)
    with pytest.raises(ValueError):
        da.detect_cores(path, channel=0, **kw)


def test_cores_cut_by_the_top_and_left_edges_are_marked(tmp_path):
    rng = np.random.default_rng(3)
    img = np.zeros((500, 500), np.float32)
    yy, xx = np.mgrid[0:500, 0:500]
    for cy, cx in ((10, 250), (250, 10), (250, 250)):
        disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= 50 ** 2
        img[disc] = rng.uniform(20, 200, disc.sum())
    path = tmp_path / "edges.ome.tif"
    tifffile.imwrite(str(path), np.stack([img.astype(np.uint8), np.zeros_like(img, np.uint8)]), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    cands = da.detect_cores(str(path), core_diameter_px=D, channel=0, params={"min_core_px": 20})["candidates"]
    by = {(round(c["cy"] / 100), round(c["cx"] / 100)): c for c in cands}
    assert [c["touches_edge"] for c in sorted(cands, key=lambda c: (c["cy"], c["cx"]))].count(True) == 2
    assert not _near(cands, 250, 250)[0]["touches_edge"]


def test_a_size_in_micrometres_uses_the_pixel_size(tmp_path):
    path = tmp_path / "um.ome.tif"
    img = np.zeros((2, 400, 400), np.uint8)
    img[0, 150:250, 150:250] = np.random.default_rng(1).uniform(20, 200, (100, 100))
    tifffile.imwrite(str(path), img, ome=True, metadata={"axes": "CYX", "PhysicalSizeX": 0.5,
                                                          "PhysicalSizeY": 0.5})
    r = da.detect_cores(str(path), core_diameter_um=50.0, channel=0, params={"min_core_px": 20})
    assert r["core_diameter_px"] == 100.0 and r["pixel_um"] == [0.5, 0.5]


def test_a_failed_registration_still_saves_and_says_so(project, tmp_path, monkeypatch, capsys):
    path, _ = _slide(tmp_path)
    sid = prov.ensure_raw_slide(project, path)
    r = da.detect_cores(path, core_diameter_px=D, channel="DAPI", params={"min_core_px": 20})
    monkeypatch.setattr(prov, "safe_register", lambda *a, **k: None)
    rec = da.save_candidates(project, r, sid)
    assert rec["artifact_id"] is None and "NOT registered" in capsys.readouterr().out
    assert da.load_candidates(project, rec["version"])["candidates"] == r["candidates"]
