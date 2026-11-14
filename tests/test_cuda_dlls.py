"""`core/cuda_dlls.register` (Windows plan W1b): torch's `lib` folder made
findable for CuPy's NVRTC before CuPy is first used; nothing off Windows."""
import importlib.util
import os
import sys

import pytest

from block01.core import cuda_dlls


@pytest.mark.skipif(os.name == "nt", reason="the POSIX no-op")
def test_nothing_happens_off_windows(monkeypatch):
    monkeypatch.setattr(cuda_dlls, "_done", False)
    path = os.environ.get("PATH", "")
    assert cuda_dlls.register() == ""
    assert os.environ.get("PATH", "") == path


def test_torch_lib_is_found_without_importing_torch():
    if importlib.util.find_spec("torch") is None:
        pytest.skip("no torch")
    was_imported = "torch" in sys.modules
    lib = cuda_dlls._torch_lib()
    assert lib and os.path.isdir(lib) and os.path.basename(lib) == "lib"
    assert ("torch" in sys.modules) == was_imported


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_windows_registers_torch_lib_once(monkeypatch):
    if importlib.util.find_spec("torch") is None:
        pytest.skip("no torch")
    monkeypatch.setattr(cuda_dlls, "_done", False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    lib = cuda_dlls.register()
    assert lib and lib.lower() in os.environ["PATH"].lower()
    assert cuda_dlls.register() == ""                       # the second call: nothing
