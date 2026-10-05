"""The ONE owner of the project's current identities (block A8, E2 / E4).

`docs/v16_A8_application.md`, minimum scope (user ruling 2026-10-05): only
the CURRENT SEGMENTATION RUN lives here -- the one Step3 shows and Step4
quantifies. The slide, the region and the pixel source stay with their
existing single holders and are recorded for v17 (plan §17.2).

Pure Python, no Qt, no signals, no general setter: two named writers,
`choose_segmentation_run` and `clear_segmentation_run`, and an immutable
snapshot to read. It stores nothing on disk -- the window persists a run of
the current workspace as `session.json`'s `viewing`, exactly where it did
before; a run from another workspace or project (Step3's `Load…`) can be
current but is never persisted.

`origin` says who made the choice ("step2", "step3", "restore"). It exists
for one rule only (characterisation C9 / C10): a finished Step2 run becomes
current only while nothing more deliberate than another Step2 result is.
"""

import dataclasses
import logging
import os
from typing import Optional

_log = logging.getLogger(__name__)

ORIGINS = ("step2", "step3", "restore")


@dataclasses.dataclass(frozen=True)
class SegmentationRun:
    """The current segmentation run: its folder, the region shown ("" when
    none was named), and who chose it."""
    run_dir: str
    region: str = ""
    origin: str = "step3"


class ProjectState:
    """The project's current segmentation run."""

    __slots__ = ("_run",)

    def __init__(self):
        self._run = None

    @property
    def active_segmentation_run(self) -> Optional[SegmentationRun]:
        return self._run

    def choose_segmentation_run(self, run_dir, region="", origin="step3"):
        if not run_dir or origin not in ORIGINS:
            return False
        if origin == "step2" and self._run is not None and self._run.origin != "step2":
            return False               # a Step3 choice / a restored run is kept (C9)
        self._run = SegmentationRun(os.path.realpath(str(run_dir)), str(region or ""), origin)
        _log.debug("project state: segmentation run <- %s", self._run)
        return True

    def clear_segmentation_run(self):
        if self._run is not None:
            _log.debug("project state: segmentation run cleared")
        self._run = None
        return True
