"""Step0 Save in place (user ruling 2026-11-02,
docs/v17_save_inplace_step1_ahead_plan.md §A).

* The changed channels are computed into a staging product and moved into
  the live one when the run has finished: the other channels are not
  rewritten (same files), no new run folder appears, nothing is left behind.
* A crash during the move is finished on the next open; one before it
  leaves the live product as it was.
* A correction change alone does not make a pre-segmentation result stale.

Synthetic slides and products in the test's temporary directory only.
"""

import os

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core import preseg_run, product_swap  # noqa: E402
from test_step0_coarse_plane_write import (  # noqa: E402
    _Loader, _plane, _run_save, _slide)


def _files(path):
    """{relative file: (inode, mtime_ns)} under `path`."""
    out = {}
    for root, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(root, name)
            st = os.stat(full)
            out[os.path.relpath(full, path)] = (st.st_ino, st.st_mtime_ns)
    return out


def _level0(out, ch):
    return np.asarray(zarr.open_group(str(out / "corrected_channels.zarr"), mode="r")
                      ["ROI_1"][ch][:, :], np.float32)


# ── the worker: only the changed channel moves ─────────────────────────────

def test_a_second_save_rewrites_only_the_changed_channel(tmp_path):
    out = tmp_path / "run"
    slide_a, data_a = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide_a, data_a), out)
    product = out / "corrected_channels.zarr"
    before_b = {k: v for k, v in _files(product).items() if k.startswith("ROI_1/CH_B")}
    old_a = _level0(out, "CH_A").copy()
    keep_b = _level0(out, "CH_B").copy()
    assert before_b

    slide_b, data_b = _slide(tmp_path, seed=2)
    _run_save(tmp_path, _Loader(slide_b, data_b), out, channels=("CH_A",), incremental=True)

    after = _files(product)
    assert {k: after[k] for k in before_b} == before_b           # CH_B: the same files
    assert np.array_equal(_level0(out, "CH_B"), keep_b)
    assert not np.array_equal(_level0(out, "CH_A"), old_a)        # CH_A: new pixels
    assert sorted(os.listdir(out)) == ["corrected_channels.zarr", "corrected_coarse.zarr",
                                       "corrected_display.zarr"]   # no staging, no journal
    attrs = zarr.open_group(str(product), mode="r").attrs
    assert attrs["output_dir"] == str(out)
    # the new channel's plane carries the new channel's token
    token = zarr.open_group(str(product), mode="r")["ROI_1"]["CH_A"].attrs["source_identity"]
    assert _plane(out, "CH_A").attrs["source_identity"] == token


def test_a_worker_error_leaves_the_product_as_it_was(tmp_path, monkeypatch):
    from block01.ui.step0 import search_ctrl
    out = tmp_path / "run"
    slide, data = _slide(tmp_path, seed=1)
    _run_save(tmp_path, _Loader(slide, data), out)
    before = _files(out)

    def boom(*a, **k):
        raise RuntimeError("GPU gone")
    monkeypatch.setattr(search_ctrl, "correct_tile", boom)
    worker = _run_save(tmp_path, _Loader(slide, data), out, channels=("CH_A",),
                       incremental=True)
    assert worker is not None
    assert _files(out) == before


# ── the move and its recovery ──────────────────────────────────────────────

def _product(run, channels, value, group="R1"):
    for name in product_swap.ORDER:
        root = zarr.open_group(os.path.join(run, name), mode="a")
        g = root.require_group(group)
        for ch in channels:
            g.create_dataset(ch, data=np.full((4, 4), value, np.float32), overwrite=True)


def _value(run, ch, name=product_swap.PRODUCT, group="R1"):
    return float(zarr.open_group(os.path.join(run, name), mode="r")[group][ch][0, 0])


def test_swap_in_replaces_and_removes_channels(tmp_path):
    run = str(tmp_path / "run")
    _product(run, ["A", "B", "C"], 1.0)
    staged = product_swap.new_staging(run)
    _product(staged, ["A"], 2.0)
    product_swap.swap_in(run, [("R1", "A")], remove=[("R1", "C")])
    assert product_swap.pending(run)                 # undoable until the Save completed
    product_swap.commit(run)
    for name in product_swap.ORDER:
        assert _value(run, "A", name) == 2.0 and _value(run, "B", name) == 1.0
        assert "C" not in zarr.open_group(os.path.join(run, name), mode="r")["R1"]
    assert sorted(os.listdir(run)) == sorted(product_swap.ORDER)


def test_a_crash_inside_the_move_is_undone_on_open(tmp_path, monkeypatch):
    """The Save never published, so its move is rolled back: the live
    product is exactly the one before it."""
    run = str(tmp_path / "run")
    _product(run, ["A", "B"], 1.0)
    staged = product_swap.new_staging(run)
    _product(staged, ["A", "N"], 2.0)            # N: a channel the product did not have
    real = os.rename
    calls = {"n": 0}

    def crash(src, dst):
        calls["n"] += 1
        if calls["n"] == 6:                      # level 0 of A: old moved away, new not in
            raise KeyboardInterrupt("power cut")
        return real(src, dst)
    monkeypatch.setattr(product_swap.os, "rename", crash)
    with pytest.raises(KeyboardInterrupt):
        product_swap.swap_in(run, [("R1", "A"), ("R1", "N")])
    monkeypatch.setattr(product_swap.os, "rename", real)
    assert not os.path.isdir(os.path.join(run, product_swap.PRODUCT, "R1", "A"))
    assert product_swap.recover(run) is True
    assert _value(run, "A") == 1.0 and _value(run, "B") == 1.0
    assert "N" not in zarr.open_group(os.path.join(run, product_swap.PRODUCT), mode="r")["R1"]
    assert sorted(os.listdir(run)) == sorted(product_swap.ORDER)


def test_a_save_that_does_not_complete_puts_everything_back(tmp_path):
    run = str(tmp_path / "run")
    _product(run, ["A", "B", "C"], 1.0)
    attrs_before = dict(zarr.open_group(os.path.join(run, product_swap.PRODUCT)).attrs)
    staged = product_swap.new_staging(run)
    _product(staged, ["A"], 2.0)
    zarr.open_group(os.path.join(staged, product_swap.PRODUCT)).attrs["correction_config"] = {"x": 1}
    product_swap.swap_in(run, [("R1", "A")], remove=[("R1", "C")])
    assert _value(run, "A") == 2.0                   # in place while the handoff is written
    assert product_swap.rollback(run) is True        # ...which failed
    for name in product_swap.ORDER:
        assert _value(run, "A", name) == 1.0 and _value(run, "C", name) == 1.0
    assert dict(zarr.open_group(os.path.join(run, product_swap.PRODUCT)).attrs) == attrs_before
    assert sorted(os.listdir(run)) == sorted(product_swap.ORDER)


def test_a_crash_before_the_move_leaves_the_product(tmp_path):
    run = str(tmp_path / "run")
    _product(run, ["A"], 1.0)
    staged = product_swap.new_staging(run)
    _product(staged, ["A"], 2.0)                 # computed, never moved (no journal)
    assert product_swap.recover(run) is True
    assert _value(run, "A") == 1.0
    assert product_swap.recover(run) is False    # nothing left


def test_swap_whole_replaces_every_product_dir(tmp_path):
    run = str(tmp_path / "run")
    _product(run, ["A", "B"], 1.0)
    staged = product_swap.new_staging(run)
    _product(staged, ["A"], 3.0, group="R2")     # a new region
    product_swap.swap_whole(run)
    product_swap.commit(run)
    root = zarr.open_group(os.path.join(run, product_swap.PRODUCT), mode="r")
    assert list(root.group_keys()) == ["R2"] and _value(run, "A", group="R2") == 3.0
    assert root.attrs["output_dir"] == os.path.abspath(run)


# ── pre-segmentation: a correction change alone is not "out of date" ───────

def _identity(token, decision="tophat", remap="r1"):
    manifest = {"corrected_decisions": {"CD3": decision},
                "source_identity": {"dataset_path": "/s.tif", "dataset_fingerprint": "f"},
                "channel_remap_config_hash": remap, "handoff_schema_version": 2}
    return preseg_run.pixel_identity(manifest, None, ["DAPI", "CD3"],
                                     {"CD3": {"source_identity": token}})


def test_a_correction_change_alone_does_not_make_a_result_stale():
    old = _identity("t1")
    run = {"source": {"pixel_key": preseg_run.pixel_key(old), "pixel_identity": old},
           "fusion": {"hash": "fh"}}
    new = _identity("t2", decision="cucim")
    key = preseg_run.pixel_key(new)
    assert preseg_run.is_stale(run, key, "fh", new) is False
    assert preseg_run.is_stale(run, key, "fh2", new) is True     # saved fusion settings moved
    other = _identity("t1", remap="r2")
    assert preseg_run.is_stale(run, preseg_run.pixel_key(other), "fh", other) is True
    # a caller without the identity keeps the old exact comparison
    assert preseg_run.is_stale(run, key, "fh") is True



def test_a_result_is_stale_once_the_fusion_settings_are_saved_again():
    """Saved again with the same weights after a component's correction
    changed: the hash is the same, the recorded component identities are
    not -- the result is out of date (ruling 2: stale after a re-save)."""
    ident = _identity("t1")
    run = {"source": {"pixel_key": preseg_run.pixel_key(ident), "pixel_identity": ident},
           "fusion": {"hash": "fh", "components": {"CD3": ["tophat", "t1"]}}}
    key = preseg_run.pixel_key(ident)
    assert preseg_run.is_stale(run, key, "fh", ident, {"CD3": ["tophat", "t1"]}) is False
    assert preseg_run.is_stale(run, key, "fh", ident, {"CD3": ["tophat", "t2"]}) is True
