"""Block A7 (E4 / gate 8): the reusable zero-write-back audit.

`camera_writes` (a pytest fixture, imported by a test module) records every
write into the window's camera owner -- `user_navigated`, `jump`,
`reset_for_dataset` -- with its origin, by wrapping the three writers with
monkeypatch (application §3 C4, ruling 7: no product counter). A9-8 and the
daily check use the same fixture.

On code from BEFORE A7 there is no owner; the fixture then wraps
`MainWindow._remember_camera` instead (ruling 13), so the same assertions run
there and must fail -- the read-back on entering a step writes.

`as_user(view_box)` stands for the user's hand in tests that move a view
programmatically: it emits the signal pyqtgraph emits for a drag / wheel
(`sigRangeChangedManually`), after the move has been made.
"""

import pytest


def as_user(view_box):
    """Announce the range `view_box` now has as the user's own move."""
    view_box.sigRangeChangedManually.emit(view_box.state["mouseEnabled"])


class CameraWrites:
    def __init__(self):
        self.events = []          # (kind, step or dataset, origin)

    def clear(self):
        self.events.clear()

    def count(self, kind=None):
        return len([e for e in self.events if kind is None or e[0] == kind])

    def __repr__(self):                                  # pragma: no cover
        return f"CameraWrites({self.events!r})"


@pytest.fixture
def camera_writes(monkeypatch):
    rec = CameraWrites()
    try:
        from block01.ui.camera_owner import CameraOwner
    except ImportError:                                  # code before A7
        CameraOwner = None
    if CameraOwner is not None:
        for name in ("user_navigated", "jump"):
            real = getattr(CameraOwner, name)

            def wrapped(self, step, snapshot, _real=real, _name=name):
                rec.events.append((_name, step, getattr(snapshot, "origin", "")))
                return _real(self, step, snapshot)
            monkeypatch.setattr(CameraOwner, name, wrapped)
        real_reset = CameraOwner.reset_for_dataset

        def reset(self, dataset):
            rec.events.append(("reset_for_dataset", dataset, ""))
            return real_reset(self, dataset)
        monkeypatch.setattr(CameraOwner, "reset_for_dataset", reset)
    else:
        from block01.ui.main_window import MainWindow
        real = MainWindow._remember_camera

        def remember(self, camera, origin=""):
            rec.events.append(("write", None, origin))
            return real(self, camera, origin)
        monkeypatch.setattr(MainWindow, "_remember_camera", remember)
    return rec
