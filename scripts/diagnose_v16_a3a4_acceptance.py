"""Block A3+A4 acceptance helpers, run on COPIES only.

    python scripts/diagnose_v16_a3a4_acceptance.py copy SRC_COPY DEST
    python scripts/diagnose_v16_a3a4_acceptance.py adopt COPY
    python scripts/diagnose_v16_a3a4_acceptance.py tree COPY OUT.json
    python scripts/diagnose_v16_a3a4_acceptance.py tree-diff A.json B.json

`copy` duplicates a path-rewritten test1 copy (it carries
`A0_COPY_INFO.json`) to DEST and rewrites SRC_COPY -> DEST in its text files;
it refuses anything under ~/fusion_data and anything that is not such a copy.
`adopt` does what the first Save of new code does to an existing project:
`ensure_project_manifest` (schema, P2 description, transforms, raw_slide)
and the Step0 corrected channels' entries for the copy's workspace. `tree`
records every file's size and sha256 (zarr chunks included); `tree-diff`
prints what was added, removed or changed between two records.

Step4 and fusion themselves run through `scripts/diagnose_v16_a2c_oracle.py`
(the same commands exist on the pre-A3 commit) and are compared with its
`same-step4` / `same-fused`.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

FUSION_DATA = os.path.realpath(os.path.expanduser("~/fusion_data"))
TEXT_SUFFIXES = (".json", ".csv", ".txt", ".log", ".zattrs", ".zarray", ".zgroup")


def _guard(path):
    if os.path.realpath(path).startswith(FUSION_DATA):
        raise SystemExit(f"never write under ~/fusion_data: {path}")


def _info(copy):
    path = os.path.join(copy, "A0_COPY_INFO.json")
    if not os.path.exists(path):
        raise SystemExit(f"{copy} is not a path-rewritten copy (no A0_COPY_INFO.json)")
    return json.load(open(path, encoding="utf-8"))


def cmd_copy(args):
    src, dest = os.path.abspath(args.src), os.path.abspath(args.dest)
    _guard(dest)
    info = _info(src)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    old, new = src.encode(), dest.encode()
    rewritten = 0
    for base, _dirs, files in os.walk(dest):
        for name in files:
            if not (name.endswith(TEXT_SUFFIXES) or name.startswith(".z")):
                continue
            p = os.path.join(base, name)
            data = open(p, "rb").read()
            if old in data:
                open(p, "wb").write(data.replace(old, new))
                rewritten += 1
    info = dict(info, dest=dest, copied_from=src)
    json.dump(info, open(os.path.join(dest, "A0_COPY_INFO.json"), "w"), indent=1)
    print(f"copied {src} -> {dest}; rewrote {rewritten} files")


def cmd_adopt(args):
    from block01.core import provenance as prov
    from block01.utils import roi_project as rp
    copy = os.path.abspath(args.copy)
    _guard(copy)
    info = _info(copy)
    manifest = json.load(open(os.path.join(copy, "project_manifest.json")))
    rp.ensure_project_manifest(copy, manifest["source_ome"])
    ws = os.path.join(copy, "rois", info["workspace"])
    zpath = os.path.join(ws, "step0", "corrected_channels.zarr")
    ids = prov.register_corrected_channels(copy, ws, zpath, manifest["source_ome"])
    print(json.dumps({"project_schema_version": json.load(open(os.path.join(
        copy, "project_manifest.json")))["project_schema_version"], "corrected_channels": ids},
        indent=1))


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cmd_tree(args):
    root = os.path.abspath(args.copy)
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            out[os.path.relpath(p, root)] = [os.path.getsize(p), _sha(p)]
    json.dump(out, open(args.out, "w"))
    print(f"{len(out)} files -> {args.out}")


def cmd_tree_diff(args):
    a, b = json.load(open(args.a)), json.load(open(args.b))
    report = {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
              "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    print(json.dumps(report, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("copy")
    p.add_argument("src")
    p.add_argument("dest")
    p = sub.add_parser("adopt")
    p.add_argument("copy")
    p = sub.add_parser("tree")
    p.add_argument("copy")
    p.add_argument("out")
    p = sub.add_parser("tree-diff")
    p.add_argument("a")
    p.add_argument("b")
    args = ap.parse_args(argv)
    return {"copy": cmd_copy, "adopt": cmd_adopt, "tree": cmd_tree,
            "tree-diff": cmd_tree_diff}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
