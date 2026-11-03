"""A Step0 Save that does not finish puts back the settings it replaced
(docs/v17_review_fixes_plan.md §3).

Save writes the workspace's `correction_config.json` and sets the loader's
settings before the run is prepared and computed. Every unfinished end --
Cancel while preparing or computing, a correction error, run or empty-zarr
creation failing, a failed handoff after the new store was applied -- leaves
the previous file's bytes, the loader's previous settings and the corrected
store it read; a first Save leaves no file. A Save that completes keeps the
new ones.

The run-store tests' page (`test_v16_rm1_run_store.dv_page`: Save's worker
and dialog stubbed) with a loader that remembers what it is told; synthetic
workspaces in the test's temporary directory only.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_v16_rm1_run_store import (  # noqa: E402,F401  (fixtures)
    app, slides, page, dv_page, _FakeWsi, _published_tophat, _prepared, _runs)
from test_v16_a6_workspace import _L  # noqa: E402

from block01.ui.step0.step0_page import Step0Page  # noqa: E402

REAL_EMIT = Step0Page._emit_complete            # before the page fixture stubs it


class _Loader(_L):
    """The A6 loader, remembering its settings and corrected store."""

    def __init__(self, path):
        super().__init__(path)
        self.correction_config = {"previous": True}
        self._store = ("/previous/corrected_channels.zarr", {"CD3": "tophat"})

    def set_correction_config(self, c):
        self.correction_config = c

    def set_corrected_zarr_store(self, p, d):
        self._store = (p, dict(d or {}))

    def corrected_store_binding(self):
        return (self._store[0], dict(self._store[1]))


@pytest.fixture
def save(dv_page, tmp_path, slides, monkeypatch):
    """A workspace with a published TopHat run, opened; CD8 changed so a
    Save recomputes. Returns (page, config path, previous bytes, ctx, base)."""
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    dv_page.loader = _Loader(dv_page.loader.filepath)
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    monkeypatch.setattr(Step0Page, "_apply_corrected_store",
                        lambda self, p, d, only=None: self.loader.set_corrected_zarr_store(p, d))
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(sp.QMessageBox, name, lambda *a, **k: None)
    path = os.path.join(ctx["step_dirs"]["step0"], "correction_config.json")
    with open(path, "rb") as f:
        before = f.read()
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}
    return dv_page, path, before, ctx, base


def _previous(page, path, before):
    with open(path, "rb") as f:
        assert f.read() == before, "the settings file was not put back"
    assert page.loader.correction_config == {"previous": True}
    assert page.loader.corrected_store_binding() == (
        "/previous/corrected_channels.zarr", {"CD3": "tophat"})
    assert page.__dict__.get("_rm_kept_settings") is None


def test_cancel_while_computing(save):
    page, path, before, ctx, base = save
    page._save_and_continue()
    _prepared(page)
    with open(path, "rb") as f:
        assert f.read() != before                   # the Save did write its settings
    page._wsi_dialog = None
    page._on_wsi_canceled(os.path.join(base, "corrected_channels.zarr"))
    _previous(page, path, before)
    assert _runs(ctx) == [os.path.basename(base)]


def test_cancel_while_preparing(save):
    page, path, before, ctx, base = save
    page._save_and_continue()
    page._save_prep["cancel"] = True                # Cancel before the copy is through
    _prepared(page)
    _previous(page, path, before)
    assert _FakeWsi.made == [] and _runs(ctx) == [os.path.basename(base)]


def test_a_correction_error(save):
    page, path, before, ctx, base = save
    page._save_and_continue()
    _prepared(page)
    page._wsi_dialog = None
    page._on_wsi_error("GPU out of memory")
    _previous(page, path, before)


def test_the_preparation_failing(save, monkeypatch):
    """(Save writes the run in place: no run is created. What can fail before
    the run starts is the viewer's hand-off.)"""
    page, path, before, ctx, base = save

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Step0Page, "_begin_release_explore_for_production", boom)
    with pytest.raises(OSError):
        page._save_and_continue()
    _previous(page, path, before)


def test_empty_zarr_creation_failing(save, monkeypatch):
    page, path, before, ctx, base = save
    page._set_channel_decision("CD3", "original")   # nothing left to correct
    page._set_channel_decision("CD8", "original")

    def boom(self, zarr_path, rois):
        raise OSError("disk full")
    monkeypatch.setattr(Step0Page, "_ensure_empty_corrected_zarr", boom)
    with pytest.raises(OSError):
        page._save_and_continue()
    _previous(page, path, before)
    assert _runs(ctx) == [os.path.basename(base)]


def test_a_first_save_cancelled_leaves_no_file(save):
    page, path, before, ctx, base = save
    os.remove(path)
    page._save_and_continue()
    _prepared(page)
    page._wsi_dialog = None
    page._on_wsi_canceled(os.path.join(base, "corrected_channels.zarr"))
    assert not os.path.exists(path)


def test_a_handoff_failure_after_the_store_was_applied(save, monkeypatch):
    page, path, before, ctx, base = save
    page._set_channel_decision("CD8", "original")   # no recompute: the store is applied first
    monkeypatch.setattr(Step0Page, "_emit_complete", REAL_EMIT)
    monkeypatch.setattr(Step0Page, "_persist_step0_remap_config", lambda self: True)

    def boom(self, config, zarr_path):
        raise OSError("disk full")
    monkeypatch.setattr(Step0Page, "_write_step0_handoff", boom)
    page._save_and_continue()
    _prepared(page)
    _previous(page, path, before)


def test_a_completed_save_keeps_the_new_settings(save, monkeypatch):
    page, path, before, ctx, base = save
    page._set_channel_decision("CD8", "original")
    monkeypatch.setattr(Step0Page, "_emit_complete", REAL_EMIT)
    monkeypatch.setattr(Step0Page, "_persist_step0_remap_config", lambda self: True)
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path: (config, [], [], {}))
    monkeypatch.setattr(Step0Page, "_rm_note_editing", lambda self, manifest: False)
    monkeypatch.setattr(Step0Page, "_handoff_payload", lambda self, *a: {})
    page._save_and_continue()
    _prepared(page)
    with open(path, "rb") as f:
        assert f.read() != before
    assert page.loader.correction_config != {"previous": True}
    assert page.loader.corrected_store_binding()[0] != "/previous/corrected_channels.zarr"
    assert page.__dict__.get("_rm_kept_settings") is None


# ── with the real loader: reads come from the previous product again ────────

REAL_APPLY = Step0Page._apply_corrected_store


@pytest.fixture
def real(save, monkeypatch):
    """`save` with the real OMETIFFLoader bound to the published run, whose
    CD8 holds 1000.0 everywhere (raw pixels are 0..255), and the real
    `_apply_corrected_store`. CD8 withdrawn: the Save applies a store that
    reads CD8 raw before the handoff."""
    import numpy as np
    import zarr
    from block01.core.io_loader import OMETIFFLoader
    page, path, before, ctx, base = save
    store = os.path.join(base, "corrected_channels.zarr")
    group = zarr.open_group(store, mode="a")
    group.create_dataset("CD8", data=np.full((1001, 999), 1000.0, np.float32), overwrite=True)
    group.create_dataset("CD3", data=np.full((1001, 999), 500.0, np.float32), overwrite=True)
    loader = OMETIFFLoader(page.loader.filepath)
    loader.set_corrected_zarr_store(store, {"CD3": "tophat", "CD8": "tophat"})
    page.loader = loader
    monkeypatch.setattr(Step0Page, "_apply_corrected_store", REAL_APPLY)
    monkeypatch.setattr(Step0Page, "_emit_complete", REAL_EMIT)
    monkeypatch.setattr(Step0Page, "_persist_step0_remap_config", lambda self: True)
    page._set_channel_decision("CD8", "original")
    return page, path, before, ctx, base, loader


def _cd8(loader):
    return float(loader.read_region("CD8", 0, 4, 0, 4, normalize=False).max())


def test_a_handoff_failure_reads_the_previous_product_again(real, monkeypatch):
    page, path, before, ctx, base, loader = real
    assert _cd8(loader) == 1000.0

    def boom(self, config, zarr_path):
        assert _cd8(loader) <= 255.0                 # the new store was applied: CD8 raw
        raise OSError("disk full")
    monkeypatch.setattr(Step0Page, "_write_step0_handoff", boom)
    page._save_and_continue()
    _prepared(page)
    with open(path, "rb") as f:
        assert f.read() == before
    assert _cd8(loader) == 1000.0                    # the previous product's pixels


def test_a_failure_after_publication_keeps_the_published_run(real, monkeypatch):
    """The handoff published the run (rewritten in place) and then failed:
    the run stays, no other run appears, the settings and the previous
    product's reads come back."""
    from block01.core import step0_handoff
    from block01.utils import run_store as rs
    page, path, before, ctx, base, loader = real
    published = []

    def write(spec, **kw):
        spec["publish_run"]()
        published.append(page.__dict__.get("_rm_pending_run") or "published")
        raise OSError("disk full after publication")
    monkeypatch.setattr(step0_handoff, "write_handoff", write)
    page._save_and_continue()
    _prepared(page)
    assert published and _runs(ctx) == [os.path.basename(base)]
    assert rs.is_done(base)
    with open(path, "rb") as f:
        assert f.read() == before
    assert _cd8(loader) == 1000.0


# ── Saved / Unsaved and the notice on opening (user ruling 2026-10-31) ──────

def _status_of(page, ch):
    # (the run-store page builds no channel rows: the line is asked directly)
    page._channel_rows = dict.fromkeys(("CD3", "CD8"))
    page.current_channel = ch
    page._show_decision_status(ch)
    return page._decision_status.full_text()


def test_the_status_line_says_unsaved_until_the_setting_is_saved(save):
    page, path, before, ctx, base = save
    page._channel_params = {}                        # back to what was saved
    assert _status_of(page, "CD8").startswith("Saved: CD8 tophat")
    page._channel_params = {"CD8": {"tophat_radius": 40}}
    assert _status_of(page, "CD8").startswith("Unsaved: CD8 tophat")
    page._save_and_continue()
    _prepared(page)
    page._wsi_dialog = None
    page._on_wsi_canceled(os.path.join(base, "corrected_channels.zarr"))
    assert page._decision_status.full_text().startswith("Unsaved: CD8 tophat")   # kept as a draft
    page._set_channel_decision("CD3", "original")                # saved: tophat
    assert _status_of(page, "CD3") == "Unsaved: CD3 original"


def test_opening_a_workspace_with_an_unsaved_draft_says_which_channels(save, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from PyQt5 import QtTest
    page, path, before, ctx, base = save
    page.rm_save_draft()                             # CD8 r=40 left unsaved, GUI closed
    shown = []
    monkeypatch.setattr(sp.QMessageBox, "information",
                        lambda *a, **k: shown.append((a[1], a[2])))
    page._open_existing_workspace()
    QtTest.QTest.qWait(50)
    notices = [text for title, text in shown if title == "Unsaved Step0 changes"]
    assert len(notices) == 1
    assert "CD8: TopHat r=40  (saved: TopHat r=25)" in notices[0] and "CD3" not in notices[0]
    assert page._channel_params.get("CD8", {}).get("tophat_radius") == 40   # the draft is shown


def test_opening_a_workspace_without_a_draft_says_nothing(save, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from PyQt5 import QtTest
    page, path, before, ctx, base = save
    shown = []
    monkeypatch.setattr(sp.QMessageBox, "information",
                        lambda *a, **k: shown.append(a[1]))
    page._open_existing_workspace()
    QtTest.QTest.qWait(50)
    assert "Unsaved Step0 changes" not in shown


def test_a_parameter_edit_turns_the_line_unsaved(save):
    page, path, before, ctx, base = save
    page._channel_params = {}
    assert _status_of(page, "CD8").startswith("Saved: CD8 tophat  (r=25")
    page._loading_decision = True
    page._dec_radius.setValue(40)
    page._loading_decision = False
    page._on_dec_param_changed("tophat")            # what editing the box does
    assert page._decision_status.full_text().startswith("Unsaved: CD8 tophat  (r=40")


def test_while_a_save_runs_its_settings_are_not_called_saved(save):
    page, path, before, ctx, base = save
    page._save_and_continue()
    _prepared(page)                                 # running: the file holds r=40 already
    assert page.__dict__.get("_rm_kept_settings")
    assert _status_of(page, "CD8").startswith("Unsaved: CD8 tophat  (r=40")
