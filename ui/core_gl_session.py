"""One Rust viewer core's session with the GUI (docs/v17_step0_engine_plan.md
§10.2): what every widget that draws a `ViewerCore` needs, apart from the
widget itself. Cut out of `Step1CoreLayer` so the Step0 host can hold one
per core (two while a new source is being prepared).

Here: the redraw-notice socket (drain -> ack -> the owner's callback), the
GL attach / release calls, one paint and the reading of its report, the
read count the A9 driver watches, the DRAWING records (`gpu.frame`,
`gpu.grow`, the core's trace) and the memory-pressure check after growth.
Not here: the widget, its geometry and device pixel ratio, who owns the GL
context, the PRESENTATION records (`gpu.present`, the coverage record --
those belong to whatever reaches the screen), and Step3's masks.
"""

from typing import Callable, Optional

from PyQt5 import QtCore

from ..utils import perf_trace
from ..utils import tile_trace
from ..viewer import read_ledger

#: Block A9-O2: a frame slot (~60 Hz) for work the last paint left over
FRAME_MS = 16


class CoreGlSession(QtCore.QObject):
    """`core` plus its notice socket pair `(read end, write end)`, owned
    here. `on_notice()` runs after a notice is drained and acknowledged;
    `owner` is the widget registered with `gpu_memory`; `tag()` names it
    in measurement records; `channel_name(i)` names a channel in traces."""

    def __init__(self, core, notice_socket=None, *, owner, on_notice: Callable[[], None],
                 tag: Callable[[], str], channel_name: Callable[[int], str], parent=None):
        super().__init__(parent)
        self.core = core
        self._owner = owner
        self._on_notice_cb = on_notice
        self._tag = tag
        self._channel_name = channel_name
        self.frame = 0
        self._reads_seen = 0
        self._pressure_pending = False
        self._closed = False
        self._sockets = notice_socket
        self._notifier = None
        if notice_socket is not None:
            notice_socket[0].setblocking(False)
            self._notifier = QtCore.QSocketNotifier(notice_socket[0].fileno(),
                                                    QtCore.QSocketNotifier.Read, self)
            self._notifier.activated.connect(self._on_notice)

    # ── notices from the core ─────────────────────────────────────────
    def _on_notice(self, *_args) -> None:
        sockets = self._sockets
        if sockets is None:
            return
        try:                                             # drain -> ack -> work
            while sockets[0].recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            return
        self.core.ack()
        self._on_notice_cb()

    # ── GL (the caller's context is current) ──────────────────────────
    def attach_gl(self, context) -> None:
        def get_proc(name):
            pointer = context.getProcAddress(QtCore.QByteArray(name))
            return int(pointer) if pointer else 0

        self.core.attach_gl(get_proc)

    def release_gl(self) -> None:
        self.core.release_gl()

    def forget_gl(self) -> None:
        self.core.forget_gl()

    # ── one paint ─────────────────────────────────────────────────────
    def paint(self, fbo: int, width: int, height: int, view, trial=None) -> dict:
        """`trial`: S0-B's `(promote, yield)` channel lists for this paint
        alone (docs/v17_step0_engine_plan.md §11.5), optionally with a third
        element, the gate level (§14.6: the report's `trial_exact`); None for
        a normal paint."""
        if trial is None:
            report = self.core.paint(int(fbo), max(1, int(width)), max(1, int(height)), view)
        else:
            level = trial[2] if len(trial) > 2 else None
            report = self.core.paint(int(fbo), max(1, int(width)), max(1, int(height)), view,
                                     (list(trial[0]), list(trial[1])),
                                     None if level is None else int(level))
        if report.get("error"):
            perf_trace.mark("gpu.core_error", error=str(report["error"])[:160].replace(" ", "_"))
        return report

    @staticmethod
    def failure_of(report) -> str:
        """Why this paint ends the core's drawing ("" when it does not):
        the frame was not drawn, or the reads were given up."""
        if report.get("error"):
            return f"paint failed: {report['error']}"
        if report.get("read_failure"):
            return f"reads failed: {report['read_failure']}"
        return ""

    @staticmethod
    def again_ms(report) -> Optional[int]:
        """When to paint again for work this paint left over, or None."""
        if report.get("deferred", 0) or report.get("reserving"):
            return FRAME_MS                    # the rest (or the next reserved block) next slot
        if report.get("retry_ms"):
            return max(1, int(report["retry_ms"]) + 1)   # a failed read is due again
        return None

    # ── measurement (A9 tools read these; default off) ────────────────
    def note_reads(self) -> None:
        """The core's reads into `read_ledger`, which the A9 driver watches."""
        reads = int(self.core.stats().get("reads", 0))
        for _ in range(max(0, reads - self._reads_seen)):
            read_ledger.note("raw", -1, 0)
        self._reads_seen = reads

    def note_frame(self, report, expected: int, input_seq=None):
        """The drawing records of one paint. Returns `(frame, result, seq,
        report)` for the presentation records, which the caller publishes
        when (and only if) this picture reaches the screen."""
        self.frame += 1
        complete = (int(report.get("channels", 0)) >= expected
                    and int(report.get("shown", 0)) >= int(report.get("wanted", 0)))
        wanted = int(report.get("wanted", 0))
        fraction = (int(report.get("shown", 0)) / wanted) if wanted else (1.0 if complete else 0.0)
        result = {"gap_cells": 0 if complete else 1, "exact_fraction": round(fraction, 4),
                  "target_fraction": round(fraction, 4), "channels": int(report.get("channels", 0)),
                  "expected": expected, "level": int(report.get("level", -1))}
        # the coverage record at composition time, as the Python binding
        # stamps it at publication (its "shown" is then the next present)
        state = (result["level"], int(report.get("wanted", 0)), int(report.get("shown", 0)),
                 int(report.get("view_id", 0)))
        if tile_trace.ON and state != getattr(self, "_traced_coverage", None):
            self._traced_coverage = state
            tile_trace.stamp("cov", level=None if state[0] < 0 else state[0], wanted=state[1],
                             shown=state[2], vp=state[3], viewer=self._tag())
        growths = self.core.drain_growths()
        if growths and not self._pressure_pending:
            # A9 §36 / R3 plan §2.2: the viewers grew -- hidden ones give
            # back if over target. Not here: releasing another viewer makes
            # ITS context current, so after this paint.
            self._pressure_pending = True
            QtCore.QTimer.singleShot(0, self._pressure_check)
        if perf_trace.enabled():
            for at, layers, alloc_ms, warm_ms, status, error in growths:   # §48: block creation
                perf_trace.mark("gpu.grow", frame=self.frame, viewer=self._tag(),
                                at=round(at, 6), layers=layers,
                                alloc_ms=round(alloc_ms, 3), warm_ms=round(warm_ms, 3),
                                fb_status=hex(status), gl_error=hex(error))
            perf_trace.mark("gpu.frame", frame=self.frame, rust_frame=int(report.get("frame", 0)),
                            viewer=self._tag(),
                            level=int(report.get("level", -1)),
                            ideal=int(report.get("ideal", -1)), channels=int(report.get("channels", 0)),
                            planes=int(report.get("planes", 0)), uploads=int(report.get("uploads", 0)),
                            deferred=int(report.get("deferred", 0)),
                            deferred_growth=int(report.get("deferred_growth", 0)),
                            capacity_exceeded=int(bool(report.get("capacity_exceeded"))),
                            paint_ms=round(float(report.get("total_ms", 0.0)), 3),
                            upload_ms=round(float(report.get("upload_ms", 0.0)), 3),
                            draw_ms=round(float(report.get("draw_ms", 0.0)), 3), backend="rust")
        return (self.frame, result, input_seq, report)

    def _pressure_check(self) -> None:
        self._pressure_pending = False
        if self._closed:
            return
        from . import gpu_memory
        gpu_memory.on_layer_active(self._owner)

    def merge_trace(self) -> None:
        """The core's stage records into `tile_trace`, with their own times."""
        for t, stage, key, extra in self.core.drain_trace():
            if len(tile_trace._records) >= tile_trace.LIMIT:
                break
            name = key and self._channel_name(key[0])
            k = None if key is None else (name, int(key[1]), int(key[2]), int(key[3]))
            # R6 §12.4: which viewer (Step1 / Step3 are two layers)
            fields = {"viewer": self._tag()}
            for n, v in extra.items():                 # the fields a9_tiletl reads
                if n == "fine":
                    fields["tier"] = "fine" if v else "coarse"
                elif n == "err":
                    fields["err"] = bool(v)
                else:
                    fields[n] = int(v) if float(v).is_integer() and n == "ok" else round(float(v), 3)
            tile_trace._records.append((t, stage, k, fields))

    # ── end of life ───────────────────────────────────────────────────
    def close(self) -> None:
        """No notice and no read after this (the GL names are released
        separately, in the owner's context)."""
        self.core.close()
        self._closed = True
        if self._notifier is not None:
            self._notifier.setEnabled(False)

    def close_sockets(self) -> None:
        if self._sockets is not None:
            for end in self._sockets:
                try:
                    end.close()
                except OSError:
                    pass
            self._sockets = None

