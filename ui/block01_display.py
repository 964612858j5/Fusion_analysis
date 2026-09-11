"""Block01's shared display state and its Tissue Preview render pipeline.

WHY THIS LAYER EXISTS. The Intensity window and the Tissue Preview are
Block01 windows: one of each for the life of the process, used from Step0,
Step1, Step2 and Step3. They were reached as `self._step0.<private method>`,
which made Step0 a service locator for two global components and had three
consequences the user reported as bugs:

* the thumbnail's pixels were a function of `Step0Page.current_channel`, so
  Step1's ticked channels, weights, colours and preview mode could not reach
  it at all -- Step1's main viewer followed a weight, the shared Tissue
  Preview did not;
* a channel had two colour answers, `Step0Page._channel_colors` and
  `ConfigPanel._colors`, reconciled only by whichever signal happened to fire
  last;
* the thumbnail was composed on the GUI thread, so it could only be drawn
  after the hand stopped moving.

WHAT IS HERE.

`ChannelDisplayState` -- the ONE answer to "what colour is this channel" and
"what Min/Max/Gamma is it shown with", per dataset, for every step. Colours
are stored here; the mapping is READ through a registered source rather than
copied here, because the numbers already have exactly one owner (the Channel
Remap params the Step0 handoff hashes) and a second copy that could drift is
the failure this class exists to remove, not a new store to add.

`TissuePreviewCoordinator` -- the owner of the whole-slide render pipeline:
the active render context, the frame clock, the compose worker and the
publish/reject rule. Steps register a CONTEXT and submit INPUTS; they never
push pixels and never reach into each other.

WHAT IS NOT HERE. Widgets. The popup and the Intensity window are still
constructed where they are (see `TissuePreviewCoordinator.attach_navigator`):
their construction is entangled with the ROI/patch toolbar and the Channel
Remap inspector, and reparenting those in the same change as the state
contract would have made both unreviewable. What has moved is everything that
decides: ownership, lifetime, the active context, who may publish a frame,
and which results are refused. A step no longer answers any of those.
"""

import math
import time

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from ..core import tissue_compose
from ..utils import perf_trace
from ..workers.tissue_compose_worker import TissueComposeWorker
from .widgets.tissue_navigator_popup import TissueNavigatorPopup

# The frame clock. 33 ms is ~30 FPS and is the interval the Step1 patch
# viewer was measured and accepted at; the Tissue Preview publishes on the
# same budget so the two pictures move together under one hand.
TISSUE_FRAME_MS = 33.0

# Render context identities. Plain strings, so a trace line and a test read
# the same word the code does.
STEP0 = "step0"
STEP1 = "step1"
STEP2 = "step2"
STEP3 = "step3"


def _to_hex(value):
    """Anything a caller might call a colour, as "#rrggbb", or "" if unusable."""
    if value is None:
        return ""
    if isinstance(value, str):
        qc = QColor(value)
        return qc.name() if qc.isValid() else ""
    try:
        r, g, b = (float(v) for v in tuple(value)[:3])
    except (TypeError, ValueError):
        return ""
    if max(r, g, b) <= 1.0:
        r, g, b = r * 255.0, g * 255.0, b * 255.0
    return QColor(int(round(r)), int(round(g)), int(round(b))).name()


def _hex_to_rgb01(value):
    qc = QColor(value)
    if not qc.isValid():
        return (1.0, 1.0, 1.0)
    return (qc.red() / 255.0, qc.green() / 255.0, qc.blue() / 255.0)


class ChannelDisplayState(QObject):
    """THE per-channel display answer every step reads and writes.

    One dataset at a time. Colours live here; mappings are read through
    `set_mapping_source`, which is how "there is one set of Min/Max/Gamma"
    is achieved without making a second copy of numbers the handoff hashes.

    THE LOOP IS CLOSED BY A COMPARISON, NOT BY A FLAG ON EACH SIDE. A write
    that does not change the answer emits nothing, so a mirror echoing its
    own value back is swallowed at the first hop. `_applying` additionally
    stops a mirror's synchronous reaction from starting a second write while
    the first is still fanning out -- the two together are why there is no
    "who emitted last" race left to lose.
    """

    color_changed = pyqtSignal(str, str)        # channel, "#rrggbb"
    mapping_changed = pyqtSignal(str)           # channel
    dataset_changed = pyqtSignal(object)        # token

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = None
        self._colors = {}
        self._default_source = None
        # THE display windows, by (channel, is-nucleus-role). Stored here,
        # not fetched from a step: a shared state that answers by calling
        # `Step0Page._display_mapping_for` is Step0's store with another
        # name, and every step then needs Step0 alive and current to know
        # what Min/Max/Gamma it is drawing with.
        self._mappings = {}
        # Two PORTS, and the difference between them is the whole point.
        # `_seed_port` computes a FIRST window for a channel nobody has set
        # one for -- a percentile over whole-slide pixels, which is pixel
        # work and belongs to whatever holds the pixels. Its answer is then
        # STORED here and never asked for again. `_persist_port` is told
        # about every write, so the remap params the Step0 handoff hashes
        # follow this state rather than competing with it.
        self._seed_port = None
        self._persist_port = None
        self._color_rev = 0
        self._mapping_rev = 0
        self._applying = False
        self._persisting = False

    # ── identity ──────────────────────────────────────────────────────
    def dataset_token(self):
        return self._token

    def bind_dataset(self, token):
        """Say which slide these answers are about.

        Colours are NOT cleared: this product treats a channel's colour as a
        display preference held by channel NAME (see `Step0Page`'s own note
        where `_channel_colors` survives a reload), and the two steps have to
        agree either way. What the token does is make every frame, every
        panel push and every worker result checkable against the slide they
        were computed for.
        """
        if token == self._token:
            return
        self._token = token
        self.dataset_changed.emit(token)

    # ── ports ─────────────────────────────────────────────────────────
    def set_default_source(self, fn):
        """`fn(channel) -> "#rrggbb"`: the palette answer for a channel nobody
        has picked a colour for. Registered by the step that deals the
        palette, so the default a user sees before touching anything is the
        same one in every step."""
        self._default_source = fn

    def set_window_ports(self, seed=None, persist=None):
        """Register the two display-window ports.

        `seed` answers `seed_display_window(channel, nucleus=False)` -- the
        automatic window for a channel nothing has set one for, computed from
        pixels. Consulted ONCE per channel; the answer becomes this state's.

        `persist` answers `write_display_window(channel, lo, hi, gamma,
        nucleus=False)` -- told about every write so the on-disk remap
        params, which the handoff hashes, follow this state.

        Ports, not sources: what they do is supply pixels and durability. The
        ANSWER lives here, which is why `mapping()` keeps working when a port
        is gone and why a value written by any step is immediately the value
        every other step reads.
        """
        if seed is not None:
            self._seed_port = seed
        if persist is not None:
            self._persist_port = persist

    # ── colours ───────────────────────────────────────────────────────
    def has_color(self, channel):
        return bool(channel) and channel in self._colors

    def color(self, channel):
        """`channel`'s colour as "#rrggbb" -- the answer every view uses."""
        if not channel:
            return "#ffffff"
        picked = self._colors.get(channel)
        if picked:
            return picked
        if self._default_source is not None:
            try:
                return _to_hex(self._default_source(channel)) or "#ffffff"
            except Exception:                               # noqa: BLE001
                pass
        return "#ffffff"

    def color_rgb01(self, channel):
        return _hex_to_rgb01(self.color(channel))

    def colors(self, channels=None):
        if channels is None:
            channels = list(self._colors)
        return {ch: self.color(ch) for ch in channels}

    def set_color(self, channel, value, origin=""):
        """Record a colour and fan it out ONCE. True when it changed anything.

        Every entry -- a Step0 swatch, the Channel Remap layer list, a Step1
        row, a restored session -- lands here, so "who wins" is not decided by
        signal order: the last writer wins because it wrote, and the mirrors
        follow rather than argue.
        """
        hexc = _to_hex(value)
        if not channel or not hexc:
            return False
        if self.color(channel) == hexc and channel in self._colors:
            return False
        if self._applying:
            # A mirror reacted synchronously to the fan-out and tried to write
            # back. Its value is the one we are already publishing.
            return False
        self._colors[channel] = hexc
        self._color_rev += 1
        self._applying = True
        try:
            self.color_changed.emit(channel, hexc)
        finally:
            self._applying = False
        return True

    def adopt_colors(self, colors, origin=""):
        """Install a whole set of colours as ONE transaction.

        For a session restore or a handoff: the mirrors are updated and the
        views redraw once at the end, rather than once per channel with a
        half-restored panel visible in between. Returns the channels that
        actually changed.
        """
        changed = []
        for channel, value in (colors or {}).items():
            hexc = _to_hex(value)
            if not channel or not hexc:
                continue
            if self._colors.get(channel) == hexc:
                continue
            self._colors[channel] = hexc
            changed.append(channel)
        if not changed:
            return []
        self._color_rev += 1
        self._applying = True
        try:
            for channel in changed:
                self.color_changed.emit(channel, self._colors[channel])
        finally:
            self._applying = False
        return changed

    def color_revision(self):
        return self._color_rev

    # ── mappings ──────────────────────────────────────────────────────
    def mapping(self, channel, nucleus=False):
        """`(min, max, gamma)` for `channel`, or None when nobody has set one.

        Reads the STORE. No port is consulted, so this answers the same in
        every step and keeps answering after a step page is gone.
        """
        if not channel:
            return None
        return self._mappings.get((channel, bool(nucleus)))

    def mapping_or_seed(self, channel, nucleus=False):
        """`mapping()`, and if nothing is stored, seed one and store it.

        The seed is pixel work (a percentile over the whole slide), so it is
        a port; its RESULT is this state's from the moment it lands. A
        channel is therefore seeded once, not once per reader, and the two
        steps looking at it cannot seed it differently.
        """
        got = self.mapping(channel, nucleus=nucleus)
        if got is not None or not channel or self._seed_port is None:
            return got
        try:
            seeded = self._seed_port.seed_display_window(channel,
                                                         nucleus=nucleus)
        except Exception:                                   # noqa: BLE001
            return None
        if seeded is None:
            return None
        lo, hi, gamma = seeded
        # Stored WITHOUT going back to the persistence port: a seed is what
        # that port just told us, and writing it back would be an echo.
        value = (float(lo), float(hi), float(gamma))
        self._mappings[(channel, bool(nucleus))] = value
        self._mapping_rev += 1
        return value

    def set_mapping(self, channel, lo, hi, gamma=None, nucleus=False,
                    origin="", persist=True):
        """Set `channel`'s display window. THE write, from any step.

        True when it changed anything. The persistence port is told, so the
        remap params on disk follow; a write that came FROM that port passes
        `persist=False`, which is what stops the two from echoing.
        """
        if not channel:
            return False
        key = (channel, bool(nucleus))
        current = self._mappings.get(key)
        if gamma is None:
            gamma = current[2] if current else 1.0
        value = (float(lo), float(hi), float(gamma))
        if current == value:
            return False
        self._mappings[key] = value
        self._mapping_rev += 1
        if persist and self._persist_port is not None and not self._persisting:
            self._persisting = True
            try:
                self._persist_port.write_display_window(
                    channel, value[0], value[1], value[2], nucleus=nucleus)
            except Exception as exc:                        # noqa: BLE001
                print(f"[Block01] could not persist the display window of "
                      f"{channel}: {exc}")
            finally:
                self._persisting = False
        self.mapping_changed.emit(channel)
        return True

    def adopt_mappings(self, mappings, origin="", persist=False):
        """Install a whole set of display windows as ONE transaction.

        For a session restore, a handoff or a dataset load: the views redraw
        once at the end rather than once per channel.
        """
        changed = []
        for key, value in (mappings or {}).items():
            channel, nucleus = (key if isinstance(key, tuple)
                                else (key, False))
            if not channel or value is None:
                continue
            lo, hi, gamma = value
            new = (float(lo), float(hi), float(gamma))
            if self._mappings.get((channel, bool(nucleus))) == new:
                continue
            self._mappings[(channel, bool(nucleus))] = new
            changed.append((channel, bool(nucleus)))
        if not changed:
            return []
        self._mapping_rev += 1
        for channel, nucleus in changed:
            if persist and self._persist_port is not None:
                value = self._mappings[(channel, nucleus)]
                try:
                    self._persist_port.write_display_window(
                        channel, *value, nucleus=nucleus)
                except Exception:                           # noqa: BLE001
                    pass
        for channel, _nucleus in changed:
            self.mapping_changed.emit(channel)
        return changed

    def mappings(self):
        """Every stored window, for a caller that is writing them out."""
        return dict(self._mappings)

    def forget_mappings(self):
        """Drop every window. A new slide's are its own."""
        self._mappings = {}
        self._mapping_rev += 1

    def note_mapping_changed(self, channel):
        """The numbers for `channel` moved underneath us.

        For the one case a store cannot see: the remap workbench's own
        controls wrote its params. The adapter reads them back and calls
        `set_mapping`; this stays for callers that only know that something
        moved.
        """
        self._mapping_rev += 1
        if channel:
            self.mapping_changed.emit(channel)

    def mapping_revision(self):
        return self._mapping_rev


class TissuePreviewCoordinator(QObject):
    """The owner of the one Tissue Preview's pixels, for the whole of Block01.

    THREE THINGS IT OWNS AND NO STEP DOES.

    * THE ACTIVE CONTEXT. Exactly one step draws at a time, and switching is
      an atomic transition that bumps a generation -- which is what makes a
      Step0 frame that finishes after the user has reached Step1 refusable by
      identity rather than by luck.
    * THE FRAME CLOCK. Leading edge, a fixed slot, latest-only at depth one,
      single-flight. The same three rules the Step1 patch viewer was measured
      and accepted at, so both pictures move under one hand at ~30 FPS instead
      of one of them waiting for the hand to stop.
    * THE WORKER. One `TissueComposeWorker` with its own cache, created here
      and retired here. A page may not stop it; walking to Step2 must not cost
      the next step its first frame.

    A step provides a CONTEXT object and submits INPUTS. The context answers
    `tissue_render_snapshot()` with values -- arrays it already holds, the
    display windows, the colours, the weights -- and nothing else is asked of
    it. It never pushes pixels, never touches the timer and never sees another
    step's context.
    """

    frame_published = pyqtSignal(object)        # the result dict, after drawing

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._contexts = {}
        self._active = None
        self._generation = 0
        self._closing = False

        # The panels a published frame is installed into. A callable rather
        # than a list: the popup is created lazily and the Step0 overview is
        # a second view over the same ROI model, so "which panels" is a
        # question with a different answer at different times.
        self._panels_source = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply_pending_frame)

        self._worker = None
        self._worker_factory = TissueComposeWorker
        self._retired_workers = []

        self._input_rev = 0
        self._pending_rev = None
        self._drawing_rev = None
        self._in_flight = False
        self._inflight_request = None
        self._request_id = 0
        self._coalesced = 0
        self._last_publish = 0.0
        self._input_at = 0.0
        self._max_pending_depth = 0

        # Evidence, for the stress runs and for the tests that assert frames
        # rather than callbacks.
        self._stats = {"inputs": 0, "dispatched": 0, "published": 0,
                       "dropped": 0, "gui_composed": 0, "coalesced": 0}
        self._last_published = None

        # A CHANGE TO THE SHARED STATE IS A FRAME REQUEST, wherever it was
        # made. This is the entry that makes the contract global rather than
        # a habit each step has to remember: a colour or a display window
        # settled in Step0's Intensity window, in Step1's channel row or by a
        # restored session all arrive here, and the ACTIVE context is the one
        # asked for the picture. Without it every step needs its own request
        # next to its own redraw, and the step that forgets is the one whose
        # Tissue Preview stops following -- which is the reported bug.
        state.color_changed.connect(self._on_shared_color_changed)
        state.mapping_changed.connect(self._on_shared_mapping_changed)

    # ── wiring ────────────────────────────────────────────────────────
    def attach_navigator(self, panels_source):
        """Register where a published frame is drawn.

        `panels_source()` returns the overview panels that are views over the
        one ROI model -- today the Step0 page's and the floating navigator's.
        The coordinator does not create them and does not know what else they
        draw; it hands each one an image and a dataset token and lets the
        panel apply its own provenance rule.
        """
        self._panels_source = panels_source

    def register_context(self, step_id, context):
        """Register a step's render context. Registering does not activate."""
        self._contexts[step_id] = context

    def unregister_context(self, step_id):
        self._contexts.pop(step_id, None)
        if self._active == step_id:
            self._active = None

    def context(self, step_id=None):
        return self._contexts.get(self._active if step_id is None else step_id)

    def active_context_id(self):
        return self._active

    def generation(self):
        return self._generation

    # ── transitions ───────────────────────────────────────────────────
    def bind_dataset(self, token):
        """A new slide. Everything in flight is about the old one."""
        self.state.bind_dataset(token)
        self._begin_generation("dataset")

    def set_active_context(self, step_id, *, request=True):
        """Make `step_id` the step that draws. ONE atomic transition.

        The generation bump is the whole point: after it, every queued timer,
        every pending revision and every worker result from the step being
        left is structurally stale and refused on arrival. Nothing has to be
        cancelled in the right order, and nothing depends on a signal landing
        before another one.
        """
        if step_id is not None and step_id not in self._contexts:
            # A step with no context registered draws nothing rather than
            # leaving the previous step's picture live under a new policy.
            self._active = step_id
            self._begin_generation("context")
            return
        changed = (step_id != self._active)
        self._active = step_id
        self._begin_generation("context")
        perf_trace.mark("tissue.context", owner=step_id, changed=changed,
                        generation=self._generation)
        if request:
            self.request_frame(kind="context", owner=step_id)

    def _begin_generation(self, why):
        """Retire everything the previous generation had in the air."""
        self._generation += 1
        try:
            self._timer.stop()
        except RuntimeError:
            pass
        self._pending_rev = None
        self._drawing_rev = None
        self._coalesced = 0
        self._in_flight = False
        self._inflight_request = None
        self._last_publish = 0.0
        self._input_at = 0.0
        perf_trace.mark("tissue.generation", why=why,
                        generation=self._generation, owner=self._active)

    # ── input ─────────────────────────────────────────────────────────
    def _on_shared_color_changed(self, channel, _hexc):
        if self._touches(channel):
            self.request_frame(kind="color", channel=channel)

    def _on_shared_mapping_changed(self, channel):
        if self._touches(channel):
            self.request_frame(kind="mapping", channel=channel)

    def _touches(self, channel):
        """Would a change to `channel` change the picture on screen?

        A dataset's mapping fan-out names every channel it touches, and at 30
        FPS a frame recomposed for a channel nothing is drawing is pure cost.
        The last published frame says which channels are in the picture; when
        nothing has been published yet, or the frame is pinned, the answer is
        yes, because "not in the last frame" is then not evidence of anything.
        A channel that has just BECOME part of the picture arrives through a
        visibility, mode or context request instead, so it is not missed.
        """
        if not channel:
            return True
        published = self._last_published
        if not published:
            return True
        channels = published.get("channels")
        if not channels:
            return True
        return channel in channels

    def request_frame(self, kind="", channel="", owner=None, delay_ms=None):
        """THE entry point for every state change that changes the picture.

        Mapping, weight, colour, visibility, mode, the current channel, a
        DAPI layer switch, a finished overview read, a step transition: all
        of them are this call. There is no second path that draws, which is
        what stops one entry from quietly keeping the old trailing behaviour.

        `owner` names the step that believes it is asking. An input from a
        step that is no longer active is dropped HERE -- a queued signal from
        the page the user has just left must not schedule a frame.
        """
        if self._closing:
            return False
        if owner is not None and owner != self._active:
            perf_trace.mark("tissue.drop", why="owner", owner=owner,
                            active=self._active, kind=kind)
            self._stats["dropped"] += 1
            return False
        self._stats["inputs"] += 1
        self._input_rev += 1
        if self._pending_rev is not None:
            self._coalesced += 1
            self._stats["coalesced"] += 1
        self._pending_rev = self._input_rev
        self._input_at = self._now()
        perf_trace.mark("tissue.input", owner=self._active, kind=kind,
                        channel=channel, rev=self._input_rev)

        depth = 1 if self._pending_rev is not None else 0
        self._max_pending_depth = max(self._max_pending_depth, depth)

        since = (self._input_at - self._last_publish) * 1000.0
        wait = max(max(0.0, TISSUE_FRAME_MS - since), float(delay_ms or 0.0))
        try:
            armed = bool(self._timer.isActive())
        except RuntimeError:
            armed = False
        perf_trace.mark("tissue.schedule", rev=self._pending_rev,
                        wait_ms=wait, armed=armed, in_flight=self._in_flight,
                        merged=self._coalesced, pending=depth,
                        leading=not (armed or self._in_flight))
        if self._in_flight or armed:
            return True         # a frame is already due; this input rides it
        if wait <= 0.0:
            self._apply_pending_frame()
            return True
        self._arm(wait)
        return True

    # ── seams (a test drives two seconds of input without waiting two) ──
    def _now(self):
        return time.monotonic()

    def _arm(self, wait_ms):
        # Rounded UP: `int()` on a 32.7 ms wait asks for 32, the slot fires
        # before the budget has elapsed, and over a long drag the clock runs
        # faster than the frame it was chosen against.
        self._timer.start(int(math.ceil(max(0.0, float(wait_ms)))))

    def _dispatch(self, snapshot):
        worker = self._worker_ready()
        if worker is None:
            return False
        return bool(worker.submit(snapshot))

    # ── the frame ─────────────────────────────────────────────────────
    def _apply_pending_frame(self):
        """Publish the newest state once, and leave the next slot to the next
        input.

        Single-flight: `_in_flight` stays true until the result arrives, so a
        drag at 200 inputs a second produces frames at the clock's rate rather
        than a queue of 400 snapshots nobody will see.
        """
        if self._closing or self._in_flight:
            return
        rev = self._pending_rev
        if rev is None:
            return
        context = self.context()
        if context is None:
            self._pending_rev = None
            return
        coalesced, self._coalesced = self._coalesced, 0
        self._pending_rev = None
        self._drawing_rev = rev
        self._in_flight = True

        snapshot = None
        try:
            snapshot = self._snapshot(context, rev)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] could not snapshot the tissue frame: {exc}")
        if snapshot is None:
            # Nothing drawable yet -- no arrays resident, nothing ticked. The
            # context has been asked to fetch what it is missing; its arrival
            # is an input like any other.
            self._in_flight = False
            self._last_publish = self._now()
            perf_trace.mark("tissue.drop", why="no_snapshot", rev=rev,
                            owner=self._active)
            self._stats["dropped"] += 1
            self._maybe_arm_next()
            return

        snapshot["coalesced"] = coalesced
        snapshot["input_rev"] = self._input_rev
        snapshot["input_at"] = self._input_at
        snapshot["dispatched_at"] = self._now()
        self._inflight_request = snapshot["request_id"]
        perf_trace.mark("tissue.dispatch", rev=rev,
                        request=snapshot["request_id"],
                        dataset=snapshot["token"], owner=snapshot["owner"],
                        mode=snapshot["mode"],
                        channels=len(snapshot["arrays"]),
                        coalesced=coalesced)
        self._stats["dispatched"] += 1
        if self._dispatch(snapshot):
            return              # published when the pixels come back

        # No worker (a test driving the synchronous path, or a thread that
        # could not start). Composed here rather than not at all, and counted
        # separately so "the GUI thread did the arithmetic" stays a number.
        self._inflight_request = None
        self._stats["gui_composed"] += 1
        try:
            result = dict(snapshot)
            result["rgb"] = tissue_compose.compose(snapshot, None)
            result.pop("arrays", None)
            result.pop("to_rgb", None)
            result.pop("fallback_norm", None)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] tissue frame failed on the GUI thread: {exc}")
            self._in_flight = False
            self._last_publish = self._now()
            self._maybe_arm_next()
            return
        self._in_flight = False
        self.on_frame(result)

    def _snapshot(self, context, rev):
        """Everything one frame needs, as values the GUI thread is done with.

        The arrays go in by reference and are never written to: a new read
        replaces the array object rather than filling the old one, which is
        what makes handing them to a thread safe and what `array_ids` lets the
        result be checked against.
        """
        ask = getattr(context, "tissue_render_snapshot", None)
        if ask is None:
            return None
        payload = ask()
        if not payload:
            return None
        arrays = payload.get("arrays") or {}
        if not arrays:
            return None
        token = self.state.dataset_token()
        context_token = payload.get("token", token)
        if token is None:
            # Never bound. ADOPTED rather than refused -- the same rule an
            # overview panel applies to its first picture: a coordinator that
            # has not been through a dataset switch has no slide to defend,
            # and refusing every frame until one happens would mean a page
            # that only ever loads one dataset never draws at all. The CHECK
            # is what is being installed here; from the next switch on it is
            # strict.
            self.state.bind_dataset(context_token)
            token = context_token
        elif context_token != token:
            # The context is describing a slide this coordinator has already
            # been told to leave.
            return None
        self._request_id += 1
        snapshot = dict(payload)
        snapshot.update({
            "request_id": self._request_id,
            "rev": rev,
            "owner": self._active,
            "generation": self._generation,
            "token": token,
            "color_rev": self.state.color_revision(),
            "mapping_rev": self.state.mapping_revision(),
            "array_ids": tuple(sorted((ch, id(arr))
                                      for ch, arr in arrays.items())),
            "channel_set": tuple(sorted(arrays)),
        })
        return snapshot

    def _maybe_arm_next(self):
        if self._pending_rev is not None and not self._closing:
            self._arm(TISSUE_FRAME_MS)

    def render_now(self, owner=None):
        """Compose and install ONE frame synchronously, and return its pixels.

        The clock's escape hatch, and deliberately a narrow one: it is for
        callers that need the picture installed before they return -- a panel
        built after the page already has a channel, a test asserting what is
        on screen. It still goes through the active context, the identity
        check and the publish path, so it cannot draw a step that is not the
        active one; what it skips is only the WAIT.
        """
        if self._closing:
            return None
        if owner is not None and owner != self._active:
            perf_trace.mark("tissue.drop", why="owner", owner=owner,
                            active=self._active, kind="render_now")
            return None
        context = self.context()
        if context is None:
            return None
        self._input_rev += 1
        rev = self._input_rev
        self._input_at = self._now()
        try:
            snapshot = self._snapshot(context, rev)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] could not snapshot the tissue frame: {exc}")
            return None
        if snapshot is None:
            return None
        snapshot["coalesced"] = 0
        snapshot["input_rev"] = rev
        snapshot["input_at"] = self._input_at
        result = dict(snapshot)
        with perf_trace.span("tissue.compose", mode=snapshot.get("mode"),
                             owner=snapshot.get("owner"), rev=rev,
                             where="gui"):
            result["rgb"] = tissue_compose.compose(snapshot, None)
        result.pop("arrays", None)
        result.pop("to_rgb", None)
        result.pop("fallback_norm", None)
        self._stats["gui_composed"] += 1
        # The pending revision is NOT consumed here: an input that was waiting
        # for a slot still describes state this synchronous frame may predate.
        self.on_frame(result)
        return result["rgb"]

    # ── results ───────────────────────────────────────────────────────
    def _result_is_current(self, result):
        """Is this result about the picture the window would draw NOW?

        STRUCTURAL identity only, and the distinction is the design:

        * the generation, the dataset token, the owning step, the mode and
          the channel set -- if any of these moved, the result describes
          something nobody is looking at and must be dropped. This is the
          entry that stops a Step0 frame from landing on Step1's preview.
        * the input revision is NOT part of it. During a drag a newer input
          has almost always arrived by the time a frame finishes, so dropping
          results for being a revision behind would starve the screen exactly
          while the hand is moving -- the complaint this work exists to fix.
        """
        if result.get("generation") != self._generation:
            return False, "generation"
        if result.get("token") != self.state.dataset_token():
            return False, "dataset"
        if result.get("owner") != self._active:
            return False, "owner"
        context = self.context()
        if context is None:
            return False, "no_context"
        expected = getattr(context, "tissue_render_mode", None)
        if expected is not None and result.get("mode") != expected():
            return False, "mode"
        return True, ""

    def on_frame(self, result):
        """Pixels from the compose worker: check, publish, space the next.

        `setImage` happens HERE and nowhere else, on the GUI thread, through
        the panels' own setter -- which keeps their camera, their ROI/patch
        artists and their viewport rectangle, because none of that is touched
        by giving them a new image.
        """
        result = dict(result or {})
        if result.get("request_id") == self._inflight_request:
            self._in_flight = False
            self._inflight_request = None
        current, why = self._result_is_current(result)
        rgb = result.get("rgb")
        if not current:
            perf_trace.mark("tissue.drop", why=why, rev=result.get("rev"),
                            request=result.get("request_id"),
                            owner=result.get("owner"), active=self._active)
            self._stats["dropped"] += 1
            self._maybe_arm_next()
            return False
        if rgb is None:
            perf_trace.mark("tissue.drop", why="no_pixels",
                            rev=result.get("rev"), owner=result.get("owner"))
            self._stats["dropped"] += 1
            self._last_publish = self._now()
            self._maybe_arm_next()
            return False
        drawn = 0
        token = result.get("token")
        with perf_trace.span("tissue.publish", rev=result.get("rev"),
                             owner=result.get("owner"),
                             mode=result.get("mode"),
                             coalesced=result.get("coalesced"),
                             input_rev=result.get("input_rev"),
                             latency_ms=(self._now()
                                         - float(result.get("input_at")
                                                 or self._input_at)) * 1000.0):
            for panel in self._panels():
                adopt = getattr(panel, "adopt_dataset", None)
                if callable(adopt):
                    adopt(token)
                setter = getattr(panel, "set_channel_image", None)
                if not callable(setter):
                    continue
                with perf_trace.span("tissue.setImage",
                                     mode=result.get("mode")):
                    if setter(rgb, token) is not False:
                        drawn += 1
        self._stats["published"] += 1
        self._last_publish = self._now()
        self._last_published = {
            "rev": result.get("rev"), "owner": result.get("owner"),
            "mode": result.get("mode"), "token": token,
            "channels": result.get("channel_set"),
            "color_rev": result.get("color_rev"),
            "mapping_rev": result.get("mapping_rev"),
            "drawn": drawn,
            "fingerprint": _fingerprint(rgb),
        }
        self.frame_published.emit(result)
        self._maybe_arm_next()
        return drawn > 0

    def _on_frame_failed(self, payload):
        payload = dict(payload or {})
        request = payload.get("request") or {}
        if request.get("request_id") == self._inflight_request:
            self._in_flight = False
            self._inflight_request = None
        print(f"[Block01] tissue frame failed: {payload.get('error')}")
        self._stats["dropped"] += 1
        self._maybe_arm_next()

    def _panels(self):
        if self._panels_source is None:
            return []
        try:
            return list(self._panels_source() or [])
        except Exception:                                   # noqa: BLE001
            return []

    # ── the worker ────────────────────────────────────────────────────
    def _worker_ready(self):
        """The compose thread, started on first use, or None if it cannot run.

        Lazy because a session that never opens a Tissue Preview should not
        carry a thread, and because a test driving the synchronous path must
        not have one appear behind it.
        """
        if self._closing:
            return None
        if self._worker is not None:
            return self._worker
        try:
            # No parent: a QObject destroyed with the window while its thread
            # is still composing would leave that thread emitting through a
            # deleted C++ object.
            worker = self._worker_factory()
            worker.done.connect(self.on_frame)
            worker.failed.connect(self._on_frame_failed)
            worker.start()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] tissue compose worker unavailable ({exc}); "
                  "frames will be composed on the GUI thread")
            return None
        self._worker = worker
        return worker

    def shutdown(self, reason="close"):
        """Stop taking requests, then retire the thread. In that order.

        Block01's teardown, not a step's: a page being replaced must never
        reach this. New requests are refused FIRST so nothing re-arms behind
        the stop, and a worker that does not finish in time is held rather
        than dropped -- a live thread whose QObject has been collected is how
        "Destroyed while thread is still running" happens.
        """
        self._closing = True
        try:
            self._timer.stop()
        except RuntimeError:
            pass
        self._pending_rev = None
        self._in_flight = False
        self._inflight_request = None
        worker, self._worker = self._worker, None
        if worker is None:
            return
        try:
            worker.done.disconnect()
            worker.failed.disconnect()
        except Exception:                                   # noqa: BLE001
            pass
        stopped = False
        try:
            stopped = bool(worker.stop())
        except Exception:                                   # noqa: BLE001
            pass
        if not stopped:
            self._retired_workers = [w for w in self._retired_workers
                                     if w.isRunning()]
            self._retired_workers.append(worker)
            print(f"[Block01] tissue compose worker still busy at {reason}; "
                  "holding a reference until it finishes")

    # ── evidence ──────────────────────────────────────────────────────
    def frame_stats(self):
        stats = dict(self._stats)
        stats.update({
            "input_rev": self._input_rev,
            "pending": 1 if self._pending_rev is not None else 0,
            "max_pending_depth": self._max_pending_depth,
            "in_flight": self._in_flight,
            "generation": self._generation,
            "owner": self._active,
        })
        return stats

    def last_published(self):
        return dict(self._last_published or {})

    def reset_stats(self):
        for key in self._stats:
            self._stats[key] = 0
        self._max_pending_depth = 0
        self._last_published = None


def _fingerprint(rgb):
    """A cheap, order-sensitive summary of a frame. NOT a hash of the array:
    this runs on the GUI thread on every publish."""
    if rgb is None:
        return None
    arr = np.asarray(rgb)
    if not arr.size:
        return None
    flat = arr.reshape(-1)
    step = max(1, flat.size // 64)
    return int(np.asarray(flat[::step], dtype=np.int64).sum())


class Block01DisplayServices(QObject):
    """The Block01-level handle every step is given, and the only one it uses.

    Constructed by the window that owns Block01's lifetime, BEFORE any step
    page exists, and shut down after them.

    IT OWNS THE TWO SHARED WIDGETS. The `TissueNavigatorPopup` and the
    Intensity window are constructed here, held here, shown and closed here.
    An earlier cut left them constructed in `Step0Page` on the argument that
    only the DECISIONS had to move; that argument did not survive review, and
    it was wrong for a concrete reason: `show_navigator` still ended in
    `Step0Page.show_tissue_navigator`, so every step's access to a global
    window went through a step object that has to be alive, current, and
    bound to the dataset. A page is now asked only for CONTENT -- the ROI
    toolbar to put in the popup, the remap inspector to put in the Intensity
    window, the whole-slide arrays -- through ports that name that
    responsibility and nothing else.

    A step asks this object for the shared windows, the canonical
    colour/mapping answers and the whole-slide arrays. It never reaches into
    another step, and this object never calls a step's private method.
    """

    navigator_created = pyqtSignal(object)      # the popup, once

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = ChannelDisplayState(self)
        self.coordinator = TissuePreviewCoordinator(self.state, self)
        self._navigator_content = None
        self._intensity_content = None
        self._lowres_source = None
        self._navigator = None
        self._intensity_window = None
        self._intensity_panel = None
        self._navigator_policy = {"roi_policy": "full", "patch_editable": True}
        self._intensity_locked = False
        self._closing = False

    # ── ports ─────────────────────────────────────────────────────────
    def set_navigator_content(self, port):
        """Register who FURNISHES the Tissue Preview popup.

        `port` answers `navigator_loader()`, `navigator_nucleus_channel()`,
        `furnish_navigator(popup)` (the ROI/patch toolbar, the lists and the
        signal wiring over the one ROI model) and
        `apply_navigator_policy(roi_policy, patch_editable)`. It is asked for
        content; it is never asked to show, hide, hold or close the window.
        """
        self._navigator_content = port

    def set_intensity_content(self, port):
        """Register who FURNISHES the Intensity window.

        `port` answers `intensity_panel_widget()` (the Channel Remap
        inspector, detached) and `focus_intensity_channel(channel, color)`.
        Same rule: content, not lifetime.
        """
        self._intensity_content = port

    def set_lowres_source(self, source):
        """Register the whole-slide low-resolution data service.

        `source` answers `tissue_lowres_array(channel)` -- RESIDENT ONLY,
        never a read on the GUI thread -- and `ensure_tissue_lowres(channels)`,
        which asks for the missing ones in the background. One service for
        every step, so a Step1 overlay and a Step0 thumbnail of the same
        channel are the same array rather than two reads of it.
        """
        self._lowres_source = source

    def mapping_owner(self):
        """Who answers "what Min/Max/Gamma is this channel drawn with".

        The shared state, and a test asserts it is not a step page: the
        previous cut answered this question with a lambda closed over
        `Step0Page._display_mapping_for`.
        """
        return self.state

    def window_owner(self):
        """Who constructs, holds and closes the two shared windows."""
        return self

    # ── the Tissue Preview popup ──────────────────────────────────────
    def navigator(self):
        """The popup if it has been created, else None. Never creates."""
        return self._navigator

    def ensure_navigator(self):
        """The ONE Tissue Preview popup, created on first use.

        Created HERE, parented to Block01's window, and furnished by the
        registered content port. Lazy because a session that never opens it
        should not carry the widget -- not because a step owns the decision.
        """
        if self._navigator is not None or self._closing:
            return self._navigator
        port = self._navigator_content
        if port is None:
            return None
        loader = getattr(port, "navigator_loader", lambda: None)()
        nuc = getattr(port, "navigator_nucleus_channel", lambda: "")()
        self._navigator = TissueNavigatorPopup(
            loader=loader, nuc_ch=nuc, parent=self._widget_parent())
        try:
            port.furnish_navigator(self._navigator)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] the Tissue Preview could not be furnished: "
                  f"{exc}")
        self.apply_navigator_policy()
        self.navigator_created.emit(self._navigator)
        # A popup created while some step is already active owes that step a
        # frame, and only that step is allowed to give it one.
        self.coordinator.request_frame(kind="navigator_created")
        return self._navigator

    def _widget_parent(self):
        parent = self.parent()
        return parent if isinstance(parent, QWidget) else None

    def set_navigator_policy(self, *, roi_policy=None, patch_editable=None):
        """Which ROI/patch edits the shared navigator accepts.

        Held HERE, so it survives the popup being created, hidden or
        reopened, and so a step change updates an already-open one.
        """
        if roi_policy is not None:
            if roi_policy not in ("full", "delete_only", "read_only"):
                raise ValueError(f"unknown roi_policy: {roi_policy!r}")
            self._navigator_policy["roi_policy"] = roi_policy
        if patch_editable is not None:
            self._navigator_policy["patch_editable"] = bool(patch_editable)
        self.apply_navigator_policy()

    def navigator_policy(self):
        return dict(self._navigator_policy)

    def apply_navigator_policy(self):
        port = self._navigator_content
        applier = getattr(port, "apply_navigator_policy", None)
        if applier is None:
            return
        try:
            applier(**self._navigator_policy)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] navigator policy could not be applied: {exc}")

    def show_navigator(self, step_id=None, **policy):
        """Put the ONE Tissue Preview in front, under the active step's policy.

        Every step's button resolves here.
        """
        if policy:
            self.set_navigator_policy(**policy)
        popup = self.ensure_navigator()
        if popup is None:
            return None
        _bring_to_front(popup)
        self.coordinator.request_frame(kind="navigator_shown")
        return popup

    def hide_navigator(self):
        if self._navigator is not None:
            self._navigator.hide()

    # ── the Intensity window ──────────────────────────────────────────
    def intensity_window(self):
        return self._intensity_window

    def intensity_panel(self):
        return self._intensity_panel

    def ensure_intensity(self):
        """The ONE Intensity window, created on first use.

        The WINDOW is this layer's; the panel inside it is the Channel Remap
        inspector, handed over by the content port and still driven by it.
        """
        if self._intensity_window is not None or self._closing:
            return self._intensity_window
        port = self._intensity_content
        if port is None:
            return None
        try:
            panel = port.intensity_panel_widget()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] the Intensity panel is unavailable: {exc}")
            return None
        if panel is None:
            return None
        win = QWidget(
            self._widget_parent(),
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint,
        )
        win.setWindowTitle("Intensity")
        win.setStyleSheet("background:#1c1c1c;")
        win.setMinimumWidth(260)
        win.resize(320, 460)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(panel)
        self._intensity_window = win
        self._intensity_panel = panel
        self.apply_intensity_policy()
        return win

    def focus_intensity(self, channel):
        """Point the ONE Intensity window at `channel`.

        The colour handed over is the canonical one -- never a step's private
        palette -- so the histogram cannot come up in a colour no view uses.
        """
        port = self._intensity_content
        focus = getattr(port, "focus_intensity_channel", None)
        if not channel or focus is None:
            return False
        try:
            return bool(focus(channel, self.state.color(channel)))
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] could not point Intensity at {channel}: {exc}")
            return False

    def show_intensity(self, channel="", color=None):
        """Put the ONE Intensity window in front, on `channel`."""
        win = self.ensure_intensity()
        if win is None:
            return None
        if channel:
            self.focus_intensity(channel)
        self.apply_intensity_policy()
        _bring_to_front(win)
        return win

    # ── Intensity rights ──────────────────────────────────────────────
    #
    # NOT BY STEP. Min/Max/Gamma are Block01's, and the product rule is that
    # the user may adjust them wherever they are -- Step0, Step1, Step2 and
    # Step3 alike -- with the Tissue Preview following live. A previous cut
    # disabled the window in Step2/Step3 on the reasoning that those steps
    # "consume a committed mapping"; that was this implementation's invention,
    # it contradicted the requirement, and it is gone.
    #
    # What DOES lock the controls is a short computation holding a frozen copy
    # of the configuration -- a fusion run writing to disk -- because an edit
    # landing mid-run would leave the screen and the file it is writing out of
    # step for the length of the run. A lock is a job, not a page.
    def lock_intensity(self, reason=""):
        self._intensity_locked = True
        if reason:
            print(f"[Block01] Intensity locked: {reason}")
        self.apply_intensity_policy()

    def unlock_intensity(self):
        self._intensity_locked = False
        self.apply_intensity_policy()

    def intensity_policy(self, step_id=None):
        """`{"editable": bool, "reason": str}`. Explicit, so "why is this
        greyed out" has an answer that is not "whatever last called the
        setter" -- and so that "because you are in Step2" can never be it."""
        if self._intensity_locked:
            return {"editable": False, "reason": "computation_lock"}
        return {"editable": True, "reason": ""}

    def apply_intensity_policy(self, step_id=None):
        """Put the current rights on the window and on every entry to it.

        The panel is the window's own; the BUTTONS that open it belong to the
        pages, so the content port is told as well -- a lock that greys the
        controls but leaves the door open is a lock the user can walk past.
        """
        policy = self.intensity_policy(step_id)
        panel = self._intensity_panel
        if panel is not None:
            try:
                panel.setEnabled(bool(policy["editable"]))
            except RuntimeError:
                pass
        port = self._intensity_content
        applier = getattr(port, "apply_intensity_policy", None)
        if applier is not None:
            try:
                applier(bool(policy["editable"]))
            except Exception:                               # noqa: BLE001
                pass
        return policy

    # ── the render spec every step draws from ─────────────────────────
    def set_render_weight(self, channel, weight):
        """THE global entry for "this channel contributes this much".

        Whichever step is active receives it: Step1 through its channel row,
        a downstream step through the render spec it inherited. This is what
        makes a weight change a real change downstream instead of something
        only Step1 can express -- and it is the entry a downstream step's UI
        would call if and when it grows one.
        """
        context = self.coordinator.context()
        setter = getattr(context, "set_render_weight", None)
        if setter is None:
            return False
        if not setter(channel, float(weight)):
            return False
        self.coordinator.request_frame(kind="weight", channel=channel)
        return True

    def render_weight(self, channel):
        context = self.coordinator.context()
        getter = getattr(context, "render_weight", None)
        return None if getter is None else getter(channel)

    # ── whole-slide data ──────────────────────────────────────────────
    def lowres_array(self, channel):
        """`channel`'s whole-slide low-resolution array if it is RESIDENT.

        Never reads. A render callback that fell through to
        `read_region_lowres` would cost 170-230 ms of the GUI thread per
        channel, measured, which is the stall this whole round removes.
        """
        source = self._lowres_source
        if source is None or not channel:
            return None
        try:
            return source.tissue_lowres_array(channel)
        except Exception:                                   # noqa: BLE001
            return None

    def ensure_lowres(self, channels):
        """Ask for the arrays a frame is missing, in the background.

        Returns the channels that are still missing. The reads are the shared
        overview store's, so a channel three viewers want is read once, and
        the arrival wakes the coordinator like any other input.
        """
        source = self._lowres_source
        wanted = [ch for ch in (channels or []) if ch]
        if source is None or not wanted:
            return list(wanted)
        try:
            return list(source.ensure_tissue_lowres(wanted) or [])
        except Exception:                                   # noqa: BLE001
            return list(wanted)

    # ── lifetime ──────────────────────────────────────────────────────
    def shutdown(self, reason="close"):
        """Block01 is closing. Refuse new work, retire the thread, then close
        the windows. In that order, and from here only.

        A step page being destroyed during an ordinary navigation must never
        reach this -- which is now structural rather than a convention, since
        the windows are not a page's to destroy.
        """
        if self._closing:
            return
        self._closing = True
        self.coordinator.shutdown(reason)
        for win in (self._intensity_window, self._navigator):
            if win is None:
                continue
            try:
                win.close()
            except RuntimeError:
                pass
        self._intensity_window = None
        self._intensity_panel = None
        self._navigator = None


def _bring_to_front(win):
    """Put `win` in front of the user, whatever state it was left in.

    MINIMISED is the state that needs saying out loud. A minimised window is
    still `isVisible()` -- Qt counts it as shown, just shown as an icon -- so
    `show()` on it is a no-op and `raise_()` raises something nobody can see.
    A popup collapsed to its own header bar is out of sight a second way that
    `show()` also does not answer, so it is restored first.

    Same object either way: the window keeps its contents, camera, ROIs and
    patches, because none of this creates or replaces a widget.
    """
    restore = getattr(win, "is_minimized", None)
    if restore is not None and restore():
        win.restore_from_bar()
    win.setWindowState(
        (win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    win.show()
    win.raise_()
    win.activateWindow()
