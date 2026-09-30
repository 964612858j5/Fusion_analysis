"""Block A2a acceptance: the PixelSource adapters against today's readers on
the real slide, bitwise, per the application's capability matrix.

    python scripts/diagnose_v16_a2a_pixel_source.py [--windows 20] [--dest COPY] [--json OUT]

  * every channel, every level, N random windows:
      OmeTiffSource.read_region  vs RawTileProvider.read_region
  * level 0:
      OmeTiffSource.read_region  vs TiffTileReader.read (Step4) and
                                    OMETIFFLoader.read_region(normalize=False)
  * the corrected channels of the copy's Step0 product:
      CorrectedZarrSource.read_region vs Step4's corrected read

Read-only. The slide is the one the path-rewritten test1 copy points at
(`diagnose_v16_a0_camera.py copy-project`); the original project's
fingerprint is checked before and after.
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import diagnose_v16_a0_camera as a0  # noqa: E402


def _step0_dir(dest):
    info = json.load(open(os.path.join(dest, "A0_COPY_INFO.json"), encoding="utf-8"))
    return os.path.join(dest, "rois", info["workspace"], "step0")


def _windows(rng, h, w, n):
    out = [(0, min(h, 256), 0, min(w, 256)), (h - 17, h + 30, w - 23, w + 40)]
    while len(out) < n:
        y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
        out.append((y0, y0 + int(rng.integers(1, 384)), x0, x0 + int(rng.integers(1, 384))))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", type=int, default=20)
    ap.add_argument("--dest", default=a0.DEFAULT_DEST)
    ap.add_argument("--json")
    args = ap.parse_args()

    from block01.core.io_loader import OMETIFFLoader
    from block01.core.quant_sources import TiffTileReader, _corrected_group
    from block01.sources import CorrectedZarrSource, OmeTiffSource
    from block01.viewer.raw_tile_provider import RawTileProvider
    import zarr

    before = a0.tree_fingerprint(a0.ORIG_PROJECT)
    step0 = _step0_dir(args.dest)
    handoff = json.load(open(os.path.join(step0, "step0_roi_result.json"), encoding="utf-8"))
    slide = handoff["raw_ome_path"]
    zpath = handoff["corrected_zarr_path"]
    st0 = os.stat(slide)
    rng = np.random.default_rng(20260930)
    report = {"slide": slide, "corrected": zpath, "rows": []}
    t0 = time.perf_counter()

    src = OmeTiffSource(slide)
    prov = RawTileProvider(slide)
    reader = TiffTileReader(slide, threads=8)
    loader = OMETIFFLoader(slide)
    names = src.channel_names()
    assert names == prov.channel_names, "channel names differ from the viewer's on this slide"
    mismatches = 0
    for level in range(src.level_count()):
        h, w = src.level_shape(level)
        n_cmp = 0
        for ci, ch in enumerate(names):
            for (y0, y1, x0, x1) in _windows(rng, h, w, args.windows):
                got, origin = src.read_region(ch, level, y0, y1, x0, x1)
                want, worigin = prov.read_region(ci, level, y0, y1, x0, x1)
                ok = origin == tuple(worigin) and got.dtype == want.dtype and np.array_equal(got, want)
                if level == 0:
                    cy0, cx0 = origin
                    cy1, cx1 = cy0 + got.shape[0], cx0 + got.shape[1]
                    ok &= np.array_equal(got, reader.read([ci], cy0, cy1, cx0, cx1)[0])
                    lo = loader.read_region(ch, cy0, cy1, cx0, cx1, normalize=False)
                    ok &= np.array_equal(got, lo.astype(got.dtype))
                mismatches += 0 if ok else 1
                n_cmp += 1
        row = {"what": "raw", "level": level, "shape": [h, w], "channels": len(names),
               "comparisons": n_cmp,
               "against": "RawTileProvider" + (" + TiffTileReader + OMETIFFLoader" if level == 0 else ""),
               "mismatches": mismatches}
        report["rows"].append(row)
        print(f"raw level {level} {h}x{w}: {n_cmp} windows x {row['against']}  "
              f"mismatches so far {mismatches}", flush=True)

    root = zarr.open_group(zpath, mode="r")
    for gname in root.group_keys():
        roi = root[gname].attrs.get("roi_name")
        csrc = CorrectedZarrSource(zpath, roi)
        gy0, gy1, gx0, gx1 = csrc.valid_bounds(0)
        cmis, n = 0, 0
        for ch in csrc.channel_names():
            for (y0, y1, x0, x1) in _windows(rng, gy1 - gy0, gx1 - gx0, args.windows):
                bbox = (gy0 + y0, gy0 + min(y1, gy1 - gy0), gx0 + x0, gx0 + min(x1, gx1 - gx0))
                if bbox[0] >= bbox[1] or bbox[2] >= bbox[3]:
                    continue
                got, origin = csrc.read_region(ch, 0, *bbox)
                container, (oy, ox), _ = _corrected_group(root, roi, bbox, zpath)
                want = np.asarray(container[ch][oy:oy + bbox[1] - bbox[0],
                                                ox:ox + bbox[3] - bbox[2]], np.float32)
                ok = origin == (bbox[0], bbox[2]) and np.array_equal(got, want)
                cmis += 0 if ok else 1
                n += 1
        csrc.close()
        report["rows"].append({"what": "corrected", "roi": roi, "bounds": [gy0, gy1, gx0, gx1],
                               "channels": csrc.channel_names(), "comparisons": n,
                               "mismatches": cmis})
        mismatches += cmis
        print(f"corrected {roi} {csrc.channel_names()}: {n} windows vs Step4's read, "
              f"mismatches {cmis}", flush=True)

    reader.close()
    prov.close()
    src.close()
    report["seconds"] = round(time.perf_counter() - t0, 1)
    report["mismatches"] = mismatches
    st1 = os.stat(slide)
    report["slide_unchanged"] = (st0.st_size, st0.st_mtime_ns) == (st1.st_size, st1.st_mtime_ns)
    report["original_project_unchanged"] = before == a0.tree_fingerprint(a0.ORIG_PROJECT)
    print(f"total mismatches: {mismatches}; slide unchanged: {report['slide_unchanged']}; "
          f"original project unchanged: {report['original_project_unchanged']}")
    if args.json:
        json.dump(report, open(args.json, "w"), indent=1)
    sys.stdout.flush()
    os._exit(0 if mismatches == 0 and report["original_project_unchanged"] else 1)


if __name__ == "__main__":
    main()
