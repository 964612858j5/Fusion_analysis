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
from ..workers.display_seed_worker import (
    DisplaySeedWorker, LowresReadWorker,
)
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


_UNSET = object()


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
        # THE display windows, by (channel, is-nucleus-role), WITHIN one
        # dataset. Stored here, not fetched from a step: a shared state that
        # answers by calling `Step0Page._display_mapping_for` is Step0's store
        # with another name, and every step then needs Step0 alive and current
        # to know what Min/Max/Gamma it is drawing with.
        #
        # One namespace per dataset token, and `_mappings` is the CURRENT
        # one. A window is a statement about pixels, so it cannot be shared
        # between two slides that merely have a channel with the same name --
        # which is what a flat `(channel, nucleus)` key did, and it meant
        # slide B opened on slide A's contrast and was never seeded.
        self._mapping_spaces = {}
        self._mappings = self._mapping_spaces.setdefault(None, {})
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
        """Say which slide these answers are about. ONE transaction.

        COLOURS ARE NOT CLEARED. This product treats a channel's colour as a
        display preference held by channel NAME (see `Step0Page`'s own note
        where `_channel_colors` survives a reload), and the steps have to
        agree either way.

        DISPLAY WINDOWS ARE. A Min/Max/Gamma is a statement about PIXELS --
        slide B's CD3 is not slide A's CD3, and the two can be orders of
        magnitude apart. Keyed by channel name alone, B's first read of CD3
        would hit A's window and B would never be seeded at all: A's tissue
        contrast, on B's picture, with no way for the user to tell. So the
        store is per dataset, and switching is a NAMESPACE SWITCH.

        The order matters and is the transaction: the token moves and the
        namespace moves with it, BEFORE `dataset_changed` goes out. An
        observer woken by that signal reads B and finds B's windows (or none
        yet), never A's.
        """
        if token == self._token:
            return
        previous, self._token = self._token, token
        # Namespace first, signal after. A seed or a restore for the previous
        # slide that lands later is written against ITS token and can no
        # longer be read as this one's -- see `set_mapping`.
        space = self._mapping_spaces.setdefault(token, {})
        if previous is None and not space and self._mapping_spaces.get(None):
            # ADOPTION, not a switch. Nothing had told this state which slide
            # it was looking at, so whatever was written meanwhile is about
            # THIS one -- the same rule an overview panel applies to its first
            # picture. Moved rather than copied, so the unnamed namespace
            # cannot be read again later as some other slide's.
            space.update(self._mapping_spaces.pop(None))
        self._mappings = space
        self._mapping_rev += 1
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
    def _space_for(self, token=_UNSET):
        """The mapping namespace for `token`, or the current one."""
        if token is _UNSET or token == self._token:
            return self._mappings
        return self._mapping_spaces.setdefault(token, {})

    def mapping(self, channel, nucleus=False):
        """`(min, max, gamma)` for `channel` ON THIS SLIDE, or None.

        Reads the CURRENT namespace. No port is consulted, so this answers
        the same in every step and keeps answering after a step page is gone
        -- and a channel this slide has never had a window for answers None
        rather than the previous slide's numbers.
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
                    origin="", persist=True, token=_UNSET):
        """Set `channel`'s display window. THE write, from any step.

        True when it changed anything. The persistence port is told, so the
        remap params on disk follow; a write that came FROM that port passes
        `persist=False`, which is what stops the two from echoing.

        `token` is the dataset the numbers are ABOUT. A background seed that
        started on slide A and finishes after the user has loaded B passes
        the token it was computed for; it is written into A's namespace and
        cannot be read as B's. Refusing it outright would be worse, not
        better: going back to A must not have to seed again.
        """
        if not channel:
            return False
        space = self._space_for(token)
        key = (channel, bool(nucleus))
        current = space.get(key)
        if gamma is None:
            gamma = current[2] if current else 1.0
        value = (float(lo), float(hi), float(gamma))
        if current == value:
            return False
        space[key] = value
        if space is not self._mappings:
            # A late answer about a slide nobody is looking at. Recorded, so
            # coming back to it is instant; announced to nobody, because
            # nothing on screen is showing it.
            perf_trace.mark("tissue.seed_late", channel=channel,
                            origin=origin)
            return True
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

    def forget_mappings(self, token=_UNSET):
        """Drop a slide's windows, so it is seeded again from its own pixels.

        For a RELOAD of the same slide, where the pixels may have changed
        under the same identity. An ordinary A -> B switch does not need it:
        B has its own namespace and A's is simply not read.
        """
        space = self._space_for(token)
        space.clear()
        if space is self._mappings:
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
        self._worker_retired = False
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
        """A new slide. Everything in flight is about the old one.

        And the new one is asked for at once: its windows have to be seeded
        from its own pixels, and nothing else would start that -- the request
        finds no computed window, names the channel as loading and sends the
        seed to its thread.
        """
        self.state.bind_dataset(token)
        self._begin_generation("dataset")
        self.request_frame(kind="dataset")

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
        elif self._last_published is None:
            # Nothing has ever been drawn, so there is no "last frame" to
            # judge relevance against: a first window arriving IS the event
            # that makes a first frame possible.
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
        if channel in (published.get("loading") or ()):
            # A channel the last frame was WAITING for. Its window or colour
            # arriving is exactly the event that completes the picture, and
            # judging it by the drawn set would drop the one change that
            # matters -- the channel would never be drawn and so would never
            # qualify.
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

    def _snapshot(self, context, rev, computed_only=True):
        """Everything one frame needs, as values the GUI thread is done with.

        The arrays go in by reference and are never written to: a new read
        replaces the array object rather than filling the old one, which is
        what makes handing them to a thread safe and what `array_ids` lets the
        result be checked against.
        """
        ask = getattr(context, "tissue_render_snapshot", None)
        if ask is None:
            return None
        # `computed_only` is the difference between the FRAME path and the
        # explicit synchronous draw: on the clock a context may not do pixel
        # work and names what it is waiting for; `render_now` means "give me
        # the picture before you return", so the context is allowed to work
        # a first display window out on the spot.
        try:
            payload = ask(computed_only=computed_only)
        except TypeError:
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
            snapshot = self._snapshot(context, rev, computed_only=False)
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
            "loading": tuple(result.get("loading") or ()),
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

    def pause(self, reason=""):
        """Stop accepting requests. REVERSIBLE, and nothing is destroyed.

        For the first phase of a close that may still be refused: no new
        frame may be scheduled, the armed slot is dropped, but the worker,
        the windows and the state are all still there.
        """
        self._closing = True
        try:
            self._timer.stop()
        except RuntimeError:
            pass
        self._pending_rev = None
        perf_trace.mark("tissue.pause", why=reason, owner=self._active)

    def resume(self):
        """Take requests again, and draw the state as it is now."""
        if self._worker_retired:
            return
        self._closing = False
        self.request_frame(kind="resume")

    def shutdown(self, reason="close"):
        """Stop taking requests, then retire the thread. In that order.

        Block01's teardown, not a step's: a page being replaced must never
        reach this. New requests are refused FIRST so nothing re-arms behind
        the stop, and a worker that does not finish in time is held rather
        than dropped -- a live thread whose QObject has been collected is how
        "Destroyed while thread is still running" happens.
        """
        self._closing = True
        self._worker_retired = True
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
        self._render_spec = None
        self._weight_owner = None
        self._weight_editor_content = None
        self._seed_worker = None
        self._read_worker = None
        self._weight_editor = None
        self._weight_editor_panel = None
        self._intensity_locked = False
        self._closing = False
        self._finalized = False

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

    # ── the first display window, off the GUI thread ──────────────────
    #
    # A channel nobody has set Min/Max/Gamma for needs an automatic one, and
    # working it out is a percentile over a whole-slide array. That used to
    # happen inside `mapping_or_seed`, synchronously, from the callback that
    # builds a frame snapshot -- which is the one place that must not do
    # pixel work. So the frame path reads COMPUTED windows only; a channel
    # without one is named as loading and its seed is asked for here.
    def request_mapping_seed(self, channel, nucleus=False):
        """Ask for `channel`'s automatic window in the background.

        Returns True when a pass was started or is already running. The
        answer is pinned to the dataset it was computed under, so a seed that
        outlives a slide switch is filed against the slide it is about.
        """
        if self._closing or not channel:
            return False
        if self.state.mapping(channel, nucleus=nucleus) is not None:
            return False
        array = self.lowres_array(channel)
        if array is None:
            # Nothing to measure yet. The read is asked for; its arrival is
            # an input, and the frame after it asks for the seed again.
            self.ensure_lowres([channel])
            return False
        worker = self._seed_worker_ready()
        if worker is None:
            return False
        return bool(worker.submit(self.state.dataset_token(), channel, array,
                                  nucleus=nucleus))

    def mapping_seed_pending(self, channel=None, nucleus=False):
        worker = self._seed_worker
        if worker is None:
            return False if channel is None else False
        return worker.pending(self.state.dataset_token(), channel, nucleus)

    def _seed_worker_ready(self):
        if self._closing:
            return None
        if self._seed_worker is not None:
            return self._seed_worker
        try:
            worker = DisplaySeedWorker()
            worker.done.connect(self._on_mapping_seeded)
            worker.start()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] display seed worker unavailable ({exc})")
            return None
        self._seed_worker = worker
        return worker

    def _on_mapping_seeded(self, result):
        """A window came back. Filed under the slide it is ABOUT.

        `token` is what the pass was computed under, not what is on screen
        now: a seed that started on A and finishes after B loaded belongs to
        A, and writing it as B's would be the cross-slide contamination the
        namespaces exist to stop. `set_mapping` files it and stays silent
        when it is not the current slide.
        """
        result = dict(result or {})
        channel = result.get("channel")
        if not channel:
            return
        token = result.get("token")
        self.state.set_mapping(channel, result["min"], result["max"],
                               result.get("gamma", 1.0),
                               nucleus=bool(result.get("nucleus")),
                               origin="seed", token=token)
        if token != self.state.dataset_token():
            return          # filed against the slide it is about, and no more
        # A SEED ALWAYS ASKS FOR A FRAME, unconditionally. The relevance test
        # the ordinary mapping fan-out uses -- "is this channel in the last
        # picture?" -- answers no here by construction: the channel was left
        # out of that picture BECAUSE it had no window. Judging a seed by it
        # would mean the channel never drew and so never qualified to draw.
        self.coordinator.request_frame(kind="seed", channel=channel)

    def set_weight_editor_content(self, port):
        """Register who FURNISHES the shared weight editor.

        `port` answers `weight_editor_widget()`. Same rule as the other two:
        content, not lifetime. The window is Block01's so it is reachable
        from Step2 and Step3, where the step page has no channel panel of its
        own -- and there is one editor, not one per step.
        """
        self._weight_editor_content = port

    def weight_editor(self):
        return self._weight_editor

    def ensure_weight_editor(self):
        if self._weight_editor is not None or self._closing:
            return self._weight_editor
        port = self._weight_editor_content
        if port is None:
            return None
        try:
            panel = port.weight_editor_widget()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] the weight editor is unavailable: {exc}")
            return None
        if panel is None:
            return None
        win = QWidget(
            self._widget_parent(),
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint,
        )
        win.setWindowTitle("Channel Weights")
        win.setStyleSheet("background:#1c1c1c;")
        win.setMinimumWidth(260)
        win.resize(300, 420)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(panel)
        self._weight_editor = win
        self._weight_editor_panel = panel
        return win

    def show_weight_editor(self):
        win = self.ensure_weight_editor()
        if win is None:
            return None
        refresh = getattr(self._weight_editor_panel, "refresh_from_state", None)
        if refresh is not None:
            refresh()
        _bring_to_front(win)
        return win

    def weight_editor_panel(self):
        return self._weight_editor_panel

    def set_lowres_source(self, source):
        """Register the whole-slide low-resolution data service.

        `source` answers `tissue_lowres_array(channel)` -- RESIDENT ONLY,
        never a read on the GUI thread -- and `ensure_tissue_lowres(channels)`,
        which asks for the missing ones in the background. One service for
        every step, so a Step1 overlay and a Step0 thumbnail of the same
        channel are the same array rather than two reads of it.
        """
        self._lowres_source = source

    def release_ports(self, owner):
        """Let go of every port `owner` registered. The windows stay.

        A page being torn down deregisters HERE rather than being held by a
        strong reference in the services until the process ends. What it took
        with it is content and data, so the effect is that the shared windows
        keep their widgets and the shared state keeps its values, while
        anything that needed the page fails CLOSED -- a frame with no arrays
        is a Loading frame, not a call into a deleted object.
        """
        for name in ("_navigator_content", "_intensity_content",
                     "_lowres_source", "_weight_editor_content",
                     "_weight_owner"):
            if getattr(self, name, None) is owner:
                setattr(self, name, None)
        if self.state._seed_port is owner:
            self.state._seed_port = None
        if self.state._persist_port is owner:
            self.state._persist_port = None
        if self.state._default_source is not None and (
                getattr(self.state._default_source, "__self__", None) is owner):
            self.state._default_source = None

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
    #
    # ONE spec, held here, not a copy per step. The previous cut gave each
    # downstream context its own writable `_spec` and re-inherited it from
    # Step1 on every transition, so a weight moved in Step2 was overwritten
    # the moment the user reached Step3 and was gone again in Step1 -- a
    # local pixel effect, not a global weight. Now Step1 PUBLISHES the spec
    # here whenever it composes, and the downstream contexts READ it; there
    # is no second copy to diverge.
    def publish_render_spec(self, spec):
        """Step1 says what the picture currently is, semantically."""
        self._render_spec = dict(spec or {}) or None

    def render_spec(self):
        return self._render_spec

    def set_render_weight(self, channel, weight):
        """THE global entry for "this channel contributes this much".

        It always goes to the WEIGHT OWNER -- the Step1 channel panel, which
        is what a Save writes and what a session restores -- whichever step
        the user is in. That is what makes a weight moved in Step2 the same
        fact in Step3 and back in Step1, rather than a value that lives only
        as long as the context that received it.
        """
        owner = self._weight_owner
        if owner is None or not channel:
            return False
        if not owner.set_render_weight(channel, float(weight)):
            return False
        self.coordinator.request_frame(kind="weight", channel=channel)
        return True

    def render_weight(self, channel):
        owner = self._weight_owner
        return None if owner is None else owner.render_weight(channel)

    def set_weight_owner(self, owner):
        """Register who holds the weights: `set_render_weight(ch, w)` and
        `render_weight(ch)`. One owner for the process."""
        self._weight_owner = owner

    def weight_owner(self):
        return self._weight_owner

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
            missing = list(source.ensure_tissue_lowres(wanted) or [])
        except Exception:                                   # noqa: BLE001
            missing = list(wanted)
        if missing:
            # Nobody is reading them -- no viewer is open on this slide, so
            # the shared overview store has no reason to. Read them HERE, on
            # this layer's own thread. The previous fallback took the read on
            # the GUI thread "only once per channel", and once is 170-230 ms,
            # measured, landing exactly when a new channel first appears.
            self._request_lowres_reads(missing)
        return missing

    def _request_lowres_reads(self, channels):
        reader = getattr(self._lowres_source, "read_tissue_lowres_blocking",
                         None)
        if reader is None or self._closing:
            return
        worker = self._read_worker
        if worker is None:
            try:
                worker = LowresReadWorker(reader)
                worker.done.connect(self._on_lowres_read)
                worker.start()
            except Exception as exc:                        # noqa: BLE001
                print(f"[Block01] low-res read worker unavailable ({exc})")
                return
            self._read_worker = worker
        token = self._lowres_token()
        for channel in channels:
            worker.submit(token, channel)

    def _lowres_token(self):
        """The identity the DATA SERVICE files its arrays under.

        Its own, not the shared state's: the array cache belongs to the
        service and is keyed by that identity, so a read submitted under one
        and installed under the other is refused on arrival and asked for
        again forever.
        """
        getter = getattr(self._lowres_source, "tissue_dataset_token", None)
        if getter is None:
            return self.state.dataset_token()
        try:
            return getter()
        except Exception:                                   # noqa: BLE001
            return self.state.dataset_token()

    def _on_lowres_read(self, result):
        """An array came back. Installed only if it is about THIS slide."""
        result = dict(result or {})
        channel = result.get("channel")
        array = result.get("array")
        if not channel or array is None:
            return
        if result.get("token") != self._lowres_token():
            perf_trace.mark("tissue.drop", why="dataset", kind="lowres_read",
                            channel=channel)
            return
        install = getattr(self._lowres_source, "install_tissue_lowres", None)
        if install is None:
            return
        try:
            install(channel, result["token"], array)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Block01] could not install {channel}'s array: {exc}")
            return
        self.coordinator.request_frame(kind="lowres", channel=channel)

    def lowres_read_pending(self, channel=None):
        worker = self._read_worker
        if worker is None:
            return False
        return worker.pending(self._lowres_token(), channel)

    # ── lifetime ──────────────────────────────────────────────────────
    # ── closing, in two phases ────────────────────────────────────────
    #
    # A close attempt can be REFUSED. The window checks whether a patch
    # loader, a fusion worker or an uninterruptible overview read is still
    # running and calls `event.ignore()` if one is -- and then goes on living,
    # waiting for the user to try again. Tearing the display services down
    # before that check destroyed the two shared windows and left the
    # coordinator permanently closed on a window the user was still using: it
    # could not draw, and nothing would ever open again.
    #
    # So closing is `begin_close()` -- stop ACCEPTING work, which is
    # reversible -- and `finalize_close()`, which is not and runs only once
    # the close is certain.
    def begin_close(self, reason="close"):
        """Stop taking new frame requests. Nothing is destroyed.

        Idempotent, because a refused close is retried. `resume()` undoes it
        if the close is refused, so a window that goes on living goes on
        drawing.
        """
        self._closing = True
        self.coordinator.pause(reason)

    def resume(self):
        """The close was refused; the session continues."""
        if self._finalized:
            return
        self._closing = False
        self.coordinator.resume()

    def finalize_close(self, reason="close"):
        """The close is certain. Retire the thread, then close the windows.

        From here only, and after `begin_close`. A step page being destroyed
        during an ordinary navigation must never reach this -- which is
        structural rather than a convention, since the windows are not a
        page's to destroy.
        """
        if self._finalized:
            return
        self._finalized = True
        self._closing = True
        self.coordinator.shutdown(reason)
        for attr in ("_seed_worker", "_read_worker"):
            worker = getattr(self, attr, None)
            setattr(self, attr, None)
            if worker is None:
                continue
            try:
                worker.done.disconnect()
                worker.failed.disconnect()
            except Exception:                               # noqa: BLE001
                pass
            try:
                worker.stop()
            except Exception:                               # noqa: BLE001
                pass
        for win in (self._intensity_window, self._navigator,
                    self._weight_editor):
            if win is None:
                continue
            try:
                win.close()
            except RuntimeError:
                pass
        self._intensity_window = None
        self._intensity_panel = None
        self._navigator = None
        self._weight_editor = None
        self._weight_editor_panel = None

    def shutdown(self, reason="close"):
        """Both phases, for a caller that knows the close cannot be refused."""
        self.begin_close(reason)
        self.finalize_close(reason)

    def is_finalized(self):
        return self._finalized


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
