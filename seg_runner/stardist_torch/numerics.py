# The settings below mirror Mesmer's torch block (seg_runner/mesmer_torch/app.py)
# so the two engines behave the same under CUDA; the code is ours.
"""The torch settings the network runs under, and only for that pass.

`mesmer_torch/app.py` holds the same block for the same reason: they are
process-wide switches, and another engine or test in the same process must
not inherit them. `torch.use_deterministic_algorithms(True)` is NOT used --
it changes how cuBLAS may run elsewhere in the process (a Cellpose run would
fail on it).
"""
import contextlib

#: What the network runs under (recorded in provenance by the engine).
NUMERICS = {"dtype": "float32", "cudnn_deterministic": True, "cudnn_benchmark": False,
            "tf32": False, "batch_size": 1}


@contextlib.contextmanager
def numerics(device="cuda"):
    """Set NUMERICS for the network pass only, then restore what was there.

    Only a CUDA device gets the cuDNN switches: touching `torch.backends.cudnn`
    loads its CUDA libraries even in a process that runs on the CPU (measured:
    it cost a second CUDA context in the test process and made the Cellpose
    tests run out of memory on an 11 GB card)."""
    import torch
    b = torch.backends
    if not str(device).startswith("cuda"):
        yield
        return
    saved = (b.cudnn.deterministic, b.cudnn.benchmark, b.cudnn.allow_tf32, b.cuda.matmul.allow_tf32)
    b.cudnn.deterministic, b.cudnn.benchmark = True, False
    b.cudnn.allow_tf32 = b.cuda.matmul.allow_tf32 = False
    try:
        yield
    finally:
        (b.cudnn.deterministic, b.cudnn.benchmark, b.cudnn.allow_tf32, b.cuda.matmul.allow_tf32) = saved
