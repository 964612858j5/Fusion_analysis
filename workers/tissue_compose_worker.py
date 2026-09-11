"""The Block01 thread that turns a Tissue Preview snapshot into pixels.

WHY A SECOND WORKER AND NOT THE STEP1 ONE. The scheduling is identical --
one computation at a time, a single pending snapshot the newest submission
replaces -- so that machinery is inherited rather than copied. What must NOT
be shared is the cache: `PreviewComposeWorker`'s entries are Step1 patch
arrays, this one's are whole-slide arrays, and the two are different pixels
for the same channel. They are also owned by different threads, and "the
worker owns its cache" is the rule that makes both of them safe.

LIFETIME. Block01's, not a step's. The user walks Step0 -> Step1 -> Step2 ->
Step3 and back with the same popup open; a worker torn down by whichever page
happened to be left would take the next step's first frame with it. It is
created by the coordinator, retired by the coordinator, and no page may stop
it.

WHAT CROSSES THE BOUNDARY. Going in: whole-slide low-resolution arrays the
GUI thread will not mutate (a new read replaces the array object rather than
writing into it), the display windows, colours, weights and fusion
configuration as plain values, and the identity the result must be checked
against. Coming back: one uint8 RGB array and that identity. No widget, no
camera, no page state.
"""

from ..core import preview_compose, tissue_compose
from ..utils import perf_trace
from .preview_compose_worker import PreviewComposeWorker


class TissueComposeWorker(PreviewComposeWorker):
    """Compose whole-slide Tissue Preview frames off the GUI thread.

    Latest-only and single-flight, inherited. Only the arithmetic differs:
    `core.tissue_compose` rather than the patch composer, so Step0's single
    channel, Step1's overlay and Step1's fusion all come out of one place.
    """

    THREAD_NAME = "tissue-compose"

    def __init__(self, parent=None, cache=None):
        super().__init__(parent, cache=cache
                         if cache is not None else preview_compose.PreviewCache())

    def _compose(self, request):
        with perf_trace.span("tissue.compose", mode=request.get("mode"),
                             owner=request.get("owner"),
                             rev=request.get("rev"),
                             channels=len(request.get("arrays") or {})):
            hits_before = len(self.cache)
            rgb = tissue_compose.compose(request, self.cache,
                                         span=perf_trace.span)
            cached = len(self.cache) - hits_before
        result = dict(request)
        # The pixels go back as ONE array: the source arrays are the GUI
        # thread's and it already has them, and shipping them back would put
        # a whole panel's worth of whole-slide data through a queued signal.
        result.pop("arrays", None)
        result.pop("to_rgb", None)
        result.pop("fallback_norm", None)
        result["rgb"] = rgb
        result["cache_added"] = cached
        return result
