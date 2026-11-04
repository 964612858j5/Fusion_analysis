"""docs/v17_install_plan.md §1.1: one compatibility check for the Rust core.

`core_unavailable_reason()` refuses a missing core, a core without or with
another `CORE_API`, and one older than `MIN_CORE_VERSION`; a newer core with
the same `CORE_API` is accepted. Every independent user asks it: Step0's
engine, the Step1 viewer (whose notice then names the reason), fusion (falls
back to Python with the reason) and the exit wait (skips the Rust readers).
"""

import os
import re
import sys
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.ui import step1_core_adapter as adapter  # noqa: E402
from block01.utils import closing_threads  # noqa: E402


def _fake_core(monkeypatch, **attrs):
    core = types.ModuleType("fusion_viewer_core")
    for k, v in attrs.items():
        setattr(core, k, v)
    monkeypatch.setitem(sys.modules, "fusion_viewer_core", core)
    return core


def test_the_installed_core_is_accepted():
    core = pytest.importorskip("fusion_viewer_core")
    assert core.CORE_API == adapter.REQUIRED_CORE_API
    assert adapter.core_unavailable_reason() == ""


def test_a_missing_core_is_refused(monkeypatch):
    monkeypatch.setitem(sys.modules, "fusion_viewer_core", None)      # import raises ImportError
    assert "not installed" in adapter.core_unavailable_reason()


@pytest.mark.parametrize("attrs, words", [
    ({"__version__": "0.2.0"}, "has no CORE_API"),
    ({"__version__": "0.1.0"}, "has no CORE_API"),                     # R0..v17 builds
    ({"__version__": "0.3.0", "CORE_API": 2}, "has CORE_API 2"),
    ({"__version__": "0.1.9", "CORE_API": 1}, "older than 0.2.0"),
    ({"CORE_API": 1}, "no readable version"),
])
def test_an_incompatible_core_is_refused_with_the_remedy(monkeypatch, attrs, words):
    _fake_core(monkeypatch, **attrs)
    why = adapter.core_unavailable_reason()
    assert words in why
    assert "run scripts/install_block01.sh" in why


@pytest.mark.parametrize("version", ["0.2.0", "0.2.1", "0.10.0", "1.0.0"])
def test_an_equal_or_newer_compatible_core_is_accepted(monkeypatch, version):
    _fake_core(monkeypatch, __version__=version, CORE_API=1)
    assert adapter.core_unavailable_reason() == ""


def test_step0_engine_refuses_an_incompatible_core(monkeypatch):
    from block01.ui.step0 import engine_mount
    monkeypatch.setenv(adapter.BACKEND_ENV, adapter.BACKEND_RUST)
    _fake_core(monkeypatch, __version__="0.2.0", CORE_API=7)
    assert "has CORE_API 7" in engine_mount.engine_refusal()


def test_the_exit_wait_skips_the_readers_of_a_refused_core(monkeypatch):
    # what main.py passes at exit
    ok = lambda: not adapter.core_unavailable_reason()          # noqa: E731
    calls = []
    _fake_core(monkeypatch, __version__="0.1.0",
               wait_closing=lambda t: calls.append(t) or 5)
    assert closing_threads.wait_all(0.1, rust_ok=ok)["rust_left"] == 0
    assert calls == []
    _fake_core(monkeypatch, __version__="0.2.0", CORE_API=1,
               wait_closing=lambda t: calls.append(t) or 2)
    assert closing_threads.wait_all(0.1, rust_ok=ok)["rust_left"] == 2
    assert len(calls) == 1


def test_main_passes_the_check_to_the_exit_wait_and_utils_imports_no_ui():
    import pathlib
    root = pathlib.Path(adapter.__file__).resolve().parent.parent
    assert "rust_ok=lambda: not core_unavailable_reason()" in (root / "main.py").read_text(encoding="utf-8")
    for d in ("core", "workers", "viewer", "utils", "sources", "seg_runner"):
        for f in (root / d).rglob("*.py"):
            text = f.read_text(encoding="utf-8", errors="replace")
            assert not re.search(r"from \.+ui\b|from block01\.ui\b|import block01\.ui\b", text), f


def test_fusion_falls_back_to_python_on_an_incompatible_core(tmp_path, monkeypatch, capsys):
    pytest.importorskip("block01.qt")
    f5 = pytest.importorskip("test_f5_rust_fusion")
    if f5.core is None or not hasattr(f5.core, "FuseJob"):
        pytest.skip("fusion_viewer_core without FuseJob")
    slide = f5._slide(tmp_path / "s.ome.tif", "uint8")
    _, _, py = f5._run_worker(tmp_path, "py", slide, "python", monkeypatch)
    monkeypatch.setattr(f5.core, "CORE_API", 2)
    w, errors, rs = f5._run_worker(tmp_path, "rs", slide, "rust", monkeypatch)
    assert not errors and w.engine_by_region == {"full": "python"}
    assert "has CORE_API 2" in capsys.readouterr().out
    f5._assert_same(py, rs)


def test_the_step1_notice_names_a_refused_core(monkeypatch):
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount
    mount = Step1WholeSlideMount.__new__(Step1WholeSlideMount)
    mount._gpu_wanted = True
    monkeypatch.setenv(adapter.BACKEND_ENV, adapter.BACKEND_RUST)
    _fake_core(monkeypatch, __version__="0.1.0")
    assert mount._decide_rust(None) is False
    assert "has no CORE_API" in mount._rust_core_refused
    mount.viewer = types.SimpleNamespace(host=types.SimpleNamespace(status="", missing_notice=()))
    mount._missing_windows = ()
    mount.gpu_binding = None
    assert "The fast viewer is not used: fusion_viewer_core 0.1.0 has no CORE_API" in mount.notice()
    _fake_core(monkeypatch, __version__="0.2.0", CORE_API=1)
    mount._decide_rust(None)
    assert mount._rust_core_refused == ""
    assert "fast viewer" not in mount.notice()
