"""Block DV: data versions of one session (docs/v16_data_versions_application.md).

Part 1 -- the version record (`utils/data_versions.py`): a version exists only
when its record is complete AND it is in the index; ids are a counter plus a
timestamp; what makes two versions the same is compared from existing fields
(ROI geometry included); incomplete folders are cleaned; published versions
are recognised as read-only.

Synthetic workspaces in the test's temporary directory only.
"""

import json
import os
from datetime import datetime

import pytest

from block01.utils import data_versions as dv


def _ws(tmp_path):
    ws = tmp_path / "proj" / "rois" / "ws1"
    ws.mkdir(parents=True)
    (ws / "roi_manifest.json").write_text("{}")
    return str(ws)


def _record(bbox=(0, 100, 0, 200), sigs=None, remap="r1", fusion="f1", poly=None,
            slide="slide_a"):
    return {
        "slide_id": slide,
        "regions": [{"roi_name": "Full WSI", "roi_id": "ws1", "bbox_fullres": list(bbox),
                     "polygon_fullres": poly, "fused_zarr_path": "/x/fused.zarr"}],
        "corrected": {"path": "/x/corrected.zarr",
                      "signatures": sigs if sigs is not None else
                      {"CD3": ["tophat", 25, "2", "cpu", "disk"]},
                      "bboxes": [list(bbox)], "source_identity": {"dataset_path": "/s"}},
        "step0_remap_hash": remap,
        "fusion_settings_hash": fusion,
        "method": "stardist_nuclei_expansion",
    }


def _commit(ws, record=None, when=None):
    alloc = dv.new_version_folder(ws, now=when)
    return dv.commit_version(ws, alloc, record or _record())


# ── existence ────────────────────────────────────────────────────────────

def test_a_committed_version_exists_and_is_current(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws, when=datetime(2026, 10, 3, 12, 0, 0))
    assert rec["version"] == "v001" and rec["folder"] == "v001_20261003_120000"
    assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
    assert dv.current_version(ws)["version"] == "v001"
    assert dv.load_index(ws)["current"] == "v001"


def test_an_allocated_but_uncommitted_version_does_not_exist(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    alloc = dv.new_version_folder(ws)              # a Generate that then failed
    open(os.path.join(alloc["path"], "partial.bin"), "wb").write(b"x")
    assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
    assert dv.current_version(ws)["version"] == "v001"   # current not moved


def test_a_record_without_complete_is_not_a_version(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    path = os.path.join(dv.version_dir(ws, rec["folder"]), dv.RECORD)
    data = json.load(open(path))
    data["complete"] = False
    json.dump(data, open(path, "w"))
    assert dv.list_versions(ws) == [] and dv.current_version(ws) is None


def test_incomplete_folders_are_cleaned_and_numbers_never_reused(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    failed = dv.new_version_folder(ws)
    os.makedirs(dv.draft_dir(ws))
    removed = dv.cleanup_incomplete(ws)
    assert removed == [failed["folder"]]
    assert os.path.isdir(dv.draft_dir(ws))                # the draft is kept
    # the failed folder is gone, so v001 is the highest number on disk
    assert dv.new_version_folder(ws)["version"] == "v002"


def test_numbers_of_failed_folders_still_on_disk_are_not_reused(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    failed = dv.new_version_folder(ws)                     # v002, not cleaned yet
    nxt = dv.new_version_folder(ws)
    assert failed["version"] == "v002" and nxt["version"] == "v003"


def test_set_current_only_to_an_existing_version(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    _commit(ws, _record(remap="r2"))
    assert dv.current_version(ws)["version"] == "v002"
    assert dv.set_current(ws, "v001") is True
    assert dv.current_version(ws)["version"] == "v001"
    assert dv.set_current(ws, "v009") is False


# ── comparing ────────────────────────────────────────────────────────────

def test_same_parameters_and_geometry_is_the_same_version(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    assert dv.find_same(ws, _record())["version"] == "v001"


@pytest.mark.parametrize("change", [
    {"bbox": (0, 100, 0, 150)},                                  # geometry
    {"poly": [[0, 0], [10, 0], [10, 10]]},                       # polygon
    {"sigs": {"CD3": ["tophat", 40, "2", "cpu", "disk"]}},       # a channel's correction
    {"remap": "r2"},                                             # Step0 Intensity
    {"fusion": "f2"},                                            # Step1 settings
    {"slide": "slide_b"},                                        # another slide
])
def test_any_difference_is_another_version(tmp_path, change):
    ws = _ws(tmp_path)
    _commit(ws)
    assert dv.find_same(ws, _record(**change)) is None


def test_corrected_is_shared_only_when_the_whole_product_is_the_same(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    # only Step1 / Intensity changed: the corrected product is the same one
    assert dv.find_same_corrected(ws, _record(remap="r2", fusion="f2"))["version"] == "v001"
    # same parameters, other ROI geometry: never shared
    assert dv.find_same_corrected(ws, _record(bbox=(0, 100, 0, 150))) is None
    # one channel differs: never shared
    assert dv.find_same_corrected(
        ws, _record(sigs={"CD3": ["tophat", 25, "2", "cpu", "disk"],
                          "CD8": ["cucim", 30, "2", "cpu", None]})) is None


# ── read-only recognition ────────────────────────────────────────────────

def test_products_of_published_versions_are_recognised(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    inside = os.path.join(dv.version_dir(ws, rec["folder"]), "corrected_channels.zarr")
    assert dv.is_published_product(ws, inside)
    assert dv.is_published_product(ws, "/x/corrected.zarr")   # a referenced legacy product
    assert not dv.is_published_product(ws, dv.draft_corrected_path(ws))


def test_a_versions_own_folder_size_excludes_what_it_references(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    with open(os.path.join(dv.version_dir(ws, rec["folder"]), "blob"), "wb") as f:
        f.write(b"0" * 1000)
    assert dv.version_size(ws, rec) >= 1000
