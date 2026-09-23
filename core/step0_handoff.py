"""Writing the Step0 handoff: the file IO, with no window attached.

WHY THIS IS A MODULE. `Step0Page._write_step0_handoff` did three different
things in one body -- read the page's geometry, write five artifacts, and
update labels -- so the writing could only happen on the GUI thread. A patch
edit therefore paid three JSON writes, a zarr attribute pass, a validating
scan of the corrected output and an fsync inside the release callback, which
is why letting go of a dragged patch froze the window.

So the middle third lives here and takes a plain SPEC: paths, geometry and
numbers the caller has already gathered. One implementation, two callers --
the page's own Save and the background persist worker -- because a second
copy of a writer is how the screen and the file on disk drift apart.

ATOMICITY, AND WHY EVERY ARTIFACT GETS IT NOW. The manifest was already
published through tmp+fsync+replace as the final marker; the other artifacts
were written in place. With persistence moved to a worker that is allowed to
be superseded mid-run, an in-place write is a revision leaking onto disk that
no manifest ever announced -- so every artifact is now staged under a
revision-tagged temporary name and replaced only at publication, in one place,
manifest last. A superseded task deletes its own temporaries and publishes
nothing.
"""

import hashlib
import json
from contextlib import nullcontext
import os
import secrets
import shutil

import zarr

from .bg_correction import (
    # The correction domain owns what a FINAL decision may be; the handoff
    # re-exports the three names because it is the boundary that publishes
    # them, and every writer already reaches for them through here.
    FINAL_CORRECTION_DECISIONS,
    PREVIEW_METHODS,
    is_final_correction_decision,
    is_preview_method,
    migrate_correction_decision,
    corrected_zarr_report,
    stamp_corrected_zarr_provenance,
    CORRECTED_ZARR_OUTPUT_KIND,
    CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
)
from ..config import CUCIM_SIGMA_DEFAULT, TOPHAT_RADIUS_DEFAULT
from ..utils.channel_remap_config import channel_remap_config_hash
from ..utils.roi_project import mark_roi_step
from ..utils import perf_trace


class Superseded(Exception):
    """Raised inside a write whose task has been replaced by a newer one.

    Not an error: the newer task describes the same geometry plus the edit
    that replaced it, so the only correct thing to publish is that one.
    """


def _write_json_staged(path, payload, tag):
    """Stage one JSON artifact next to its destination. Returns the tmp path."""
    tmp = f"{path}.tmp.{tag}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def ensure_empty_corrected_zarr(zarr_path, rois, *, source_ome,
                                analysis_region_type, roi_id="", roi_dir="",
                                out_dir=""):
    """Create the corrected zarr's skeleton: groups and attributes, no pixels."""
    if os.path.exists(zarr_path):
        shutil.rmtree(zarr_path, ignore_errors=True)
    out_dir = out_dir or os.path.dirname(zarr_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    root = zarr.open_group(zarr_path, mode="w")
    root.attrs["mode"] = "roi_only"
    root.attrs["analysis_region_type"] = analysis_region_type
    root.attrs["source_ome"] = os.path.abspath(source_ome) if source_ome else ""
    root.attrs["output_dir"] = os.path.abspath(out_dir)
    if roi_id or roi_dir:
        root.attrs["roi_id"] = roi_id
        root.attrs["roi_dir"] = os.path.abspath(roi_dir) if roi_dir else ""
    root.attrs["roi_names"] = [r.get("name", f"ROI_{i}")
                               for i, r in enumerate(rois, start=1)]
    root.attrs["created_by"] = "Step0"
    for idx, roi in enumerate(rois, start=1):
        name = str(roi.get("name") or f"ROI_{idx}")
        group = root.create_group(name, overwrite=True)
        group.attrs["roi_name"] = name
        group.attrs["analysis_region_type"] = analysis_region_type
        group.attrs["bbox_fullres"] = roi.get("bbox_fullres") or []
        group.attrs["polygon_fullres"] = roi.get("polygon_fullres") or []
        group.attrs["shape"] = roi.get("shape") or _shape_from_bbox(
            roi.get("bbox_fullres"))


def _shape_from_bbox(bbox):
    if not bbox or len(bbox) != 4:
        return [0, 0]
    y0, y1, x0, x1 = [int(v) for v in bbox]
    return [max(0, y1 - y0), max(0, x1 - x0)]


def write_handoff(spec, *, superseded=None, tag="0", publication_lock=None):
    """Write and publish one Step0 handoff. Returns a result dict.

    `spec` is plain data -- see `Step0Page._handoff_spec` for the fields.
    `superseded` is an optional predicate, consulted before anything is
    published; when it says yes, the staged temporaries are removed, nothing
    on disk changes and `Superseded` is raised. `tag` isolates this task's
    temporary files from any other task's, so two revisions cannot stage over
    each other.

    The GUI-visible consequences are NOT applied here -- they are in the
    result (`corrected_report`, `manifest`) for the caller's thread to apply.
    """
    raw_path = spec["raw_path"]
    if not raw_path:
        raise RuntimeError("raw OME-TIFF path is empty")
    step0_dir = spec["step0_dir"]
    os.makedirs(step0_dir, exist_ok=True)
    config = spec["config"]
    rois = spec["rois"]
    patches = spec["patches"]
    corrected_path = spec["corrected_path"]
    manifest_path = spec["manifest_path"]
    analysis_region_type = spec["analysis_region_type"]
    roi_id = spec.get("roi_id", "")
    roi_dir = spec.get("roi_dir", "")
    project_dir = spec.get("project_dir", "")

    corr_path = os.path.join(step0_dir, "correction_config.json")
    roi_path = os.path.join(step0_dir, "roi_config.json")
    patch_path = os.path.join(step0_dir, "patch_config.json")

    def _check(phase):
        """Ask whether this task still matters. `phase` names WHERE we are --
        the last one is "publish", the only check that can still be reached
        with every artifact staged and nothing durable touched."""
        if superseded is not None and superseded(phase):
            raise Superseded()

    staged = []
    try:
        with perf_trace.span("handoff.write_json", files=3):
            for path, payload in ((corr_path, config), (roi_path, rois),
                                  (patch_path, patches)):
                staged.append((_write_json_staged(path, payload, tag), path))
        _check("staged_json")

        if not os.path.exists(corrected_path):
            ensure_empty_corrected_zarr(
                corrected_path, rois, source_ome=raw_path,
                analysis_region_type=analysis_region_type, roi_id=roi_id,
                roi_dir=roi_dir, out_dir=step0_dir)

        if os.path.exists(corrected_path):
            try:
                root = zarr.open_group(corrected_path, mode="a")
                root.attrs["mode"] = "roi_only"
                root.attrs["analysis_region_type"] = analysis_region_type
                root.attrs["source_ome"] = raw_path
                root.attrs["output_dir"] = os.path.abspath(step0_dir)
                root.attrs["project_output_dir"] = (
                    os.path.abspath(project_dir) if project_dir else "")
                root.attrs["roi_id"] = roi_id
                root.attrs["roi_dir"] = (os.path.abspath(roi_dir)
                                         if roi_dir else "")
                root.attrs["roi_names"] = [
                    r.get("name", f"ROI_{i}")
                    for i, r in enumerate(rois, start=1)]
                root.attrs["created_by"] = "Step0"
                # v14.4: honest preprocessing provenance (NOT step2_ready).
                stamp_corrected_zarr_provenance(root)
                for roi in rois:
                    name = str(roi.get("name") or "")
                    group = root[name] if name and name in root else None
                    if group is None:
                        for group_name in root.group_keys():
                            candidate = root[group_name]
                            if str(candidate.attrs.get("roi_name")
                                   or group_name) == name:
                                group = candidate
                                break
                    if group is not None:
                        group.attrs["roi_name"] = name
                        group.attrs["analysis_region_type"] = (
                            analysis_region_type)
                        group.attrs["bbox_fullres"] = (
                            roi.get("bbox_fullres") or [])
                        group.attrs["polygon_fullres"] = (
                            roi.get("polygon_fullres") or [])
                        group.attrs["shape"] = roi.get("shape") or (
                            _shape_from_bbox(roi.get("bbox_fullres")))
            except Superseded:
                raise
            except Exception as e:
                print(f"[Step0] failed to update corrected zarr attrs: {e}")
                raise RuntimeError(
                    "failed to commit corrected zarr handoff metadata") from e
        _check("zarr_attrs")

        # v14.4: validate the corrected output (a directory existing is NOT
        # proof of a valid corrected zarr) and report it honestly to the UI +
        # manifest.
        with perf_trace.span("handoff.zarr_report"):
            corrected_report = corrected_zarr_report(corrected_path)

        try:
            raw_stat = os.stat(raw_path)
            raw_fingerprint = f"{raw_stat.st_size}:{raw_stat.st_mtime_ns}"
        except OSError as exc:
            raise RuntimeError(
                f"raw OME-TIFF identity could not be read: {exc}") from exc
        source_identity = {
            "dataset_path": raw_path,
            "dataset_fingerprint": raw_fingerprint,
            "stage": "raw",
            "corrected_artifact": None,
        }
        # The manifest names the remap config it is hashed over. A display
        # mapping commit passes an immutable, hash-named file so that
        # publishing the manifest is the only moment anything changes for a
        # consumer.
        remap_path = spec["remap_path"]
        remap_hash = ""
        if os.path.exists(remap_path):
            try:
                with open(remap_path, "r", encoding="utf-8") as f:
                    remap_hash = channel_remap_config_hash(json.load(f))
            except Exception as exc:
                raise RuntimeError(
                    f"invalid Step0 remap config at {remap_path}: "
                    f"{exc}") from exc

        manifest = {
            "version": "v6_roi_handoff_1",
            "handoff_schema_version": 2,
            "created_from_step": CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
            "output_kind": CORRECTED_ZARR_OUTPUT_KIND,
            "corrected_zarr_valid": bool(corrected_report["non_empty"]),
            "corrected_zarr_n_channel_arrays": int(
                corrected_report["n_channel_arrays"]),
            "roi_id": roi_id,
            "display_name": rois[0]["name"] if rois else "",
            "analysis_region_type": analysis_region_type,
            "mode": ("full_wsi" if analysis_region_type == "full_wsi"
                     else "roi_only"),
            "project_output_dir": (os.path.abspath(project_dir)
                                   if project_dir else ""),
            "roi_dir": os.path.abspath(roi_dir) if roi_dir else "",
            "step0_dir": os.path.abspath(step0_dir),
            "step1_dir": spec.get("step1_dir", ""),
            "step2_dir": spec.get("step2_dir", ""),
            "output_dir": os.path.abspath(step0_dir),
            "raw_ome_path": raw_path,
            "panel_csv_path": spec.get("panel_csv_path", ""),
            "panel_groups": dict(spec.get("panel_groups") or {}),
            "panel_nucleus": spec.get("nucleus_channel", ""),
            "source_identity": source_identity,
            "channel_remap_config_path": remap_path,
            "channel_remap_config_hash": remap_hash,
            "corrected_decisions": {
                str(ch): str(method).strip().lower()
                for ch, method in (config.get("channel_decisions")
                                   or {}).items()
                if str(method).strip().lower() in {"tophat", "cucim"}
            },
            "nucleus_channel": spec.get("nucleus_channel", ""),
            "corrected_zarr_path": os.path.abspath(corrected_path),
            "correction_config_path": os.path.abspath(corr_path),
            "roi_config_path": os.path.abspath(roi_path),
            "patch_config_path": os.path.abspath(patch_path),
            "active_roi": rois[0]["name"] if rois else "",
            "bbox_fullres": rois[0].get("bbox_fullres", []) if rois else [],
            "shape": rois[0].get("shape", []) if rois else [],
            "n_rois": len(rois),
            "n_patches": len(patches),
            # The patch-id baseline (ids are never reused): above every id
            # this Save writes, and never below what the handoff on disk
            # already promised.
            "next_patch_id": next_patch_id(
                (published_handoff(step0_dir) or (None,) * 5)[4], patches,
                spec.get("next_patch_id")),
            # Carried through a full Save as well: it is the baseline every
            # later geometry revision is numbered above, and a Save that
            # dropped it would hand the next patch edit a number that a file
            # on disk already uses. Never LOWERED either -- the page's counter
            # starts at zero every time the application does, and a Save from
            # a page that has not adopted the published baseline would
            # otherwise reset it.
            "geometry_revision": max(
                int(spec.get("geometry_revision") or 0),
                published_revision(step0_dir)),
        }
        manifest["step0_roi_result_path"] = os.path.abspath(manifest_path)
        manifest_tmp = f"{manifest_path}.tmp.{tag}"
        with open(manifest_tmp, "w", encoding="utf-8") as f, \
                perf_trace.span("handoff.manifest_write"):
            json.dump(manifest, f, indent=2, ensure_ascii=False)
            f.flush()
            with perf_trace.span("handoff.fsync"):
                os.fsync(f.fileno())
        staged.append((manifest_tmp, manifest_path))

        # PUBLICATION. Last possible moment to find out this task has been
        # replaced: everything above is a temporary file, so a Superseded here
        # leaves the published handoff exactly as the previous revision left
        # it, and the newer task publishes the newer geometry.
        # Full Save and geometry-only persistence share this tiny critical
        # section. The supersession check must happen AFTER acquiring it: a
        # synchronous Save may have become authoritative while this worker was
        # waiting to publish.
        with (publication_lock if publication_lock is not None else nullcontext()):
            _check("publish")
            with perf_trace.span("handoff.publish_replace", files=len(staged)):
                for tmp, dest in staged:
                    os.replace(tmp, dest)
        staged = []
    finally:
        for tmp, _dest in staged:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    print(f"[Step0] step0_roi_result={manifest_path}")
    if roi_id and project_dir:
        try:
            mark_roi_step(project_dir, roi_id, "step0", "done")
        except Exception as e:
            print(f"[Step0] failed to update ROI index: {e}")
            # The manifest above is the authoritative handoff commit marker.
            # ROI index bookkeeping is auxiliary; a stale index must not turn
            # a durable handoff into a reported save failure.
    return {"config": config, "rois": rois, "patches": patches,
            "manifest": manifest, "corrected_report": corrected_report,
            "manifest_path": os.path.abspath(manifest_path)}


def published_handoff(step0_dir):
    """The handoff already on disk, or None. A pair of file reads.

    On the worker's thread, deliberately: reading the manifest and the
    correction config used to happen inside the patch-release callback, where
    two more file reads (`published_geometry`) followed it.
    """
    if not step0_dir:
        return None
    manifest_path = os.path.join(step0_dir, "step0_roi_result.json")
    corr_path = os.path.join(step0_dir, "correction_config.json")
    if not (os.path.exists(manifest_path) and os.path.exists(corr_path)):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(corr_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as exc:                                # noqa: BLE001
        print(f"[Step0] cannot read the published handoff: {exc}")
        return None
    if not isinstance(manifest, dict) or not isinstance(config, dict):
        return None
    zarr_path = str(manifest.get("corrected_zarr_path")
                    or os.path.join(step0_dir, "corrected_channels.zarr"))
    return step0_dir, manifest_path, zarr_path, config, manifest


def geometry_bboxes(items):
    out = []
    for item in items or []:
        bbox = item.get("bbox_fullres") if isinstance(item, dict) else item
        if bbox and len(bbox) == 4:
            out.append([int(v) for v in bbox])
    return out


def patch_identities(items):
    """`(id, name, bbox)` per patch record, in order.

    What makes two patch lists the same once patches have stable ids and
    names (user ruling, 2026-09-24): a rename moves no rectangle, and a list
    compared by bboxes alone would call it unchanged and never publish it.
    A record from before stable ids has neither, and compares as (None, "").
    """
    out = []
    for item in items or []:
        if isinstance(item, dict):
            bbox = item.get("bbox_fullres")
            pid, name = item.get("id"), item.get("name") if item.get("id") is not None else ""
        else:
            bbox, pid, name = item, getattr(item, "id", None), getattr(item, "name", "")
        if bbox and len(bbox) == 4:
            out.append((None if pid is None else int(pid), str(name or ""),
                        [int(v) for v in bbox]))
    return out


def next_patch_id(manifest, patches, requested=0):
    """The patch-id baseline a manifest must carry: never lowered, above
    every id in `patches`. A manifest from before stable ids numbered its
    patches 1..n by position."""
    manifest = manifest or {}
    try:
        on_disk = int(manifest.get("next_patch_id")
                      or int(manifest.get("n_patches") or 0) + 1)
    except (TypeError, ValueError):
        on_disk = 1
    ids = [pid for pid, _name, _bbox in patch_identities(patches) if pid is not None]
    return max([int(requested or 0), on_disk] + [i + 1 for i in ids] + [1])


def published_geometry(step0_dir, manifest=None):
    """(roi bboxes, patch bboxes) as the PUBLISHED MANIFEST describes them.

    Through the manifest's own paths, never through fixed names: a geometry
    revision is published as a file named by its revision, and
    `patch_config.json` is a copy written after the fact. Reading the fixed
    name would answer with whatever was last copied there rather than with
    what the current handoff points at.
    """
    if manifest is None:
        published = published_handoff(step0_dir)
        manifest = published[4] if published else {}
    roi_path, patch_path = manifest_geometry_paths(manifest, step0_dir)
    return (geometry_bboxes(_read_json(roi_path)),
            geometry_bboxes(_read_json(patch_path)))


PATCH_CONFIG_NAME = "patch_config.json"
ROI_CONFIG_NAME = "roi_config.json"


def patch_config_revision_name(revision, token=""):
    """The immutable name a geometry revision's patches are written under.

    Immutable because publication has to be ATOMIC, and a fixed name cannot
    be: while `patch_config.json` is being replaced, the manifest on disk
    still points at that name, so a reader following the published manifest
    sees the NEW patches under the OLD manifest. One file per revision, named
    by it, means the only moment anything a manifest points at changes is the
    moment the manifest itself is replaced.

    The TOKEN is what makes that true across a restart. A revision counter
    lives in a page, and a page starts at zero: restart the application, edit
    a patch, and revision 1 comes round again -- straight on top of
    `patch_config.rev1.json`, which the manifest on disk is pointing at right
    now. A name nothing can collide with cannot be built from a counter
    alone, so it carries a content hash and a random token as well.
    """
    revision = int(revision)
    if not token:
        return f"patch_config.rev{revision}.json"
    return f"patch_config.rev{revision}.{token}.json"


def _revision_token(patches):
    digest = hashlib.sha1(
        json.dumps(patches, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    return f"{digest}{secrets.token_hex(3)}"


def published_revision(step0_dir, manifest=None):
    """The geometry revision the manifest ON DISK describes.

    Read at every commit, not remembered from this process: the counter in
    memory says nothing about what a previous run published, and a commit that
    trusted it would write a revision number that is already taken.
    """
    if manifest is None:
        published = published_handoff(step0_dir)
        manifest = published[4] if published else {}
    try:
        return int((manifest or {}).get("geometry_revision") or 0)
    except (TypeError, ValueError):
        return 0


def geometry_matches(step0_dir, rois, patches):
    """Does the published handoff already describe exactly this geometry?

    The question behind "that task was superseded / was older than what is on
    disk, is the file still safe to read?". Answered from the files, not from
    a revision number: two revisions can describe the same rectangles, and a
    number can be stale in either direction.
    """
    old_rois, old_patches = published_geometry(step0_dir)
    if not (geometry_bboxes(rois) == old_rois
            and geometry_bboxes(patches) == old_patches):
        return False
    published = published_handoff(step0_dir)
    manifest = published[4] if published else {}
    _roi_path, patch_path = manifest_geometry_paths(manifest, step0_dir)
    return patch_identities(patches) == patch_identities(_read_json(patch_path))


def _resolve(path, base):
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(base, path)


def manifest_geometry_paths(manifest, step0_dir):
    """The geometry files the MANIFEST names, falling back to the fixed names.

    Every reader of the published geometry has to come through here: with
    revision-named patch files, "the geometry Step0 published" is whatever the
    published manifest points at, and `patch_config.json` is only a
    compatibility copy written afterwards.
    """
    manifest = manifest or {}
    patch_path = _resolve(manifest.get("patch_config_path"), step0_dir) or \
        os.path.join(step0_dir, PATCH_CONFIG_NAME)
    roi_path = _resolve(manifest.get("roi_config_path"), step0_dir) or \
        os.path.join(step0_dir, ROI_CONFIG_NAME)
    return roi_path, patch_path


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return [] if default is None else default


def commit_geometry_only(task, *, superseded=None, publication_lock=None):
    """Publish a patch-geometry revision of an already published handoff.

    ATOMIC, which the general writer is not and cannot cheaply be made:

    * the patches are written to a file named by their REVISION, which no
      manifest points at yet;
    * nothing else on disk is touched -- not the correction config, not the
      ROI config, and in particular NOT the corrected zarr, whose attributes a
      patch edit does not change and whose validity scan it cannot affect. The
      previous version rewrote those attributes before the last supersede
      check, so a task that published nothing had still changed durable state;
    * the new manifest names the revision file and carries `geometry_revision`;
    * the manifest is replaced, once. That replace IS the publication, and
      before it a superseded task deletes its own file and leaves the
      published handoff exactly as the previous revision left it;
    * `patch_config.json` is refreshed afterwards as a NON-AUTHORITATIVE copy
      for anything that still opens it by name. It is written after the
      manifest deliberately: it is a convenience, and a reader that follows
      the manifest never sees it.

    Returns a result dict whose `outcome` is one of: committed,
    no_published_handoff, unchanged, roi_changed, corrected_zarr_missing,
    write_failed.
    """
    revision = int(task.get("revision") or 0)
    info = {"task": task, "revision": revision,
            "step0_manifest_path": "", "geometry_revision": revision}
    step0_dir = task.get("step0_dir") or ""
    published = published_handoff(step0_dir)
    if published is None:
        return dict(info, outcome="no_published_handoff")
    step0_dir, manifest_path, zarr_path, _config, manifest = published
    info["step0_manifest_path"] = os.path.abspath(manifest_path)


    rois = task["rois"]
    patches = task["patches"]
    old_roi_path, old_patch_path = manifest_geometry_paths(manifest, step0_dir)
    old_rois = geometry_bboxes(_read_json(old_roi_path))
    old_patch_records = _read_json(old_patch_path)
    old_patches = geometry_bboxes(old_patch_records)
    roi_changed = (bool(task.get("roi_context_changed"))
                   or geometry_bboxes(rois) != old_rois)
    # Identity as well as geometry: a rename moves nothing and still has to
    # be published.
    patch_changed = (geometry_bboxes(patches) != old_patches
                     or patch_identities(patches) != patch_identities(old_patch_records))
    if not roi_changed and not patch_changed:
        return dict(info, outcome="unchanged")

    # An ROI edit moves the analysis region, which mints a NEW roi_context
    # (roi_id, roi_dir, step directories) and invalidates the corrected zarr
    # computed for the old region; a NEW ROI has no channel group in that zarr
    # at all, and a deleted one leaves a zarr that describes a region that no
    # longer exists. There is no safe cross-ROI reuse rule to apply here, so
    # this refuses instead of inventing one or quietly running a full Save.
    if roi_changed:
        return dict(info, outcome="roi_changed")
    if not os.path.exists(zarr_path):
        return dict(info, outcome="corrected_zarr_missing")

    # Only now, with a publication actually needed, is a number taken. The
    # baseline comes from DISK: a fresh page counts from zero, so after a
    # restart its "revision 1" is one the published manifest already used --
    # and the file of that name is the one it is pointing at. Numbering an
    # outcome that publishes NOTHING would hand the page a revision no file
    # on disk ever describes.
    revision = max(revision, published_revision(step0_dir, manifest) + 1)
    info["revision"] = info["geometry_revision"] = revision

    def _check(phase):
        if superseded is not None and superseded(phase):
            raise Superseded()

    patch_path = os.path.join(
        step0_dir, patch_config_revision_name(revision,
                                              _revision_token(patches)))
    if os.path.exists(patch_path) or os.path.abspath(patch_path) == \
            os.path.abspath(old_patch_path):
        # Cannot happen with a token in the name; asserted anyway, because
        # the whole atomicity argument rests on this file being one nothing
        # else names.
        return dict(info, outcome="write_failed",
                    reason="revision_file_collision",
                    error=f"{patch_path} already exists")
    written = []
    try:
        _check("start")
        tmp = _write_json_staged(patch_path, patches, f"rev{revision}")
        os.replace(tmp, patch_path)
        written.append(patch_path)

        new_manifest = dict(manifest)
        new_manifest["patch_config_path"] = os.path.abspath(patch_path)
        new_manifest["n_patches"] = len(patches)
        new_manifest["next_patch_id"] = next_patch_id(
            manifest, patches, task.get("next_patch_id"))
        new_manifest["geometry_revision"] = revision
        new_manifest["step0_roi_result_path"] = os.path.abspath(manifest_path)

        manifest_tmp = f"{manifest_path}.tmp.rev{revision}"
        with open(manifest_tmp, "w", encoding="utf-8") as f, \
                perf_trace.span("handoff.manifest_write"):
            json.dump(new_manifest, f, indent=2, ensure_ascii=False)
            f.flush()
            with perf_trace.span("handoff.fsync"):
                os.fsync(f.fileno())
        written.append(manifest_tmp)

        # THE publication. One replace, of one file, and every path it names
        # already exists with its final contents.
        # Same final-publication lock as canonical full Save. An old geometry
        # task cannot pass its check, then overwrite a newer Save manifest.
        with (publication_lock if publication_lock is not None else nullcontext()):
            _check("publish")
            with perf_trace.span("handoff.publish_replace", files=1):
                os.replace(manifest_tmp, manifest_path)
        written = []
    except Superseded:
        for path in written:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        raise
    except Exception as exc:                                # noqa: BLE001
        for path in written:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        print(f"[Step0] geometry-only commit FAILED: {exc}")
        return dict(info, outcome="write_failed",
                    reason=f"write_failed: {exc}", error=str(exc))

    _refresh_compat_patch_config(step0_dir, patches, revision)
    _drop_superseded_patch_revisions(step0_dir, os.path.basename(patch_path))
    print(f"[Step0] geometry-only commit published: patches={len(patches)} "
          f"revision={revision}")
    return dict(info, outcome="committed", rois=rois, patches=patches,
                patch_config_path=os.path.abspath(patch_path),
                result={"manifest_path": os.path.abspath(manifest_path)})


def _refresh_compat_patch_config(step0_dir, patches, revision):
    """Keep `patch_config.json` in step with the published revision.

    A COPY, not the source of truth: written after publication, and a failure
    here is not a failure of the commit -- the manifest already names the file
    that matters.
    """
    path = os.path.join(step0_dir, PATCH_CONFIG_NAME)
    try:
        tmp = _write_json_staged(path, patches, f"compat{revision}")
        os.replace(tmp, path)
    except Exception as exc:                                # noqa: BLE001
        print(f"[Step0] compatibility patch_config.json not refreshed: {exc}")


def _drop_superseded_patch_revisions(step0_dir, keep):
    """Remove revision files no published manifest can name any more."""
    try:
        names = os.listdir(step0_dir)
    except OSError:
        return
    for name in names:
        if (name.startswith("patch_config.rev") and name.endswith(".json")
                and name != keep):
            try:
                os.unlink(os.path.join(step0_dir, name))
            except OSError:
                pass


def clean_correction_config(config):
    """The correction config in the one shape the handoff describes.

    Here rather than on the page because both writers normalise it the same
    way: a Save's config and the published config a geometry-only commit
    republishes have to come out identical, or the two paths would write
    different files from the same numbers.
    """
    cfg = dict(config or {})
    params = dict(cfg.get("method_params") or {})
    decisions = {}
    for ch, method in (cfg.get("channel_decisions") or {}).items():
        # A READ BOUNDARY: this function also cleans configs that came off
        # disk, so legacy `both` (and anything unreadable) is migrated here.
        decisions[str(ch)] = migrate_correction_decision(method)
    channel_params = {}
    for ch, cp in (cfg.get("channel_params") or {}).items():
        cp = cp or {}
        channel_params[str(ch)] = {
            "tophat_radius": int(cp.get("tophat_radius", params.get(
                "tophat_radius", TOPHAT_RADIUS_DEFAULT))),
            "cucim_sigma": int(cp.get("cucim_sigma", params.get(
                "cucim_sigma", CUCIM_SIGMA_DEFAULT))),
        }
    return {
        "method_params": {
            "tophat_radius": int(params.get("tophat_radius",
                                            TOPHAT_RADIUS_DEFAULT)),
            "cucim_sigma": int(params.get("cucim_sigma",
                                          CUCIM_SIGMA_DEFAULT)),
        },
        "channel_decisions": decisions,
        "channel_params": channel_params,
    }
