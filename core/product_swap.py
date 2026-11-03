"""Step0 Save in place (docs/v17_save_inplace_step1_ahead_plan.md §A2, §A3b, §8).

A Save overwrites the workspace's corrected product instead of copying it
into a new correct run (user ruling 2026-11-02). The channels it changes are
computed into a STAGING product beside the live one -- `<run>/.staging/`,
laid out like the run itself (`corrected_channels.zarr`, the coarse sidecar,
the display layers). Until the run has finished the live product is not
touched, so a cancel or an error only deletes the staging folder.

Then, on the GUI thread, the staged channels are MOVED IN (`swap_in`,
`swap_whole`, `remove_channels`): renames, each recorded in a journal before
any is made, the replaced live channels kept in `<run>/.swap_old/`. The move
stays undoable until the Save has published its handoff:

* `commit` -- the Save is complete: what it replaced, the journal and the
  staging go;
* `rollback` -- the Save did not complete: every live channel is put back as
  it was (moved-in channels removed, replaced ones returned, root attributes
  restored);
* `recover` -- on opening a workspace: a journal left by a crash means the
  Save never completed, so it is rolled back; staging or old folders without
  a journal are leftovers and go.

Within one channel the coarse plane and display layers move before level 0:
in between, their identity token does not match level 0's and Step1 refuses
them (it reduces at runtime), never the other way round.
"""

import json
import os
import shutil

import zarr

PRODUCT = "corrected_channels.zarr"
COARSE = "corrected_coarse.zarr"           # = viewer.step1_source.COARSE_SIDECAR_DIRNAME
DISPLAY = "corrected_display.zarr"         # = viewer.display_pyramid.DISPLAY_DIRNAME
#: the moves within one channel, level 0 LAST
ORDER = (COARSE, DISPLAY, PRODUCT)
STAGING = ".staging"
OLD = ".swap_old"
JOURNAL = "swap_journal.json"


def staging_dir(run_dir):
    return os.path.join(str(run_dir), STAGING)


def new_staging(run_dir):
    """An empty staging folder (a leftover of an unfinished Save goes)."""
    path = staging_dir(run_dir)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def discard_staging(run_dir):
    shutil.rmtree(staging_dir(run_dir), ignore_errors=True)


def pending(run_dir):
    """A move that is neither committed nor rolled back."""
    return os.path.isfile(os.path.join(str(run_dir), JOURNAL))


def groups(product_path):
    """The ROI groups of a corrected product ([] when there is none)."""
    try:
        return list(zarr.open_group(str(product_path), mode="r").group_keys())
    except Exception:                                       # noqa: BLE001
        return []


def _root_attrs(run_dir):
    path = os.path.join(run_dir, PRODUCT)
    if not os.path.isdir(path):
        return None
    try:
        return dict(zarr.open_group(path, mode="r").attrs)
    except Exception:                                       # noqa: BLE001
        return None


def _write_journal(run_dir, journal):
    path = os.path.join(run_dir, JOURNAL)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _move(run_dir, pairs, whole=False):
    """Journal, then rename: each replaced live folder to its backup, each
    staged folder (src) to its live place (dst); src None only removes."""
    if pending(run_dir):
        raise RuntimeError(f"an earlier Save's move in {run_dir} was not settled")
    entries = []
    for i, (src, dst) in enumerate(p for p in pairs if p[0] is None or os.path.isdir(p[0])):
        backup = os.path.join(run_dir, OLD, str(i)) if os.path.isdir(dst) else None
        entries.append({"src": src, "dst": dst, "backup": backup})
    _write_journal(run_dir, {"entries": entries, "root_attrs": _root_attrs(run_dir),
                             "whole": bool(whole)})
    os.makedirs(os.path.join(run_dir, OLD), exist_ok=True)
    for e in entries:
        if e["backup"] is not None:
            os.rename(e["dst"], e["backup"])
        if e["src"] is not None:
            os.makedirs(os.path.dirname(e["dst"]), exist_ok=True)
            os.rename(e["src"], e["dst"])


def swap_in(run_dir, channels, remove=()):
    """Move the staged `channels` [(group, channel)] into the live product
    and take `remove` [(group, channel)] out of it; the staged product's root
    attributes (settings, ROI names, versions) replace the live ones.
    Undoable until `commit`."""
    run_dir = os.path.abspath(str(run_dir))
    staged = staging_dir(run_dir)
    pairs = []
    for group, ch in channels:
        for name in ORDER:
            src = os.path.join(staged, name, group, ch)
            if os.path.isdir(src):
                pairs.append((src, os.path.join(run_dir, name, group, ch)))
    for group, ch in remove:
        for name in ORDER:
            dst = os.path.join(run_dir, name, group, ch)
            if os.path.isdir(dst):
                pairs.append((None, dst))
    # the live groups the channels move into (made like the staged ones)
    for group, _ch in channels:
        for name in ORDER:
            src_root = os.path.join(staged, name)
            if os.path.isdir(os.path.join(src_root, group)):
                attrs = dict(zarr.open_group(src_root, mode="r")[group].attrs)
                live = zarr.open_group(os.path.join(run_dir, name), mode="a")
                live.require_group(group).attrs.update(attrs)
    _move(run_dir, pairs)
    staged_root = os.path.join(staged, PRODUCT)
    if channels and os.path.isdir(staged_root) and os.path.isdir(os.path.join(run_dir, PRODUCT)):
        attrs = dict(zarr.open_group(staged_root, mode="r").attrs)
        attrs["output_dir"] = run_dir
        zarr.open_group(os.path.join(run_dir, PRODUCT), mode="a").attrs.update(attrs)


def swap_whole(run_dir):
    """The staged product replaces the live one as a whole (a new analysis
    region, or nothing corrected any more). Undoable until `commit`."""
    run_dir = os.path.abspath(str(run_dir))
    staged = staging_dir(run_dir)
    pairs = [(os.path.join(staged, name) if os.path.isdir(os.path.join(staged, name)) else None,
              os.path.join(run_dir, name)) for name in ORDER]
    _move(run_dir, pairs, whole=True)
    product = os.path.join(run_dir, PRODUCT)
    if os.path.isdir(product):
        zarr.open_group(product, mode="a").attrs["output_dir"] = run_dir


def remove_channels(run_dir, remove):
    """Channels withdrawn from correction leave the product (no staging).
    Undoable until `commit`."""
    run_dir = os.path.abspath(str(run_dir))
    pairs = [(None, os.path.join(run_dir, name, group, ch))
             for group, ch in remove for name in ORDER
             if os.path.isdir(os.path.join(run_dir, name, group, ch))]
    _move(run_dir, pairs)


def apply_plan(run_dir, plan):
    """The move a `WsiCorrectionWorker.swap_plan` describes (None: none)."""
    if not plan:
        return False
    if plan[0] == "whole":
        swap_whole(run_dir)
    else:
        _kind, channels, remove = plan
        if channels:
            swap_in(run_dir, channels, remove)
        elif remove:
            remove_channels(run_dir, remove)
        else:
            return False
    return True


def _cleanup(run_dir):
    try:
        os.remove(os.path.join(run_dir, JOURNAL))
    except FileNotFoundError:
        pass
    shutil.rmtree(os.path.join(run_dir, OLD), ignore_errors=True)
    discard_staging(run_dir)


def commit(run_dir):
    """The Save is complete: what it replaced goes."""
    _cleanup(os.path.abspath(str(run_dir)))


def rollback(run_dir):
    """The Save did not complete: the live product is put back as it was.
    Safe after a crash at any point of the move. True when there was one."""
    run_dir = os.path.abspath(str(run_dir))
    path = os.path.join(run_dir, JOURNAL)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            journal = json.load(f) or {}
    except (OSError, ValueError):
        journal = {}
    for e in reversed(journal.get("entries") or []):
        src, dst, backup = e.get("src"), e.get("dst"), e.get("backup")
        if backup is not None:
            if os.path.isdir(backup):           # it was moved away: back it comes
                if os.path.isdir(dst):
                    shutil.rmtree(dst)          # what was moved in instead
                os.rename(backup, dst)
            # else: never moved -- dst is still the original
        elif src is not None and os.path.isdir(dst):
            shutil.rmtree(dst)                  # a channel that was not there before
    attrs = journal.get("root_attrs")
    product = os.path.join(run_dir, PRODUCT)
    if attrs is not None and os.path.isdir(product):
        root = zarr.open_group(product, mode="a")
        root.attrs.clear()
        root.attrs.update(attrs)
    _cleanup(run_dir)
    return True


def recover(run_dir):
    """On opening (or before a Save): a journal means a Save that never
    completed -- rolled back; leftovers without one go. True when anything
    was done."""
    run_dir = os.path.abspath(str(run_dir))
    if rollback(run_dir):
        return True
    did = any(os.path.isdir(p) for p in (staging_dir(run_dir), os.path.join(run_dir, OLD)))
    _cleanup(run_dir)
    return did
