"""block01/core/fusion_domain.py — Block01's fusion state, outside the page.

WHY THIS EXISTS. Step1's scientific answer -- which channels take part in the
fusion, the groups they are in, what each one weighs in each group, and which
of those numbers are answers rather than absences -- used to live in
`ConfigPanel`: a QWidget on a stacked page. So the only way to ask "what does
CD3 weigh" was to have that widget alive, built, and populated, and the only
way to say it was to move a spin box. A weight edited in Step3 wrote into a
Step1 row; a weight asked for before Step1 was ever opened had nobody to ask.

This module is the answer instead. It is owned by `Block01DisplayServices`, so
it exists before the first page does and outlives every one of them, and it
has no QWidget in it: the panel, the rows and the shared Weights window are
editors and projections of it.

THREE COMMANDS, THREE FIELDS. They are separate on purpose, and the separation
is the point of this module:

    ChannelDisplayState.set_display_visible   -- can you see it
    FusionDomainModel.set_fusion_enabled      -- does it take part
    FusionDomainModel.edit_channel_weight     -- how much, scientifically

Hiding a channel is not a scientific act: it cannot shrink the effective
configuration, cannot dirty the draft and cannot make a committed snapshot
stale. Taking a channel OUT of the fusion is a scientific act and does all
three -- while keeping its group memberships and every per-group weight, so
putting it back brings its own numbers back rather than a default.

WHAT A NUMBER MEANS. `0.0` is two different facts and only one may be
replaced: a marker nobody has ever weighted, and a marker somebody
deliberately set to nothing. The numbers cannot tell them apart, so this model
records WHO said it, apart from the value:

    absent          nobody has said anything; the first enable may answer 1.0
    auto            the first enable answered 1.0; an answer, but not an edit
    explicit        a user or an API said this number, 0.0 included
    authoritative   a config, session or handoff supplied it, 0.0 included

Only `absent` may be overwritten by the first-enable default. `auto` is an
answer, so it survives -- but it is not an EDIT, so it never unifies a channel
that sits in several groups at different weights. Only `edit_channel_weight`
does that.
"""

import contextlib
import copy

from PyQt5.QtCore import QObject, pyqtSignal

# Provenance of a channel's scientific weight. See the module docstring.
ABSENT = "absent"
AUTO = "auto"
EXPLICIT = "explicit"
AUTHORITATIVE = "authoritative"

#: What the first enable answers when nobody has weighted the channel yet.
FIRST_ENABLE_WEIGHT = 1.0


def _identity_key(identity):
    """The comparable form of a `DatasetIdentity`, or None.

    A dataset with no fingerprint cannot be shown to be the same dataset
    twice, so it compares equal to nothing -- including itself.
    """
    if identity is None:
        return None
    path = str(getattr(identity, "path", "") or "")
    fingerprint = str(getattr(identity, "fingerprint", "") or "")
    if not path or not fingerprint:
        return None
    if getattr(identity, "ephemeral", False):
        # An ephemeral identity is a nonce for ONE bind. It may be compared
        # with itself (a rebind of the same object is not a new slide) but a
        # fresh nonce for the same path is a different one.
        return (path, fingerprint)
    return (path, fingerprint)


class RepresentativeWeight:
    """What one row may show for a channel that may be in several groups.

    A row has one number and an old project may put the same channel in two
    groups at 0.2 and 0.7. The row shows the LARGEST and says the values
    disagree; it never writes that largest value back, because that would
    flatten the project by drawing it.
    """

    __slots__ = ("value", "absent", "mixed", "values", "provenance")

    def __init__(self, value, absent, mixed, values, provenance):
        self.value = float(value)
        self.absent = bool(absent)
        self.mixed = bool(mixed)
        self.values = tuple(values)
        self.provenance = str(provenance)

    def __repr__(self):                                     # pragma: no cover
        return (f"RepresentativeWeight(value={self.value}, "
                f"absent={self.absent}, mixed={self.mixed}, "
                f"values={self.values}, provenance={self.provenance!r})")

    def __eq__(self, other):
        if isinstance(other, RepresentativeWeight):
            return (self.value, self.absent, self.mixed, self.values,
                    self.provenance) == (other.value, other.absent,
                                         other.mixed, other.values,
                                         other.provenance)
        return NotImplemented


class FusionDomainModel(QObject):
    """The one owner of Block01's scientific fusion state.

    Signals are how the existing widgets follow it; they are not how it
    stores anything. Every one of them is emitted AFTER the change is whole,
    and a write that changes nothing emits nothing at all.
    """

    #: The scientific draft moved: weights, groups, nucleus or participation.
    draft_changed = pyqtSignal()
    #: One channel entered or left the scientific input set.
    participation_changed = pyqtSignal(str, bool)
    #: One channel's scientific weight moved.
    weight_changed = pyqtSignal(str)
    #: A whole draft was installed at once (restore/handoff). One notice.
    draft_restored = pyqtSignal()
    #: The immutable committed snapshot was replaced.
    committed_changed = pyqtSignal()
    #: The draft was bound to another dataset (or to none). Carries the
    #: `DatasetIdentity`, or None.
    dataset_bound = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # group -> {"weight": float, "members": [channel]}
        self._groups = {}
        # group -> {channel: weight} -- every value exactly as supplied
        self._group_weights = {}
        self._nucleus_channel = ""
        self._nucleus_weight = 0.0
        # The scientific input set. NOT display visibility.
        self._enabled = set()
        # channel -> one of ABSENT/AUTO/EXPLICIT/AUTHORITATIVE
        self._provenance = {}
        # A channel's scientific answer when it is in no group yet. A weight
        # edited in Step0, before Step1 has built any group, has to land
        # somewhere real -- inventing a group to hold it would put a group in
        # the project that nobody asked for, so it waits here and the first
        # group that takes the channel adopts it.
        self._channel_weight = {}
        self._committed = None
        self._hash_provider = None
        self._installing = False
        #: While a caller holds `deferred_notices()`, what this model has to
        #: announce once both owners are final.
        self._deferred = None
        # WHICH DATASET this draft is about. A scientific weight is not a
        # process preference keyed by channel NAME: `CD3` on another slide is
        # another measurement, and a draft that followed the name across a
        # dataset switch would put one slide's numbers into another's project.
        self._identity = None
        # HAS THIS SLIDE HAD ITS FIRST HANDOFF YET. Binding a dataset and
        # initialising its science are two different facts, and one boolean
        # answering "did the identity move" cannot say both: Step0 binds the
        # display in the middle of a load, the science follows that bind, and
        # the reader that arrives afterwards must still build this slide's
        # groups even though the identity did not move again.
        self._initialized = False
        # A restore in progress: the state is written, the notice is held
        # until the display half has been bound to the same identity.
        self._pending_restore = None
        # Bumped once per logical command. The window that refreshes the
        # preview, the Unsaved label and the session uses it to do that ONCE
        # for one command, however many field signals the command produced.
        self._draft_rev = 0

    # ── ports ─────────────────────────────────────────────────────────
    def set_hash_provider(self, fn):
        """Register the EXISTING settings-hash function.

        The hash is the fusion config plus the display mapping, canonicalised
        and digested by Step1's own algorithm, and old projects are named by
        it. This model derives `Unsaved` from it; it does not invent a second
        one. `fn(draft_or_None) -> str`.
        """
        self._hash_provider = fn

    #: The draft has no dataset at all.
    UNBOUND = "unbound"
    #: Bound to a slide whose first handoff has not been read yet. A weight
    #: named now is a PENDING answer for this slide: real, and waiting for
    #: the groups that will adopt it.
    BOUND_UNINITIALIZED = "bound_uninitialized"
    #: Bound, and this slide's groups, nucleus and participation exist.
    INITIALIZED = "initialized"

    # ── the dataset this draft belongs to ────────────────────────────
    def scientific_identity(self):
        """The dataset whose science this draft is, or None when unbound."""
        return self._identity

    def lifecycle(self):
        """Where this draft is: unbound, bound but not yet initialised, or
        initialised. The reader asks THIS, not whether a bind moved."""
        if self._identity is None:
            return self.UNBOUND
        return self.INITIALIZED if self._initialized \
            else self.BOUND_UNINITIALIZED

    def is_initialized(self):
        return self.lifecycle() == self.INITIALIZED

    def draft_revision(self):
        """How many logical commands have changed the draft."""
        return self._draft_rev

    def bind_dataset(self, identity, reason="", install=None):
        """Bind the draft to `identity`. A CHANGE wipes the previous project.

        Reuses B2's stable dataset identity -- canonical path plus source
        fingerprint -- so a handoff republished for the SAME slide rebinds to
        the same identity and keeps a legal draft, while a committed switch to
        another slide arrives as a different identity and takes the old
        project's groups, weights, provenance, participation and committed
        snapshot with it.

        Fail closed on both sides of "unknown": an unresolvable source gets an
        ephemeral identity that is new every time, so it never inherits; and
        answers given while NOTHING was bound cannot be signed over to the
        first real slide that arrives.

        `install` makes the bind and its restore ONE transaction. Without it a
        session restore is two announcements -- the bind clearing the previous
        slide, then the install putting this one back -- and an observer sees
        an empty project in between that nobody ever meant.
        """
        key = _identity_key(identity)
        same = key is not None and key == _identity_key(self._identity)
        if same and install is None:
            return False                       # same slide: nothing to wipe
        # A REAL bind overrules a restore nobody committed: the staged
        # project describes the slide this bind is leaving, and committing it
        # afterwards would announce it over the slide that arrived.
        if self._pending_restore is not None:
            self._pending_restore = None
            print("[Block01] fusion restore dropped: another dataset was "
                  "bound before it was committed")
        before = self.draft_snapshot()
        self._installing = True
        try:
            if not same:
                self._identity = identity
                self._reset_scientific_state()
                # A new slide has had no handoff yet: whatever is named for
                # it now is pending, and the first handoff still has to build
                # its groups.
                self._initialized = False
            if install is not None:
                self._apply_spec(dict(install), reset=True)
        finally:
            self._installing = False
        if same and self.draft_snapshot() == before:
            return False
        if reason:
            print(f"[Block01] fusion draft bound to {key or 'nothing'}"
                  f" ({reason})")
        self._draft_rev += 1
        if not same:
            self.dataset_bound.emit(identity)
        if install is not None:
            self._initialized = True
            self.draft_restored.emit()
        self.draft_changed.emit()
        return True

    def initialize_dataset(self, groups, nucleus_channel, channels=None):
        """THIS SLIDE's first handoff: build its project. ONE notice.

        Group membership comes from the handoff's panel, every marker starts
        as a placeholder rather than an answer, and the nucleus is the
        handoff's channel at the first-enable default -- so the first `f`
        tick of each marker still answers 1.0.

        The exception is a PENDING answer: a weight named for this slide
        before Step1 built any group to hold it, in Step0 or through the
        shared Weights window. Those are this dataset's, they are adopted by
        the groups being built, and `0.0` is one of them.
        """
        nuc = str(nucleus_channel or "")
        known = {str(ch) for ch in (channels or [])}
        spec_groups = {}
        for name, members in (groups or {}).items():
            spec_groups[str(name)] = {
                "group_weight": 1.0,
                "channels": {str(ch): 0.0 for ch in members
                             if str(ch) != nuc}}
        pending = self.pending_answers()
        provenance = {nuc: AUTHORITATIVE} if nuc else {}
        weights = {nuc: 1.0} if nuc else {}
        for ch, (value, prov) in pending.items():
            if ch == nuc or (known and ch not in known):
                continue
            provenance[ch] = prov
            weights[ch] = value
        self._installing = True
        try:
            self._apply_spec({
                "groups": spec_groups,
                "nucleus": {"channel": nuc, "weight": 1.0 if nuc else 0.0},
                "enabled": [nuc] if nuc else [],
                "provenance": provenance,
                "channel_weight": weights,
            }, reset=True)
            # The groups were built from the handoff's placeholder zeros; a
            # pending answer replaces its own channel's placeholder in every
            # one of them, the way the first group always adopts it.
            for ch, (value, prov) in pending.items():
                if ch in weights and ch != nuc:
                    self._write_weight(ch, value, prov)
        finally:
            self._installing = False
        self._initialized = True
        self._draft_rev += 1
        self.draft_restored.emit()
        self.draft_changed.emit()
        return True

    def prepare_restore(self, identity, spec):
        """Write a restored project WITHOUT announcing it.

        The session restore is one fact about two owners: this draft and the
        display state. Announcing as soon as the science is in leaves every
        handler that reads BOTH -- and the frame clock is one -- looking at a
        new project against the display's previous slide. So the science is
        written silently here, the display is bound to the same identity, and
        `commit_restore` publishes once, with both halves final.
        """
        if self._pending_restore is not None:
            # A second prepare over an uncommitted one would strand the first
            # one's rollback point. Take the earlier one back first, so the
            # state this one is measured against is the real one.
            self.cancel_restore("superseded")
        before = self.draft_snapshot()
        same = (_identity_key(identity) is not None
                and _identity_key(identity) == _identity_key(self._identity))
        self._pending_restore = {"before": before, "same": same,
                                 "was_initialized": self._initialized,
                                 "identity_before": self._identity,
                                 "committed_before": copy.deepcopy(
                                     self._committed)}
        self._installing = True
        try:
            self._identity = identity
            self._reset_scientific_state()
            self._apply_spec(dict(spec or {}), reset=True)
            self._initialized = True
        except Exception:
            # Same rule as the display half: a prepare that fails part way
            # through takes itself back rather than leaving a project nobody
            # asked for standing against the other owner.
            self._installing = False
            self.cancel_restore("prepare failed")
            raise
        finally:
            self._installing = False
        return True

    def restore_pending(self):
        """Is a prepared restore waiting to be committed or cancelled."""
        return self._pending_restore is not None

    def pending_restore_changes(self):
        """Would committing the prepared restore change anything at all.

        Asked BEFORE the display half is announced, because a restore that
        moves neither owner must announce nothing on either side -- and the
        coordinator cannot know that from this half alone after the fact.
        """
        pending = self._pending_restore
        if not pending:
            return False
        # A restore that put back exactly what was already there, on the
        # slide that was already current, is not a change: announcing it
        # makes every consumer redraw, re-dirty and re-save a state nobody
        # moved.
        return not (pending["same"] and pending["was_initialized"]
                    and self.draft_snapshot() == pending["before"])

    def cancel_restore(self, reason=""):
        """Take a prepared restore back, leaving no trace of it.

        The staging is a WRITE, so abandoning it has to undo that write --
        otherwise a failure halfway through a session load, or a restore the
        coordinator decides is a no-op, would leave this model describing a
        project nobody committed.
        """
        pending = self._pending_restore
        if not pending:
            return False
        self._pending_restore = None
        self._installing = True
        try:
            self._identity = pending["identity_before"]
            self._reset_scientific_state()
            self._apply_spec(dict(pending["before"]), reset=True)
            self._committed = copy.deepcopy(pending["committed_before"])
            self._initialized = bool(pending["was_initialized"])
        finally:
            self._installing = False
        if reason:
            print(f"[Block01] fusion restore cancelled ({reason})")
        return True

    def commit_restore(self, reason=""):
        """Publish the prepared restore. ONE notice, both owners final."""
        pending = self._pending_restore
        if not pending:
            return False
        announce = self.pending_restore_changes()
        self._pending_restore = None
        if not announce:
            return False
        if reason:
            print(f"[Block01] fusion draft restored for "
                  f"{_identity_key(self._identity) or 'nothing'} ({reason})")
        self._draft_rev += 1
        self.dataset_bound.emit(self._identity)
        self.draft_restored.emit()
        self.draft_changed.emit()
        return True

    def discard_dataset_state(self, reason=""):
        """Another dataset was committed: this project is over.

        Called from the dataset SWITCH, not from a handoff invalidation --
        republishing the same slide's geometry does not make its weights
        untrue, and clearing them there would delete a legal draft.
        """
        if self._pending_restore is not None:
            # Same rule as a bind: a discard ends the project a pending
            # restore belongs to, so that restore may never be committed.
            self._pending_restore = None
            print("[Block01] fusion restore dropped: the dataset state was "
                  "discarded before it was committed")
        if (self._identity is None and not self._groups
                and not self._channel_weight and not self._enabled
                and self._committed is None):
            return False
        self._identity = None
        self._reset_scientific_state()
        self._initialized = False
        if reason:
            print(f"[Block01] fusion draft discarded ({reason})")
        self._draft_rev += 1
        self.dataset_bound.emit(None)
        self.draft_changed.emit()
        return True

    def _reset_scientific_state(self):
        """Drop every scientific answer, in one go. No field signals."""
        self._groups = {}
        self._group_weights = {}
        self._nucleus_channel = ""
        self._nucleus_weight = 0.0
        self._enabled = set()
        self._provenance = {}
        self._channel_weight = {}
        self._committed = None

    # ── channel universe ──────────────────────────────────────────────
    def channels(self):
        """Every channel this model has an answer about."""
        out = []
        for name in self._groups:
            for ch in self._groups[name]["members"]:
                if ch not in out:
                    out.append(ch)
        for ch in self._channel_weight:
            if ch not in out:
                out.append(ch)
        if self._nucleus_channel and self._nucleus_channel not in out:
            out.append(self._nucleus_channel)
        return out

    # ── participation ─────────────────────────────────────────────────
    def fusion_enabled(self, channel):
        return str(channel) in self._enabled

    def enabled_channels(self):
        return sorted(self._enabled)

    def set_fusion_enabled(self, channel, enabled, origin="api"):
        """Put a channel into the scientific input set, or take it out.

        Taking it out keeps every group membership and every per-group weight:
        the channel simply stops contributing. Putting it back restores those
        values exactly -- an explicit `0.0` included, and `0.2/0.7` group for
        group -- because they were never removed.

        The FIRST enable of a channel nobody has weighted answers `1.0`: a
        channel asked into the fusion at nothing is not an answer to anything.
        That is an automatic answer, not an edit, so it does not unify a
        channel that is in several groups at different weights.
        """
        channel = str(channel or "")
        if not channel:
            return False
        enabled = bool(enabled)
        if (channel in self._enabled) == enabled:
            return False
        weighted = False
        if enabled and self.weight_provenance(channel) == ABSENT:
            weighted = self._write_weight(channel, FIRST_ENABLE_WEIGHT, AUTO)
        if enabled:
            self._enabled.add(channel)
        else:
            self._enabled.discard(channel)
        if self._deferred is not None:
            # Held until the caller's whole command is done -- see
            # `deferred_notices`.
            self._deferred.append((channel, enabled, weighted))
            return True
        if self._installing:
            return True
        # ONE logical command, whatever it had to write: a first enable moves
        # participation AND answers the weight, and the window that redraws
        # must not do it twice for that.
        self._draft_rev += 1
        if weighted:
            self.weight_changed.emit(channel)
        self.participation_changed.emit(channel, enabled)
        self.draft_changed.emit()
        return True

    @contextlib.contextmanager
    def deferred_notices(self):
        """Hold this model's notices until the caller's command is complete.

        Step1's tick is ONE decision that lands in TWO owners: participation
        and the weight here, display visibility in `ChannelDisplayState`.
        Whichever owner announces first, an observer that reads the other one
        inside that notice sees a half-finished command -- "fused but still
        hidden" if this model goes first, "shown but not fused" if the state
        does.

        So the caller writes this model, writes the display answer, and lets
        go: everything this model has to say is said afterwards, when both
        owners are final. Re-entrant callers share the outermost block.
        """
        if self._deferred is not None:
            yield                       # an outer block already owns this
            return
        self._deferred = []
        try:
            yield
        finally:
            pending, self._deferred = self._deferred, None
        if not pending:
            return
        self._draft_rev += 1
        for channel, enabled, weighted in pending:
            if weighted:
                self.weight_changed.emit(channel)
            self.participation_changed.emit(channel, enabled)
        self.draft_changed.emit()

    # ── weights ───────────────────────────────────────────────────────
    def weight_provenance(self, channel):
        return self._provenance.get(str(channel or ""), ABSENT)

    def _write_weight(self, channel, value, provenance):
        """Put one number in every place this channel's weight is kept.

        A channel in several groups gets the same number in all of them: that
        is what "this channel weighs 0.4" means once somebody has said it.
        Loading does not come through here -- `install_draft` writes the
        per-group values it was given, value for value.
        """
        channel = str(channel)
        value = float(value)
        moved = self._channel_weight.get(channel) != value
        self._channel_weight[channel] = value
        for name, weights in self._group_weights.items():
            if channel in weights:
                if weights[channel] != value:
                    moved = True
                weights[channel] = value
        if channel and channel == self._nucleus_channel:
            if self._nucleus_weight != value:
                moved = True
            self._nucleus_weight = value
        if self._provenance.get(channel) != provenance:
            self._provenance[channel] = provenance
            moved = True
        return moved

    def edit_channel_weight(self, channel, value, origin="user"):
        """The scientific edit: this channel weighs this, everywhere.

        `0.0` included -- a zero somebody chose is a decision, and a later
        first enable must not replace it. A channel in several groups is
        unified by this command and by nothing else.
        """
        channel = str(channel or "")
        if not channel:
            return False
        moved = self._write_weight(channel, value, EXPLICIT)
        if not moved or self._installing:
            return moved
        self._draft_rev += 1
        self.weight_changed.emit(channel)
        self.draft_changed.emit()
        return True

    def adopt_channel_weight(self, channel, value):
        """A config, session or handoff supplied this weight. Authoritative,
        `0.0` included, but not a user edit."""
        channel = str(channel or "")
        if not channel:
            return False
        moved = self._write_weight(channel, value, AUTHORITATIVE)
        if not moved or self._installing:
            return moved
        self._draft_rev += 1
        self.weight_changed.emit(channel)
        self.draft_changed.emit()
        return True

    def set_channel_answer(self, channel, value, provenance):
        """Write a weight AND say who said it. For a migration or a carry
        across a dataset load, where the answer and its provenance are both
        already known and neither is being decided here."""
        channel = str(channel or "")
        if not channel or provenance not in (ABSENT, AUTO, EXPLICIT,
                                             AUTHORITATIVE):
            return False
        moved = self._write_weight(channel, value, provenance)
        if not moved or self._installing:
            return moved
        self._draft_rev += 1
        self.weight_changed.emit(channel)
        self.draft_changed.emit()
        return True

    def stored_weights(self, channel):
        """Every weight this channel carries: one per group, plus the nucleus
        slot when it is the nucleus."""
        channel = str(channel or "")
        values = [weights[channel]
                  for weights in self._group_weights.values()
                  if channel in weights]
        if channel and channel == self._nucleus_channel:
            values.append(float(self._nucleus_weight))
        return values

    def representative_weight(self, channel):
        """What a row may show for this channel, and what it must not do."""
        channel = str(channel or "")
        provenance = self.weight_provenance(channel)
        values = self.stored_weights(channel)
        if not values:
            fallback = self._channel_weight.get(channel)
            if fallback is None:
                return RepresentativeWeight(0.0, True, False, (), provenance)
            return RepresentativeWeight(fallback, provenance == ABSENT, False,
                                        (float(fallback),), provenance)
        distinct = {round(float(v), 6) for v in values}
        return RepresentativeWeight(max(values), provenance == ABSENT,
                                    len(distinct) > 1,
                                    tuple(sorted(distinct)), provenance)

    def channel_weight(self, channel):
        """The plain number a row shows. See `representative_weight` for what
        it does NOT say."""
        return float(self.representative_weight(channel).value)

    def ambiguous_channels(self):
        """Channels whose groups disagree: named rather than quietly
        resolved."""
        out = {}
        for ch in self.channels():
            rep = self.representative_weight(ch)
            if rep.mixed:
                out[ch] = list(rep.values)
        return out

    # ── groups and nucleus ────────────────────────────────────────────
    def add_group(self, name, channel_weights=None, group_weight=1.0):
        """Create or update a group. ONE notice for the whole group.

        The members go in with `_installing` set, so building a group of
        twenty channels is one logical command rather than twenty.
        """
        name = str(name)
        before = self._group_fingerprint()
        was_installing = self._installing
        self._installing = True
        try:
            data = self._groups.setdefault(
                name, {"weight": float(group_weight), "members": []})
            data["weight"] = float(group_weight)
            for ch, w in (channel_weights or {}).items():
                self.add_to_group(name, ch, w)
        finally:
            self._installing = was_installing
        self._announce_structure(before)
        return self._groups[name]

    def _group_fingerprint(self):
        """Everything `groups`, the nucleus and membership currently say."""
        return (
            {name: (float(data["weight"]), tuple(data["members"]))
             for name, data in self._groups.items()},
            {name: dict(weights)
             for name, weights in self._group_weights.items()},
            self._nucleus_channel, float(self._nucleus_weight),
        )

    def _announce_structure(self, before):
        """Announce a structural change, and only a real one.

        A setter that writes the value it already held is not a change: it
        must not dirty the settings, redraw a preview or schedule a session
        save. A setter that DOES move something goes through the same one
        notice as every other scientific command.
        """
        if self._installing or before == self._group_fingerprint():
            return False
        self._draft_rev += 1
        self.draft_changed.emit()
        return True

    def add_to_group(self, name, channel, weight=0.0):  # noqa: D401
        """Put a channel in a group.

        A channel that already has a scientific answer with nowhere to live --
        a weight edited in Step0 before Step1 built any group -- is taken at
        that answer rather than at the placeholder the caller passed, because
        the answer came from somebody and the placeholder did not.
        """
        name = str(name)
        channel = str(channel)
        data = self._groups.setdefault(name, {"weight": 1.0, "members": []})
        if channel not in data["members"]:
            data["members"].append(channel)
        before = self._group_fingerprint()
        pending = self._channel_weight.get(channel)
        if pending is not None and self.weight_provenance(channel) != ABSENT:
            weight = pending
        self._group_weights.setdefault(name, {})[channel] = float(weight)
        self._announce_structure(before)

    def remove_group(self, name):
        name = str(name)
        before = self._group_fingerprint()
        self._groups.pop(name, None)
        self._group_weights.pop(name, None)
        return self._announce_structure(before)

    def group_weight(self, name):
        data = self._groups.get(str(name))
        return float(data["weight"]) if data else 1.0

    def set_group_weight(self, name, weight):
        before = self._group_fingerprint()
        data = self._groups.setdefault(str(name), {"weight": 1.0,
                                                   "members": []})
        data["weight"] = float(weight)
        return self._announce_structure(before)

    def groups(self):
        """`{group: {channel: weight}}` -- a copy."""
        return {name: {ch: float(self._group_weights.get(name, {}).get(ch, 0.0))
                       for ch in data["members"]}
                for name, data in self._groups.items()}

    def group_weights(self):
        return {name: float(data["weight"])
                for name, data in self._groups.items()}

    def nucleus(self):
        return self._nucleus_channel, float(self._nucleus_weight)

    def set_nucleus(self, channel, weight=None):
        before = self._group_fingerprint()
        self._nucleus_channel = str(channel or "")
        if weight is not None:
            self._nucleus_weight = float(weight)
        return self._announce_structure(before)

    def set_nucleus_weight(self, weight):
        before = self._group_fingerprint()
        self._nucleus_weight = float(weight)
        return self._announce_structure(before)

    def pending_answers(self):
        """Scientific answers that have nowhere to live yet.

        A weight edited in Step0 -- before Step1 has built any group for this
        dataset -- is a real answer about a real channel with no group to hold
        it. It is NOT the previous slide's history: a channel that has been in
        a group carries its numbers there, and those go with that dataset.
        Returns `{channel: (value, provenance)}`.
        """
        grouped = {ch for weights in self._group_weights.values()
                   for ch in weights}
        return {ch: (float(value), self._provenance.get(ch, ABSENT))
                for ch, value in self._channel_weight.items()
                if ch not in grouped and ch != self._nucleus_channel
                and self._provenance.get(ch, ABSENT) != ABSENT}

    def forget_channels_outside(self, channels):
        """Drop every answer about channels this dataset does not have.

        The nucleus is kept whatever the list says: it is the Step0 handoff's
        answer, not a row in this list.

        A REAL scientific change when it removes anything -- group members,
        per-group weights, provenance, participation -- so it is announced
        like every other one. It used to prune in silence, which left the
        Unsaved label, the preview and the session describing a configuration
        that no longer existed. A caller that is about to replace the whole
        draft anyway passes the pruning by and lets `install_draft` do it in
        one transaction.
        """
        keep = {str(ch) for ch in (channels or [])}
        if not keep:
            return False
        keep.add(self._nucleus_channel)
        before = self.draft_snapshot()
        moved = False
        for name, weights in self._group_weights.items():
            for ch in [ch for ch in weights if ch not in keep]:
                weights.pop(ch, None)
                moved = True
            members = self._groups.get(name, {}).get("members")
            if members is not None:
                kept = [ch for ch in members if ch in keep]
                if kept != members:
                    self._groups[name]["members"] = kept
                    moved = True
        for store in (self._provenance, self._channel_weight):
            for ch in [ch for ch in store if ch not in keep]:
                store.pop(ch, None)
                moved = True
        for ch in [ch for ch in self._enabled if ch not in keep]:
            self._enabled.discard(ch)
            moved = True
        if not moved or self._installing or self.draft_snapshot() == before:
            return False
        self._draft_rev += 1
        self.draft_changed.emit()
        return True

    # ── the configuration ─────────────────────────────────────────────
    def full_config(self):
        """The whole scientific draft, disabled channels included, because a
        session has to round-trip it."""
        return {
            "nucleus": {"channel": self._nucleus_channel,
                        "weight": float(self._nucleus_weight)},
            "groups": {
                name: {"group_weight": float(data["weight"]),
                       "channels": {
                           ch: float(self._group_weights
                                     .get(name, {}).get(ch, 0.0))
                           for ch in data["members"]}}
                for name, data in self._groups.items()},
        }

    def effective_config(self):
        """What is fused and what a Save freezes: the ENABLED subset.

        Display visibility has no vote here. Hiding a channel is a question
        about the screen; this is the science.
        """
        cfg = self.full_config()
        nuc = cfg.get("nucleus") or {}
        if nuc.get("channel") and not self.fusion_enabled(nuc["channel"]):
            cfg["nucleus"] = {"channel": nuc.get("channel"), "weight": 0.0}
        cfg["groups"] = {
            name: {"group_weight": data.get("group_weight", 1.0),
                   "channels": {ch: w
                                for ch, w in (data.get("channels") or {}).items()
                                if self.fusion_enabled(ch)}}
            for name, data in (cfg.get("groups") or {}).items()}
        return cfg

    # ── atomic install ────────────────────────────────────────────────
    def draft_snapshot(self):
        """Everything this model knows, as plain data the caller owns.

        Deep-copied: a caller that edits what it was given must not be editing
        the model.
        """
        return copy.deepcopy({
            "groups": {name: {"weight": float(data["weight"]),
                              "members": list(data["members"])}
                       for name, data in self._groups.items()},
            "group_weights": {name: dict(weights)
                              for name, weights in self._group_weights.items()},
            "nucleus": {"channel": self._nucleus_channel,
                        "weight": float(self._nucleus_weight)},
            "enabled": sorted(self._enabled),
            "provenance": dict(self._provenance),
            "channel_weight": dict(self._channel_weight),
        })

    def install_draft(self, spec, reset=True):
        """Install a whole draft at once. ONE notice, at the end.

        Nothing observes half of it: no field signal is emitted while the
        install runs, so no handler can read a restored group against an old
        participation set. Nor does an install run any of the three commands'
        rules -- a restore is not a first enable, not an edit and not a
        unification. It puts back what it was given.
        """
        spec = dict(spec or {})
        # WHAT IT WOULD BECOME, before anything is written. A restore that
        # puts back exactly what is already there is not a change, and a
        # completion notice for it makes every consumer redraw, re-dirty and
        # re-save a state nobody moved.
        before = self.draft_snapshot()
        self._installing = True
        try:
            self._apply_spec(spec, reset=reset)
        finally:
            self._installing = False
        # INSTALLING A WHOLE DRAFT IS THIS SLIDE'S INITIALISATION, whether or
        # not it moved anything: a handoff that re-installs an identical
        # project has still been read.
        self._initialized = True
        if self.draft_snapshot() == before:
            return False
        self._draft_rev += 1
        self.draft_restored.emit()
        self.draft_changed.emit()
        return True

    def _apply_spec(self, spec, reset=True):
        """Write a draft spec into the model. No signals; the caller speaks."""
        if reset:
            self._groups = {}
            self._group_weights = {}
            self._enabled = set()
            self._provenance = {}
            self._channel_weight = {}
            self._nucleus_channel = ""
            self._nucleus_weight = 0.0
        nucleus = spec.get("nucleus")
        if nucleus is not None:
            self._nucleus_channel = str(nucleus.get("channel") or "")
            self._nucleus_weight = float(nucleus.get("weight", 0.0) or 0.0)
        groups = spec.get("groups")
        if groups is not None:
            self._groups = {}
            self._group_weights = {}
            stored = dict(spec.get("group_weights") or {})
            for name, data in groups.items():
                data = dict(data or {})
                # Two shapes, both of them ours: a CONFIG says
                # `{group_weight, channels}`; a DRAFT SNAPSHOT says
                # `{weight, members}` with the numbers alongside in
                # `group_weights`. Neither is guessed at.
                members = data.get("members")
                if isinstance(members, (list, tuple)):
                    numbers = dict(stored.get(name) or {})
                    channels = {str(ch): float(numbers.get(ch, 0.0))
                                for ch in members}
                else:
                    channels = {str(ch): float(w) for ch, w
                                in (data.get("channels") or {}).items()}
                self._groups[str(name)] = {
                    "weight": float(data.get("group_weight",
                                             data.get("weight", 1.0))),
                    "members": list(channels)}
                self._group_weights[str(name)] = channels
        provenance = spec.get("provenance")
        if provenance is not None:
            self._provenance = {str(ch): str(p)
                                for ch, p in provenance.items()}
        weights = spec.get("channel_weight")
        if weights is not None:
            self._channel_weight = {str(ch): float(w)
                                    for ch, w in weights.items()}
        else:
            # Keep the channel-level answer in step with the groups, so a
            # later group adopts the restored number rather than a stale
            # one from the dataset before.
            for ch in self.channels():
                values = self.stored_weights(ch)
                if values:
                    self._channel_weight[ch] = max(values)
        enabled = spec.get("enabled")
        if enabled is not None:
            self._enabled = {str(ch) for ch in enabled}

    # ── committed snapshot ────────────────────────────────────────────
    def install_committed_snapshot(self, snapshot):
        """Adopt the frozen scientific fact. Immutable here: a copy goes in,
        a copy comes out, and a failed commit never reaches this method.

        Re-adopting the SAME snapshot -- a restore that read back what is
        already held, `None` over `None` -- says nothing: nothing about what
        a job would run on has changed.
        """
        incoming = copy.deepcopy(snapshot) if snapshot else None
        if incoming == self._committed:
            return False
        self._committed = incoming
        self.committed_changed.emit()
        return True

    def committed_snapshot(self):
        return copy.deepcopy(self._committed) if self._committed else None

    def committed_hash(self):
        return str((self._committed or {}).get("hash") or "")

    def settings_hash(self, draft=None):
        """The draft's hash, through Step1's own algorithm."""
        if self._hash_provider is None:
            return ""
        return self._hash_provider(draft)

    def is_dirty(self):
        """Does the draft say something the committed snapshot does not?"""
        if not self._committed:
            return True
        if self._hash_provider is None:
            return True
        return self.committed_hash() != self.settings_hash()


# ── old sessions ──────────────────────────────────────────────────────
#
# Three historical session shapes share one version number, so "which fields
# are present" is the only thing that can tell them apart. That test is made
# ONCE, here, and turned into an explicit install spec -- rather than being
# sniffed again in every handler that happens to need one of the fields.

S1_FLAT = "S1"          # legacy flat weights, no groups, no visibility
S2_GROUPED = "S2"       # grouped config, no visibility
S3_VISIBILITY = "S3"    # current old format, carries `channel_visibility`
S4_SPLIT = "S4"         # the new schema: display and fusion said separately

#: The session schema this writer produces. Bumped for the split; the three
#: older shapes above keep their own meaning and are migrated, not reread.
SESSION_SCHEMA_VERSION = 2


def classify_session(sess):
    """Which of the four shapes this session is."""
    sess = dict(sess or {})
    try:
        version = int(sess.get("version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= SESSION_SCHEMA_VERSION and "fusion_draft" in sess:
        return S4_SPLIT
    if isinstance(sess.get("channel_visibility"), dict):
        return S3_VISIBILITY
    if (sess.get("fusion_config") or {}).get("groups"):
        return S2_GROUPED
    return S1_FLAT


def migrate_session(sess, fusion_config=None, channels=None):
    """Turn any session shape into `(install_spec, display_visibility)`.

    The rules are the ones ruled on in the B0 review, implemented once:

    S1/S2 -- there was no visibility field, so GROUP MEMBERSHIP itself was the
    participating set. Every member becomes both visible and fusion-enabled,
    zero-weight members included, and every restored weight is authoritative:
    a missing `channel_weight_initialized` marker is not evidence of absence.

    S3 -- `channel_visibility` was one tick meaning both things, so it
    initialises both fields to keep the old effective output. A channel that
    was off keeps its memberships and all of its weights and stays
    fusion-disabled until somebody enables it; it is never turned on merely
    because it has a weight. Where the recorded marker disagrees with the
    science -- any non-zero or per-group-heterogeneous value -- the science
    wins, because a missing or wrong marker cannot make a real number absent.

    S4 -- the new schema says everything separately and is read, not guessed.
    """
    sess = dict(sess or {})
    shape = classify_session(sess)
    cfg = dict(fusion_config or sess.get("fusion_config") or {})
    groups_cfg = dict(cfg.get("groups") or {})
    nucleus_cfg = dict(cfg.get("nucleus") or {})
    known = set(channels or []) or None

    groups = {}
    group_weights = {}
    members = set()
    for name, data in groups_cfg.items():
        data = dict(data or {})
        channel_weights = {}
        for ch, w in (data.get("channels") or {}).items():
            ch = str(ch)
            if known is not None and ch not in known:
                continue
            channel_weights[ch] = float(w)
            members.add(ch)
        groups[str(name)] = {"group_weight": float(data.get("group_weight", 1.0)),
                             "channels": channel_weights}
        group_weights[str(name)] = channel_weights

    nucleus = {"channel": str(nucleus_cfg.get("channel") or ""),
               "weight": float(nucleus_cfg.get("weight", 0.0) or 0.0)}

    if shape == S4_SPLIT:
        draft = dict(sess.get("fusion_draft") or {})
        # PRESENCE, not truthiness. An empty group set, an empty enabled list
        # and an empty provenance map are all legal states of a real project,
        # and `x or legacy` silently replaced each of them with the legacy
        # field -- or, for `group_weights`, dropped the numbers entirely and
        # let every group/channel value come back as 0.0.
        #
        # The draft snapshot keeps MEMBERSHIP and VALUES apart:
        #   groups        -> {group: {weight, members}}
        #   group_weights -> {group: {channel: scientific weight}}
        #   channel_weight-> {channel: the answer with no group to live in}
        # so all three have to travel, and none of them may be reconstructed
        # from the representative maximum of another.
        spec = {
            "groups": draft["groups"] if "groups" in draft else groups,
            "group_weights": draft.get("group_weights", {}),
            "nucleus": draft["nucleus"] if "nucleus" in draft else nucleus,
            "enabled": draft.get("enabled", []),
            "provenance": draft.get("provenance", {}),
            "channel_weight": draft.get("channel_weight", {}),
        }
        if "display_visibility" in sess:
            recorded = sess.get("display_visibility") or {}
        else:
            recorded = sess.get("channel_visibility") or {}
        visibility = {str(ch): bool(v) for ch, v in recorded.items()}
        # THE SPLIT S4 EVER RECORDED IS RECONCILED, by union.
        #
        # For one release Step1 carried two controls -- a tick for the screen
        # and an `f` box for the fusion -- so a session written then can say
        # "visible but not fused" or "fused but not shown". The tick means
        # both things again, and a restored project must not show the user a
        # state they have no control left to correct.
        #
        # UNION, in both directions: either side being true means the user
        # asked for that channel, in the result or on the screen, so it comes
        # back visible AND participating. Dropping it from the fusion instead
        # would silently delete a scientific input, which no reading of a
        # saved project justifies.
        #
        # Weights are NOT touched by the reconciliation except where there is
        # no answer at all: a channel the union enables whose provenance is
        # `absent` gets the same 1.0/auto a tick itself would have written.
        # An explicit 0.0, an authoritative 0.2/0.7, a per-group spread --
        # all survive verbatim.
        enabled = {str(ch) for ch in (spec.get("enabled") or [])}
        visible = {ch for ch, on in visibility.items() if on}
        # MARKERS ONLY. The nucleus is not a marker and its tick never meant
        # "use this channel": it is the reference layer, shown and hidden on
        # its own switch and weighted by the Step0 handoff. Folding it into
        # the union would enable a nucleus a project had deliberately left
        # out of the fusion, or show one the user had turned off, neither of
        # which the split ever recorded by accident.
        nucleus = str((spec.get("nucleus") or {}).get("channel") or "")
        markers_enabled = enabled - {nucleus}
        markers_visible = visible - {nucleus}
        union = (markers_enabled | markers_visible) | (enabled & {nucleus})
        if (markers_enabled | markers_visible) != markers_enabled or \
                (markers_enabled | markers_visible) != markers_visible:
            provenance = dict(spec.get("provenance") or {})
            weights = dict(spec.get("channel_weight") or {})
            group_weights = {name: dict(values) for name, values
                             in (spec.get("group_weights") or {}).items()}
            for ch in sorted((markers_enabled | markers_visible) - enabled):
                if provenance.get(ch, ABSENT) != ABSENT:
                    continue
                provenance[ch] = AUTO
                weights[ch] = FIRST_ENABLE_WEIGHT
                for values in group_weights.values():
                    if ch in values:
                        values[ch] = FIRST_ENABLE_WEIGHT
            spec["enabled"] = sorted(union)
            spec["provenance"] = provenance
            spec["channel_weight"] = weights
            if group_weights:
                spec["group_weights"] = group_weights
            # the nucleus keeps the visibility the session recorded for it
            visibility.update({ch: True for ch in
                               (markers_enabled | markers_visible)})
        return spec, visibility

    provenance = {ch: AUTHORITATIVE for ch in members}
    if nucleus["channel"]:
        provenance[nucleus["channel"]] = AUTHORITATIVE

    if shape in (S1_FLAT, S2_GROUPED):
        enabled = set(members)
        if nucleus["channel"]:
            enabled.add(nucleus["channel"])
        visibility = {ch: True for ch in enabled}
    else:
        recorded = {str(ch): bool(v) for ch, v in
                    (sess.get("channel_visibility") or {}).items()}
        visibility = dict(recorded)
        enabled = {ch for ch, on in recorded.items() if on}
        # The old marker only ever named channels whose weight was an answer.
        # Where it is absent or disagrees, the SCIENCE decides: a non-zero
        # weight, or one that differs between groups, was chosen by somebody
        # and an empty marker cannot turn it back into an absence.
        marker = sess.get("channel_weight_initialized")
        if isinstance(marker, dict):
            marker = [ch for ch, flag in marker.items() if flag]
        if isinstance(marker, (list, tuple)):
            marked = {str(ch) for ch in marker}
            for ch in list(provenance):
                if ch in marked or ch == nucleus["channel"]:
                    continue
                values = [w.get(ch) for w in group_weights.values()
                          if ch in w]
                distinct = {round(float(v), 6) for v in values}
                if distinct and distinct != {0.0}:
                    continue                     # non-zero: authoritative
                if len(distinct) > 1:
                    continue                     # heterogeneous: authoritative
                provenance[ch] = ABSENT          # a real, unclaimed zero

    spec = {"groups": groups, "nucleus": nucleus,
            "enabled": sorted(enabled), "provenance": provenance,
            "channel_weight": {}}
    return spec, visibility
