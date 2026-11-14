"""
block01/workers/cellpose_worker.py -- the image loaders of Step0 / Step1:
the DAPI overview and one patch's channels, each in a QThread.

No segmentation runs here (Windows plan W2): every method runs in the
seg_runner engine (Step1 pre-segmentation, Step2). The old in-process
Phase 1 / Phase 2 / patch-preview workers were removed with their panel.
"""

from ..qt.QtCore import QThread, Signal

from ..utils import perf_trace


class OverviewLoaderThread(QThread):
    """Background thread to load overview (DAPI full image downsampled)"""
    done  = Signal(object)   # float32 ndarray
    error = Signal(str)

    def __init__(self, loader, nuc_ch, downsample):
        super().__init__()
        self.loader     = loader
        self.nuc_ch     = nuc_ch
        self.downsample = downsample

    def run(self):
        try:
            arr = self.loader.read_region(
                self.nuc_ch,
                0, self.loader.shape[0],
                0, self.loader.shape[1],
                downsample=self.downsample,
            )
            self.done.emit(arr)
        except Exception as e:
            self.error.emit(str(e))


class PreviewLoaderThread(QThread):
    """Background thread to load all channels for one patch ROI.
    patch_idx is carried through all signals so multiple concurrent threads
    can be distinguished when their results arrive.
    """
    done     = Signal(int, dict)         # (patch_idx, {ch_name: ndarray})
    progress = Signal(int, int, int, str) # (patch_idx, done, total, ch_name)
    error    = Signal(int, str)           # (patch_idx, msg)

    def __init__(self, patch_idx, loader, channels, y0, y1, x0, x1, downsample,
                 normalize=True):
        super().__init__()
        self.patch_idx  = patch_idx
        self.loader     = loader
        self.channels   = channels
        self.y0, self.y1, self.x0, self.x1 = y0, y1, x0, x1
        self.downsample = downsample
        # Default True keeps every existing caller unchanged; Step1 passes False
        # so the fusion preview caches RAW/corrected native intensities (needed
        # for the manual remap, whose Min/Max are in raw units).
        self.normalize  = normalize
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        # Traced as a JOB with both ends and every read inside it. `stop()` is
        # a flag: a thread already inside one `read_region` runs to the end of
        # that read, which is exactly the overlap a stalled GUI needs
        # attributing, so both ends of every read are reported with the
        # process-wide active counts at that instant.
        with perf_trace.job("step1.preview", patch=self.patch_idx,
                            channels=len(self.channels),
                            bbox=f"{self.y0}:{self.y1},{self.x0}:{self.x1}",
                            downsample=self.downsample) as _job:
            try:
                cache = {}
                total = len(self.channels)
                for i, ch in enumerate(self.channels):
                    if self._stop:
                        _job.add(read=len(cache), stopped=1)
                        return
                    self.progress.emit(self.patch_idx, i, total, ch)
                    if ch in self.loader.ch_map:
                        with _job.read(patch=self.patch_idx, channel=ch) as _rd:
                            cache[ch] = self.loader.read_region(
                                ch,
                                self.y0, self.y1,
                                self.x0, self.x1,
                                downsample=self.downsample,
                                normalize=self.normalize,
                            )
                            if self._stop:
                                _rd.add(outcome="cancelled_after_read")
                _job.add(read=len(cache), stopped=int(bool(self._stop)))
                if not self._stop:
                    self.done.emit(self.patch_idx, cache)
            except Exception as e:
                _job.add(failed=1)
                if not self._stop:
                    self.error.emit(self.patch_idx, str(e))
