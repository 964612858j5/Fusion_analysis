"""Which loader serves a slide's SAVED corrected product (Step0 Save).

A process-wide answer, keyed by the slide's path, so a viewer that only
knows its pixel source (`SourceIdentity.dataset_path`) can ask "is there a
saved product for this channel?" without being handed the page that owns
the loader. `OMETIFFLoader.set_corrected_zarr_store` registers itself here;
every registration bumps `version()`, which is how a viewer notices a Save.

Held weakly: a closed slide's loader is not kept alive by this table.
"""

import os
import threading
import weakref

_LOCK = threading.Lock()
_LOADERS = {}
_VERSION = [0]


def _key(path):
    return os.path.abspath(str(path or ""))


def register_loader(loader):
    """`loader` now answers for its slide's saved product."""
    path = getattr(loader, "filepath", None)
    if not path:
        return
    with _LOCK:
        _LOADERS[_key(path)] = weakref.ref(loader)
        _VERSION[0] += 1


def version():
    """Moves on every registration (a Save, a project load, a reset)."""
    return _VERSION[0]


def saved_store(dataset_path):
    """`(corrected_zarr_path, decisions)` for the slide, or `("", {})`."""
    with _LOCK:
        ref = _LOADERS.get(_key(dataset_path))
    loader = ref() if ref is not None else None
    if loader is None:
        return "", {}
    path = getattr(loader, "_corrected_zarr_path", None) or ""
    decisions = dict(getattr(loader, "_corrected_decisions", None) or {})
    if not path or not os.path.exists(path):
        return "", {}
    return str(path), decisions
