"""S0-C (docs/v17_step0_engine_plan.md §16): the compare strip's three panels
(Original | TopHat | cuCIM) drawn by the Rust viewer core.

One engine mount per panel (`Step0EngineMount.start_on`), each drawing into
its own `Panel` of the full image's GL host -- borrowed, never owned: the full
image's mount made it and disposes it, and tells this one first
(`closing_hooks`). While the strip runs, the host is laid over the strip's
three ViewBoxes (`Step0GlHost.use_compare`); the full image's panel is paused
and its cores give their GPU tiles back down to the coarse level.

What this file adds to three mounts is what makes them one instrument:

* ONE TURN. A selection change reaches the three controllers in one GUI
  turn; their mounts are synced here in one turn too (`display_changed` is
  coalesced into one `sync_all`), so no paint can come between them and
  publish one panel's new picture alone.
* ONE PUBLICATION. The host publishes the three panels' new pictures in the
  same paint or not at all (§16.5); this engine shows the host once all
  three have a picture (until then the Python panels are the picture), and
  tells the strip when a publication made all three show what the
  controllers ask for (`committed`), and when that frame was presented
  (`presented`).
* ONE FALLBACK. Any failure on the host sends every mount to the Python
  picture (the host's `failure_hooks`); this dataset is not tried again.
"""

import time

from PyQt5 import QtCore

from ...utils import perf_trace
from .engine_mount import ENGINE, PREPARE_BUDGET_S, PREPARE_POLL_S, Step0EngineMount

#: §16.6: three panels, current + candidate each, share one viewer's allowance
COMPARE_BUDGET_SHARE = 6
#: `prepare_ahead`: the longest it goes on, and the pause between its paints
AHEAD_BUDGET_S = 5.0
AHEAD_POLL_MS = 15


class _PanelStack:
    """What a mount reads of a stack, for one compare panel: its own
    controller, view and nucleus overlay; the strip's shared provider and
    scheduler."""

    def __init__(self, controller, view, overlay, provider, scheduler):
        self.controller = controller
        self.view = view
        self.overlay = overlay
        self.provider = provider
        self.scheduler = scheduler


class CompareEngine(QtCore.QObject):
    """The engine on the compare strip's stacks. `refused` says why it is
    not used (then nothing here acts again)."""

    #: a paint published pictures that made all three panels show their
    #: controllers' selections (inside that paint's turn, queued)
    committed = QtCore.pyqtSignal()
    #: the frame of such a publication reached the screen
    presented = QtCore.pyqtSignal()
    #: any frame of this layout reached the screen
    frame_presented = QtCore.pyqtSignal()
    #: (why) the engine failed: the Python panels are the picture from now on
    failed = QtCore.pyqtSignal(str)

    def __init__(self, strip, host, stacks, parent=None):
        super().__init__(parent)
        from .gl_host import Panel
        self.strip = strip
        self.host = host
        self.stacks = stacks
        self.refused = ""
        self._running = False
        self._closed = False
        self._sync_queued = False
        self._done_pending = False
        self._ahead = None
        self.mounts = []
        self.panels = []
        for i, (controller, view, overlay) in enumerate(
                zip(stacks.controllers, stacks.views, stacks.overlays)):
            panel = Panel(host, f"compare{i}")
            panel.keep_standby = False
            mount = Step0EngineMount(_PanelStack(controller, view, overlay,
                                                 stacks.provider, stacks.scheduler), parent=self)
            mount.budget_share = COMPARE_BUDGET_SHARE
            mount.group = self
            # the full image's manifest of the same file, or the first
            # panel's: read once, not once per panel
            offers = [host.manifest_offer] + [m.manifest_offer for m in self.mounts]
            if not mount.start_on(host, panel, offers):
                self.refused = mount.refused or "a compare panel could not use the engine"
                break
            self.mounts.append(mount)
            self.panels.append(panel)
        if self.refused:
            self._close_mounts()
            return
        host.closing_hooks.append(self.close)
        host.commit_hooks.append(self._committed_in_paint)
        host.failure_hooks.append(self._failed)
        host.frameSwapped.connect(self._frame_swapped)

    # ── state ─────────────────────────────────────────────────────────
    @property
    def usable(self) -> bool:
        return not (self.refused or self._closed) and len(self.mounts) == len(self.panels) > 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def picture(self) -> str:
        """`engine` while the host shows the three panels' pictures."""
        host = self.host
        if not self.usable or host is None or host.mode != "compare" or not host.isVisible():
            return "python"
        return ENGINE if all(p.published is not None for p in self.panels) else "python"

    def waiting(self) -> bool:
        """A new picture is being prepared on some panel (§16.5)."""
        return self.usable and any(p.waiting() for p in self.panels)

    def shows_wanted(self) -> bool:
        """Every panel's published picture is its controller's selection."""
        return self.picture == ENGINE and all(m.shows_wanted() for m in self.mounts)

    # ── one turn (§16.5) ──────────────────────────────────────────────
    def display_changed(self) -> None:
        """A mount's controller or overlay changed: all three are synced
        together, on the next turn."""
        if not self._sync_queued:
            self._sync_queued = True
            QtCore.QTimer.singleShot(0, self.sync_all)

    def sync_all(self) -> None:
        """The three mounts take their controllers' selections now, in this
        turn (also called directly by the strip and before a hand-off)."""
        self._sync_queued = False
        if not self.usable:
            return
        for mount in self.mounts:
            mount.sync()

    # ── running ───────────────────────────────────────────────────────
    def set_running(self, running: bool) -> None:
        """The strip is on screen (and its stacks resumed), or not."""
        running = bool(running)
        if not self.usable or running == self._running:
            return
        self._running = running
        host = self.host
        if not running:
            for mount in self.mounts:
                mount.set_running(False)
            if host.mode == "compare":
                host.hide()
            return
        views = [m.stack.view for m in self.mounts]
        host.use_compare(self.strip.panes_widget, views, self.panels)
        # §16.13 5: the full image's cores keep their coarse level only
        host.full.release_to_coarse()
        for mount in self.mounts:
            mount.set_running(True)
        self._show_if_ready()

    def published(self, mount) -> None:
        """A mount's candidate is its panel's picture now: on screen at once
        if the three are, else when all three have one."""
        host = self.host
        if self.usable and self._running and host.mode == "compare" and host.isVisible():
            mount.take_picture()
            return
        self._show_if_ready()

    def _show_if_ready(self) -> None:
        """The host over the three panels once all three have a picture;
        the Python panels stop in the same turn, all three together --
        until then they are the picture and follow the camera."""
        host = self.host
        # every published picture must be its panel's selection (codex
        # review 2026-10-30, P2-3): one prepared for another channel is
        # never put up under this channel's labels -- the Python panels
        # stay the picture until the switch is published (`_after_commit`)
        if (self.usable and self._running and host.mode == "compare"
                and all(m.ahead_ready() for m in self.mounts) and not host.isVisible()):
            host.show()
            host.raise_()
            for mount in self.mounts:
                if mount.panel.current is not None:
                    mount.take_picture()
            perf_trace.mark("step0.compare_engine_shown")

    # ── publications ──────────────────────────────────────────────────
    def _committed_in_paint(self, panels) -> None:
        if not any(p in self.panels for p in panels):
            return
        # a switch's display follows inside this paint (`on_switched`): if
        # the three now show their selections, this paint's frame is the one
        if self.shows_wanted():
            self._done_pending = True
        # the strip's labels follow on the next turn, never inside the paint
        QtCore.QTimer.singleShot(0, self._after_commit)

    def _after_commit(self) -> None:
        if self.usable and self._running and not self.host.isVisible():
            self._show_if_ready()                # a switch published off screen
        if self.shows_wanted():
            self._done_pending = True            # (a candidate's: shown after the paint)
            self.committed.emit()

    def _frame_swapped(self) -> None:
        if not self.usable or self.host.mode != "compare":
            return
        self.frame_presented.emit()
        if self._done_pending and self.shows_wanted():
            self._done_pending = False
            self.presented.emit()

    # ── Enter while comparing (§16.7, 16.13 2) ────────────────────────
    def prepare_now(self) -> bool:
        """Before a production run starts: the latest selections synced in
        this turn, then rounds of -- each panel's demands and gate tiles, one
        paint of the three (the group publication is in it) -- until all
        three show their selections, nothing more can be done, or
        `PREPARE_BUDGET_S` has gone; what is published is then presented at
        once. True when all three show their selections."""
        if not (self.usable and self._running) or self.picture != ENGINE:
            return False
        self.sync_all()
        host = self.host
        mounts = self.mounts
        if any(m.panel.current is None for m in mounts):
            return False

        def done(m):
            panel = m.panel
            return (panel.candidate is None and panel.current.identity == m._wanted_identity()
                    and m._switch_done(panel.current))
        if all(done(m) for m in mounts):
            return False                             # nothing to switch to
        deadline = time.monotonic() + PREPARE_BUDGET_S
        peek = self.stacks.scheduler.peek_corrected
        # a parameter edit is a switch on a panel's raw core -- or, from a
        # saved product, a candidate raw core (codex S0-C code 1, P2-5)
        adapters = [slot.adapter for m in mounts for slot in m.panel.live_cores()
                    if slot.adapter is not None]
        for a in adapters:
            a.set_sync(True)
        try:
            while True:
                news = 0
                for a in adapters:
                    left_ms = max(1.0, (deadline - time.monotonic()) * 1000.0)
                    news += a.step_sync(peek, left_ms)["new"]
                if not host.paint_now(present=False, candidates=True):
                    return False
                if all(done(m) for m in mounts):
                    perf_trace.mark("step0.compare_switch_ready")
                    host.present_now()
                    return True
                if time.monotonic() >= deadline:
                    perf_trace.mark("step0.keep_fallback", why="budget", where="compare")
                    return False
                last = host.last_paint
                progress = (news or last.get("uploads") or last.get("deferred")
                            or last.get("deferred_growth") or last.get("reserving"))
                if not progress and not any(a.gate_pending() for a in adapters):
                    perf_trace.mark("step0.keep_fallback", why="stalled", where="compare")
                    return False
                time.sleep(PREPARE_POLL_S)           # the workers run meanwhile
        finally:
            for a in adapters:
                a.set_sync(False)

    # ── prepared ahead (codex review 2026-10-30, a trial) ─────────────
    def prepare_ahead(self, width, height) -> bool:
        """While the strip is not running (the full image just loaded): the
        three panels' cores made now and painted off screen, one panel per
        turn, until each has published a picture and reserved its GPU
        blocks (a first entry measured 1.4 s cold, 65 ms once entered
        before) -- then paused, kept for the entry. Stops when the strip
        runs, or after `AHEAD_BUDGET_S`. False when nothing is prepared."""
        if not self.usable or self._running or self._ahead is not None:
            return False
        queue = [m for m in self.mounts if m.prepare_ahead()]
        if not queue:
            return False
        self._ahead = {"queue": queue, "size": (int(width), int(height)), "paints": 0,
                       "t0": time.monotonic()}
        QtCore.QTimer.singleShot(0, self._ahead_step)
        return True

    def _ahead_step(self) -> None:
        ahead = self._ahead
        if ahead is None:
            return
        if not self.usable or self._running:
            self._end_ahead("running" if self.usable else "closed")
            return
        if time.monotonic() - ahead["t0"] > AHEAD_BUDGET_S:
            self._end_ahead("budget")
            return
        mount = ahead["queue"].pop(0)
        report = self.host.paint_ahead(mount.panel, *ahead["size"])
        ahead["paints"] += 1
        if report is None:
            ahead["unpainted"] = True
        elif not mount.ahead_ready() or report["reserving"] or report["uploads"]:
            ahead["queue"].append(mount)
        if not ahead["queue"]:
            self._end_ahead("stopped" if ahead.get("unpainted") else "done")
            return
        QtCore.QTimer.singleShot(AHEAD_POLL_MS, self._ahead_step)

    def release_hidden_for_production(self) -> None:
        """A production run while the strip is not on screen: the paints
        ahead stop, every panel's Python sources go (their requests
        cancelled) and its tiles are given back, as the full image's."""
        self.stop_ahead("production")
        if not self.usable or self._running:
            return
        for mount in self.mounts:
            mount.release_for_production()

    def stop_ahead(self, why="stopped") -> bool:
        """No more off-screen paints (a production run, the full image
        moved). True when they were still going."""
        if self._ahead is None:
            return False
        self._end_ahead(why)
        return True

    def _end_ahead(self, why) -> None:
        ahead, self._ahead = self._ahead, None
        if not self._running:
            for mount in self.mounts:
                mount.pause_ahead()
        host = self.host
        perf_trace.mark("step0.compare_ahead", why=why, paints=ahead["paints"],
                        ms=round((time.monotonic() - ahead["t0"]) * 1000.0),
                        published=sum(p.published is not None for p in self.panels),
                        gpu_mb=round(sum(int(s.core.gpu_bytes()) for p in self.panels for s in p.cores())
                                     / 2 ** 20) if self.usable and host is not None else -1)

    # ── failure and end of life ───────────────────────────────────────
    def _failed(self, why) -> None:
        """The host failed (the mounts fall back on their own hooks): the
        strip draws with Python from now on."""
        if not self.refused:
            self.refused = why or "the engine failed"
            self._running = False
            # after the mounts' own fallbacks (queued before this one)
            QtCore.QTimer.singleShot(0, lambda: self.failed.emit(self.refused))

    def _close_mounts(self) -> None:
        for mount in self.mounts:
            mount.close()
            mount.deleteLater()
        self.mounts = []

    def close(self) -> None:
        """Every panel's cores and Python sources go (before the strip's
        scheduler is shut down); the host stays, it is the full image's."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        host = self.host
        for hooks, fn in ((host.closing_hooks, self.close), (host.commit_hooks, self._committed_in_paint),
                          (host.failure_hooks, self._failed)):
            if fn in hooks:
                hooks.remove(fn)
        try:
            host.frameSwapped.disconnect(self._frame_swapped)
        except (TypeError, RuntimeError):
            pass
        if host.mode == "compare":
            host.hide()
        self._close_mounts()
