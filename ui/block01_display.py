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

import collections
import copy
import math
import time

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from ..core import display_identity as _identity
from ..core import tissue_compose
from ..core.fusion_domain import FusionDomainModel
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
    dataset_changed = pyqtSignal(object)        # DatasetIdentity
    selection_changed = pyqtSignal(str)         # channel, "" = nothing
    visibility_changed = pyqtSignal(str, bool)  # channel, visible
    # ONE completion notice per transaction. An observer that only wants to
    # know "the state is now whole" listens here rather than counting field
    # signals -- which is what made a restore look like a run of user actions.
    state_installed = pyqtSignal(object)        # DisplayBinding

    def __init__(self, parent=None):
        super().__init__(parent)
        # WHICH SLIDE and WHICH BINDING, separately. See
        # `core.display_identity` for why one value could not do both jobs.
        self._binding = None
        self._generation = 0
        # Display namespaces, most-recently-bound last, at most
        # `NAMESPACE_LIMIT` of them. Keyed by the STABLE identity, so walking
        # A -> B -> A finds A's own selection, visibility and windows again
        # instead of an empty space and a re-seed.
        self._namespaces = collections.OrderedDict()
        self._ns = _identity.DisplayNamespace()   # the current one, always set
        self._colors = {}
        self._default_source = None
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
        # True while a whole-state install is being announced. See
        # `install_fanout_active`.
        self._announcing_install = False
        # A session restore staged but not yet announced. See
        # `prepare_restore`: the writes are done, the notices are held until
        # the scientific half is final too.
        self._pending_restore = None

    # ── identity ──────────────────────────────────────────────────────
    # How many datasets' display state this process keeps. Bounded on
    # purpose: the whole point of keeping A's namespace is a quick walk back
    # to it, and a session that opens fifty slides should not carry fifty
    # namespaces to make the fiftieth-but-one instant.
    NAMESPACE_LIMIT = 8

    # ── identity and binding ──────────────────────────────────────────
    def binding(self):
        """The current `DisplayBinding`, or None before the first bind."""
        return self._binding

    def identity(self):
        """WHICH SLIDE, or None before the first bind."""
        return None if self._binding is None else self._binding.identity

    def generation(self):
        """WHICH BINDING. Never repeats, A -> B -> A included."""
        return 0 if self._binding is None else self._binding.generation

    def dataset_token(self):
        """The identity, for the callers that only need "which slide".

        Kept as a name because a lot of code and several tests ask for it.
        What it returns is now the STABLE identity, so two visits to the same
        file compare equal -- which is the whole B2 change.
        """
        return self.identity()

    def namespace_identities(self):
        """The identities whose display state is resident, oldest first."""
        return list(self._namespaces)

    def bind(self, identity, *, install=None):
        """Bind to `identity` and return the new `DisplayBinding`.

        ONE TRANSACTION. The binding moves, the namespace moves with it, the
        optional `install` payload is written, and only then does anything go
        out -- so an observer woken by `dataset_changed` reads the new slide
        and finds the new slide's answers, never a mixture.

        A NEW GENERATION EVERY TIME, including a return to a slide already
        resident. That is what lets A's namespace come back while a task
        started under A's first binding is still refused.

        `identity` must be a resolved `DatasetIdentity`. An unresolved one is
        refused rather than guessed at: see `core.display_identity`.
        """
        if identity is None or not getattr(identity, "path", ""):
            raise ValueError(
                "a display binding needs a DatasetIdentity with a path; "
                f"got {identity!r}. Fail closed rather than sign unknown "
                "pixels with the current slide.")
        if identity.ephemeral:
            # Usable now, restorable never. `ephemeral` says so in the name
            # and in the log; it is not a verified slide identity.
            perf_trace.mark("display.identity_ephemeral", path=identity.path)
        elif not identity.resolved:
            raise ValueError(
                f"a display binding needs a source version; {identity!r} has "
                "no fingerprint. Use ephemeral_identity() for a source whose "
                "version cannot be established, or supply an explicit "
                "fingerprint for a synthetic source that must be restorable.")
        self._drop_pending_restore("another slide was bound")
        installed_colors, installed_mappings = self._bind_into(
            identity, install)
        self.dataset_changed.emit(identity)
        self._announce_install(installed_colors, installed_mappings)
        return self._binding

    def _bind_into(self, identity, install=None):
        """Do a bind's WRITES. No signals; the caller announces.

        Split out so a session restore can take the binding and the payload
        in silence and announce them with the scientific half, rather than
        waking every view halfway through the transaction.
        """
        self._generation += 1
        self._binding = _identity.DisplayBinding(identity=identity,
                                                 generation=self._generation)
        ns = self._namespaces.get(identity)
        if ns is None:
            ns = _identity.DisplayNamespace()
            self._namespaces[identity] = ns
        ns.binding_generation = self._binding.generation
        self._ns = ns
        # BIND-RECENCY: a formal bind is what refreshes the store's ordering.
        # A background read, a late seed or a refused result must not, or
        # stale activity would keep an old namespace alive past its turn.
        self._namespaces.move_to_end(identity)
        self._evict_over_limit()
        installed_colors, installed_mappings = [], []
        if install:
            _changed, installed_colors, installed_mappings = \
                self._install_into(ns, install)
        self._mapping_rev += 1
        return installed_colors, installed_mappings

    # ── the session restore transaction ───────────────────────────────
    #
    # A session restore is ONE fact about two owners -- this state and the
    # fusion model -- and it is coordinated by `Block01DisplayServices`. This
    # half stages its writes here, silently, and announces them only when the
    # scientific half is final too. Nothing else may use it: a bind, an
    # install or a close takes a staged restore back rather than leaving it
    # to be announced over whatever arrived in the meantime.

    def prepare_restore(self, identity, payload=None):
        """Stage a whole display state for `identity`. No signals.

        IDENTITY SAME, NO REBIND. A restore of the session already loaded is
        not a visit to another slide: rebinding would burn a generation and
        retire every frame, seed and read in flight for a state that did not
        move. The payload is written into the CURRENT namespace instead, and
        `_install_into` already ignores the fields that are equal.
        """
        if self._pending_restore is not None:
            self.cancel_restore("superseded")
        payload = dict(payload or {})
        moved = identity is not None and identity != self.identity()
        # THE ROLLBACK POINT IS TAKEN AND ARMED BEFORE THE FIRST WRITE. A
        # snapshot that is only armed afterwards is no rollback point at all:
        # a bind or an install that raises half way through left this state on
        # the new slide with nothing recorded to take it back, so the host's
        # `cancel_restore` found no pending restore, returned False, and the
        # transaction ended with the science on A and the display on B --
        # exactly the half-state it exists to prevent.
        self._pending_restore = {"before": self._restore_snapshot(),
                                 "moved": bool(moved), "colors": [],
                                 "mappings": [], "changed": False}
        try:
            if moved:
                colors, mappings = self._bind_into(identity, payload)
                changed = True
            elif self._binding is None:
                # Nothing bound and no identity to bind: there is no display
                # half to restore. Staged as a no-op so the caller's
                # cancel/commit pairing still holds.
                colors, mappings, changed = [], [], False
            else:
                changed, colors, mappings = self._install_into(self._ns,
                                                               payload)
                if changed:
                    self._mapping_rev += 1
        except Exception:
            # Whatever was written before it failed goes back, and nothing was
            # announced: a failed prepare leaves no trace either way.
            self.cancel_restore("prepare failed")
            raise
        self._pending_restore.update({"colors": colors, "mappings": mappings,
                                      "changed": bool(changed)})
        return True

    def restore_pending(self):
        """Is a prepared restore waiting to be committed or cancelled."""
        return self._pending_restore is not None

    def pending_restore_changes(self):
        """Would committing the prepared restore change anything at all."""
        pending = self._pending_restore
        return bool(pending and pending["changed"])

    def commit_restore(self):
        """Announce a prepared restore. One completion notice, then fields."""
        pending = self._pending_restore
        if not pending:
            return False
        self._pending_restore = None
        if not pending["changed"]:
            return False
        if pending["moved"]:
            self.dataset_changed.emit(self.identity())
        self._announce_install(pending["colors"], pending["mappings"])
        return True

    def cancel_restore(self, reason=""):
        """Take a prepared restore back: no signals, and no trace of it."""
        pending = self._pending_restore
        if not pending:
            return False
        self._pending_restore = None
        self._restore_from_snapshot(pending["before"])
        if reason:
            perf_trace.mark("display.restore_cancelled", reason=str(reason))
        return True

    def _drop_pending_restore(self, why):
        """Something else moved this state: a staged restore is void."""
        if self._pending_restore is None:
            return False
        self.cancel_restore(why)
        return True

    def _restore_snapshot(self):
        """Everything a staged restore may touch, deep-copied."""
        return {
            "binding": self._binding,
            "generation": self._generation,
            "namespaces": collections.OrderedDict(
                (key, copy.deepcopy(ns))
                for key, ns in self._namespaces.items()),
            "current": self.identity(),
            "ns": copy.deepcopy(self._ns),
            "colors": dict(self._colors),
            "color_rev": self._color_rev,
            "mapping_rev": self._mapping_rev,
        }

    def _restore_from_snapshot(self, shot):
        self._binding = shot["binding"]
        self._generation = shot["generation"]
        self._namespaces = shot["namespaces"]
        current = shot["current"]
        if current is not None and current in self._namespaces:
            self._ns = self._namespaces[current]
        else:
            self._ns = shot["ns"]
        self._colors = shot["colors"]
        self._color_rev = shot["color_rev"]
        self._mapping_rev = shot["mapping_rev"]

    def install(self, payload, *, identity=None):
        """Write a whole display state for the current slide, atomically.

        For a restore or a handoff. Same rule as `bind`: everything lands
        before anything is announced, so nothing observes "the new mapping
        with the old selection". Writing the fields individually is what used
        to make a restore look like a series of user actions.
        """
        if identity is not None and identity != self.identity():
            return False
        if self._binding is None:
            return False
        self._drop_pending_restore("an install landed before the commit")
        changed, colors, mappings = self._install_into(self._ns, payload)
        if not changed:
            return False
        self._mapping_rev += 1
        self._announce_install(colors, mappings)
        return True

    def _install_into(self, ns, payload):
        """Write `payload` into `ns`. No signals; the caller announces.

        Returns `(changed, colors, mappings)`: whether anything moved, and
        WHICH colours and display windows did. The caller needs the lists
        because a completion notice on its own reaches nobody -- see
        `_announce_install`.
        """
        changed = False
        changed_colors = []
        changed_mappings = []
        order = payload.get("order")
        if order is not None and tuple(order) != ns.order:
            ns.order = tuple(order)
            ns.bump("order")
            changed = True
        caps = payload.get("capabilities")
        if caps is not None and dict(caps) != ns.capabilities:
            ns.capabilities = dict(caps)
            ns.bump("capabilities")
            changed = True
        if "selection" in payload:
            sel = str(payload.get("selection") or "")
            if sel != ns.selection:
                ns.selection = sel
                ns.bump("selection")
                changed = True
        visibility = payload.get("visibility")
        if visibility is not None:
            new_vis = {str(ch): bool(v) for ch, v in visibility.items()}
            if new_vis != ns.visibility:
                ns.visibility = new_vis
                ns.bump("visibility")
                changed = True
        mappings = payload.get("mappings")
        if mappings is not None:
            for key, value in mappings.items():
                ch, nucleus = (key if isinstance(key, tuple) else (key, False))
                if value is None:
                    continue
                lo, hi, gamma = value
                new = (float(lo), float(hi), float(gamma))
                mkey = (str(ch), bool(nucleus))
                if ns.mappings.get(mkey) != new:
                    ns.mappings[mkey] = new
                    ns.bump(("mapping", mkey))
                    changed_mappings.append(mkey)
                    changed = True
        colors = payload.get("colors")
        if colors:
            # Process-level, so written to the colour store rather than the
            # namespace -- but inside this transaction, so the one completion
            # notice covers it too.
            for ch, value in colors.items():
                hexc = _to_hex(value)
                if hexc and self._colors.get(str(ch)) != hexc:
                    self._colors[str(ch)] = hexc
                    self._color_rev += 1
                    changed_colors.append(str(ch))
                    changed = True
        return changed, changed_colors, changed_mappings

    def _announce_install(self, colors, mappings):
        """Tell the views what an install changed, AFTER it is whole.

        A completion notice alone reached nobody. The display consumers --
        Step0's swatches and its Channel Remap layer list, the compare
        panels, the full image, the Tissue Preview's frame clock, Step1's
        viewer -- listen for `color_changed` and `mapping_changed`, so a
        session restore moved the state's answer while every view went on
        drawing the previous one.

        ORDER MATTERS AND IS THE POINT. The completion notice goes first,
        then the per-field signals, and every one of them is emitted with the
        transaction ALREADY WRITTEN -- so a handler that reads selection,
        visibility, colour or mapping sees the finished state, never the
        half of it that happens to have been announced.

        Selection and visibility are deliberately NOT announced here: those
        signals mean "somebody chose this", and a restore is not a choice.
        Their consumers follow the completion notice instead.
        """
        self._announcing_install = True
        try:
            self._announce_install_fields(colors, mappings)
        finally:
            self._announcing_install = False

    def install_fanout_active(self):
        """Is a whole-state install being announced right now.

        A consumer that reacts to EVERY field -- the frame clock does -- asks
        this so one transaction costs it one reaction instead of one per
        colour and one per window. The fields are still announced: a view
        that draws one channel needs to know which one moved.
        """
        return bool(self._announcing_install)

    def _announce_install_fields(self, colors, mappings):
        self.state_installed.emit(self._binding)
        for channel in colors:
            self.color_changed.emit(channel, self._colors[channel])
        # One `mapping_changed` per CHANNEL: the signal names a channel, and
        # a channel whose marker and nucleus windows both moved has not
        # changed twice as far as a view is concerned.
        for channel in dict.fromkeys(ch for ch, _nucleus in mappings):
            self.mapping_changed.emit(channel)

    def _evict_over_limit(self):
        """Drop the oldest namespaces past the limit. Never the current one."""
        while len(self._namespaces) > self.NAMESPACE_LIMIT:
            oldest, _ns = next(iter(self._namespaces.items()))
            if oldest == self.identity():
                # Cannot happen with `move_to_end` above, but a namespace
                # limit that could evict the slide on screen would be worse
                # than no limit at all.
                break
            self._namespaces.pop(oldest, None)
            perf_trace.mark("display.namespace_evicted", identity=str(oldest),
                            resident=len(self._namespaces))

    def _namespace_for(self, identity=_UNSET):
        """The namespace for `identity`, or None when it is not resident.

        NEVER CREATES. A late callback for an evicted slide must not bring its
        namespace back: the state would then hold a space nothing is bound to,
        built from one stale value, and the limit would be a suggestion. Only
        `bind` admits an identity.
        """
        if identity is _UNSET or identity is None or identity == self.identity():
            return self._ns
        return self._namespaces.get(identity)

    # ── the compatibility adapter ─────────────────────────────────────
    def bind_dataset(self, token):
        """Bind from a legacy `(generation, path)` token or a path.

        The callers that still hold the old token shape reach the new identity
        through here: the PATH is the stable half and the fingerprint is read
        from disk, while the token's own counter is discarded -- it was the
        thing preventing A -> B -> A from finding A again.
        """
        path = token
        if isinstance(token, tuple) and len(token) == 2:
            path = token[1]
        elif isinstance(token, _identity.DatasetIdentity):
            return self.bind(token)
        resolved = _identity.resolve_identity(path)
        if resolved is None:
            if not path:
                return None
            # FAIL CLOSED on restoration, not on usability: a source whose
            # version cannot be read gets a fresh one-bind identity, so it
            # displays now and promises nothing across binds. Two binds of
            # the same unreadable path are two datasets, because nothing can
            # show that they are one.
            perf_trace.mark("display.identity_unresolved", path=str(path))
            return self.bind(_identity.ephemeral_identity(path))
        # A new binding generation every time, the same slide included: the
        # caller is telling us a fresh bind happened, and any task from
        # before it is now stale.
        return self.bind(resolved)

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
        """`(min, max, gamma)` for `channel` ON THIS SLIDE, or None.

        Reads the CURRENT namespace. No port is consulted, so this answers
        the same in every step and keeps answering after a step page is gone
        -- and a channel this slide has never had a window for answers None
        rather than the previous slide's numbers.
        """
        if not channel:
            return None
        return self._ns.mappings.get((channel, bool(nucleus)))

    def mapping_in(self, identity, channel, nucleus=False):
        """`channel`'s window in a NAMED namespace, or None.

        For a late task checking its own precondition against the slide it
        was computed for rather than against whatever is on screen now.
        """
        ns = self._namespace_for(identity)
        if ns is None or not channel:
            return None
        return ns.mappings.get((channel, bool(nucleus)))

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
        self._ns.mappings[(channel, bool(nucleus))] = value
        self._ns.bump(("mapping", (channel, bool(nucleus))))
        self._mapping_rev += 1
        return value

    def set_mapping(self, channel, lo, hi, gamma=None, nucleus=False,
                    origin="", persist=True, token=_UNSET):
        """Set `channel`'s display window. THE write, from any step.

        True when it changed anything. The persistence port is told, so the
        remap params on disk follow; a write that came FROM that port passes
        `persist=False`, which is what stops the two from echoing.

        `token` is the dataset the numbers are ABOUT -- a `DatasetIdentity`
        from a background task. A seed that started on slide A and finishes
        after the user has loaded B is written into A's namespace and cannot
        be read as B's; refusing it outright would be worse, because going
        back to A must not have to seed again.

        AN EVICTED IDENTITY IS REFUSED, not recreated. A late write is not a
        reason to bring a namespace back: the state would then hold a space
        nothing is bound to, built from one stale value.
        """
        if not channel:
            return False
        space_ns = self._namespace_for(token)
        if space_ns is None:
            perf_trace.mark("display.late_write_refused", channel=channel,
                            origin=origin, why="evicted",
                            identity=str(token))
            return False
        key = (channel, bool(nucleus))
        current = space_ns.mappings.get(key)
        if gamma is None:
            gamma = current[2] if current else 1.0
        value = (float(lo), float(hi), float(gamma))
        if current == value:
            return False
        space_ns.mappings[key] = value
        space_ns.bump(("mapping", key))
        if space_ns is not self._ns:
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
            if self._ns.mappings.get((channel, bool(nucleus))) == new:
                continue
            self._ns.mappings[(channel, bool(nucleus))] = new
            self._ns.bump(("mapping", (channel, bool(nucleus))))
            changed.append((channel, bool(nucleus)))
        if not changed:
            return []
        self._mapping_rev += 1
        for channel, nucleus in changed:
            if persist and self._persist_port is not None:
                value = self._ns.mappings[(channel, nucleus)]
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
        return dict(self._ns.mappings)

    def forget_mappings(self, token=_UNSET):
        """Drop a slide's windows, so it is seeded again from its own pixels.

        For a RELOAD of the same slide, where the pixels may have changed
        under the same identity. An ordinary A -> B switch does not need it:
        B has its own namespace and A's is simply not read.
        """
        ns = self._namespace_for(token)
        if ns is None:
            return
        ns.mappings.clear()
        ns.bump("mappings")
        if ns is self._ns:
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

    def latest_binding_generation(self, identity=_UNSET):
        """The generation of the most recent formal bind of a namespace.

        None when the identity is not resident. This, not the CURRENT
        binding, is what a returning result must match: compared only with
        what is bound now, the sequence A1 -> B -> A2 -> C -> A1-returns let
        A1 write into resident A, because at that moment A was merely "some
        other dataset" and its own second visit was invisible to the check.
        """
        ns = self._namespace_for(identity)
        return None if ns is None else ns.binding_generation

    def accepts_binding(self, binding):
        """May work started under `binding` still be written?

        Only when its namespace is still resident AND that namespace has not
        been bound again since. A -> B with A still resident: yes, silently.
        Once A has been bound a second time, no -- for ever, whatever is
        current now.
        """
        if binding is None:
            return False
        latest = self.latest_binding_generation(
            getattr(binding, "identity", None))
        return latest is not None and latest == binding.generation

    def field_revision(self, key, identity=_UNSET):
        """How many times `key` has been written in a namespace.

        A background task records this when it starts and hands it back when
        it finishes; a value that has moved since means the answer is about a
        state nobody is in any more. `("mapping", (channel, nucleus))` is the
        key a display window uses; `None` means "the field was ABSENT when I
        started", which is the precondition an automatic seed carries.
        """
        ns = self._namespace_for(identity)
        return None if ns is None else ns.revision(key)

    # ── selection ─────────────────────────────────────────────────────
    def selected_channel(self):
        """The channel the steps are editing. Dataset-scoped."""
        return self._ns.selection

    def set_selected_channel(self, channel, origin=""):
        """THE selection write. True when it changed anything.

        Programmatic restore reaches this too, and it stays a plain write: it
        shows nothing, ticks nothing and enables nothing. Auto-show on
        selection is a PAGE's rule about a user click, not a property of the
        selection itself.
        """
        channel = str(channel or "")
        if channel == self._ns.selection:
            return False
        self._ns.selection = channel
        self._ns.bump("selection")
        self.selection_changed.emit(channel)
        return True

    # ── display visibility ────────────────────────────────────────────
    def display_visible(self, channel, default=False):
        """Whether `channel` is shown. A VIEW fact, dataset-scoped.

        Not fusion participation: that is Step1's scientific draft and is not
        this model's (see the plan, 2.1). B2 records the display answer; B3
        and B4 separate the scientific one.
        """
        return bool(self._ns.visibility.get(str(channel), default))

    def display_visibility(self):
        return dict(self._ns.visibility)

    def set_display_visible(self, channel, visible, origin=""):
        """THE display-visibility write. True when it changed anything."""
        channel = str(channel or "")
        if not channel:
            return False
        visible = bool(visible)
        if self._ns.visibility.get(channel) == visible:
            return False
        self._ns.visibility[channel] = visible
        self._ns.bump("visibility")
        self.visibility_changed.emit(channel, visible)
        return True

    # ── channel identity, order and capabilities ──────────────────────
    def channel_order(self):
        return tuple(self._ns.order)

    def capabilities(self, channel):
        """What may be done to `channel`, as separate facts.

        A channel this state has never been told about answers with the
        permissive default rather than None: a caller asking "may I toggle
        this?" before the order is installed should not have to special-case
        an absence.
        """
        return self._ns.capabilities.get(
            str(channel), _identity.ChannelCapabilities())


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
        self._panel_token_source = None

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
        # ...and ONE request for a whole-state install. Its fields are
        # announced too, but a transaction is one event: reacting to each of
        # its colours and windows asked for the same picture several times
        # over, and a session restore is the transaction that shows it.
        state.state_installed.connect(self._on_state_installed)

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
    def bind_dataset(self, token, *, install=None):
        """A new binding. Everything in flight is about the old one.

        And the new one is asked for at once: a slide the state has never
        held has no windows, and nothing else would start the seeds -- the
        request finds no computed window, names the channel as loading and
        sends the seed to its thread. A slide whose namespace is still
        resident answers immediately instead, from what it already has.

        `install` is the optional atomic payload (order, capabilities,
        selection, visibility, colours, mappings) so a bind and its restore
        are ONE transaction rather than a bind followed by a run of writes.
        """
        binding = self.state.bind_dataset(token) if install is None else None
        if install is not None:
            identity = (token if isinstance(token, _identity.DatasetIdentity)
                        else _identity.resolve_identity(
                            token[1] if isinstance(token, tuple) else token))
            binding = (None if identity is None
                       else self.state.bind(identity, install=install))
        self._begin_generation("dataset")
        self.request_frame(kind="dataset")
        return binding

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
    def _on_state_installed(self, _binding):
        """A whole display state landed at once: ONE frame for all of it."""
        self.request_frame(kind="install")

    def _on_shared_color_changed(self, channel, _hexc):
        if self.state.install_fanout_active():
            return                      # counted once, by `_on_state_installed`
        if self._touches(channel):
            self.request_frame(kind="color", channel=channel)

    def _on_shared_mapping_changed(self, channel):
        if self.state.install_fanout_active():
            return
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
        # THE FRAME'S TOKEN IS THE PAGE'S DATASET KEY, and only that. It
        # travels with the pixels to the overview panels, which the page
        # bound under the same key, and it is what `_result_is_current`
        # judges a returning frame by. Block01's `DatasetIdentity` is a
        # different question -- which NAMESPACE the display answers live in --
        # and running both through this pipeline only made each of them
        # answer the other's question sometimes.
        token = self._panel_token()
        context_token = payload.get("token", token)
        if token is None and context_token is not None:
            # Never bound. ADOPTED rather than refused -- the same rule an
            # overview panel applies to its first picture: a coordinator that
            # has not been through a dataset switch has no slide to defend,
            # and refusing every frame until one happens would mean a page
            # that only ever loads one dataset never draws at all. The CHECK
            # is what is being installed here; from the next switch on it is
            # strict.
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
        if result.get("token") != self._panel_token():
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
        # The token that travels WITH THE PIXELS: the page's own dataset key,
        # which is what the overview panels were bound under and what they
        # check a push against. The frame's `identity` answered a different
        # question a moment ago, in `_result_is_current`.
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

    def _panel_token(self, fallback=None):
        """The dataset key the overview panels were BOUND with.

        Asked of the page that owns them, because it is the page that binds
        them -- and it is therefore the one value a frame can be judged by
        that the panels will also accept.
        """
        source = getattr(self, "_panel_token_source", None)
        getter = getattr(source, "tissue_dataset_token", None)
        if getter is None:
            return fallback
        try:
            return getter()
        except Exception:                                   # noqa: BLE001
            return fallback

    def set_panel_token_source(self, source):
        """Register who binds the overview panels (and with what key)."""
        self._panel_token_source = source

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


#: What a session restore did, for the ONE host that refreshes after it.
#: `display_announced` says the display half announced an install -- which
#: the frame clock already answered with one frame, so the host must not ask
#: for a second one.
SessionRestore = collections.namedtuple(
    "SessionRestore", "binding display_announced draft_announced")


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
    #: A session restore finished: BOTH owners are final. The one notice a
    #: host follows to refresh once for a restore, whichever half moved.
    session_restored = pyqtSignal(object)      # a `SessionRestore`

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = ChannelDisplayState(self)
        # Block01's SCIENTIFIC state, built here for the same reason as the
        # display state: it has to answer before the first page exists and
        # after the last one is gone. The Step1 panel is an editor of it.
        self.fusion = FusionDomainModel(self)
        # ONE dataset answer for both halves. The display state already binds
        # every slide to B2's stable identity; the science follows the same
        # bind, so a committed switch takes the previous project's weights
        # with it and a handoff republished for the SAME slide keeps them.
        self.state.dataset_changed.connect(self._on_dataset_identity_changed)
        self.coordinator = TissuePreviewCoordinator(self.state, self)
        self._navigator_content = None
        self._intensity_content = None
        self._lowres_source = None
        self._navigator = None
        self._intensity_window = None
        self._intensity_panel = None
        self._navigator_policy = {"roi_policy": "full", "patch_editable": True}
        self._render_spec = None
        self._weight_editor_content = None
        self._seed_worker = None
        self._read_worker = None
        self._weight_editor = None
        self._weight_editor_panel = None
        # THE one public channel dock (B4-B). Built here, outside the stacked
        # pages, because a per-step list is what let Step0, Step1 and Step3
        # disagree about the same channel and lose the user's place on every
        # transition. Lazily: a headless service that never shows a channel
        # list must not need a QWidget.
        self._channel_dock = None
        self._intensity_locked = False
        self._closing = False
        self._finalized = False

    # ── the one public channel dock ───────────────────────────────────
    def channel_dock(self):
        """The public channel dock, or None while nothing has asked for it."""
        return self._channel_dock

    def ensure_channel_dock(self):
        """Build the ONE public channel dock, once.

        A projection of this object's two owners and of nothing else, so it
        answers for every step -- including the steps that only consume
        display state -- and survives every step transition. A finalized
        session builds nothing: the dock is retired with the windows.
        """
        if self._finalized:
            return None
        if self._channel_dock is None:
            from .widgets.channel_dock.global_dock import GlobalChannelDock
            self._channel_dock = GlobalChannelDock(self.state, self.fusion)
        return self._channel_dock

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

        Returns True when a pass was started or is already running.

        The request carries the WHOLE precondition it was started under: the
        stable identity, the binding generation, and the fact that this
        field was ABSENT. All three are checked on the way back, so a seed
        cannot land on another slide, cannot land after A -> B -> A, and
        cannot overwrite a value the user typed while it was running.
        """
        if self._closing or not channel:
            return False
        if self.state.mapping(channel, nucleus=nucleus) is not None:
            return False
        binding = self._ensure_binding()
        if binding is None:
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
        return bool(worker.submit(binding, channel, array, nucleus=nucleus))

    def _ensure_binding(self):
        """The current binding, binding from the data source if there is none.

        A page that has never been through a dataset commit -- the landing
        before the first Load, a standalone page, a tool script -- still has
        a path, and the display state can be bound from it. Binding lazily
        here rather than refusing keeps "there is always a namespace to
        answer from" true without making every caller check.
        """
        binding = self.state.binding()
        if binding is not None:
            return binding
        source = self._lowres_source
        getter = getattr(source, "tissue_dataset_identity", None)
        identity = None if getter is None else getter()
        if identity is None:
            return None
        return self.state.bind(identity)

    def mapping_seed_pending(self, channel=None, nucleus=False):
        worker = self._seed_worker
        if worker is None:
            return False
        return worker.pending(self.state.binding(), channel, nucleus)

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
        """A window came back. THREE checks before it may land.

        1. THE BINDING. A seed started under A's first binding is refused
           after A -> B -> A even though the identity says A both times --
           the generation is what tells the two visits apart.
        2. THE NAMESPACE. An identity that has been evicted is refused
           rather than resurrected, so a late answer cannot recreate a space
           nothing is bound to.
        3. THE PRECONDITION. This pass was started because the field was
           ABSENT. If it is no longer absent -- the user typed a number while
           the percentile was running -- the automatic answer is stale and
           the user's stands.

        A seed that survives all three but is about a slide the user has
        moved off is still FILED in its own namespace, silently: going back
        to it must not have to seed again.
        """
        result = dict(result or {})
        channel = result.get("channel")
        binding = result.get("binding")
        if not channel or binding is None:
            return
        nucleus = bool(result.get("nucleus"))
        identity = getattr(binding, "identity", None)
        current = self.state.binding()
        is_current = (current is not None and binding == current)
        # AGAINST ITS OWN NAMESPACE'S LATEST BIND, not against whatever is
        # current. Compared only with the current binding, A1 -> B -> A2 -> C
        # -> A1-returns let A1 write into resident A: at that moment A was
        # merely "some other dataset", and A's own second visit -- which is
        # exactly what made A1 stale -- was invisible to the check.
        if not self.state.accepts_binding(binding):
            perf_trace.mark("display.seed_refused",
                            why=("evicted" if self.state.
                                 latest_binding_generation(identity) is None
                                 else "binding"),
                            channel=channel, seed=str(binding),
                            current=str(current))
            return
        if self.state.mapping_in(identity, channel, nucleus=nucleus) is not None:
            # The precondition is gone: somebody answered while we measured.
            perf_trace.mark("display.seed_refused", why="answered",
                            channel=channel, seed=str(binding))
            return
        self.state.set_mapping(channel, result["min"], result["max"],
                               result.get("gamma", 1.0), nucleus=nucleus,
                               origin="seed", token=identity)
        if not is_current:
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

        ALSO the panel-token source: the page that owns the whole-slide
        arrays is the page that binds the overview panels, so it is the one
        that can say which dataset key those panels were bound with.

        `source` answers `tissue_lowres_array(channel)` -- RESIDENT ONLY,
        never a read on the GUI thread -- and `ensure_tissue_lowres(channels)`,
        which asks for the missing ones in the background. One service for
        every step, so a Step1 overlay and a Step0 thumbnail of the same
        channel are the same array rather than two reads of it.
        """
        self._lowres_source = source
        self.coordinator.set_panel_token_source(source)

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
                     "_lowres_source", "_weight_editor_content"):
            if getattr(self, name, None) is owner:
                setattr(self, name, None)
        if self.state._seed_port is owner:
            self.state._seed_port = None
        if self.state._persist_port is owner:
            self.state._persist_port = None
        if self.state._default_source is not None and (
                getattr(self.state._default_source, "__self__", None) is owner):
            self.state._default_source = None
        # ...and the coordinator's two callables into the page. They are
        # bound methods, so leaving them behind keeps the page ALIVE as well
        # as callable: a torn-down Step0 that is still reachable is a page
        # that can still be asked for panels it no longer has.
        coordinator = self.coordinator
        for name in ("_panels_source", "_panel_token_source"):
            held = getattr(coordinator, name, None)
            if held is owner or getattr(held, "__self__", None) is owner:
                setattr(coordinator, name, None)
        # ...and the low-resolution read thread, which was STARTED with a
        # bound method of this owner. A worker holding it keeps the page
        # alive and, worse, keeps it readable: the next missing channel would
        # be read through a page that has been torn down.
        worker = self._read_worker
        if worker is not None and getattr(
                getattr(worker, "_reader", None), "__self__", None) is owner:
            self._read_worker = None
            for signal in ("done", "failed"):
                try:
                    getattr(worker, signal).disconnect()
                except (TypeError, RuntimeError):
                    pass
            try:
                worker.stop()
            except Exception:                               # noqa: BLE001
                pass

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

    def _on_dataset_identity_changed(self, identity):
        """The display bound another slide: the science binds with it."""
        self.fusion.bind_dataset(identity, reason="dataset bind")

    # ── the session restore transaction ───────────────────────────────
    def restore_session_state(self, identity, fusion_spec,
                              display_payload=None, reason="session restore"):
        """Put a saved session back into BOTH owners as one fact.

        THE ONLY ENTRY for a session restore, and the reason it exists is
        that a session names one slide's science AND its display answers,
        while the two live in two objects. Restoring them one after the other
        -- whatever the order -- leaves a window in which a synchronous
        handler reads a new project against the previous slide's visibility,
        selection and colours. So:

        1. the whole input is staged SILENTLY in both owners;
        2. a restore that moves neither of them is taken back and returns
           False, having emitted nothing and burned no generation;
        3. otherwise the display announces, the science announces, and
           `session_restored` says the transaction is over.

        Every one of those notices is emitted with BOTH halves already
        written, which is what makes "no callback sees half a restore" a
        property of this method rather than of each caller's ordering.

        The same session restored again is a NO-OP by the same test: same
        identity, same draft, same display payload -- no rebind, no
        generation, no revision, no signal.
        """
        if identity is None:
            return False
        try:
            self.fusion.prepare_restore(identity, fusion_spec or {})
            self.state.prepare_restore(identity, display_payload or {})
        except Exception:
            self.fusion.cancel_restore("restore failed")
            self.state.cancel_restore("restore failed")
            raise
        if not (self.fusion.pending_restore_changes()
                or self.state.pending_restore_changes()):
            self.fusion.cancel_restore()
            self.state.cancel_restore()
            perf_trace.mark("display.restore_noop", reason=str(reason))
            return False
        # The display half first: its `dataset_changed` reaches this object's
        # own handler, and the science it would bind is already staged to the
        # same identity, so that bind is a no-op rather than a second
        # announcement over the restore.
        display_announced = self.state.commit_restore()
        draft_announced = self.fusion.commit_restore(reason)
        self.session_restored.emit(SessionRestore(
            binding=self.state.binding(),
            display_announced=bool(display_announced),
            draft_announced=bool(draft_announced)))
        return True

    def set_render_weight(self, channel, weight):
        """THE global entry for "this channel contributes this much".

        It goes straight to the fusion model, whichever step the user is in.
        It used to go to a registered WEIGHT OWNER -- the Step1 channel panel
        -- which made a Step3 edit depend on a built, populated widget on
        another page; the model is the owner now and the panel follows it.

        A COMMAND, and nothing else. It does not ask for a frame: the model's
        consumer does that for every scientific change from every entry, so
        asking here as well queued the same frame twice for one edit -- once
        from the caller and once from the observer.
        """
        if not channel:
            return False
        return bool(self.fusion.edit_channel_weight(channel, float(weight),
                                                    origin="shared-weights"))

    def render_weight(self, channel):
        """What this channel weighs scientifically, or None when the model
        has never heard of it."""
        if not channel:
            return None
        rep = self.fusion.representative_weight(channel)
        return None if rep.absent and not rep.values else float(rep.value)

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
        # A staged restore may never outlive the session it belongs to: it is
        # a write waiting for a notice, and a close that left one behind
        # would keep a project nobody committed.
        self.fusion.cancel_restore("close")
        self.state.cancel_restore("close")
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
        # The public dock goes with them, and only here: `begin_close` may be
        # refused, and a dock destroyed on a refused close would leave the
        # session without the list every step edits in.
        dock = self._channel_dock
        self._channel_dock = None
        if dock is not None:
            try:
                dock.setParent(None)
                dock.deleteLater()
            except RuntimeError:
                pass

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
