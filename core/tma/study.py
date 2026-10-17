"""TMA study record (F1, docs/tma_foundation_application.md §3, §4 F1):
``<project>/tma/tma_study.json`` ties the slide (its A3 ``slide_id``) to
one map sheet (file fingerprint + the parsed, checked layout). It is
registered in the provenance as kind ``tma_map_sheet``.

One study per project (the TMA project is one slide). Creating it again
with the same slide and the same map sheet returns the record as it is;
a different slide or map sheet is refused -- nothing is overwritten
silently (a new map sheet is a new set of position identities, §3).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Optional

from .. import provenance as prov
from . import map_sheet as ms

SCHEMA_VERSION = 1
STUDY_DIR = "tma"
STUDY_FILE = "tma_study.json"


class StudyError(ValueError):
    pass


def study_path(project_dir) -> str:
    return os.path.join(project_dir, STUDY_DIR, STUDY_FILE)


def load_study(project_dir) -> Optional[Dict]:
    """The study record, or None when the project has none; raises
    `StudyError` on an unreadable record or another schema."""
    path = study_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        raise StudyError(f"{path}: unreadable ({exc})")
    if rec.get("schema_version") != SCHEMA_VERSION:
        raise StudyError(f"{path}: schema_version {rec.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    return rec


def create_study(project_dir, slide_path, map_path) -> Dict:
    """Parse and check `map_path` (raises `ms.MapSheetError`), identify the
    slide (registers its ``raw_slide`` entry; the project needs its schema
    stamp), write the record and register it. Returns the record."""
    sheet = ms.parse(map_path)
    slide_id = prov.ensure_raw_slide(project_dir, slide_path)
    existing = load_study(project_dir)
    if existing is not None:
        same = (existing.get("slide_id") == slide_id
                and existing.get("map_sheet", {}).get("sha256") == sheet.sha256)
        if same:
            return existing
        raise StudyError(f"{study_path(project_dir)}: the project already has a TMA study for "
                         f"{existing.get('slide_id')} with map sheet {existing.get('map_sheet', {}).get('sha256', '')[:12]}; "
                         "a different slide or map sheet is not overwritten")
    rec = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "slide_id": slide_id,
        "slide_path": os.path.abspath(slide_path),
        "map_sheet": sheet.to_dict(),
    }
    path = study_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prov.write_json_atomic(path, rec)
    prov.safe_register(project_dir, "tma_map_sheet", prov.location(project_dir, path), sheet.sha256,
                       slide_id=slide_id,
                       parameters={"map_sheet_path": sheet.path, "map_sheet_sha256": sheet.sha256,
                                   "block_name": sheet.block_name, "layout": f"{sheet.rows}x{sheet.cols}",
                                   "occupied": len(sheet.occupied())},
                       software={"module": "core.tma.study", "commit": prov.git_commit()})
    return rec
