"""Shared test set-up.

The Mesmer engine makes or refreshes files in its torch-weights directory at
start (docs/mesmer_torch_plan.md §10). No test may write into the real one:
for the whole session BLOCK01_MESMER_TORCH_PATH points at a scratch
directory holding a link to the real weights (when present), so engines --
in this process or in their own -- read the real file and write beside the
link. A test that sets its own override still wins.
"""
import json
import os
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_ENV = "BLOCK01_MESMER_TORCH_PATH"


@pytest.fixture(scope="session", autouse=True)
def _mesmer_weights_in_scratch(tmp_path_factory):
    entry = json.loads((_REPO / "envs" / "fusion_mesmer" / "models.json").read_text())[
        "models"]["mesmer_multiplex_segmentation_torch"]
    real = pathlib.Path(os.path.expanduser(os.environ.get(_ENV) or entry["root"]))
    scratch = tmp_path_factory.mktemp("mesmer_torch")
    for f in entry["files"]:
        if (real / f["path"]).is_file():
            (scratch / f["path"]).symlink_to(real / f["path"])
    old = os.environ.get(_ENV)
    os.environ[_ENV] = str(scratch)
    yield scratch
    if old is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = old
