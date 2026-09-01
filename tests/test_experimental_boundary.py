"""The production path must not import `viewer.experimental`.

Narrow on purpose: an AST scan for IMPORTS of that package from `ui/`,
`main.py` and `viewer/` (excluding `viewer/experimental/` itself). It does
not police class names, does not look at `scripts/` -- benchmarks and the
demo are exactly who is allowed to opt in -- and is not a repo-wide symbol
blacklist.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENTAL = "viewer.experimental"


def _production_python_files():
    files = [ROOT / "main.py"]
    for directory in ("ui", "viewer"):
        for path in (ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if "experimental" in path.relative_to(ROOT).parts:
                continue
            files.append(path)
    return [f for f in files if f.exists()]


def _imports_experimental(tree, path):
    """Every shape the import can take.

    Four of them, and the fourth is the one a name-based check misses:

    * `import viewer.experimental.coverage_prefetch`
    * `from block01.viewer.experimental.coverage_prefetch import X`
    * `from . import experimental` / `from ..viewer import experimental`
      (relative, `node.level > 0`)
    * `from block01.viewer import experimental` / `from viewer import
      experimental` -- ABSOLUTE, so `node.level == 0`, and the package name
      is in the imported NAMES, not in `node.module`.
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if EXPERIMENTAL in alias.name or alias.name.endswith(
                        ".experimental"):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if EXPERIMENTAL in module or module.endswith("experimental"):
                hits.append(module)
                continue
            # `from <anything> import experimental`, relative or absolute.
            for alias in node.names:
                if alias.name == "experimental":
                    hits.append(f"{'.' * node.level}{module} experimental"
                                .strip())
    return hits


def test_no_production_module_imports_viewer_experimental():
    files = _production_python_files()
    assert len(files) > 20, (
        f"the scan found only {len(files)} production files -- it is not "
        "looking where it thinks it is")

    offenders = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _imports_experimental(tree, path)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits

    assert offenders == {}, (
        "production code imported the experimental package: " f"{offenders}")


OFFENDING_IMPORTS = [
    "from block01.viewer.experimental.coverage_prefetch import X",
    "import viewer.experimental.coverage_prefetch",
    "import block01.viewer.experimental.coverage_prefetch as cp",
    "from viewer.experimental import coverage_prefetch",
    "from .. import experimental",
    "from . import experimental",
    "from ..viewer import experimental",
    # The absolute `from <pkg> import experimental` forms -- `node.level`
    # is 0 and the package name is in the NAMES, not in `node.module`, so a
    # module-string check alone lets these through.
    "from block01.viewer import experimental",
    "from viewer import experimental",
]

INNOCENT_IMPORTS = [
    "from block01.viewer.multichannel_prefetch import X",
    "import viewer.scheduler",
    "from . import prefetch_policy",
    "from viewer import prefetch_policy",
    "experimental = 1",
]


@pytest.mark.parametrize("source", OFFENDING_IMPORTS)
def test_the_scan_catches_every_import_shape(source):
    """A guard on the guard: a scan that can never fail is worthless, and
    one that misses a shape is worse -- it reads as proof."""
    assert _imports_experimental(ast.parse(source), "fake.py"), source


@pytest.mark.parametrize("source", INNOCENT_IMPORTS)
def test_the_scan_does_not_flag_innocent_imports(source):
    assert _imports_experimental(ast.parse(source), "fake.py") == [], source
