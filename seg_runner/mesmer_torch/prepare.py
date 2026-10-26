"""Make the torch weights on first use (docs/mesmer_torch_plan.md §10).

`ensure()` is the one way the `.pt` comes into being -- the Mesmer engine
calls it before loading, `scripts/convert_mesmer_to_torch.py` calls it by
hand. With the file already there it only refreshes SOURCE.txt. Otherwise,
under a lock in the output directory, a CPU-only child process (`convert`,
the only code that imports TensorFlow) writes the file into a temporary
directory beside it; its content digest is checked and ONE `os.replace`
publishes it. That rename is the only commit point: a failure or a kill
before it leaves no final file (or the old one, when replacing), and the
leftover temporary directory is removed by the next holder of the lock.

SOURCE.txt only describes the published file and is always regenerated from
it, so it can be missing or stale for a moment but never describes other
weights. It is not part of success; an unwritable directory costs a warning.
"""
import fcntl
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from . import convert, weights

LOCK_FILE = ".convert.lock"
TEMP_PREFIX = ".convert-"
SOURCE_FILE = "SOURCE.txt"
CHILD_MODULE = "seg_runner.mesmer_torch.convert"
HAND_RUN = "scripts/convert_mesmer_to_torch.py"

SOURCE_TEXT = """content sha256 {digest}

Mesmer weights converted for PyTorch by Block01
(seg_runner/mesmer_torch/convert.py, docs/mesmer_torch_plan.md).

Source model: {root}
Source files (sha256):
{files}
deepcell {deepcell}  tensorflow {tensorflow}  torch {torch}
Head check against the Keras model (max |difference|): {diffs}

Changes: Keras Conv2D kernels transposed HWIO -> OIHW and stored with their
biases; batch-norm gamma/beta/moving mean/moving variance stored with each
layer's epsilon. No value is altered otherwise.

Licence: the original Mesmer weights are licensed under the deepcell-auth
ASSET_LICENSE (modified Apache 2.0): non-commercial academic use only; a
conversion is still the licensed Work. Do not commit or redistribute this
file. https://github.com/vanvalenlab/deepcell-auth/blob/main/ASSET_LICENSE
"""


class PrepareError(RuntimeError):
    """The torch weights could not be made; the message says what to do."""


def ensure(root, source_root, expected_digest, log, force=False):
    """The torch weights in directory `root`, made from the Keras model in
    `source_root` if missing (or always, with `force`). `expected_digest` is
    the manifest's content digest; None only when building a new baseline
    (`scripts/convert_mesmer_to_torch.py --update-manifest`). Returns the
    content digest of the file it made, or None when one was already there."""
    root = pathlib.Path(os.path.abspath(root))     # the child runs elsewhere
    target = root / weights.WEIGHTS_FILE
    if target.is_file() and not force:
        refresh_source(root, log)
        return None
    source_root = pathlib.Path(os.path.abspath(source_root))
    if not source_root.is_dir():
        raise PrepareError(f"no Keras Mesmer model at {source_root}: copy it there first "
                           "(or point DEEPCELL_MESMER_MODEL_PATH at it)")
    try:
        root.mkdir(parents=True, exist_ok=True)
        lock = open(root / LOCK_FILE, "a")
    except OSError as exc:
        raise PrepareError(f"cannot write the Mesmer torch weights into {root} ({exc}); "
                           "point BLOCK01_MESMER_TORCH_PATH at a writable directory") from exc
    with lock:
        try:                                      # released when the file closes
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("[Mesmer] another run is preparing the PyTorch weights; waiting for it. "
                f"Should this start time out, run {HAND_RUN} by hand, then start again.")
            fcntl.flock(lock, fcntl.LOCK_EX)
        for stale in root.glob(TEMP_PREFIX + "*"):    # left by a killed run
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)
        if target.is_file() and not force:        # another run made it meanwhile
            refresh_source(root, log)
            return None
        log("[Mesmer] first use on this machine: preparing the PyTorch weights "
            f"from the Keras model (one time). Should this start time out, run {HAND_RUN} "
            "by hand, then start again.")
        tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{os.getpid()}-", dir=root))
        try:
            made = tmp / weights.WEIGHTS_FILE
            run_converter(source_root, made, tmp)
            digest = weights.content_digest(weights.read(made))
            if expected_digest is not None and digest != expected_digest:
                raise PrepareError(f"the conversion gave other weights than the manifest names "
                                   f"(content {digest[:12]}..., want {expected_digest[:12]}...): "
                                   f"is {source_root} the original Mesmer model?")
            os.replace(made, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        log(f"[Mesmer] PyTorch weights ready: {target}")
        refresh_source(root, log)
        return digest


def run_converter(source_root, out, workdir):
    """`convert` in a child process: CPU only, TensorFlow there and not here.
    It stays in this process group, so stopping the engine stops it too.
    Always imported as `seg_runner.mesmer_torch.convert` from the directory
    holding `seg_runner` (self-contained), whatever name this process
    imported it under -- an alias such as `block01.` is not inherited."""
    top = pathlib.Path(os.path.abspath(__file__)).parents[2]
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["TF_CPP_MIN_LOG_LEVEL"] = "3"             # its CUDA library notes mean nothing here
    env["PYTHONPATH"] = str(top) + os.pathsep + env.get("PYTHONPATH", "")
    code = subprocess.run([sys.executable, "-m", CHILD_MODULE, "--source", str(source_root),
                           "--out", str(out)], cwd=str(workdir), env=env).returncode
    if code == convert.NO_TENSORFLOW:
        raise PrepareError("TensorFlow is needed once to convert the Keras Mesmer model; "
                           "run in the fusion_mesmer environment (envs/fusion_mesmer)")
    if code == convert.HEAD_CHECK:
        raise PrepareError("the converted Mesmer network does not reproduce the Keras model "
                           "(see the conversion messages above)")
    if code != 0 or not pathlib.Path(out).is_file():
        raise PrepareError(f"the Mesmer weight conversion failed (exit code {code}; "
                           "see the conversion messages above)")


def source_text(data):
    """SOURCE.txt for the weights `data` -- from the file alone, no clock."""
    src = data.get("source") or {}
    return SOURCE_TEXT.format(
        digest=weights.content_digest(data), root=src.get("root", "?"),
        files="\n".join(f"  {h}  {n}" for n, h in sorted((src.get("files") or {}).items())),
        deepcell=src.get("deepcell", "?"), tensorflow=src.get("tensorflow", "?"),
        torch=src.get("torch", "?"),
        diffs=", ".join(f"{d:.2e}" for d in src.get("head_diffs") or []) or "?")


def refresh_source(root, log):
    """Rewrite SOURCE.txt when it is missing or does not match the `.pt`."""
    root = pathlib.Path(root)
    path = root / SOURCE_FILE
    try:
        text = source_text(weights.read(root / weights.WEIGHTS_FILE))
    except Exception:  # noqa: BLE001 -- an unreadable file: loading reports it
        return
    try:
        if path.is_file() and path.read_bytes() == text.encode("utf-8"):
            return                                 # anything else, garbled too, is rewritten
        tmp = root / f"{TEMP_PREFIX}source-{os.getpid()}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log(f"[Mesmer] warning: could not write {path} ({exc}); the weights are unaffected")
