"""TMA core table (F1, docs/tma_foundation_application.md §3): one row per
LAYOUT position of the study's map sheet, empty positions included.

* ``position_key`` -- the position identity, stable within one map-sheet
  snapshot: ``tmapos_`` + sha256(slide_id | map sheet sha256 | position id).
  Another map-sheet file gives another set of keys (§3).
* ``region_id`` -- the A3 region of the core's geometry in a LOCKED layout
  version; empty until the layout is locked (F3), empty for an empty
  position. ``geometry`` and ``status`` are filled by the dearray (F2) and
  the manual correction (F3); ``status`` is a candidate mark only.
* ``layout_version`` -- 0 is the draft from the map sheet alone.

Stored as ``<project>/tma/tma_cores_v<version>.parquet``, written
atomically through `core.object_tables.write_typed_table` (A4-G8: pyarrow
stays in that module); it does NOT touch the fixed ``regions.parquet``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional

SCHEMA_NAME = "tma_cores"
SCHEMA = "1"
TYPES = (("position_key", "string"), ("position_id", "int32"), ("x", "int32"), ("y", "int32"),
         ("label", "string"), ("occupied", "bool_"), ("donor_block_id", "string"),
         ("sample_id", "string"), ("region_id", "string"), ("status", "string"),
         ("geometry", "string"), ("layout_version", "int32"))
COLUMNS = tuple(c for c, _ in TYPES)


class CoreTableError(ValueError):
    pass


def position_key(slide_id: str, map_sha256: str, position_id: int) -> str:
    if not slide_id or not map_sha256:
        raise CoreTableError("a position identity needs the slide id and the map sheet sha256")
    raw = f"{slide_id}|{map_sha256}|{int(position_id)}".encode()
    return "tmapos_" + hashlib.sha256(raw).hexdigest()[:16]


def draft_rows(study: Dict) -> List[Dict]:
    """Version 0: every layout position of the study's map sheet."""
    sheet = study["map_sheet"]
    rows = []
    for p in sheet["positions"]:
        rows.append({
            "position_key": position_key(study["slide_id"], sheet["sha256"], p["position_id"]),
            "position_id": int(p["position_id"]), "x": int(p["x"]), "y": int(p["y"]),
            "label": f"X{p['x']}-Y{int(p['y']):02d}", "occupied": bool(p["donor_block_id"]),
            "donor_block_id": p["donor_block_id"], "sample_id": p["sample_id"],
            "region_id": "", "status": "", "geometry": "", "layout_version": 0,
        })
    return rows


def table_path(project_dir, version: int) -> str:
    from .study import STUDY_DIR
    return os.path.join(project_dir, STUDY_DIR, f"tma_cores_v{int(version)}.parquet")


def write_table(project_dir, rows: List[Dict], version: int, path: Optional[str] = None) -> str:
    """`path`: where to write instead of the numbered file (Regions' Save
    overwrites one ``tma_cores.parquet``; `version` is then its save count)."""
    from ..object_tables import write_typed_table
    for r in rows:
        if tuple(sorted(r)) != tuple(sorted(COLUMNS)):
            raise CoreTableError(f"row {r.get('position_id')}: columns {sorted(r)}")
        if int(r["layout_version"]) != int(version):
            raise CoreTableError(f"row {r['position_id']}: layout_version {r['layout_version']} != {version}")
    keys = [r["position_key"] for r in rows]
    if len(set(keys)) != len(keys):
        raise CoreTableError("repeated position_key")
    return write_typed_table(path or table_path(project_dir, version), TYPES, rows, SCHEMA_NAME, SCHEMA)


def read_table(path) -> List[Dict]:
    from ..object_tables import ObjectTableError, read_typed_table
    try:
        rows = read_typed_table(path, TYPES, SCHEMA_NAME, SCHEMA)
    except ObjectTableError as exc:
        raise CoreTableError(str(exc))
    keys = [r["position_key"] for r in rows]
    if len(set(keys)) != len(keys):
        raise CoreTableError(f"{path}: repeated position_key")
    versions = {r["layout_version"] for r in rows}
    if len(versions) > 1:
        raise CoreTableError(f"{path}: several layout versions {sorted(versions)}")
    return rows
