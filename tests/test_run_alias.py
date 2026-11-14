"""A Step2 run's alias names (`SegmentMergeWorker._make_alias`, Windows plan
W1): a link to the source, never a copy of it, for a folder (a zarr) and a
file (an OME-TIFF). POSIX: a relative symlink, as before. Windows without
the symlink right: a junction for a folder, a hard link for a file."""
import logging
import os
import types

from block01.workers import segment_merge_worker as smw


def _owner(out):
    return types.SimpleNamespace(output_dir=str(out), _abs=smw.SegmentMergeWorker._abs,
                                 _logger=logging.getLogger("test_run_alias"))


def _alias(out, src, name):
    return smw.SegmentMergeWorker._make_alias(_owner(out), str(src), name)


def _is_link_to_folder(path, target):
    if os.path.islink(path):
        return True
    return os.name == "nt" and os.path.isdir(path) and os.path.samefile(path, target)


def test_a_folder_alias_is_a_link_not_a_copy(tmp_path):
    src = tmp_path / "roi" / "mask.zarr"
    (src / "0").mkdir(parents=True)
    (src / "0" / "chunk").write_bytes(b"x" * 1000)
    out = tmp_path / "run"
    out.mkdir()
    made = _alias(out, src, "global_mask.zarr")
    assert made == os.path.abspath(out / "global_mask.zarr")
    assert _is_link_to_folder(made, src)
    (src / "0" / "later").write_bytes(b"y")              # a link sees the source change
    assert os.path.exists(os.path.join(made, "0", "later"))


def test_a_file_alias_shares_the_source(tmp_path):
    src = tmp_path / "roi" / "mask.ome.tiff"
    src.parent.mkdir()
    src.write_bytes(b"z" * 4096)
    out = tmp_path / "run"
    out.mkdir()
    made = _alias(out, src, "global_mask.ome.tiff")
    assert os.path.samefile(made, src)                   # a link, not a copy
    assert open(made, "rb").read() == b"z" * 4096


def test_an_existing_alias_is_kept_and_a_missing_source_makes_none(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    assert _alias(out, tmp_path / "nothing", "global_mask.zarr") == ""
    keep = out / "global_dapi.ome.tiff"
    keep.write_bytes(b"old")
    src = tmp_path / "dapi.ome.tiff"
    src.write_bytes(b"new")
    assert _alias(out, src, "global_dapi.ome.tiff") == os.path.abspath(keep)
    assert keep.read_bytes() == b"old"
