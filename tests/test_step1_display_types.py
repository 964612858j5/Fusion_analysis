"""Block A6 (docs/v17_small_review_items_plan.md §2): the display types the
Rust / Step3 / CPU code shares with the Python GPU path live in
`ui/step1_display_types.py`.

They were MOVED, not copied: the old modules import them back, so every name
is the identical object in the old and the new module (an `isinstance` or an
`except Step1GpuLayerError` written against either one still matches), and
the consumers no longer import the Python GPU modules for them. Also the
backend switch: unset / "rust" -> rust, "python" -> python, anything else ->
rust with one printed warning (it silently meant python before).
"""

import ast
import os
import pathlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("block01.qt")

from block01.ui import step1_core_adapter as adapter  # noqa: E402
from block01.ui import step1_display_types as display_types  # noqa: E402
from block01.ui import step1_gpu_binding as gpu_binding  # noqa: E402
from block01.ui import step1_gpu_layer as gpu_layer  # noqa: E402

FROM_LAYER = (
    "MODE_OVERLAY", "MODE_FUSION", "ROI_POLYGON_BOUNDS_TOLERANCE", "sanitize_roi_polygon",
    "DisplaySnapshot", "ViewportSnapshot", "LabelPlane", "LabelLayer", "LabelSnapshot",
    "LABEL_OUTLINE", "LABEL_FILL", "LABEL_TEXTURE_BYTES", "MAX_LABEL_RADIUS", "label_radius",
    "_LabelTextureLru", "_as_name", "_TextureRecord", "_check_gl", "Step1GpuLayerError",
)
FROM_BINDING = ("BindingBudgets",)
CONSUMERS = ("step1_core_binding.py", "step1_core_labels.py", "step3_core_labels.py",
             "step3_label_binding.py", "step1_viewer_mount.py")
UI = pathlib.Path(display_types.__file__).resolve().parent


@pytest.mark.parametrize("name", FROM_LAYER)
def test_layer_names_are_the_moved_objects(name):
    assert getattr(gpu_layer, name) is getattr(display_types, name)


@pytest.mark.parametrize("name", FROM_BINDING)
def test_binding_names_are_the_moved_objects(name):
    assert getattr(gpu_binding, name) is getattr(display_types, name)


def test_moved_names_are_defined_in_the_new_module():
    for name in FROM_LAYER + FROM_BINDING:
        value = getattr(display_types, name)
        if hasattr(value, "__module__") and callable(value):
            assert value.__module__ == display_types.__name__, name


@pytest.mark.parametrize("filename", CONSUMERS)
def test_consumers_take_moved_names_from_the_new_module(filename):
    moved = set(FROM_LAYER + FROM_BINDING)
    for node in ast.walk(ast.parse((UI / filename).read_text(encoding="utf-8"))):
        if (isinstance(node, ast.ImportFrom) and node.level == 1
                and node.module in ("step1_gpu_layer", "step1_gpu_binding")):
            names = {alias.name for alias in node.names}
            assert not names & moved, (filename, sorted(names & moved))


def _backend(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv(adapter.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(adapter.BACKEND_ENV, value)
    result = adapter.requested_backend()
    return result, capsys.readouterr().out


@pytest.fixture
def no_warnings_yet(monkeypatch):
    monkeypatch.setattr(adapter, "_WARNED_BACKENDS", set())


def test_backend_unset_is_rust(monkeypatch, capsys, no_warnings_yet):
    assert _backend(monkeypatch, capsys, None) == (adapter.BACKEND_RUST, "")


def test_backend_rust_is_rust(monkeypatch, capsys, no_warnings_yet):
    assert _backend(monkeypatch, capsys, "rust") == (adapter.BACKEND_RUST, "")
    assert _backend(monkeypatch, capsys, " Rust ") == (adapter.BACKEND_RUST, "")


def test_backend_python_is_python(monkeypatch, capsys, no_warnings_yet):
    assert _backend(monkeypatch, capsys, "python") == (adapter.BACKEND_PYTHON, "")


def test_backend_typo_is_rust_with_one_warning(monkeypatch, capsys, no_warnings_yet):
    result, out = _backend(monkeypatch, capsys, "pyhton")
    assert result == adapter.BACKEND_RUST
    assert out.count("\n") == 1
    assert adapter.BACKEND_ENV in out and "pyhton" in out and "rust" in out
    # asked again (every mount asks): still rust, no second warning
    assert _backend(monkeypatch, capsys, "pyhton") == (adapter.BACKEND_RUST, "")
