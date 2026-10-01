"""Build the object layer v1 of a project (block A4).

    python scripts/build_object_tables.py PROJECT cells SEGMENTATION_RUN_ID
    python scripts/build_object_tables.py PROJECT regions

Writes ``objects/<run>/cells.parquet`` / ``objects/regions.parquet`` and their
provenance entries; reads the LabelStore and the workspaces' committed
regions, writes nothing else. Exit code 2 for a project whose schema this
program does not know or that cannot be built.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))


def main(argv=None):
    from block01.core import object_tables as ot
    from block01.core.project_identity import ProjectSchemaError

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("what", choices=["cells", "regions"])
    ap.add_argument("run_id", nargs="?")
    args = ap.parse_args(argv)
    try:
        if args.what == "cells":
            if not args.run_id:
                ap.error("cells needs a SEGMENTATION_RUN_ID")
            out = ot.build_cells(args.project, args.run_id)
        else:
            out = ot.build_regions(args.project)
    except (ProjectSchemaError, ot.ObjectTableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
