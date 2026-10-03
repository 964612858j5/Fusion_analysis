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


# ── part 2: Step0's corrected draft and copy-on-write (§3.15) ─────────────

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_v16_a6_workspace import (  # noqa: E402,F401  (fixtures)
    app, slides, page, _project, _commit_step0, _tree)


from PyQt5 import QtCore  # noqa: E402


class _FakeWsi(QtCore.QThread):
    """WsiCorrectionWorker's contract (a QThread whose business `finished`
    shadows the base one); records what it was asked to do, runs nothing."""
    progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
    finished = QtCore.pyqtSignal(str, dict)
    canceled = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    made = []

    def __init__(self, loader, output_dir, config, rois=None, parent=None,
                 process_channels=None, incremental=False):
        super().__init__()
        self.output_dir, self.process_channels = output_dir, set(process_channels or ())
        self.incremental = incremental
        _FakeWsi.made.append(self)

    def stop_after_current_channel(self):
        pass

    def start(self):
        pass


def _published_with_tophat(tmp_path, slides):
    """A workspace whose published handoff references a corrected product
    that holds CD3 and CD8 (TopHat r=25) -- the product a version owns."""
    proj, made = _project(tmp_path, slides["a"], commit=False)
    ctx = made[0]
    _commit_step0(ctx, decisions={"CD3": "tophat", "CD8": "tophat"})
    z = os.path.join(ctx["step_dirs"]["step0"], "corrected_channels.zarr")
    with open(os.path.join(z, "marker.bin"), "wb") as f:
        f.write(b"published pixels")
    sidecar = os.path.join(ctx["step_dirs"]["step0"], "corrected_coarse.zarr")
    os.makedirs(sidecar)
    return proj, ctx, z


@pytest.fixture
def dv_page(page, monkeypatch, tmp_path, slides):
    import block01.ui.step0.step0_page as sp
    _FakeWsi.made = []
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _FakeWsi)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog",
                        lambda parent: type("D", (), {"cancel_requested": type(
                            "S", (), {"connect": lambda *a, **k: None})(),
                            "show": lambda self: None, "exec_": lambda self: 0})())
    return page


def _sigs(page, config_mp, decisions):
    cfg = {"method_params": config_mp, "channel_params": {}}
    return {ch: page._save_signature(cfg, ch, m) for ch, m in decisions.items()}


def test_changing_one_channel_copies_the_published_product_once_and_corrects_only_it(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    before = _tree(published)
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}     # CD8 25 -> 40
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    draft = dv.draft_corrected_path(ctx["roi_dir"])
    assert w.output_dir == os.path.dirname(draft)                 # written into the draft
    assert w.incremental is True and w.process_channels == {"CD8"}
    assert os.path.isfile(os.path.join(draft, "marker.bin"))     # copied once
    assert os.path.isdir(os.path.join(dv.draft_dir(ctx["roi_dir"]), "corrected_coarse.zarr"))
    assert _tree(published) == before                             # read-only


def test_a_save_with_no_corrected_change_references_the_published_product(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    dv_page._save_and_continue()
    assert _FakeWsi.made == []                                    # nothing recomputed
    assert not os.path.exists(dv.draft_dir(ctx["roi_dir"]))       # nothing copied


def test_another_roi_makes_a_fresh_draft_and_recomputes_every_channel(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    monkeypatch.setattr(Step0Page, "_ask_region_changed", lambda self: "overwrite")
    roi = {"name": "R1", "bbox_fullres": [100, 400, 200, 600], "type": "roi",
           "polygon_fullres": [[200, 100], [600, 100], [600, 400]]}
    monkeypatch.setattr(type(dv_page.overview), "get_rois", lambda self: [dict(roi)])
    monkeypatch.setattr(Step0Page, "_roi_count", lambda self: 1)
    before = _tree(published)
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    draft = dv.draft_corrected_path(ctx["roi_dir"])
    assert w.output_dir == os.path.dirname(draft)
    assert w.incremental is False and w.process_channels == {"CD3", "CD8"}
    assert not os.path.exists(os.path.join(draft, "marker.bin"))  # not copied
    assert _tree(published) == before


def test_once_a_draft_exists_saves_only_touch_the_draft(dv_page, tmp_path, slides,
                                                         monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    draft = dv.draft_corrected_path(ctx["roi_dir"])
    os.makedirs(draft)
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    before = _tree(published)
    dv_page._channel_params = {"CD3": {"tophat_radius": 30}}
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    assert w.output_dir == os.path.dirname(draft) and w.process_channels == {"CD3"}
    assert _tree(published) == before


def test_the_handoff_stays_in_the_workspaces_step0_and_registers_nothing(
        dv_page, tmp_path, slides):
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    spec = dv_page._handoff_spec({}, dv.draft_corrected_path(ctx["roi_dir"]))
    assert spec["step0_dir"] == ctx["step_dirs"]["step0"]
    assert spec["manifest_path"] == os.path.join(ctx["step_dirs"]["step0"],
                                                 "step0_roi_result.json")
    assert spec["register_corrected"] is False
