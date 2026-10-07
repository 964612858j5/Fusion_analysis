"""A9 baseline driver (block A9-M; plan v2.4 §20.3, application §10.1 item 5).

Loaded only when ``BLOCK01_A9_SCRIPT=<scenario.json>`` is set (and tracing,
``BLOCK01_PERF=1``): the main window calls `attach(window)`. It waits until a
project is open and Step0 is on screen, then plays the scenario with QTimer,
marking every action in the perf log so `analyze_perf_log.py --a9` can turn
the run into the A9 numbers. Nothing here changes the program: gestures are
REAL Qt mouse / wheel events posted to the widget under the viewer's centre
(the user's own path); actions that are not gestures (a channel tick, an
Intensity window, a step change) use the same widgets / calls a click does
and are marked ``kind=programmatic``.

Scenario: a JSON list of actions --
  {"do": "step", "to": 1}
  {"do": "settle", "timeout_s": 15}
  {"do": "drag", "dx": 300, "dy": 0, "steps": 20, "ms": 16}
  {"do": "wheel", "notches": 3}            (+ zoom in, - zoom out)
  {"do": "tick", "channel": "CD3D", "on": true}
  {"do": "window", "channel": "CD3D", "min": 0, "max": 120, "gamma": 1.0}
  {"do": "pause", "ms": 500}
  {"do": "mark", "label": "..."}
  {"do": "repeat", "n": 20, "body": [ ... ]}
"""

import json
import os
import time

from PyQt5 import QtCore, QtGui, QtWidgets

from block01.utils import perf_trace
from block01.viewer import coverage_probe, read_ledger

ENV = "BLOCK01_A9_SCRIPT"
DEFAULT_SCENARIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "a9_default.json")


def _flatten(actions):
    out = []
    for action in actions:
        if action.get("do") == "repeat":
            for i in range(int(action.get("n", 1))):
                out.append({"do": "mark", "label": f"repeat {i + 1}"})
                out.extend(_flatten(action.get("body") or []))
        else:
            out.append(action)
    return out


class _Handled(QtCore.QObject):
    """Marks the moment Qt HANDS a posted input to the widget (not when it
    was posted): the latency clock starts here (codex A9-M)."""

    KINDS = (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseMove,
             QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.Wheel)

    def __init__(self, target, n):
        super().__init__(target)
        self.n = n
        target.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in self.KINDS:
            perf_trace.mark("a9.handled", n=self.n, qt=int(event.type()))
        return False

    def done(self):
        try:
            self.parent().removeEventFilter(self)
        except Exception:                                    # noqa: BLE001
            pass
        self.deleteLater()


def _viewer_widget():
    """The largest visible viewer on screen (a pyqtgraph view or the GPU
    layer), then whatever widget is on top at its centre."""
    import pyqtgraph as pg
    best, area = None, 0
    for w in QtWidgets.QApplication.allWidgets():
        if not isinstance(w, (pg.GraphicsView, QtWidgets.QOpenGLWidget)):
            continue
        if not w.isVisible() or not w.window().isVisible():
            continue
        a = w.width() * w.height()
        if a > area:
            best, area = w, a
    if best is None:
        return None, None
    centre = best.mapToGlobal(best.rect().center())
    top = QtWidgets.QApplication.widgetAt(centre) or best
    return top, top.mapFromGlobal(centre)


def _mark_target(n, target):
    """Which widget a gesture went to (A9 7-level run: a Step0 drag drew a
    patch instead of panning) -- class, size and three ancestors."""
    chain, w = [], target.parentWidget() if target is not None else None
    while w is not None and len(chain) < 3:
        chain.append(type(w).__name__)
        w = w.parentWidget()
    perf_trace.mark("a9.target", n=n, cls=type(target).__name__,
                    w=target.width(), h=target.height(), up="/".join(chain))


class Driver(QtCore.QObject):
    def __init__(self, window, actions):
        super().__init__(window)
        self.w = window
        self.actions = _flatten(actions)
        self.i = 0
        self.camera = {"user": 0, "jump": 0}
        self._wrap_camera()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._step)
        self._started = False
        self._base = None

    # ── camera writes (A9-8): counted, not changed ──────────────────────
    def _wrap_camera(self):
        owner = getattr(self.w, "_camera_owner", None)
        if owner is None:
            return
        # The owner has __slots__: the CLASS's two writers are wrapped, in
        # this (driver) process only -- counted, then the real method runs.
        cls = type(owner)
        for name, key in (("user_navigated", "user"), ("jump", "jump")):
            real = getattr(cls, name)

            def counted(owner_self, *a, _real=real, _key=key, **k):
                self.camera[_key] += 1
                return _real(owner_self, *a, **k)
            setattr(cls, name, counted)

    def start(self):
        if self.actions and self.actions[0].get("do") == "open":
            QtCore.QTimer.singleShot(1500, lambda: self._open(self.actions[0]))
            self.i = 1
        self._wait_for_project()

    def _open(self, a):
        """Open a project as the user does: the three paths, Load, and the
        named workspace in the chooser (answered here instead of clicked)."""
        page = self.w._step0
        page._ome_path_edit.setText(a["ome"])
        page._out_path_edit.setText(a["out"])
        page._panel_csv_edit.setText(a.get("panel", ""))
        wanted = a.get("workspace")
        real = page._choose_workspace

        def choose(rows):
            for row in rows:
                ws = row[0] if isinstance(row, tuple) else row
                if wanted is None or getattr(ws, "workspace_id", None) == wanted:
                    return row
            return real(rows)
        page._choose_workspace = choose
        perf_trace.mark("a9.open", workspace=wanted)
        page._reload_from_paths()

    def _wait_for_project(self):
        ready = bool((getattr(self.w, "step0_output", None) or {}).get("step0_manifest_path"))
        if not ready:
            QtCore.QTimer.singleShot(500, self._wait_for_project)
            return
        # an unexposed window paints nothing and presents no frame: bring it
        # to the front, and record whether Qt considers it exposed
        try:
            self.w.showNormal()
            self.w.raise_()
            self.w.activateWindow()
            handle = self.w.windowHandle()
            exposed = bool(handle.isExposed()) if handle is not None else None
        except Exception:                                    # noqa: BLE001
            exposed = None
        perf_trace.mark("a9.window", exposed=exposed, minimized=self.w.isMinimized(),
                        active=self.w.isActiveWindow())
        perf_trace.mark("a9.platform", wsl=_on_wsl())
        perf_trace.mark("a9.begin", actions=len(self.actions))
        QtCore.QTimer.singleShot(3000, self._step)

    def _next(self, delay_ms=0):
        self._timer.start(int(max(0, delay_ms)))

    #: settles in a row that timed out with NO new frame of the expected
    #: viewer -- a run whose window stopped painting (minimised or covered:
    #: WSLg then gives it no frames) measures nothing and is stopped
    BLIND_LIMIT = 3

    #: actions that always change the picture: a settle after one with no
    #: new frame is evidence of a blind window (codex: a mode or channel set
    #: to what it already was legitimately draws nothing)
    MUST_DRAW = ("drag", "wheel", "step")

    def _run_is_blind(self, where, done, presented, prior="drag"):
        """Is this run no longer measuring the screen? If so, say so and stop."""
        reason = None
        try:
            handle = self.w.windowHandle()
            if self.w.isMinimized():
                reason = "minimized"
            elif handle is not None and not handle.isExposed():
                reason = "not_exposed"
        except Exception:                                    # noqa: BLE001
            pass
        if reason is None and prior in self.MUST_DRAW:
            if where and not done and not presented:
                self._blind = getattr(self, "_blind", 0) + 1
                if self._blind >= self.BLIND_LIMIT:
                    reason = "no_frames"
            else:
                self._blind = 0
        if reason is None:
            return False
        perf_trace.mark("a9.invalid", n=self.i, reason=reason)
        print(f"[A9] RUN INVALID at action {self.i}: {reason} -- stopping")
        self.i = len(self.actions)
        self._invalid = reason
        if os.environ.get("BLOCK01_A9_QUIT"):
            QtCore.QTimer.singleShot(500, QtWidgets.QApplication.quit)
        return True

    def _step(self):
        if self.i >= len(self.actions):
            if os.environ.get("BLOCK01_A9_QUIT"):
                QtCore.QTimer.singleShot(2000, QtWidgets.QApplication.quit)
            perf_trace.mark("a9.end", camera_user=self.camera["user"],
                            camera_jump=self.camera["jump"],
                            reads_total=read_ledger.snapshot().get("total", 0))
            print("[A9] scenario finished")
            return
        action = self.actions[self.i]
        self.i += 1
        do = action.get("do")
        scheduled = time.monotonic()
        expect = (self._baseline(action) if do not in ("settle", "mark", "pause")
                  else (self._base or {}).get("where"))
        perf_trace.mark("a9.action", n=self.i, do=do, expect=expect,
                        **{k: v for k, v in action.items() if k != "do" and not isinstance(v, (list, dict))})
        try:
            getattr(self, f"_do_{do}")(action, scheduled)
        except Exception as exc:                          # noqa: BLE001 -- recorded, not fatal
            perf_trace.mark("a9.error", n=self.i, do=do, error=str(exc)[:200])
            self._next(200)

    # ── actions ─────────────────────────────────────────────────────────
    def _do_mark(self, a, _t):
        self._next(0)

    def _do_pause(self, a, _t):
        self._next(int(a.get("ms", 500)))

    def _do_step(self, a, _t):
        to = int(a["to"])
        go = {0: "_go_to_step0", 1: "_go_to_step1", 2: "_go_to_step2",
              3: "_go_to_step3", 4: "_go_to_step4"}[to]
        perf_trace.mark("a9.delivered", n=self.i, kind="programmatic")
        getattr(self.w, go)()
        self._next(0)

    def _do_mode(self, a, _t):
        """Overlay / Fusion, by the button the user clicks."""
        name = "_btn_mode_fusion" if a.get("value") == "fusion" else "_btn_mode_overlay"
        perf_trace.mark("a9.delivered", n=self.i, kind="programmatic")
        getattr(self.w, name).click()
        self._next(0)

    def _do_tick(self, a, _t):
        dock = getattr(self.w, "_channel_dock", None)
        row = dock.row(a["channel"]) if dock is not None else None
        if row is None:
            raise RuntimeError(f"no row {a['channel']!r}")
        perf_trace.mark("a9.delivered", n=self.i, kind="programmatic")
        row.checkbox.setChecked(bool(a.get("on", True)))
        self._next(0)

    def _do_window(self, a, _t):
        state = self.w._display.state
        before = read_ledger.snapshot()
        perf_trace.mark("a9.delivered", n=self.i, kind="programmatic")
        state.set_mapping(a["channel"], float(a["min"]), float(a["max"]),
                          float(a.get("gamma", 1.0)), origin="a9-drive")
        self._after_settle(lambda: perf_trace.mark(
            "a9.window_reads", n=self.i, reads=read_ledger.delta(before).get("total", 0)))

    def _do_wheel(self, a, _t):
        target, pos = _viewer_widget()
        if target is None:
            raise RuntimeError("no viewer on screen")
        _mark_target(self.i, target)
        handled = _Handled(target, self.i)
        notches = int(a.get("notches", 1))
        step = 1 if notches > 0 else -1
        for _ in range(abs(notches)):
            ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(target.mapToGlobal(pos)),
                                   QtCore.QPoint(0, 0), QtCore.QPoint(0, 120 * step),
                                   QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                                   QtCore.Qt.NoScrollPhase, False)
            QtWidgets.QApplication.postEvent(target, ev)
        perf_trace.mark("a9.delivered", n=self.i, kind="gesture")
        QtCore.QTimer.singleShot(0, handled.done)
        self._next(0)

    def _do_drag(self, a, _t):
        target, pos = _viewer_widget()
        if target is None:
            raise RuntimeError("no viewer on screen")
        _mark_target(self.i, target)
        steps = max(1, int(a.get("steps", 20)))
        dx, dy = float(a.get("dx", 200)), float(a.get("dy", 0))
        ms = int(a.get("ms", 16))
        btn = QtCore.Qt.LeftButton

        def send(kind, p, buttons):
            ev = QtGui.QMouseEvent(kind, QtCore.QPointF(p), QtCore.QPointF(target.mapToGlobal(p)),
                                   btn if kind != QtCore.QEvent.MouseMove else QtCore.Qt.NoButton,
                                   buttons, QtCore.Qt.NoModifier)
            QtWidgets.QApplication.postEvent(target, ev)

        handled = _Handled(target, self.i)
        send(QtCore.QEvent.MouseButtonPress, pos, btn)
        state = {"k": 0}

        def move():
            state["k"] += 1
            k = state["k"]
            p = QtCore.QPoint(int(pos.x() + dx * k / steps), int(pos.y() + dy * k / steps))
            send(QtCore.QEvent.MouseMove, p, btn)
            perf_trace.mark("a9.delivered", n=self.i, kind="gesture", step=k)
            if k < steps:
                QtCore.QTimer.singleShot(ms, move)
            else:
                send(QtCore.QEvent.MouseButtonRelease, p, QtCore.Qt.NoButton)
                QtCore.QTimer.singleShot(0, handled.done)
                self._next(0)
        QtCore.QTimer.singleShot(ms, move)

    def _do_settle(self, a, _t):
        perf_trace.mark("a9.settle_begin", n=self.i)
        self._after_settle(lambda: None, timeout_s=float(a.get("timeout_s", 15)))

    def _schedulers(self):
        """The schedulers of the viewers that can be on screen."""
        out = []
        w = self.w
        tab = getattr(getattr(w, "_step0", None), "_explore_tab", None)
        stack = getattr(tab, "stack", None)
        out.append(getattr(stack, "scheduler", None))
        for name in ("_step1_mount", "_step3_mount"):
            mount = w.__dict__.get(name)
            try:
                host = mount.host if mount is not None else None
            except Exception:                                  # noqa: BLE001
                host = None
            hstack = getattr(host, "_stack", None) or getattr(host, "stack", None)
            out.append(getattr(hstack, "scheduler", None))
        return [s for s in out if s is not None and hasattr(s, "idle")]

    def _expected_where(self, step=None):
        step = int(getattr(self.w, "_current_step", 0) or 0) if step is None else int(step)
        return "cpu" if step == 0 else "gpu" if step in (1, 3) else None

    def _baseline(self, action):
        """Taken BEFORE the action runs (codex A9-M): what its settle is
        measured against -- frames published, reads, camera writes."""
        where = self._expected_where(action["to"] if action.get("do") == "step" else None)
        self._base = {"published": dict(coverage_probe.PUBLISHED_BY),
                      "reads": read_ledger.snapshot(), "camera": dict(self.camera),
                      "where": where}
        return where

    def _after_settle(self, then, timeout_s=15.0):
        """Settled (codex A9-M): no read under way and none new for 300 ms,
        every viewer scheduler idle, and a frame of the viewer on screen
        PRESENTED after this action began whose coverage is complete at its
        target level. A timeout is reported as such, never as settled."""
        deadline = time.monotonic() + timeout_s
        base = self._base or {"published": dict(coverage_probe.PUBLISHED_BY),
                              "reads": read_ledger.snapshot(), "camera": dict(self.camera),
                              "where": self._expected_where()}
        start_published = base["published"]
        last = {"reads": read_ledger.snapshot().get("total", 0), "since": time.monotonic()}
        where = base["where"]
        camera0 = base["camera"]
        reads0 = base["reads"]
        n = self.i

        def poll():
            now = time.monotonic()
            reads = read_ledger.snapshot().get("total", 0)
            if reads != last["reads"] or read_ledger.in_flight():
                last["reads"], last["since"] = reads, now
            # Step1/3: the GPU layer's frames, or -- if the GPU could not start
            # -- the host's own CPU view (hidden beneath a working GPU layer
            # its frames are always empty, so they never qualify)
            kinds = (("gpu", "cpu-step1") if where == "gpu" else (where,)) if where else ()
            frame, presented = {}, False
            for kind in kinds:
                f = coverage_probe.LAST_BY.get(kind) or {}
                if coverage_probe.PUBLISHED_BY[kind] > start_published.get(kind, 0):
                    presented = True
                    if f.get("gap_cells", 1) == 0 or not frame:
                        frame = f
            covered = (frame.get("gap_cells", 1) == 0
                       and frame.get("target_fraction", 0.0) >= 0.999)
            idle = all(s.idle() for s in self._schedulers())
            done = (now - last["since"] >= 0.3 and idle
                    and (where is None or (presented and covered)))
            if done or now >= deadline:
                prior = (self.actions[n - 2].get("do")
                         if 2 <= n <= len(self.actions) else None)
                if self._run_is_blind(where, done, presented, prior):
                    return
                perf_trace.mark("a9.settled", n=n, timed_out=not done,
                                gap_cells=frame.get("gap_cells"),
                                target_fraction=frame.get("target_fraction"),
                                reads=read_ledger.delta(reads0).get("total", 0),
                                camera_user=self.camera["user"] - camera0["user"],
                                camera_jump=self.camera["jump"] - camera0["jump"])
                then()
                self._next(0)
                return
            QtCore.QTimer.singleShot(20, poll)
        QtCore.QTimer.singleShot(20, poll)


def _on_wsl():
    """Is this WSL? Its GPU wake-up stall is classified only there."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def attach(window):
    """Called by the main window at start-up when `BLOCK01_A9_SCRIPT` is set."""
    path = os.environ.get(ENV) or ""
    if not path:
        return None
    if path in ("1", "default"):
        path = DEFAULT_SCENARIO
    with open(path, encoding="utf-8") as f:
        actions = json.load(f)
    driver = Driver(window, actions)
    QtCore.QTimer.singleShot(0, driver.start)
    print(f"[A9] driver attached: {path} ({len(driver.actions)} actions)")
    return driver
