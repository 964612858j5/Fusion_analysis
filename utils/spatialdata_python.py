"""Which Python writes Step4's SpatialData export
(docs/v17_spatialdata_export_plan.md §2).

Block01's own environment cannot hold spatialdata (it needs NumPy 2;
TensorFlow keeps Block01 on 1.26), so the export runs in another
environment's Python -- Step5's. Order: `BLOCK01_SPATIALDATA_PYTHON`, then
the path chosen once in Step4 (saved in `<repo>/.block01/settings.json`).
A candidate counts only when `<python> -I -c "import spatialdata"` works;
nothing is guessed from environment names.
"""

import json
import os
import subprocess

ENV = "BLOCK01_SPATIALDATA_PYTHON"
SETTINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        ".block01", "settings.json")
KEY = "spatialdata_python"
CHECK_TIMEOUT_S = 180

_checked = {}                                   # path -> (ok, version or reason)
CHECK_ARGS = ["-I", "-c", "import spatialdata; print(spatialdata.__version__)"]


def cached(python):
    """The remembered answer for `python`, or None. A path that is not an
    executable file is answered at once."""
    python = str(python or "").strip()
    if not python:
        return False, "no Python chosen"
    if python in _checked:
        return _checked[python]
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        return remember(python, False, f"{python} is not an executable file")
    return None


def remember(python, ok, info):
    _checked[str(python)] = (bool(ok), str(info))
    return _checked[str(python)]


def answer(returncode, stdout, stderr):
    """(ok, version or why) from a finished `<python> CHECK_ARGS`."""
    out = (stdout or "").strip().splitlines()
    if returncode == 0 and out:
        return True, out[-1]
    last = ((stderr or "").strip().splitlines() or ["no output"])[-1]
    return False, f"spatialdata cannot be imported there: {last}"


def check(python):
    """(True, spatialdata version) or (False, why), cached per path. Blocks
    (seconds): for the worker thread; the page uses `cached` / `answer`
    with a QProcess."""
    python = str(python or "").strip()
    known = cached(python)
    if known is not None:
        return known
    try:
        p = subprocess.run([python] + CHECK_ARGS, capture_output=True, text=True,
                           timeout=CHECK_TIMEOUT_S)
        return remember(python, *answer(p.returncode, p.stdout, p.stderr))
    except (OSError, subprocess.SubprocessError) as exc:
        return remember(python, False, f"{python} could not be run: {exc}")


def saved():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return str(json.load(f).get(KEY) or "")
    except (OSError, ValueError):
        return ""


def save(python):
    """Remember `python` (already checked) for the next runs."""
    data = {}
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    data[KEY] = str(python)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS)


def candidates():
    """[(label, python)]: the environment variable, then the saved choice."""
    return [(label, python) for label, python in (("environment variable " + ENV, os.environ.get(ENV, "")),
                                                  ("Step4's saved choice", saved())) if python]


def find():
    """(python, version) of the first usable candidate, or (None, why)."""
    reasons = []
    for label, python in candidates():
        ok, info = check(python)
        if ok:
            return python, info
        reasons.append(f"{label}: {info}")
    return None, "; ".join(reasons) or "no SpatialData Python chosen yet"


__all__ = ["ENV", "CHECK_ARGS", "cached", "remember", "answer", "check", "candidates",
           "saved", "save", "find"]
