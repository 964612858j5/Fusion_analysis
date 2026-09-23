"""Write or verify the model manifest in envs/fusion_mesmer/models.json.

The model weights are too large for the repository (cpsam alone is 1.2 GB),
so the repository records where each engine expects them and the size and
SHA-256 of every file. A deployment copies the files into place and runs
this with --verify; a missing file or a checksum mismatch is an error.

    python scripts/model_manifest.py --write
    python scripts/model_manifest.py --verify
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys

MANIFEST = pathlib.Path(__file__).resolve().parent.parent / "envs" / "fusion_mesmer" / "models.json"

# Where each engine loads its weights from today. `~` is the running user's
# home. The Mesmer path is the one hard-coded in
# utils/mesmer_utils.py:_default_mesmer_model_path (DEEPCELL_MESMER_MODEL_PATH
# overrides it).
MODELS = {
    "cellpose_cpsam": {"engine": "cellpose", "root": "~/.cellpose/models",
                       "files": ["cpsam"]},
    "stardist_2D_versatile_fluo": {"engine": "stardist", "root": "~/.keras/models/StarDist2D/2D_versatile_fluo",
                                   "files": None},
    "mesmer_multiplex_segmentation": {"engine": "mesmer",
                                      "root": "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation",
                                      "env_override": "DEEPCELL_MESMER_MODEL_PATH",
                                      "files": None},
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


def write():
    out = {"schema": 1, "models": {}}
    for key, spec in MODELS.items():
        base, names = _files(spec["root"], spec["files"])
        entry = {k: v for k, v in spec.items() if k != "files"}
        entry["files"] = [{"path": n, "size": (base / n).stat().st_size, "sha256": _sha256(base / n)}
                          for n in names]
        out["models"][key] = entry
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    total = sum(f["size"] for m in out["models"].values() for f in m["files"])
    print(f"wrote {MANIFEST} ({sum(len(m['files']) for m in out['models'].values())} files, {total} bytes)")


def verify():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    for key, entry in data["models"].items():
        root = os.environ.get(entry.get("env_override", ""), "") or entry["root"]
        base = pathlib.Path(os.path.expanduser(root))
        for f in entry["files"]:
            p = base / f["path"]
            if not p.is_file():
                print(f"MISSING {key}: {p}")
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
