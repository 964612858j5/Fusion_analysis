"""Windows: where CuPy finds the CUDA 12 DLLs (docs/v17_windows_native_plan.md,
W1b; the laptop acceptance of 2026-11-14).

`cupy-cuda12x` compiles its kernels with NVRTC (`nvrtc64_120_0.dll`). A
machine without the CUDA 12 toolkit has that DLL only in torch's `lib`
folder, and Python (3.8+) does not search PATH for a DLL an extension
needs -- so before torch was imported, Step0's smoke test failed and the
whole session fell back to CPU filters. torch registers its folder itself
on import; this registers it before CuPy is first used, without importing
torch. A no-op off Windows, when torch is absent, and on a second call.
"""
import os

_done = False
_handle = None          # kept: closing it would remove the folder again


def _torch_lib():
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")        # torch is not imported
    except (ImportError, ValueError):
        return ""
    for root in (spec.submodule_search_locations or []) if spec else []:
        lib = os.path.join(root, "lib")
        if os.path.isdir(lib):
            return lib
    return ""


def register():
    """Make torch's CUDA DLLs findable by CuPy (Windows only); returns the
    folder registered, or ""."""
    global _done, _handle
    if os.name != "nt" or _done:
        return ""
    _done = True
    lib = _torch_lib()
    if not lib:
        return ""
    try:
        _handle = os.add_dll_directory(lib)             # an extension's dependencies
    except OSError:
        return ""
    path = os.environ.get("PATH", "")                   # a plain LoadLibrary by name
    if lib.lower() not in path.lower().split(os.pathsep):
        os.environ["PATH"] = path + os.pathsep + lib if path else lib
    return lib
