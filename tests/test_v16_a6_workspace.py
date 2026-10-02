"""Block A6 W1-W3: Step0 opens the slide's existing workspace; Save writes
back into it; Save as makes a new one; a changed region asks first.

Synthetic slides and projects in the test's temporary directory only.
Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os
import shutil

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("tifffile")

from PyQt5 import QtWidgets  # noqa: E402

from test_v16_project_identity import _write_slide  # noqa: E402
from block01.utils import roi_project, workspace_session as wsess  # noqa: E402

SHAPE = (1001, 999)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def slides(tmp_path_factory):
    d = tmp_path_factory.mktemp("a6_slides")
    a = _write_slide(d / "a.ome.tif", shape=SHAPE, seed=0)
    b = _write_slide(d / "b.ome.tif", shape=SHAPE, seed=1)
    return {"a": a, "b": b}


def _commit_step0(ctx, decisions=None, rois=None, patches=None):
    """What a finished Step0 Save leaves in a workspace (the files W1 reads)."""
    step0 = ctx["step_dirs"]["step0"]
    os.makedirs(step0, exist_ok=True)
    full = rois is None
    rois = rois or [{"name": "Full WSI", "type": "full_wsi",
                     "bbox_fullres": [0, SHAPE[0], 0, SHAPE[1]],
                     "polygon_fullres": None}]
    with open(os.path.join(step0, "roi_config.json"), "w") as f:
        json.dump(rois, f)
    with open(os.path.join(step0, "patch_config.json"), "w") as f:
        json.dump(patches or [], f)
    with open(os.path.join(step0, "correction_config.json"), "w") as f:
        json.dump({"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
                   # a real Save writes every marker channel's decision
                   "channel_decisions": decisions or {"CD3": "original", "CD8": "original"},
                   "channel_params": {}}, f)
    os.makedirs(os.path.join(step0, "corrected_channels.zarr"), exist_ok=True)
    with open(os.path.join(step0, "step0_roi_result.json"), "w") as f:
        json.dump({"roi_id": ctx["roi_id"],
                   "analysis_region_type": "full_wsi" if full else "roi",
                   "roi_config_path": os.path.join(step0, "roi_config.json"),
                   "patch_config_path": os.path.join(step0, "patch_config.json"),
                   "corrected_zarr_path": os.path.join(step0, "corrected_channels.zarr")}, f)
    return ctx


def _project(tmp_path, slide, n=1, commit=True):
    proj = str(tmp_path / "proj")
    made = []
    for _ in range(n):
        ctx = roi_project.create_full_wsi_context(proj, SHAPE, slide)
        made.append(_commit_step0(ctx) if commit else ctx)
    return proj, made


def _tree(path):
    out = []
    for base, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(base, name)
            with open(p, "rb") as f:
                out.append((os.path.relpath(p, path), f.read()))
    return sorted(out)


# ── finding the slide's workspaces ──────────────────────────────────────

def test_the_slides_committed_workspaces_are_found_newest_first(tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"], n=2)
    roi_project.create_full_wsi_context(proj, SHAPE, slides["a"])   # never committed
    sid, found = wsess.find_workspaces(proj, slides["a"])
    assert sid and sid.startswith("slide_")
    assert {w.workspace_id for w in found} == {c["roi_id"] for c in made}
    assert [w.created_at for w in found] == sorted((w.created_at for w in found),
                                                   reverse=True)


def test_another_slide_finds_nothing(tmp_path, slides):
    proj, _ = _project(tmp_path, slides["a"])
    assert wsess.find_workspaces(proj, slides["b"])[1] == []


def test_the_same_slide_at_another_path_is_still_the_same_slide(tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"])
    moved = str(tmp_path / "elsewhere.ome.tif")
    shutil.copyfile(slides["a"], moved)
    found = wsess.find_workspaces(proj, moved)[1]
    assert [w.workspace_id for w in found] == [made[0]["roi_id"]]


def test_a_project_made_before_a3_is_identified_by_its_slide_file(tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"])
    path = roi_project.project_manifest_path(proj)
    with open(path) as f:
        manifest = json.load(f)
    manifest.pop("sources", None)                      # what the test1 copy looks like
    with open(path, "w") as f:
        json.dump(manifest, f)
    found = wsess.find_workspaces(proj, slides["a"])[1]
    assert [w.workspace_id for w in found] == [made[0]["roi_id"]]


def test_no_project_finds_nothing(tmp_path, slides):
    assert wsess.find_workspaces(str(tmp_path / "none"), slides["a"]) == (None, [])


# ── the page ────────────────────────────────────────────────────────────

class _L:
    shape = SHAPE
    ch_map = {"DAPI": 0, "CD3": 1, "CD8": 2}

    def __init__(self, path):
        self.filepath = path

    def channel_names(self):
        return list(self.ch_map)

    def set_correction_config(self, c):
        pass

    def set_corrected_zarr_store(self, p, d):
        pass


@pytest.fixture
def page(app, tmp_path, monkeypatch, slides):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()
    p.loader = _L(slides["a"])
    p.ome_path = slides["a"]
    p.output_dir = str(tmp_path / "proj")
    p.nucleus_channel = "DAPI"
    p._channel_order = ["DAPI", "CD3", "CD8"]
    seen = {"no_changes": 0, "created": [], "asked": [], "chosen": [],
            "emitted": 0}
    real_full = sp.create_full_wsi_context

    def _full(out, shape, ome):
        ctx = real_full(out, shape, ome)
        seen["created"].append(ctx["roi_id"])
        return ctx
    monkeypatch.setattr(sp, "create_full_wsi_context", _full)
    monkeypatch.setattr(Step0Page, "_show_save_no_changes",
                        lambda self: seen.__setitem__("no_changes", seen["no_changes"] + 1))

    def _emit(self, config, zarr_path, decisions):
        seen["emitted"] += 1
        _commit_step0(self._roi_context, decisions=config.get("channel_decisions"))
        return True
    monkeypatch.setattr(Step0Page, "_emit_complete", _emit)
    monkeypatch.setattr(Step0Page, "_apply_corrected_store", lambda self, *a, **k: None)
    monkeypatch.setattr(Step0Page, "_ensure_empty_corrected_zarr",
                        lambda self, path, rois: os.makedirs(path, exist_ok=True))
    monkeypatch.setattr(sp.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sp.QMessageBox, "question",
                        lambda *a, **k: sp.QMessageBox.Ok)
    p._seen = seen
    yield p
    p.deleteLater()


def _workspaces(proj):
    return sorted(os.listdir(os.path.join(proj, "rois")))


def test_opening_the_only_workspace_and_saving_unchanged_writes_nothing(page, tmp_path,
                                                                         slides):
    proj, made = _project(tmp_path, slides["a"])
    ws_id = made[0]["roi_id"]
    opened = page._open_existing_workspace()
    assert opened is not None and opened.workspace_id == ws_id
    assert page._roi_context["roi_id"] == ws_id
    assert f"Workspace: {ws_id}" in page._project_status_text()

    before = _tree(made[0]["roi_dir"])
    page._save_and_continue()
    assert page._seen["no_changes"] == 1             # "No changes", nothing rewritten
    assert page._seen["created"] == []
    assert _workspaces(proj) == [ws_id]
    assert _tree(made[0]["roi_dir"]) == before


def test_the_workspaces_decisions_and_parameters_come_back(page, tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"], commit=False)
    _commit_step0(made[0], decisions={"CD3": "original", "CD8": "original"})
    cfg_path = os.path.join(made[0]["step_dirs"]["step0"], "correction_config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["method_params"] = {"tophat_radius": 41, "cucim_sigma": 77}
    cfg["channel_params"] = {"CD8": {"tophat_radius": 9}}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    page._open_existing_workspace()
    assert page._tophat_slider.value() == 41 and page._cucim_slider.value() == 77
    assert page._channel_params == {"CD8": {"tophat_radius": 9}}
    assert page._build_config()["channel_decisions"] == {"CD3": "original",
                                                         "CD8": "original"}


def test_a_channel_the_workspace_holds_corrected_counts_as_computed(page, tmp_path,
                                                                    slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, made = _project(tmp_path, slides["a"], commit=False)
    _commit_step0(made[0], decisions={"CD3": "tophat", "CD8": "original"})
    from block01.ui.step0.step0_page import Step0Page
    config = {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
              "channel_params": {}}
    held = {"CD3": Step0Page._save_signature(config, "CD3", "tophat")}
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, SHAPE[0], 0, SHAPE[1])]))
    page._open_existing_workspace()
    assert page._channel_final_decision("CD3") == "tophat"
    assert "CD3" not in page._raw_save_channels()     # not "will be saved raw"
    assert "CD8" in page._raw_save_channels()
    page._save_and_continue()
    assert page._seen["no_changes"] == 1 and page._seen["created"] == []


def test_save_as_always_makes_a_new_workspace(page, tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"])
    page._open_existing_workspace()
    before = _tree(made[0]["roi_dir"])
    page._save_as_new_workspace()
    assert len(page._seen["created"]) == 1
    new_id = page._seen["created"][0]
    assert page._roi_context["roi_id"] == new_id      # later steps use the new one
    assert _workspaces(proj) == sorted([made[0]["roi_id"], new_id])
    assert _tree(made[0]["roi_dir"]) == before         # the old one untouched
    assert page._save_as_requested is False


def test_save_as_is_in_the_menu_beside_save(page):
    actions = [a.text() for a in page._btn_save_menu.menu().actions()]
    assert actions == ["Save as new workspace"]
    page._set_save_enabled(False)
    assert not page._btn_continue.isEnabled() and not page._btn_save_menu.isEnabled()
    page._set_save_enabled(True)
    assert page._btn_save_menu.isEnabled()


def test_several_workspaces_ask_which_one(page, tmp_path, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _project(tmp_path, slides["a"], n=3)
    offered = []

    def _choose(self, found):
        offered.append([w.workspace_id for w in found])
        return found[-1]                               # the oldest
    monkeypatch.setattr(Step0Page, "_choose_workspace", _choose)
    opened = page._open_existing_workspace()
    assert len(offered) == 1 and len(offered[0]) == 3
    assert opened.workspace_id == offered[0][-1]
    with open(roi_project.project_roi_index_path(proj)) as f:
        assert json.load(f)["active_roi_id"] == opened.workspace_id


def test_choosing_no_workspace_keeps_todays_first_save(page, tmp_path, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _project(tmp_path, slides["a"], n=2)
    monkeypatch.setattr(Step0Page, "_choose_workspace", lambda self, found: None)
    assert page._open_existing_workspace() is None
    assert page._roi_context is None
    page._save_and_continue()
    assert len(page._seen["created"]) == 1


def test_another_slides_workspace_is_not_opened(page, tmp_path, slides):
    _project(tmp_path, slides["b"])
    assert page._open_existing_workspace() is None
    assert page._roi_context is None


def _redraw_roi(page, monkeypatch, bbox):
    from block01.ui.step0.step0_page import Step0Page
    roi = {"name": "R1", "bbox_fullres": list(bbox), "type": "roi",
           "polygon_fullres": [[bbox[2], bbox[0]], [bbox[3], bbox[0]],
                               [bbox[3], bbox[1]]]}
    monkeypatch.setattr(type(page.overview), "get_rois", lambda self: [dict(roi)])
    monkeypatch.setattr(Step0Page, "_roi_count", lambda self: 1)
    return roi


@pytest.mark.parametrize("answer", ["save_as", "overwrite", "cancel"])
def test_a_changed_region_asks_first(page, tmp_path, slides, monkeypatch, answer):
    from block01.ui.step0.step0_page import Step0Page
    import block01.ui.step0.step0_page as sp
    proj, made = _project(tmp_path, slides["a"])
    page._open_existing_workspace()
    ws_id = made[0]["roi_id"]
    real_roi = sp.create_roi_context

    def _roi_ctx(out, roi, ome):
        ctx = real_roi(out, roi, ome)
        page._seen["created"].append(ctx["roi_id"])
        return ctx
    monkeypatch.setattr(sp, "create_roi_context", _roi_ctx)
    monkeypatch.setattr(Step0Page, "_ask_region_changed",
                        lambda self: page._seen["asked"].append(1) or answer)
    _redraw_roi(page, monkeypatch, (100, 400, 200, 600))
    page._save_and_continue()

    assert page._seen["asked"] == [1]
    if answer == "save_as":
        assert len(page._seen["created"]) == 1
        assert page._roi_context["roi_id"] != ws_id
    elif answer == "overwrite":
        assert page._seen["created"] == []
        assert page._roi_context["roi_id"] == ws_id
        with open(roi_project.roi_manifest_path(made[0]["roi_dir"])) as f:
            m = json.load(f)
        assert m["bbox_fullres"] == [100, 400, 200, 600] and m["type"] == "roi"
        assert page._seen["emitted"] == 1
    else:
        assert page._seen["created"] == [] and page._seen["emitted"] == 0
        assert page._roi_context["roi_id"] == ws_id
    assert len(_workspaces(proj)) == (2 if answer == "save_as" else 1)


def test_a_new_project_still_makes_its_first_workspace(page, tmp_path):
    assert page._open_existing_workspace() is None
    page._save_and_continue()
    assert len(page._seen["created"]) == 1
    page._save_and_continue()                           # same region: the same one
    assert len(page._seen["created"]) == 1
