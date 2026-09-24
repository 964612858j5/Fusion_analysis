"""One random-patch generation, off the GUI thread (plan block A2).

Reading the nucleus channel at ~16x and building the tissue mask takes about
two seconds on the real slide (A2 phase-1 measurement), so it runs on a
thread of its own that ends with the job (user approval, 2026-09-24). It
reads through the loader's own `read_region_lowres` and the slide's pyramid
metadata, and touches no viewer, scheduler or cache. The answer comes back
on the GUI thread through `finished`.
"""

import threading
import traceback

from PyQt5.QtCore import QObject, pyqtSignal

from ...core import random_patches


def slide_level_downsamples(loader):
    """The pyramid's level downsamples, from the file's own metadata; [16]
    when there is no readable pyramid (the loader then serves 16x itself)."""
    path = getattr(loader, "filepath", "") or ""
    try:
        import tifffile
        with tifffile.TiffFile(path) as tif:
            levels = list(tif.series[0].levels)
            h0 = float(levels[0].shape[-2])
            return [max(1.0, h0 / float(lv.shape[-2])) for lv in levels]
    except Exception:  # noqa: BLE001 -- not a pyramid we can read
        return [float(random_patches.TARGET_DS)]


def run_generation(request):
    """The whole job, synchronously: read, mask, draw. Returns a dict."""
    loader = request["loader"]
    downsamples = slide_level_downsamples(loader)
    ds = downsamples[random_patches.pick_mask_level(downsamples)]
    ds_int = max(1, int(round(ds)))
    h, w = (int(v) for v in loader.shape[:2])
    signal = loader.read_region_lowres(request["channel"], 0, h, 0, w, ds_int,
                                       normalize=False)
    mask = random_patches.tissue_mask(signal, ds_int)
    result = random_patches.generate(
        request["count"], request["height"], request["width"],
        mask=mask, ds=ds_int, region=request.get("region") or (0, h, 0, w),
        polygon=request.get("polygon"), existing=request.get("existing") or (),
        seed=request.get("seed", random_patches.DEFAULT_SEED))
    return {"generation": result, "error": ""}


class RandomPatchJob(QObject):
    """Runs `run_generation` on a thread; `finished(dict)` on the GUI thread."""

    finished = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, request):
        if self.is_running():
            return False
        self._thread = threading.Thread(target=self._run, args=(request,),
                                        name="random-patches", daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, request):
        try:
            answer = run_generation(request)
        except Exception:  # noqa: BLE001 -- reported to the GUI, not raised on a thread
            answer = {"generation": None, "error": traceback.format_exc()}
        self.finished.emit(answer)
