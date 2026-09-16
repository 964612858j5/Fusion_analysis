"""Which slide a display answer is about, and which binding produced it.

WHY TWO THINGS AND NOT ONE. Until B2 the shared display state keyed everything
on one value, `(monotonic generation, absolute path)`. That single value was
doing two incompatible jobs:

* saying WHICH SLIDE a stored window belongs to -- for which the generation is
  noise, because A is still A the second time it is opened;
* saying WHICH BINDING a background task was started under -- for which the
  path is useless, because A→B→A visits the same path twice.

Mixing them meant the identity changed every time the user re-opened the same
slide, so walking A→B→A found an empty namespace and re-seeded every display
window from pixels the state already had the answer for. Splitting them is
what makes "go back to A and everything is as you left it" possible while
still refusing a result computed under A's FIRST binding.

    DatasetIdentity   stable   canonical path + source fingerprint
    DisplayBinding    transient identity + a generation that never repeats

No Qt here: these are values, comparable and hashable, so a namespace can be
keyed by one and a worker can carry the other across a thread boundary.
"""

import os
import uuid
from dataclasses import dataclass, field
from typing import Optional


EPHEMERAL_PREFIX = "ephemeral:"


@dataclass(frozen=True)
class DatasetIdentity:
    """WHICH SLIDE. Stable across closing and re-opening the same file.

    `fingerprint` is the same `size:mtime_ns` rule the Step0 handoff already
    stamps into its manifest (`core.step0_handoff`), so "the same slide" means
    the same thing to the handoff and to the display state. A file that has
    been rewritten under the same name is a DIFFERENT identity, which is what
    stops a stale window being restored over new pixels.

    An identity whose fingerprint begins with `ephemeral:` is NOT a verified
    slide identity: see `ephemeral_identity`.
    """

    path: str
    fingerprint: str = ""

    @property
    def ephemeral(self) -> bool:
        """True when this identity is a one-bind nonce, not a source version."""
        return self.fingerprint.startswith(EPHEMERAL_PREFIX)

    @property
    def resolved(self) -> bool:
        """True when this identity actually describes a source VERSION.

        A path alone is not an identity: two sources at the same path that
        cannot be shown to be the same file are not the same dataset, and a
        namespace keyed on the path would restore one's display state over
        the other's pixels. An ephemeral nonce is deliberately not resolved.
        """
        return (bool(self.path) and bool(self.fingerprint)
                and not self.ephemeral)

    def __str__(self):
        kind = "ephemeral" if self.ephemeral else (
            self.fingerprint or "unresolved")
        return f"{os.path.basename(self.path) or '<none>'}@{kind}"


def resolve_identity(path) -> Optional[DatasetIdentity]:
    """`DatasetIdentity` for `path`, or None when it cannot be established.

    FAIL CLOSED. A path that cannot be stat'd has no source version, so there
    is nothing to key a restorable namespace on: two sources at that same
    path cannot be shown to be the same dataset, and treating them as one
    would restore the first's display state over the second's pixels. The
    caller decides what to do with None -- `ephemeral_identity` is the answer
    for "show something now, promise nothing across binds".

    A caller with a synthetic source that DOES want restoration across binds
    supplies its own non-empty fingerprint describing that source's version,
    through `DatasetIdentity` directly.
    """
    if not path:
        return None
    try:
        full = os.path.abspath(str(path))
        st = os.stat(full)
    except (OSError, TypeError, ValueError):
        return None
    return DatasetIdentity(path=full,
                           fingerprint=f"{st.st_size}:{st.st_mtime_ns}")


def ephemeral_identity(path) -> DatasetIdentity:
    """A ONE-BIND identity for a source whose version cannot be established.

    It keeps a slide that is not (yet) on disk usable -- a synthetic source, a
    test harness, a project being assembled still gets display state for as
    long as it is bound. What it deliberately does NOT do is promise
    restoration: every call produces a different nonce, so binding the same
    unresolvable path twice yields two identities, two namespaces, and no
    chance of one visit's answers being shown as another's.

    The nonce is a `uuid4`, not an object `id()`: an address is reused by the
    next object and would make two unrelated binds compare equal.
    """
    full = os.path.abspath(str(path)) if path else ""
    return DatasetIdentity(path=full,
                           fingerprint=f"{EPHEMERAL_PREFIX}{uuid.uuid4().hex}")


@dataclass(frozen=True)
class DisplayBinding:
    """WHICH BINDING. One identity, plus a generation that never repeats.

    A background task carries this. Coming back, it is accepted only when the
    identity still matches AND the generation is still the current one -- so
    work started under A's first binding is refused after A→B→A even though
    the identity says A both times.
    """

    identity: DatasetIdentity
    generation: int = 0

    def __str__(self):
        return f"{self.identity}#{self.generation}"


@dataclass
class DisplayNamespace:
    """Everything display-scoped that belongs to ONE dataset identity.

    Colours are deliberately NOT here: this product treats a channel's colour
    as a preference held by channel NAME for the process (see the plan, 2.1),
    so it survives a dataset switch on purpose.

    `revisions` counts writes per field key, so a late task can say "I was
    computed while this field was at revision N, or while it was absent" and
    be refused when the user has moved it since.
    """

    # The generation of the LATEST formal bind of this identity. A result
    # carries the binding it started under; comparing it against this, rather
    # than against whatever is bound NOW, is what makes an old visit's work
    # permanently stale. Compared only with the current binding, the sequence
    # A1 -> B -> A2 -> C -> A1-returns let A1 write back into resident A,
    # because at that moment A was "some other dataset" and its own second
    # visit was invisible to the check.
    binding_generation: int = 0
    order: tuple = ()
    capabilities: dict = field(default_factory=dict)   # channel -> Capabilities
    selection: str = ""
    visibility: dict = field(default_factory=dict)     # channel -> bool
    # PER-STEP display answers (user ruling, 2026-09-16). `selection` and
    # `visibility` above are the SHARED pair, which the steps that were never
    # separated keep using; a step with its own scope keeps its tick and its
    # current channel here, so looking at CD3 in Step0 does not tick it in
    # Step1. Colours, Min/Max/Gamma, the channel order and the dock itself
    # stay shared -- those are answers about the slide, not about a step.
    #
    #   {scope name: {"selection": str, "visibility": {channel: bool}}}
    scopes: dict = field(default_factory=dict)
    mappings: dict = field(default_factory=dict)       # (channel, nucleus) -> (lo, hi, gamma)
    revisions: dict = field(default_factory=dict)      # field key -> int

    def bump(self, key):
        self.revisions[key] = self.revisions.get(key, 0) + 1
        return self.revisions[key]

    def revision(self, key):
        return self.revisions.get(key, 0)


@dataclass(frozen=True)
class ChannelCapabilities:
    """What may be done to a channel, as SEPARATE facts.

    One `locked` boolean used to stand for four different decisions at once --
    "this is the nucleus", "it is never background-corrected", "its method
    combo is disabled" and "bulk show/hide skips it" -- so a page that wanted
    one of them got all four. They are named apart here; B4 is where Step0
    stops deriving them from each other.
    """

    is_nucleus: bool = False
    display_toggleable: bool = True
    weight_editable: bool = True
    correction_eligible: bool = True
    #: May a bulk Show all / Hide all include this channel. Separate from
    #: `display_toggleable`, because "the user may show this one" and "a
    #: sweep over every channel may move it" are different permissions: the
    #: nucleus layer is shown and hidden deliberately and is not swept.
    bulk_toggleable: bool = True
    #: May this channel be put into or taken out of the fusion. The nucleus
    #: is always in it; a marker is the user's choice.
    fusion_toggleable: bool = True
