"""Artifact graph v0 (block A3; plan v2.3 §6.8): a READ-ONLY view of a
project's provenance records.

Its edges are ONLY ``depends_on`` (artifact_id -> artifact_id). Spatial scope
(``operates_on``) is reported beside it and is never an edge. The graph
answers "what does X depend on", "what depends on X" and, for a Step4
result, which segmentation run, fused product and corrected channels it
came from. It also reports what it cannot answer: inputs that were never
registered, edges to ids that do not exist, flagged entries, and locations
that a later artifact has since overwritten (a shared target such as
``fused_<roi>.zarr``).

It never marks anything stale, rebuilds, deletes or writes a file.
"""

import os
from collections import defaultdict
from typing import Dict, List, Optional

from . import provenance as prov
from .project_identity import LEGACY, read_project_schema


class ArtifactGraph:
    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.schema = read_project_schema(self.project_dir)     # raises when unknown
        self.legacy = self.schema == LEGACY
        entries = [] if self.legacy else prov.load_entries(self.project_dir)
        self.entries: Dict[str, Dict] = {e["artifact_id"]: e for e in entries}
        self._dependents = defaultdict(set)
        for aid, e in self.entries.items():
            for dep in e.get("depends_on") or []:
                self._dependents[dep].add(aid)

    # ── edges ──────────────────────────────────────────────────────────
    def depends_on(self, aid, recursive=False) -> List[str]:
        return self._walk(aid, lambda a: (self.entries.get(a) or {}).get("depends_on") or [],
                          recursive)

    def dependents(self, aid, recursive=False) -> List[str]:
        return self._walk(aid, lambda a: sorted(self._dependents.get(a, ())), recursive)

    def _walk(self, aid, nxt, recursive):
        seen, order, todo = set(), [], list(nxt(aid))
        while todo:
            a = todo.pop(0)
            if a in seen:
                continue
            seen.add(a)
            order.append(a)
            if recursive:
                todo.extend(nxt(a))
        return order

    def kind(self, aid) -> Optional[str]:
        return (self.entries.get(aid) or {}).get("kind")

    # ── lookups ────────────────────────────────────────────────────────
    def find_by_path(self, path, kind=None) -> List[str]:
        """Entries located at `path` (newest last). A lookup by location is
        how a file is found in the graph; edges never come from paths."""
        loc = prov.location(self.project_dir, path)
        return [aid for aid, e in self.entries.items()
                if (kind is None or e.get("kind") == kind)
                and (e.get("location") or {}).get("path") == loc["path"]
                and (e.get("location") or {}).get("relative_to") == loc["relative_to"]]

    def step4_lineage(self, h5ad_path) -> Dict:
        """Which segmentation run, fused product(s) and corrected channels a
        Step4 h5ad used -- through ``depends_on`` only."""
        hits = self.find_by_path(h5ad_path, kind="step4_h5ad")
        if not hits:
            raise KeyError(f"no registered step4_h5ad at {h5ad_path}")
        step4 = hits[-1]
        direct = self.depends_on(step4)
        seg = [a for a in direct if self.kind(a) == "segmentation_run"]
        fused = sorted({a for s in seg for a in self.depends_on(s) if self.kind(a) == "fused"})
        return {
            "step4_h5ad": step4,
            "segmentation_run": seg,
            "fused": fused,
            "corrected_channels_step4": [a for a in direct if self.kind(a) == "corrected_channel"],
            "corrected_channels_fused": sorted({a for f in fused for a in self.depends_on(f)
                                                if self.kind(a) == "corrected_channel"}),
            "raw_slide": sorted({a for a in self.depends_on(step4, recursive=True)
                                 if self.kind(a) == "raw_slide"}),
            "unresolved_inputs": {a: (self.entries.get(a) or {}).get("unresolved_inputs")
                                  for a in [step4] + seg + fused
                                  if (self.entries.get(a) or {}).get("unresolved_inputs")},
            "operates_on": (self.entries[step4].get("operates_on") or []),
        }

    # ── what the graph cannot answer ───────────────────────────────────
    def issues(self) -> Dict[str, List]:
        dangling = [(aid, dep) for aid, e in self.entries.items()
                    for dep in e.get("depends_on") or [] if dep not in self.entries]
        unresolved = [(aid, e["unresolved_inputs"]) for aid, e in self.entries.items()
                      if e.get("unresolved_inputs")]
        flagged = [(aid, e["flags"]) for aid, e in self.entries.items() if e.get("flags")]
        by_loc = defaultdict(list)
        for aid, e in self.entries.items():
            loc = e.get("location") or {}
            by_loc[(e.get("kind"), loc.get("relative_to"), loc.get("path"),
                    loc.get("member"))].append(e)
        overwritten = []
        for key, group in by_loc.items():
            if len(group) > 1:
                group = sorted(group, key=lambda e: e.get("created_at", ""))
                for old in group[:-1]:
                    overwritten.append({"artifact_id": old["artifact_id"], "location": key[2],
                                        "now_holds": group[-1]["artifact_id"],
                                        "depended_on_by": self.dependents(old["artifact_id"])})
        return {"dangling": dangling, "unresolved_inputs": unresolved, "flagged": flagged,
                "overwritten": overwritten}


__all__ = ["ArtifactGraph"]
