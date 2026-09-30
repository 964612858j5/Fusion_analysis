"""Block A2a: the `PixelSource` contract and its legacy adapters.

The adapters DELEGATE to today's readers, so the tests compare them with
those readers bitwise, each comparison only on the levels both sides
support (the A2a capability matrix):

  OmeTiffSource.read_region / read_tile  vs RawTileProvider      every level
  OmeTiffSource.read_region / scan       vs TiffTileReader        level 0
  OmeTiffSource.read_region              vs OMETIFFLoader         level 0
  CorrectedZarrSource.read_region        vs the quant_sources read  level 0

All data is synthetic, in a temporary directory.
"""

import ast
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core.pixel_source import OutOfBounds, PixelSource, SourceClosed  # noqa: E402
from block01.sources import CorrectedZarrSource, OmeTiffSource  # noqa: E402
from block01.sources.ome_tiff import (channel_names_from_ome,  # noqa: E402
                                      physical_size_from_ome)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = ["DAPI", "CD3", "CD8", "FOXP3"]
H, W = 520, 776


def _write(path, data, levels, tile, physical=None):
    meta = {"axes": "CYX", "Channel": {"Name": NAMES[:data.shape[0]]}}
    if physical:
        meta.update(physical)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        kw = {"tile": tile} if tile else {}
        tw.write(data, subifds=levels - 1, metadata=meta, **kw)
        cur = data
        for _ in range(levels - 1):
            c, h, w = cur.shape
            cur = cur[:, :h // 2 * 2, :w // 2 * 2].reshape(c, h // 2, 2, w // 2, 2)
            cur = cur.mean(axis=(2, 4)).astype(data.dtype)
            tw.write(cur, subfiletype=1, **kw)


def _data(dtype, seed=0):
    rng = np.random.default_rng(seed)
    top = 255 if dtype == np.uint8 else 65535
    return rng.integers(0, top, size=(len(NAMES), H, W), endpoint=True).astype(dtype)


@pytest.fixture(scope="module", params=[("uint8", 3, (64, 64)), ("uint16", 3, (64, 64)),
                                        ("uint8", 1, None)],
                ids=["uint8-tiled-3lv", "uint16-tiled-3lv", "uint8-strips-1lv"])
def slide(request, tmp_path_factory):
    dtype, levels, tile = request.param
    data = _data(np.dtype(dtype))
    path = tmp_path_factory.mktemp("a2a") / f"slide_{dtype}_{levels}.ome.tif"
    _write(path, data, levels, tile,
           physical={"PhysicalSizeX": 0.5, "PhysicalSizeY": 0.25,
                     "PhysicalSizeXUnit": "µm", "PhysicalSizeYUnit": "µm"})
    return str(path), data


def _windows(h, w, n=20, seed=1):
    rng = np.random.default_rng(seed)
    out = [(0, h, 0, w), (h - 7, h + 40, w - 9, w + 50), (-5, 13, -8, 21)]
    for _ in range(n):
        y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
        out.append((y0, y0 + int(rng.integers(1, 300)), x0, x0 + int(rng.integers(1, 300))))
    return out


# ── the contract module itself ───────────────────────────────────────────

def test_the_contract_imports_no_viewer_ui_or_qt():
    tree = ast.parse(open(os.path.join(ROOT, "core", "pixel_source.py"), encoding="utf-8").read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(("." * node.level) + (node.module or ""))
    bad = [m for m in mods if any(k in m for k in ("viewer", "ui", "PyQt", "pyqtgraph", "Qt"))]
    assert bad == [], bad


# The consumers migrated onto the contract so far (block A2c): Step4's
# JobReader (2/3) and FullFusionWorker (3/3). Any other user is a migration
# nobody approved.
MIGRATED = {os.path.join("core", "pixel_source.py"),
            os.path.join("core", "quant_sources.py"),
            os.path.join("ui", "step0", "overview_panel.py"),
            # wiring only: it hands FullFusionWorker `use_pixel_sources=True`
            # and the corrected product's path / decisions (A2c 3/3)
            os.path.join("ui", "main_window.py"),
            os.path.join("scripts", "diagnose_v16_a2c_oracle.py")}


def test_only_the_migrated_consumers_use_the_contract():
    """A2a changed no behaviour; A2c migrates Step4 and Step1 fusion only.
    Nothing else outside the contract, its adapters, tests and probes
    imports them."""
    allowed = MIGRATED
    users = []
    for base, dirs, files in os.walk(ROOT):
        rel = os.path.relpath(base, ROOT)
        top = rel.split(os.sep)[0]
        if top in ("tests", "sources", "docs", "envs", ".git") or "__pycache__" in rel:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(rel, f) if rel != "." else f
            if path in allowed or f == "diagnose_v16_a2a_pixel_source.py":
                continue
            text = open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore").read()
            if "pixel_source" in text or "block01.sources" in text or "from ..sources" in text \
                    or "from .sources" in text:
                users.append(path)
    assert users == [], users


def test_an_adapter_is_a_pixel_source(slide):
    with OmeTiffSource(slide[0]) as src:
        assert isinstance(src, PixelSource)


# ── OmeTiffSource vs RawTileProvider (every level) ──────────────────────

def test_region_and_tile_equal_the_raw_tile_provider_on_every_level(slide):
    from block01.viewer.raw_tile_provider import RawTileProvider
    path, data = slide
    prov = RawTileProvider(path)
    with OmeTiffSource(path) as src:
        assert src.level_count() == prov.num_levels
        for level in range(src.level_count()):
            h, w = src.level_shape(level)
            assert (h, w) == prov.level_shape(level)
            assert src.level_downsample(level) == prov.level_downsample_yx(level)
            assert src.legacy_level_downsample_rounded(level) == prov.level_downsample(level)
            for ch in NAMES:
                for (y0, y1, x0, x1) in _windows(h, w):
                    got, origin = src.read_region(ch, level, y0, y1, x0, x1)
                    want, worigin = prov.read_region(NAMES.index(ch), level, y0, y1, x0, x1)
                    assert got.dtype == data.dtype                 # native, never float
                    assert origin == tuple(worigin)
                    assert np.array_equal(got, want)
                tile, torigin = src.read_tile(ch, level, 1, 2, 64)
                rect, rorigin = src.read_region(ch, level, 64, 128, 128, 192)
                assert torigin == rorigin and np.array_equal(tile, rect)
    prov.close()


def test_a_region_is_clipped_to_what_the_source_owns(slide):
    path, data = slide
    with OmeTiffSource(path) as src:
        arr, origin = src.read_region("CD3", 0, H - 10, H + 90, -30, 20)
        assert origin == (H - 10, 0) and arr.shape == (10, 20)
        assert np.array_equal(arr, data[1, H - 10:H, 0:20])
        for req in [(H, H + 5, 0, 10), (-9, 0, 0, 10), (0, 10, W + 1, W + 9), (5, 5, 0, 10)]:
            with pytest.raises(OutOfBounds):
                src.read_region("CD3", 0, *req)


# ── level 0: vs TiffTileReader and OMETIFFLoader ────────────────────────

def test_level_zero_equals_step4_s_reader_and_the_loader(slide):
    from block01.core.io_loader import OMETIFFLoader
    from block01.core.quant_sources import TiffTileReader
    path, data = slide
    reader = TiffTileReader(path, threads=4)
    loader = OMETIFFLoader(path)
    with OmeTiffSource(path) as src:
        for (y0, y1, x0, x1) in _windows(H, W):
            try:
                cy0, cy1, cx0, cx1 = y0, y1, x0, x1
                got, (cy0, cx0) = src.read_region("CD8", 0, y0, y1, x0, x1)
            except OutOfBounds:
                continue
            cy1, cx1 = cy0 + got.shape[0], cx0 + got.shape[1]
            step4 = reader.read([NAMES.index("CD8")], cy0, cy1, cx0, cx1)[0]
            assert np.array_equal(got, step4)
            lo = loader.read_region("CD8", cy0, cy1, cx0, cx1, normalize=False)
            assert lo.dtype == np.float32
            assert np.array_equal(got, lo.astype(data.dtype))
            assert np.array_equal(got, data[2, cy0:cy1, cx0:cx1])
    reader.close()


def test_scan_blocks_equal_region_reads_in_the_requested_order(slide):
    from block01.core.quant_sources import TiffTileReader
    path, data = slide
    order = ["FOXP3", "DAPI", "CD8"]
    reader = TiffTileReader(path, threads=2)
    with OmeTiffSource(path) as src:
        seen = []
        for y0, x0, block in src.scan(order, 0, 200):
            h, w = block.shape[1:]
            assert block.dtype == data.dtype
            want, origin = src.read_regions(order, 0, y0, y0 + h, x0, x0 + w)
            assert origin == (y0, x0) and np.array_equal(block, want)
            assert np.array_equal(block, reader.read([NAMES.index(c) for c in order],
                                                     y0, y0 + h, x0, x0 + w))
            seen.append((y0, x0))
        assert seen == [(y, x) for y in range(0, H, 200) for x in range(0, W, 200)]
        if src.level_count() > 1:
            with pytest.raises(NotImplementedError):
                next(iter(src.scan(order, 1, 200)))
    reader.close()


def test_batched_reads_equal_per_channel_reads(slide):
    with OmeTiffSource(slide[0]) as src:
        batch, origin = src.read_regions(NAMES, 0, 10, 90, 20, 140)
        for k, ch in enumerate(NAMES):
            one, o = src.read_region(ch, 0, 10, 90, 20, 140)
            assert o == origin and np.array_equal(batch[k], one)


# ── concurrency and lifecycle ────────────────────────────────────────────

def test_concurrent_reads_equal_serial_reads(slide):
    path, _data = slide
    wins = _windows(H, W, n=40, seed=7)
    with OmeTiffSource(path) as src:
        serial = [src.read_region(NAMES[k % 4], 0, *win)[0] for k, win in enumerate(wins)
                  if _owned(win)]
        with ThreadPoolExecutor(8) as pool:
            parallel = list(pool.map(lambda kw: src.read_region(NAMES[kw[0] % 4], 0, *kw[1])[0],
                                     [(k, win) for k, win in enumerate(wins) if _owned(win)]))
        assert all(np.array_equal(a, b) for a, b in zip(serial, parallel))
        want = [b for _, _, b in src.scan(NAMES, 0, 128)]
        results = {}

        def run(key):
            results[key] = [b for _, _, b in src.scan(NAMES, 0, 128)]
        threads = [threading.Thread(target=run, args=(k,)) for k in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        for key in results:
            assert all(np.array_equal(a, b) for a, b in zip(results[key], want))


def _owned(win):
    y0, y1, x0, x1 = win
    return max(y0, 0) < min(y1, H) and max(x0, 0) < min(x1, W)


def test_close_ends_the_source(slide):
    src = OmeTiffSource(slide[0])
    src.read_region("DAPI", 0, 0, 5, 0, 5)
    src.close()
    with pytest.raises(SourceClosed):
        src.read_region("DAPI", 0, 0, 5, 0, 5)
    with OmeTiffSource(slide[0]) as src2:
        pass
    with pytest.raises(SourceClosed):
        src2.read_region("DAPI", 0, 0, 5, 0, 5)


# ── identity, names, physical size ───────────────────────────────────────

def test_identity_is_the_runtime_identity(slide):
    path = slide[0]
    with OmeTiffSource(path) as src:
        ident = src.source_identity()
        st = os.stat(path)
        assert ident.dataset_path == os.path.abspath(path)
        assert ident.dataset_fingerprint == f"{st.st_size}:{st.st_mtime_ns}"
        assert ident.stage == "raw" and ident.slide_id is None
        assert src.channel_names() == NAMES


def _ome(channels):
    body = "".join(f'<Channel ID="Channel:0:{i}"{name}/>'
                   for i, name in enumerate(f' Name="{n}"' if n else "" for n in channels))
    return ('<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0">'
            f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint8" SizeX="1" SizeY="1" '
            f'SizeC="{len(channels)}" SizeZ="1" SizeT="1">{body}</Pixels></Image></OME>')


def test_channel_names_rule_c():
    assert channel_names_from_ome(_ome(["DAPI", "CD3", "CD8"]), 3) == ["DAPI", "CD3", "CD8"]
    # one missing name: only that channel falls back
    assert channel_names_from_ome(_ome(["DAPI", "", "CD8"]), 3) == ["DAPI", "ch_01", "CD8"]
    # the elements do not number the pages: names and pages cannot be matched
    assert channel_names_from_ome(_ome(["DAPI", "CD3"]), 3) == ["ch_00", "ch_01", "ch_02"]
    assert channel_names_from_ome("", 2) == ["ch_00", "ch_01"]


def test_physical_size_is_read_in_micrometres(slide, tmp_path):
    with OmeTiffSource(slide[0]) as src:
        assert src.physical_size() == pytest.approx((0.25, 0.5))
    xml = _ome(["A"]).replace('SizeX="1"', 'SizeX="1" PhysicalSizeX="500" PhysicalSizeXUnit="nm" '
                                           'PhysicalSizeY="0.4"')
    assert physical_size_from_ome(xml) == pytest.approx((0.4, 0.5))     # Y default µm, X nm
    assert physical_size_from_ome(_ome(["A"])) is None                  # not recorded
    odd = _ome(["A"]).replace('SizeX="1"', 'SizeX="1" PhysicalSizeX="1" PhysicalSizeXUnit="pc" '
                                           'PhysicalSizeY="1"')
    assert physical_size_from_ome(odd) is None                          # unknown unit
    plain = tmp_path / "plain.ome.tif"
    _write(plain, _data(np.uint8)[:2, :64, :64], 1, None)
    with OmeTiffSource(str(plain)) as src:
        assert src.physical_size() is None


# ── CorrectedZarrSource ──────────────────────────────────────────────────

ROI_BBOX = (100, 300, 200, 500)


@pytest.fixture
def corrected(slide, tmp_path):
    path, _data = slide
    zpath = tmp_path / "corrected_channels.zarr"
    root = zarr.open_group(str(zpath), mode="w")
    root.attrs.update({"mode": "roi_only", "source_ome": os.path.abspath(path)})
    rng = np.random.default_rng(3)
    y0, y1, x0, x1 = ROI_BBOX
    arrays = {}
    for gname, roi, bbox in (("ROI_1", "ROI 1", ROI_BBOX), ("ROI_2", "ROI 2", (0, 50, 0, 60))):
        g = root.create_group(gname)
        g.attrs.update({"roi_name": roi, "bbox_fullres": list(bbox)})
        for ch in ("CD3", "FOXP3"):
            a = rng.normal(size=(bbox[1] - bbox[0], bbox[3] - bbox[2])).astype(np.float32)
            g.create_dataset(ch, data=a, chunks=(64, 64))
            g[ch].attrs["source_identity"] = f"tok-{gname}"
            arrays[(gname, ch)] = a
    return str(zpath), arrays


def _quant_read(zpath, roi, ch, bbox):
    """How Step4 reads a corrected region today (`_corrected_group` +
    `JobReader.channels`)."""
    from block01.core.quant_sources import _corrected_group
    root = zarr.open_group(zpath, mode="r")
    container, (oy, ox), _ = _corrected_group(root, roi, bbox, zpath)
    y0, y1, x0, x1 = bbox
    return np.asarray(container[ch][oy:oy + (y1 - y0), ox:ox + (x1 - x0)], np.float32)


def test_corrected_reads_equal_step4_s_corrected_read(corrected):
    zpath, arrays = corrected
    with CorrectedZarrSource(zpath, "ROI 1") as src:
        assert src.valid_bounds(0) == ROI_BBOX
        assert src.level_count() == 1 and src.level_shape(0) == (H, W)
        assert sorted(src.channel_names()) == ["CD3", "FOXP3"]
        assert src.dtype("CD3") == np.float32
        assert src.source_identity().stage == "corrected_saved"
        for bbox in [ROI_BBOX, (120, 170, 250, 400), (299, 300, 499, 500)]:
            got, origin = src.read_region("CD3", 0, *bbox)
            assert origin == (bbox[0], bbox[2])
            assert np.array_equal(got, _quant_read(zpath, "ROI 1", "CD3", bbox))


def test_corrected_never_serves_a_pixel_outside_its_roi(corrected):
    zpath, arrays = corrected
    y0, y1, x0, x1 = ROI_BBOX
    with CorrectedZarrSource(zpath, "ROI 1") as src:
        got, origin = src.read_region("FOXP3", 0, y0 - 50, y0 + 10, x0 - 30, x0 + 20)
        assert origin == (y0, x0) and got.shape == (10, 20)       # the intersection only
        assert np.array_equal(got, arrays[("ROI_1", "FOXP3")][:10, :20])
        for req in [(0, 50, 0, 60), (y1, y1 + 5, x0, x1), (y0, y1, x1, x1 + 3)]:
            with pytest.raises(OutOfBounds):
                src.read_region("FOXP3", 0, *req)
        with pytest.raises(KeyError):
            src.read_region("DAPI", 0, *ROI_BBOX)                  # not a corrected channel
        with pytest.raises(IndexError):
            src.read_region("CD3", 1, *ROI_BBOX)


def test_corrected_needs_exactly_its_own_group(corrected):
    from block01.core.quant_sources import QuantSourceError
    zpath, _ = corrected
    with pytest.raises(QuantSourceError):
        CorrectedZarrSource(zpath, "ROI 9")
    with CorrectedZarrSource(zpath, "ROI 2") as src:
        assert src.valid_bounds(0) == (0, 50, 0, 60)


def test_level_downsample_is_the_exact_per_axis_ratio(tmp_path):
    """An odd-sized pyramid (as test1's real 4.0003 / 16.013): the contract's
    scale is H0/H_L and W0/W_L exactly, never a rounded factor."""
    path = tmp_path / "odd.ome.tif"
    odd = np.random.default_rng(5).integers(0, 255, size=(2, 521, 777)).astype(np.uint8)
    _write(path, odd, 3, (64, 64))
    with OmeTiffSource(str(path)) as src:
        assert src.level_shape(1) == (260, 388) and src.level_shape(2) == (130, 194)
        assert src.level_downsample(1) == (521 / 260, 777 / 388)
        assert src.level_downsample(2) == (521 / 130, 777 / 194)
        assert src.level_downsample(2) != (4.0, 4.0)
        assert src.legacy_level_downsample_rounded(2) == 4


# ── native tiles (block A2c, plan v2.4 P1) ───────────────────────────────

def test_native_tiles_are_the_tiff_blocks_on_every_level(slide):
    import tifffile
    path, data = slide
    with tifffile.TiffFile(path) as tf:
        pages = [lv.pages[0] for lv in tf.series[0].levels]
        want = []
        for p in pages:
            p = p.aspage() if hasattr(p, "aspage") else p
            want.append((int(p.tilelength), int(p.tilewidth)) if p.is_tiled
                        else (min(int(p.rowsperstrip), int(p.imagelength)), int(p.imagewidth)))
    with OmeTiffSource(path) as src:
        for level in range(src.level_count()):
            th, tw = src.native_tile_shape(level)
            assert (th, tw) == want[level]
            h, w = src.level_shape(level)
            for ty in range(-(-h // th)):
                for tx in range(-(-w // tw)):
                    tile, origin = src.read_native_tile("CD3", level, ty, tx)
                    assert origin == (ty * th, tx * tw)
                    assert tile.shape == (min(th, h - ty * th), min(tw, w - tx * tw))   # edge clipped
                    rect, _ = src.read_region("CD3", level, ty * th, ty * th + th, tx * tw, tx * tw + tw)
                    assert tile.dtype == data.dtype and np.array_equal(tile, rect)
                    if level == 0:
                        assert np.array_equal(tile, data[1, ty * th:ty * th + th, tx * tw:tx * tw + tw])
            with pytest.raises(OutOfBounds):
                src.read_native_tile("CD3", level, -(-h // th), 0)


def test_corrected_native_tiles_start_at_the_products_bbox(corrected):
    zpath, arrays = corrected
    y0, y1, x0, x1 = ROI_BBOX
    with CorrectedZarrSource(zpath, "ROI 1") as src:
        assert src.native_tile_shape(0) == (64, 64)
        assert src.native_tile_origin(0) == (y0, x0)
        a = arrays[("ROI_1", "CD3")]
        for ty in range(-(-(y1 - y0) // 64)):
            for tx in range(-(-(x1 - x0) // 64)):
                tile, origin = src.read_native_tile("CD3", 0, ty, tx)
                assert origin == (y0 + ty * 64, x0 + tx * 64)
                assert np.array_equal(tile, a[ty * 64:ty * 64 + 64, tx * 64:tx * 64 + 64])
