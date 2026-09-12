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
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DatasetIdentity:
    """WHICH SLIDE. Stable across closing and re-opening the same file.

    `fingerprint` is the same `size:mtime_ns` rule the Step0 handoff already
    stamps into its manifest (`core.step0_handoff`), so "the same slide" means
    the same thing to the handoff and to the display state. A file that has
    been rewritten under the same name is a DIFFERENT identity, which is what
    stops a stale window being restored over new pixels.
    """

    path: str
    fingerprint: str = ""

    @property
    def resolved(self) -> bool:
        """True when the fingerprint was actually read.

        An unresolved identity is not a weaker identity, it is an ABSENCE:
        callers fail closed rather than guessing, because signing an unknown
        slide with the current one is exactly how one dataset's window ends up
        on another's pixels.
        """
        return bool(self.path) and bool(self.fingerprint)

    def __str__(self):
        return f"{os.path.basename(self.path) or '<none>'}@{self.fingerprint or '?'}"


def resolve_identity(path) -> Optional[DatasetIdentity]:
    """`DatasetIdentity` for `path`, or None when there is no path at all.

    A path that cannot be stat'd yields an identity with an EMPTY
    fingerprint. That is deliberate and is not the same as guessing one:

    * `resolved` is False, so anything that must not act on a guess can see
      it and say so;
    * the identity still compares equal to itself, so within THIS process run
      two visits to the same path share a namespace and a late result can
      still be matched or refused by value;
    * it can never compare equal to a resolved identity for the same path, so
      once the file does exist the state starts a clean namespace rather than
      inheriting one keyed on an absence.

    Refusing to produce anything here would have meant a slide whose file is
    not yet on disk -- a synthetic source, a test harness, a project being
    assembled -- had no display state at all, which is a worse failure than a
    namespace that is honest about what it does not know.
    """
    if not path:
        return None
    full = str(path)
    try:
        full = os.path.abspath(full)
        st = os.stat(full)
    except (OSError, TypeError, ValueError):
        return DatasetIdentity(path=full, fingerprint="")
    return DatasetIdentity(path=full,
                           fingerprint=f"{st.st_size}:{st.st_mtime_ns}")


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

    order: tuple = ()
    capabilities: dict = field(default_factory=dict)   # channel -> Capabilities
    selection: str = ""
    visibility: dict = field(default_factory=dict)     # channel -> bool
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
