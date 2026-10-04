"""The ONE owner of where the user is looking (block A7, E4 / gate 8).

`docs/v16_A7_application.md`. The owner holds the USER'S INTENT -- the place
the user navigated to or jumped to -- not what a viewer happens to show. Each
step's viewer renders it with its own clamp, and nothing a viewer does on its
own (a layout, a resize, a source refresh, a tile or mask arriving, a channel
being switched on) is ever written back.

Three write entries and no general `set`:
  * `user_navigated(step, snapshot)` -- a drag, a wheel or a right-drag zoom
    (pyqtgraph's `sigRangeChangedManually`), including on a compare panel;
  * `jump(step, snapshot)` -- an explicit command: Navigator, a patch, a
    preview, Fit, entering compare, the first fit after a Load;
  * `reset_for_dataset(dataset)` -- another slide: the old position is not a
    place on it.
`current(dataset)` reads. Each write logs one plain line (debug level).
"""

import logging

from .shared_camera import CameraSnapshot

_log = logging.getLogger(__name__)


class CameraOwner:
    """Where the user is, on one slide."""

    __slots__ = ("_shot", "_dataset")

    def __init__(self):
        self._shot = None
        self._dataset = ""

    # ── reading ───────────────────────────────────────────────────────
    def current(self, dataset):
        """The user's camera on `dataset`, or None."""
        shot = self._shot
        if shot is None or not shot.valid_for(dataset):
            return None
        return shot

    @property
    def dataset(self):
        """The slide the owner belongs to ("" before the first one)."""
        return self._dataset

    # ── the three writers ─────────────────────────────────────────────
    def user_navigated(self, step, snapshot):
        return self._write(snapshot, f"user:step{step}")

    def jump(self, step, snapshot):
        return self._write(snapshot, f"jump:step{step}")

    def reset_for_dataset(self, dataset):
        self._shot = None
        self._dataset = str(dataset or "")
        _log.debug("camera owner reset for %s", self._dataset)
        return True

    def _write(self, snapshot, origin):
        if not isinstance(snapshot, CameraSnapshot) or not snapshot.usable():
            return False
        self._shot = snapshot
        self._dataset = snapshot.dataset
        _log.debug("camera owner %s <- %s", origin, snapshot)
        return True
