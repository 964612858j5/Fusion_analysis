"""Background work of the Regions `Cores` tab (first written for F3's TMA
page): finding cores (F2's `detect_cores` + `save_candidates`). Each job
carries the dataset generation it was started for; the page drops a result of an older
one. Every started thread is kept in `LIVE` until Qt reports it finished,
so none is destroyed while running; `shutdown` asks them to stop and waits
(application close, plan §4).
"""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal

LIVE = set()


def keep(worker):
    LIVE.add(worker)
    worker.finished.connect(lambda w=worker: LIVE.discard(w))
    return worker


def shutdown(timeout_ms: int = 15000) -> bool:
    """Wait for every running TMA job; True when all finished in time."""
    ok = True
    for w in list(LIVE):
        req = getattr(w, "requestInterruption", None)
        if req is not None:
            req()
        ok = w.wait(timeout_ms) and ok
    return ok


class DetectWorker(QThread):
    done = pyqtSignal(int, object)       # generation, saved candidate record
    failed = pyqtSignal(int, str)

    def __init__(self, gen, project_dir, slide_path, slide_id, kw):
        super().__init__()
        self.gen, self.project_dir, self.slide_path, self.slide_id, self.kw = \
            gen, project_dir, slide_path, slide_id, dict(kw)

    def run(self):
        try:
            from ...core.tma import dearray as da
            res = da.detect_cores(self.slide_path, **self.kw)
            # not cut short on a close request: a refused close must find the
            # page usable, so the job always ends with done / failed
            rec = da.save_candidates(self.project_dir, res, self.slide_id)
            self.done.emit(self.gen, rec)
        except Exception as exc:                            # noqa: BLE001
            self.failed.emit(self.gen, f"{type(exc).__name__}: {exc}")
