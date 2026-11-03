"""The loader's corrected binding is one immutable version per Save /
handoff (docs/v17_patch_reader_versions_plan.md §1.1), on a synthetic
OME-TIFF with two corrected products A and B of distinguishable pixels:

  * a read held inside the corrected open of A while the store is set to B
    returns A's pixels; the next read returns B's, and the loader never
    goes back to serving A (the old "stale store pinned" race);
  * clearing the store during a read never raises (the old crash);
  * decisions and store always come from the same version (the old torn
    read), also for `read_region_lowres` and `saved_store`.
"""

import os
import threading

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core import io_loader, saved_correction  # noqa: E402
from block01.core.io_loader import OMETIFFLoader  # noqa: E402

H, W = 64, 80
A, B = 111.0, 222.0


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    root = tmp_path_factory.mktemp("binding")
    path = root / "s.ome.tif"
    data = np.full((2, H, W), 7, np.uint16)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, tile=(32, 32),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3"]}})
    stores = {}
    for name, value in (("A", A), ("B", B)):
        z = zarr.open_group(str(root / f"{name}.zarr"), mode="w")
        z.attrs["mode"] = "full"
        z.create_dataset("CD3", data=np.full((H, W), value, np.float32), chunks=(32, 32))
        stores[name] = str(root / f"{name}.zarr")
    return str(path), stores


def _read(loader):
    return loader.read_region("CD3", 0, H, 0, W, normalize=False)


def test_a_read_held_inside_A_returns_A_then_B_for_good(slide, monkeypatch):
    path, stores = slide
    loader = OMETIFFLoader(path)
    loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    inside, release = threading.Event(), threading.Event()
    real_open = io_loader.zarr.open

    def held_open(p, *a, **k):
        if p == stores["A"]:
            inside.set()
            assert release.wait(10)
        return real_open(p, *a, **k)
    monkeypatch.setattr(io_loader.zarr, "open", held_open)

    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", _read(loader)))
    t.start()
    assert inside.wait(10)
    loader.set_corrected_zarr_store(stores["B"], {"CD3": "tophat"})   # the Save
    release.set()
    t.join(10)
    assert np.all(out["a"] == A), "the read that started on A must finish on A"
    assert np.all(_read(loader) == B)
    assert np.all(_read(loader) == B), "the loader went back to serving A"
    assert loader.corrected_store_binding()[0] == stores["B"]


def test_clearing_the_store_during_a_read_never_raises(slide, monkeypatch):
    """Cleared AFTER the store was opened and BEFORE it is used: the old code
    re-read the attribute and hit `None` (`channel_name in None`)."""
    path, stores = slide
    loader = OMETIFFLoader(path)
    loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    inside, release = threading.Event(), threading.Event()
    real_open = io_loader.zarr.open

    class _Held:
        def __init__(self, store):
            self._store = store

        @property
        def attrs(self):
            inside.set()
            assert release.wait(10)
            return self._store.attrs

        def __contains__(self, key):
            return key in self._store

        def __getitem__(self, key):
            return self._store[key]

        def group_keys(self):
            return self._store.group_keys()
    monkeypatch.setattr(io_loader.zarr, "open",
                        lambda p, *a, **k: _Held(real_open(p, *a, **k)))
    out, errors = {}, []

    def run():
        try:
            out["a"] = _read(loader)
        except Exception as exc:                           # noqa: BLE001
            errors.append(exc)
    t = threading.Thread(target=run)
    t.start()
    assert inside.wait(10)
    loader.set_corrected_zarr_store(None, {})              # a dataset switch clears it
    release.set()
    t.join(10)
    assert errors == [], errors
    assert np.all(out["a"] == A)
    monkeypatch.setattr(io_loader.zarr, "open", real_open)
    assert np.all(_read(loader) == 7), "cleared: raw pixels"


def test_one_version_for_decisions_and_store(slide):
    path, stores = slide
    loader = OMETIFFLoader(path)
    loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    first = loader._binding
    loader.set_corrected_zarr_store(stores["B"], {})       # B, nothing decided
    assert first.zarr_path == stores["A"] and first.decisions == {"CD3": "tophat"}
    assert loader.corrected_store_binding() == (stores["B"], {})
    assert np.all(_read(loader) == 7)                     # never A's store with B's decisions
    loader.set_correction_config({"channel_decisions": {"CD3": "original"}})
    assert loader.corrected_store_binding() == (stores["B"], {}), "config keeps the store"
    assert loader.correction_config["channel_decisions"]["CD3"] == "original"


def test_lowres_takes_one_version_for_its_fallback(slide, monkeypatch):
    """The store is cleared between the lowres dispatch's "is it corrected?"
    and the read it falls back to: the old code answered from one binding
    and read from another (raw pixels for a channel it had just called
    corrected)."""
    path, stores = slide
    loader = OMETIFFLoader(path)
    loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    inside, release = threading.Event(), threading.Event()
    real_exists = io_loader.os.path.exists
    calls = []

    def held_exists(p):
        calls.append(p)
        if p == stores["A"] and len(calls) == 1:
            inside.set()
            assert release.wait(10)
        return real_exists(p)
    monkeypatch.setattr(io_loader.os.path, "exists", held_exists)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "a", loader.read_region_lowres("CD3", 0, H, 0, W, 4, normalize=False)))
    t.start()
    assert inside.wait(10)
    loader.set_corrected_zarr_store(None, {})
    release.set()
    t.join(10)
    assert np.all(out["a"] == A), "the dispatch and the read used different versions"


def test_saved_store_answers_from_one_version(slide):
    """A Save lands between reading the path and reading the decisions: the
    old `saved_store` returned A's path with B's decisions."""
    path, stores = slide

    class _Loader(OMETIFFLoader):
        armed = False

        @property
        def _corrected_decisions(self):
            if self.armed:
                self.armed = False
                self.set_corrected_zarr_store(stores["B"], {"CD3": "cucim"})
            return self.__dict__.get("_dec", {})

        @_corrected_decisions.setter
        def _corrected_decisions(self, value):              # the old code assigned it
            self.__dict__["_dec"] = value

    loader = _Loader(path)
    loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    loader.armed = True
    got = saved_correction.saved_store(path)
    assert got in ((stores["A"], {"CD3": "tophat"}), (stores["B"], {"CD3": "cucim"})), got
