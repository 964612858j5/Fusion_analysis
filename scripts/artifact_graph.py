"""Query a project's artifact graph v0 (block A3; read only).

    python scripts/artifact_graph.py PROJECT list
    python scripts/artifact_graph.py PROJECT lineage H5AD
    python scripts/artifact_graph.py PROJECT deps ID [--recursive]
    python scripts/artifact_graph.py PROJECT dependents ID [--recursive]
    python scripts/artifact_graph.py PROJECT issues

Exit code 2 for a project whose schema this program does not know.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))


def main(argv=None):
    from block01.core.artifact_graph import ArtifactGraph
    from block01.core.project_identity import ProjectSchemaError

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("cmd", choices=["list", "lineage", "deps", "dependents", "issues"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args(argv)
    try:
        graph = ArtifactGraph(args.project)
    except ProjectSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if graph.legacy:
        print("legacy project: no project_schema_version, no provenance records")
        return 0
    if args.cmd == "list":
        out = [{"artifact_id": a, "kind": e["kind"], "location": e["location"]["path"],
                "created_at": e["created_at"]} for a, e in graph.entries.items()]
    elif args.cmd == "lineage":
        out = graph.step4_lineage(args.target)
        out["details"] = {a: {"kind": graph.kind(a),
                              "location": graph.entries[a]["location"]}
                          for v in out.values() if isinstance(v, list)
                          for a in v if a in graph.entries}
    elif args.cmd == "deps":
        out = graph.depends_on(args.target, recursive=args.recursive)
    elif args.cmd == "dependents":
        out = graph.dependents(args.target, recursive=args.recursive)
    else:
        out = graph.issues()
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
