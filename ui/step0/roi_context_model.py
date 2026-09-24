"""
block01/ui/step0/roi_context_model.py — v14.2b single authoritative ROI/context model.

Plain-data model owned by Step0Page. It is the ONE source of truth for the
ROI / patch / full-WSI-mode context shared between the Step0 main OverviewPanel
and the TissueNavigatorPopup OverviewPanel. Both panels are views/editors over
this model: an edit on either panel is adopted into this model, then BOTH panels
are re-rendered from it. The panels' internal `_rois`/`_patches` are only render
caches derived from this model — never the authority.

This object holds plain Python data only (no Qt widget, no OverviewPanel import),
so it cannot become a second widget-coupled ROI store. utils/roi_project.py is a
stateless serialization/id helper, not a stateful model, so it is not used as the
authority here.
"""

from __future__ import annotations


class Patch(tuple):
    """A patch rectangle `(y0, y1, x0, x1)` that also carries its identity.

    Still the 4-tuple every existing reader unpacks, converts and compares,
    so it travels through `patches_changed`, the popup, Step0's lists and the
    handoff untouched -- but it keeps its `id` and `name` on the way. Tuple
    equality compares the rectangle only; identity is compared explicitly
    wherever it matters (`same_patches`).

    Identity (user ruling, 2026-09-24): `id` is assigned once and never
    changes -- deleting a patch does not renumber the others -- and ids are
    never reused. `name` is what every view shows: `P{id}` until the user
    renames the patch.
    """

    def __new__(cls, coords, pid=None, name=None):
        self = super().__new__(cls, tuple(int(v) for v in coords))
        if len(self) != 4:
            raise ValueError(f"a patch is (y0, y1, x0, x1), got {coords!r}")
        self.id = None if pid is None else int(pid)
        self.name = str(name or "") or (default_patch_name(self.id)
                                        if self.id is not None else "")
        return self

    def __reduce__(self):
        return (Patch, (tuple(self), self.id, self.name))

    def with_coords(self, coords):
        return Patch(coords, self.id, self.name)

    def with_name(self, name):
        return Patch(tuple(self), self.id, name)

    def __repr__(self):
        return f"Patch({tuple(self)!r}, id={self.id!r}, name={self.name!r})"


def default_patch_name(pid):
    return f"P{int(pid)}"


def patch_id(patch):
    """The patch's permanent id, or None for a bare rectangle."""
    return getattr(patch, "id", None)


def patch_name(patch, index):
    """What a view shows for `patch` at position `index` (0-based)."""
    name = getattr(patch, "name", "")
    return name or default_patch_name(index + 1)


def patch_color_for_id(pid, index):
    """A patch's colour, fixed by its permanent id (user ruling, 2026-09-24):
    P3 keeps its colour when P2 is deleted. `P<n>` takes the (n-1)th colour,
    so a list that was never edited looks exactly as it did by position.
    `index` is only the fallback for a bare rectangle without an id."""
    from ...config import PATCH_COLORS
    key = int(pid) - 1 if pid is not None else int(index)
    return PATCH_COLORS[key % len(PATCH_COLORS)]


def patch_color(patch, index):
    """`patch_color_for_id` for a Patch (or a bare rectangle at `index`)."""
    return patch_color_for_id(patch_id(patch), index)


def same_patches(a, b):
    """Same rectangles AND the same identities, in the same order."""
    a, b = list(a or []), list(b or [])
    return (len(a) == len(b)
            and all(tuple(x) == tuple(y) and patch_id(x) == patch_id(y)
                    and getattr(x, "name", "") == getattr(y, "name", "")
                    for x, y in zip(a, b)))


def with_patch_ids(patches, next_id=1):
    """`patches` as `Patch` objects, giving an id to every one that has none.

    Ids are handed out from `next_id` upwards in list order, so a list that
    predates stable ids (every old project) keeps the numbers it was shown
    with: P1..Pn. Returns `(patches, next_id)` with `next_id` above every id
    in the list.
    """
    out = []
    ids = [patch_id(p) for p in patches or [] if patch_id(p) is not None]
    nxt = max([int(next_id or 1)] + [i + 1 for i in ids])
    for p in patches or []:
        pid = patch_id(p)
        if pid is None:
            pid, nxt = nxt, nxt + 1
        out.append(Patch(p, pid, getattr(p, "name", "")))
    return out, nxt


def patch_from_record(item):
    """A `Patch` from a persisted record (dict) or a bare rectangle."""
    if isinstance(item, dict):
        coords = item.get("bbox_fullres") or item.get("coords")
        if not coords or len(coords) != 4:
            return None
        return Patch(coords, item.get("id"), item.get("name") if item.get("id") is not None else "")
    if item is not None and len(item) == 4:
        return Patch(item, patch_id(item), getattr(item, "name", ""))
    return None


class RoiContextModel:
    """Authoritative ROI/context state: rois, patches, full_wsi_mode, loader, nuc."""

    def __init__(self):
        self.loader = None
        self.nucleus_channel = ""
        self.rois = []            # list[dict]  — ROI dicts (name/polygon/color/...)
        self.patches = []         # list[Patch] — (y0, y1, x0, x1) + id/name
        self.full_wsi_mode = False
        #: The next patch id to hand out. Only ever raised: an id, once used,
        #: is never given to another patch (user ruling, 2026-09-24).
        self.next_patch_id = 1

    # ── adoption (write-back from whichever panel was edited) ─────────────
    def adopt_rois(self, rois):
        self.rois = [dict(r) for r in (rois or [])]

    def adopt_patches(self, patches):
        self.patches, self.next_patch_id = with_patch_ids(
            patches, self.next_patch_id)

    def allocate_patch_id(self):
        """A fresh id for a patch that is being created. Never reused."""
        pid = int(self.next_patch_id)
        self.next_patch_id = pid + 1
        return pid

    def raise_next_patch_id(self, value):
        """Take a baseline from elsewhere (the published handoff). Never lowers."""
        try:
            self.next_patch_id = max(int(self.next_patch_id), int(value or 1))
        except (TypeError, ValueError):
            pass
        return self.next_patch_id

    def reset_patch_ids(self):
        """A new dataset: its own numbering, from its own baseline."""
        self.next_patch_id = 1

    def adopt(self, rois=None, patches=None, full_wsi_mode=None,
              loader=Ellipsis, nucleus_channel=Ellipsis):
        """Adopt the authoritative state. Only provided fields are replaced.

        loader / nucleus_channel use Ellipsis as the "unset" sentinel so an
        explicit None (clearing the loader) is still honored.
        """
        if rois is not None:
            self.adopt_rois(rois)
        if patches is not None:
            self.adopt_patches(patches)
        if full_wsi_mode is not None:
            self.full_wsi_mode = bool(full_wsi_mode)
        if loader is not Ellipsis:
            self.loader = loader
        if nucleus_channel is not Ellipsis:
            self.nucleus_channel = nucleus_channel or ""

    def clear_rois_and_patches(self):
        self.rois = []
        self.patches = []

    # ── read-only views ──────────────────────────────────────────────────
    def roi_count(self):
        return len(self.rois)

    def snapshot(self):
        """Plain dict copy for feeding a panel (rois/patches are fresh copies)."""
        return {
            "loader": self.loader,
            "nucleus_channel": self.nucleus_channel,
            "rois": [dict(r) for r in self.rois],
            "patches": list(self.patches),
            "full_wsi_mode": self.full_wsi_mode,
            "next_patch_id": self.next_patch_id,
        }
