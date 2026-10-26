"""Write or verify the model manifest in envs/fusion_mesmer/models.json.

The model weights are too large for the repository (cpsam alone is 1.2 GB),
so the repository records where each engine expects them and the size and
SHA-256 of every file. A deployment copies the files into place and runs
this with --verify; a missing file or a checksum mismatch is an error.

A `generated` entry (the Mesmer torch weights) is made on each machine from
another entry, so its bytes may differ: it records the CONTENT digest of its
file instead (`content_sha256`, seg_runner/mesmer_torch/weights.py), which
--write keeps and --verify compares. Only
`scripts/convert_mesmer_to_torch.py --update-manifest` sets a new one.

    python scripts/model_manifest.py --write
    python scripts/model_manifest.py --verify
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = _REPO / "envs" / "fusion_mesmer" / "models.json"
sys.path.insert(0, str(_REPO))
from seg_runner.engines import model_root  # noqa: E402  (the one path rule)

# Where each engine loads its weights from today. `~` is the running user's
# home. The Mesmer path is the one hard-coded in
# utils/mesmer_utils.py:_default_mesmer_model_path (DEEPCELL_MESMER_MODEL_PATH
# overrides it).
MODELS = {
    "cellpose_cpsam": {"engine": "cellpose", "root": "~/.cellpose/models",
                       "files": ["cpsam"]},
    # Listed, not globbed: StarDist unpacks its zip into a `_extracted`
    # folder beside it at first use, which is not part of the deployment.
    "stardist_2D_versatile_fluo": {"engine": "stardist", "root": "~/.keras/models/StarDist2D/2D_versatile_fluo",
                                   "files": ["2D_versatile_fluo.zip", "config.json", "thresholds.json",
                                             "weights_best.h5"]},
    "mesmer_multiplex_segmentation": {"engine": "mesmer",
                                      "root": "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation",
                                      "env_override": "DEEPCELL_MESMER_MODEL_PATH",
                                      "files": None},
    # The torch conversion the Mesmer engine loads (docs/mesmer_torch_plan.md
    # §10): made from the entry above at its first use on each machine,
    # never committed (non-commercial weights licence).
    "mesmer_multiplex_segmentation_torch": {"engine": "mesmer",
                                            "root": "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation_torch",
                                            "env_override": "BLOCK01_MESMER_TORCH_PATH",
                                            "derived_from": "mesmer_multiplex_segmentation",
                                            "generated": True,
                                            "files": ["mesmer_multiplex_segmentation.pt"]},
}


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _files(root, names):
    base = pathlib.Path(os.path.expanduser(root))
    if names is not None:
        return base, sorted(names)
    return base, sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())


def _content_digest(path):
    from seg_runner.mesmer_torch import weights
    return weights.content_digest(weights.read(path))


def _recorded():
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8")).get("models", {})
    except (OSError, ValueError):
        return {}


def write():
    out = {"schema": 1, "models": {}}
    recorded = _recorded()
    for key, spec in MODELS.items():
        if spec.get("generated"):
            digest = recorded.get(key, {}).get("content_sha256")
            if not digest:
                print(f"cannot write the manifest: {key} has no recorded content_sha256 "
                      "(set one with scripts/convert_mesmer_to_torch.py --update-manifest)")
                return 1
            entry = {k: v for k, v in spec.items() if k != "files"}
            entry["content_sha256"] = digest
            entry["files"] = [{"path": n} for n in spec["files"]]
            out["models"][key] = entry
            continue
        base, names = _files(model_root(spec), spec["files"])
        missing = [n for n in names if not (base / n).is_file()]
        if not names or missing:
            print(f"cannot write the manifest: {key} has "
                  + (f"missing files {missing} in {base}" if missing else f"no files in {base}"))
            return 1
        entry = {k: v for k, v in spec.items() if k != "files"}
        entry["files"] = [{"path": n, "size": (base / n).stat().st_size, "sha256": _sha256(base / n)}
                          for n in names]
        out["models"][key] = entry
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    total = sum(f.get("size", 0) for m in out["models"].values() for f in m["files"])
    print(f"wrote {MANIFEST} ({sum(len(m['files']) for m in out['models'].values())} files, {total} bytes)")
    return 0


def set_content_digest(key, digest):
    """Record `digest` as entry `key`'s content (a new baseline); only this
    entry changes."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["models"][key]["content_sha256"] = digest
    MANIFEST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {key} content_sha256 {digest} in {MANIFEST}")


def verify():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    for key, entry in data["models"].items():
        base = pathlib.Path(model_root(entry))
        for f in entry["files"]:
            p = base / f["path"]
            if not p.is_file():
                print(f"MISSING {key}: {p}")
                bad += 1
            elif entry.get("generated"):
                try:
                    same = _content_digest(p) == entry.get("content_sha256")
                except Exception as exc:  # noqa: BLE001 -- an unreadable file is a mismatch
                    print(f"UNREADABLE {key}: {p} ({exc})")
                    same = False
                if not same:
                    print(f"MISMATCH {key}: {p} (content)")
                    bad += 1
            elif p.stat().st_size != f["size"] or _sha256(p) != f["sha256"]:
                print(f"MISMATCH {key}: {p}")
                bad += 1
    print("models: ok" if not bad else f"models: {bad} problem(s)")
    return 0 if not bad else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    sys.exit(write() if args.write else verify())
