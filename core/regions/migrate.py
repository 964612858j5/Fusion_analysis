"""Old data into the Regions document (plan §3 "旧数据迁移").

* F3's draft (``tma_layout_draft.json``, schema 1, circle pieces) is
  converted ONCE into ``regions_draft.json``; the old file is renamed
  ``.migrated`` so a discarded draft can never come back. When both exist
  (an interrupted conversion), the Regions draft wins and the old file is
  renamed too.
* F3's locked versions (``tma_layout_v<n>.json``) stay read-only; they can
  be read as a document (`document_from_f3`) to start a new draft.
* Circles become 64-gons (`geometry.circle_polygon`).
"""

from __future__ import annotations

import copy
import json
import os
from typing import Dict, Optional, Tuple

from . import geometry as G
from . import lock as K
from .document import RegionsError, new_document

F3_DRAFT = "tma_layout_draft.json"


def _piece_from_f3(p: Dict, shape) -> Dict:
    poly = p.get("polygon") or G.circle_polygon(p["cy"], p["cx"], p["r"], shape)
    return {"polygon": [list(q) for q in poly], "source": p.get("source", "detected"),
            "candidate_id": p.get("candidate_id"), "tissue": p.get("tissue"),
            "owner": p.get("owner"), "dropped": p.get("dropped")}


F3_SCHEMA = 1


def document_from_f3(old: Dict, shape: Tuple[int, int], locked: bool = False,
                     slide_id: Optional[str] = None) -> Dict:
    """An F3 draft or locked version as a Regions document; another schema
    or another slide is refused."""
    if old.get("schema_version") != F3_SCHEMA:
        raise RegionsError(f"not an F3 TMA layout of schema {F3_SCHEMA}: {old.get('schema_version')!r}")
    if slide_id is not None and old.get("slide_id") != slide_id:
        raise RegionsError(f"the F3 layout is for another slide ({old.get('slide_id')})")
    d = new_document(old["slide_id"], shape, based_on=None)
    d.update(map_sha256=old.get("map_sha256"), candidates_version=old.get("candidates_version"),
             core_diameter_px=old.get("core_diameter_px"))
    d["pieces"] = {pid: _piece_from_f3(p, shape) for pid, p in old["pieces"].items()}
    d["positions"] = {k: {f: copy.deepcopy(v.get(f, dflt)) for f, dflt in K.DRAFT_POSITION_FIELDS}
                      for k, v in old["positions"].items()}
    for uid, c in old["cores"].items():
        d["cores"][uid] = {"pieces": list(c["pieces"]), "position_key": c.get("position_key"),
                           "custom_label": c.get("custom_label"),
                           "mapping": "matched" if locked else c.get("mapping", "unmatched")}
        for p in c["pieces"]:
            d["pieces"][p]["owner"] = uid
    d["operations"] = [] if locked else list(old.get("operations", []))
    d["operations"].append({"kind": "migrated_from_f3", "locked_version": old.get("version") if locked else None})
    return d


def migrate_draft(project_dir, shape: Tuple[int, int], slide_id: Optional[str] = None) -> Optional[Dict]:
    """The Regions draft of the project, converting F3's once. None when
    there is no draft at all. An unreadable F3 draft is refused and left
    where it is."""
    old_path = os.path.join(K._dir(project_dir), F3_DRAFT)
    new = K.load_draft(project_dir, slide_id)
    if os.path.isfile(old_path):
        if new is None:
            with open(old_path, "r", encoding="utf-8") as f:
                new = document_from_f3(json.load(f), shape, slide_id=slide_id)
            K.save_draft(project_dir, new)
            new["session"] = None
        os.replace(old_path, old_path + ".migrated")
    return new


def f3_locked_versions(project_dir):
    return sorted(K._numbered(project_dir, "tma_layout_v", ".json"))


def load_f3_locked(project_dir, version: int, shape, slide_id: Optional[str] = None) -> Dict:
    """An F3 locked version as a (read-only) Regions document."""
    path = os.path.join(K._dir(project_dir), f"tma_layout_v{int(version)}.json")
    with open(path, "r", encoding="utf-8") as f:
        old = json.load(f)
    d = document_from_f3(old, shape, locked=True, slide_id=slide_id)
    d["based_on"] = None
    return d
