"""Block A2c oracle: Step4 and Step1 fusion on the path-rewritten test1 copy,
run by the code that is checked out, so the pre-migration HEAD and the
migrated tree can be compared bitwise with the A2b-probe comparators.

    python scripts/diagnose_v16_a2c_oracle.py step4  --out DIR [--copy COPY]
    python scripts/diagnose_v16_a2c_oracle.py fusion --out DIR [--copy COPY] [--mode loader|sources]
    python scripts/diagnose_v16_a2c_oracle.py same-step4 DIR_A DIR_B
    python scripts/diagnose_v16_a2c_oracle.py same-fused DIR_A DIR_B

`fusion --mode loader` builds the page loader the way the main window does
(`set_correction_config` + `set_corrected_zarr_store` with the handoff's
decisions), so corrected channels come from Step0's saved product; `--mode
sources` runs the migrated FullFusionWorker (block A2c 3/3) on the same
settings. The copy is written only under --out; the original project and the
slide are read only.
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import diagnose_v16_a0_camera as a0  # noqa: E402

DEFAULT_COPY = a0.DEFAULT_DEST


def _ws(copy):
    info = json.load(open(os.path.join(copy, "A0_COPY_INFO.json"), encoding="utf-8"))
    return os.path.join(copy, "rois", info["workspace"])


def _no_write(path):
    if os.path.realpath(path).startswith(os.path.realpath(os.path.expanduser("~/fusion_data"))):
        raise SystemExit(f"never write under ~/fusion_data: {path}")


def cmd_step4(args):
    from block01.workers import feature_extract_worker as few
    ws = _ws(args.copy)
    runs = os.path.join(ws, "step2", "segmentation_runs")
    run = os.path.join(runs, sorted(os.listdir(runs))[0])
    prov = json.load(open(os.path.join(ws, "step4", "quantification_runs", os.path.basename(run),
                                       "Full_WSI", "cell_features_provenance.json")))
    _no_write(args.out)
    shutil.rmtree(args.out, ignore_errors=True)
    t0 = time.perf_counter()
    few.run_extraction(run, args.out, roi_name="Full WSI", statistics=prov["statistics"],
                       write_csv=True)
    print(f"[step4] {time.perf_counter() - t0:.2f}s -> {args.out}", flush=True)
    sys.stdout.flush()
    os._exit(0)


def fusion_inputs(copy):
    """What the main window hands FullFusionWorker, rebuilt from the copy."""
    from block01.utils.channel_remap_config import load_channel_remap_config
    ws = _ws(copy)
    settings = json.load(open(os.path.join(ws, "step1", "step1_fusion_settings.json")))
    handoff = json.load(open(os.path.join(ws, "step0", "step0_roi_result.json")))
    cfg_path = handoff.get("correction_config_path") or os.path.join(ws, "step0", "correction_config.json")
    correction = json.load(open(cfg_path))
    decisions = {str(ch): str(m).strip().lower()
                 for ch, m in (correction.get("channel_decisions") or {}).items()
                 if str(m).strip().lower() in {"tophat", "cucim"}}
    decisions.update(handoff.get("corrected_decisions") or {})
    remap = load_channel_remap_config(handoff["channel_remap_config_path"])
    params = {str(n): dict(p) for n, p in (remap.get("channels") or {}).items()}
    rois = json.load(open(os.path.join(ws, "step1", "roi_config.json")))
    return {"slide": handoff["raw_ome_path"], "corrected": handoff["corrected_zarr_path"],
            "correction": correction, "decisions": decisions, "params": params,
            "fusion_config": settings["fusion_config"], "rois": rois}


def cmd_fusion(args):
    from PyQt5 import QtCore
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0.overview_panel import FullFusionWorker
    inp = fusion_inputs(args.copy)
    _no_write(args.out)
    shutil.rmtree(args.out, ignore_errors=True)
    loader = OMETIFFLoader(inp["slide"])
    loader.set_correction_config(inp["correction"])
    loader.set_corrected_zarr_store(inp["corrected"], inp["decisions"])
    cfg = dict(inp["fusion_config"])
    cfg.update({"ome_tiff": inp["slide"], "output_dir": args.out,
                "channel_remap_params": inp["params"], "artifact_kind": "a2c_oracle",
                "config_hash": "a2c_oracle"})
    kw = {}
    if args.mode == "sources":
        kw = {"corrected_zarr_path": inp["corrected"], "corrected_decisions": inp["decisions"],
              "use_pixel_sources": True}
    worker = FullFusionWorker(loader=loader, fusion_cfg=cfg, n_rows=2, n_cols=2,
                              rois=inp["rois"] or None, **kw)
    errs = []
    worker.error.connect(errs.append)
    t0 = time.perf_counter()
    worker.run()
    if errs:
        print(errs[0])
        sys.stdout.flush()
        os._exit(1)
    print(f"[fusion] {args.mode}: {time.perf_counter() - t0:.2f}s decisions={inp['decisions']} "
          f"-> {args.out}", flush=True)
    sys.stdout.flush()
    os._exit(0)


def main():
    import probe_v16_a2b_ngff as probe
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("step4")
    p.add_argument("--out", required=True)
    p.add_argument("--copy", default=DEFAULT_COPY)
    p = sub.add_parser("fusion")
    p.add_argument("--out", required=True)
    p.add_argument("--copy", default=DEFAULT_COPY)
    p.add_argument("--mode", choices=["loader", "sources"], default="loader")
    for name in ("same-step4", "same-fused"):
        p = sub.add_parser(name)
        p.add_argument("a")
        p.add_argument("b")
    args = ap.parse_args()
    return {"step4": cmd_step4, "fusion": cmd_fusion, "same-step4": probe.cmd_same_step4,
            "same-fused": probe.cmd_same_fused}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
