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

import copy

from PyQt5.QtCore import QObject, pyqtSignal

# Provenance of a channel's scientific weight. See the module docstring.
ABSENT = "absent"
AUTO = "auto"
EXPLICIT = "explicit"
AUTHORITATIVE = "authoritative"

#: What the first enable answers when nobody has weighted the channel yet.
FIRST_ENABLE_WEIGHT = 1.0


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

    # ── ports ─────────────────────────────────────────────────────────
    def set_hash_provider(self, fn):
        """Register the EXISTING settings-hash function.

        The hash is the fusion config plus the display mapping, canonicalised
        and digested by Step1's own algorithm, and old projects are named by
        it. This model derives `Unsaved` from it; it does not invent a second
        one. `fn(draft_or_None) -> str`.
        """
        self._hash_provider = fn

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
        if self._installing:
            return True
        if weighted:
            self.weight_changed.emit(channel)
        self.participation_changed.emit(channel, enabled)
        self.draft_changed.emit()
        return True

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
        name = str(name)
        data = self._groups.setdefault(name, {"weight": float(group_weight),
                                              "members": []})
        data["weight"] = float(group_weight)
        for ch, w in (channel_weights or {}).items():
            self.add_to_group(name, ch, w)
        return data

    def add_to_group(self, name, channel, weight=0.0):
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
        pending = self._channel_weight.get(channel)
        if pending is not None and self.weight_provenance(channel) != ABSENT:
            weight = pending
        self._group_weights.setdefault(name, {})[channel] = float(weight)

    def remove_group(self, name):
        name = str(name)
        self._groups.pop(name, None)
        self._group_weights.pop(name, None)

    def group_weight(self, name):
        data = self._groups.get(str(name))
        return float(data["weight"]) if data else 1.0

    def set_group_weight(self, name, weight):
        data = self._groups.setdefault(str(name), {"weight": 1.0,
                                                   "members": []})
        data["weight"] = float(weight)

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
        self._nucleus_channel = str(channel or "")
        if weight is not None:
            self._nucleus_weight = float(weight)

    def set_nucleus_weight(self, weight):
        self._nucleus_weight = float(weight)

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
        """
        keep = {str(ch) for ch in (channels or [])}
        if not keep:
            return False
        keep.add(self._nucleus_channel)
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
        return moved

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
        self._installing = True
        try:
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
        finally:
            self._installing = False
        self.draft_restored.emit()
        self.draft_changed.emit()

    # ── committed snapshot ────────────────────────────────────────────
    def install_committed_snapshot(self, snapshot):
        """Adopt the frozen scientific fact. Immutable here: a copy goes in,
        a copy comes out, and a failed commit never reaches this method."""
        self._committed = copy.deepcopy(snapshot) if snapshot else None
        self.committed_changed.emit()

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
        spec = {
            "groups": draft.get("groups") or groups,
            "nucleus": draft.get("nucleus") or nucleus,
            "enabled": draft.get("enabled") or [],
            "provenance": draft.get("provenance") or {},
            "channel_weight": draft.get("channel_weight") or {},
        }
        visibility = {str(ch): bool(v) for ch, v in
                      (sess.get("display_visibility")
                       or sess.get("channel_visibility") or {}).items()}
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
