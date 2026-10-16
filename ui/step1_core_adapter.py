"""v16_5 (R0): the shape of the Rust viewer core's Python boundary.

DEFINITIONS ONLY. Nothing in the product imports this module yet; Step1
still mounts `Step1GpuLayer`. The full contract -- every member below, the
manifest fields, the notice socket and the switch -- is written out in
`docs/v16_5_r0_interfaces.md`, which is the authority; this file is the
same contract as code, plus the one piece R0 really runs: building the raw
manifest the Rust reader opens (`build_raw_manifest`).
"""

import os
from typing import Any, Dict, Optional, Protocol, Tuple

import numpy as np

#: The switch between the two viewer backends. "rust" (the default since
#: 2026-10-16, user ruling) asks for the core and falls back to "python",
#: with the reason logged, when the core cannot start (plan §5.3);
#: "python" is the existing path, kept whole as the fallback.
BACKEND_ENV = "BLOCK01_STEP1_BACKEND"
BACKEND_PYTHON = "python"
BACKEND_RUST = "rust"

#: TIFF compression codes the Rust reader decodes.
RUST_COMPRESSIONS = (1, 5, 8, 32946, 50000)

_DECODER_CODES = {None: 1, "lzw_decode": 5, "zlib_decode": 8, "zstd_decode": 50000}


def requested_backend() -> str:
    """What the user or a test asked for; unset means "rust" (the default),
    unknown values mean "python"."""
    value = os.environ.get(BACKEND_ENV, BACKEND_RUST).strip().lower()
    return value if value in (BACKEND_PYTHON, BACKEND_RUST) else BACKEND_PYTHON


def core_unavailable_reason(minimum_version: str = "0.1.0") -> str:
    """"" when `fusion_viewer_core` imports at `minimum_version` or later,
    else why not -- the reason a "rust" request falls back."""
    try:
        import fusion_viewer_core as core
    except ImportError as exc:
        return f"fusion_viewer_core is not installed: {exc}"
    have = tuple(int(p) for p in core.__version__.split(".")[:3])
    want = tuple(int(p) for p in minimum_version.split(".")[:3])
    if have < want:
        return f"fusion_viewer_core {core.__version__} is older than {minimum_version}"
    return ""


class Step1CoreBackend(Protocol):
    """Everything Step1's users ask of the viewer layer (plan §5.2).

    `Step1GpuLayer` already answers this; the Rust-backed `Step1CoreLayer`
    (a QOpenGLWidget, from S-Raw) answers the same, so the mount, the
    memory registry, Step3 and the A9 tools do not care which is mounted.
    """

    # Step1WholeSlideMount
    initialized: bool
    init_error: Optional[Exception]
    hidden: bool
    hold_growth: bool
    upload_budget_ms: Optional[float]

    def attach(self, view_adapter) -> None: ...
    def submit(self, source_descriptor, display_snapshot, viewport_snapshot) -> Dict[str, Any]: ...
    def is_resident(self, plane) -> bool: ...
    def environment_report(self) -> Dict[str, Any]: ...
    def cache_stats(self) -> Dict[str, int]: ...
    def dispose(self) -> Dict[str, Any]: ...

    # gpu_memory
    def gpu_bytes(self) -> int: ...
    def release_to_coarse(self) -> int: ...
    def set_smooth(self, smooth: bool) -> None: ...
    def prepare_headroom(self) -> bool: ...

    # Step3
    def set_labels(self, snapshot) -> Dict[str, Any]: ...
    def label_stats(self) -> Dict[str, Any]: ...
    def readback_label_ids_for_test(self) -> np.ndarray: ...

    # tests
    def readback_rgba_for_test(self) -> np.ndarray: ...


def _identity(st) -> Tuple[int, int, int]:
    return int(st.st_size), int(st.st_mtime_ns), int(st.st_ino)


def _u64(values) -> bytes:
    return np.ascontiguousarray(np.asarray(values, dtype="<u8")).tobytes()


def _level_from_plan(plan, channels: int) -> Dict[str, Any]:
    decode = getattr(plan.decode, "__name__", None)
    code = _DECODER_CODES.get(decode)
    if code not in RUST_COMPRESSIONS:
        raise ValueError(f"compression {decode!r} is not decoded by the core yet")
    return {
        "height": plan.height, "width": plan.width,
        "block_rows": plan.tile[0], "block_cols": plan.tile[1], "stripped": False,
        "compression": code, "predictor": int(plan.predictor),
        "sample_bytes": plan.dtype.itemsize, "kind": plan.dtype.kind,
        "big_endian": plan.dtype.byteorder == ">"
                      or (plan.dtype.byteorder == "=" and np.little_endian is False),
        "channels": channels,
        "offsets": _u64(np.concatenate(plan.offsets)),
        "counts": _u64(np.concatenate(plan.counts)),
    }


def _level_from_strips(path: str, level: int, channels: int, identity) -> Dict[str, Any]:
    """A level the Python direct path does not take because its pages are
    stored in strips (Kevin's qptiff, coarsest level): the same table, one
    full-width block per strip. `identity` is the file the other levels'
    tables came from; the file parsed here must be that one."""
    import tifffile

    with tifffile.TiffFile(path) as tf:
        if _identity(os.fstat(tf.filehandle.fileno())) != identity:
            raise ValueError(f"{path} changed while its manifest was being built")
        pages = [p.aspage() if hasattr(p, "aspage") else p
                 for p in tf.series[0].levels[level].pages]
        if len(pages) != channels:
            raise ValueError(f"level {level}: {len(pages)} pages for {channels} channels")
        first = pages[0]
        dtype = np.dtype(first.dtype).newbyteorder(tf.byteorder)
        height, width = (int(v) for v in first.shape[-2:])
        rows = int(first.rowsperstrip)
        for page in pages:
            if (page.is_tiled or int(page.samplesperpixel) != 1
                    or int(page.rowsperstrip) != rows
                    or int(page.compression) != int(first.compression)
                    or int(page.predictor) != int(first.predictor)
                    or tuple(int(v) for v in page.shape[-2:]) != (height, width)
                    or int(page.bitspersample) != dtype.itemsize * 8):
                raise ValueError(f"level {level}: strips are not one simple layout")
        code = int(first.compression)
        if code not in RUST_COMPRESSIONS:
            raise ValueError(f"level {level}: compression {code} not decoded yet")
        return {
            "height": height, "width": width, "block_rows": rows, "block_cols": width,
            "stripped": True, "compression": code, "predictor": int(first.predictor),
            "sample_bytes": dtype.itemsize, "kind": dtype.kind,
            "big_endian": tf.byteorder == ">", "channels": channels,
            "offsets": _u64(np.concatenate([np.asarray(p.dataoffsets) for p in pages])),
            "counts": _u64(np.concatenate([np.asarray(p.databytecounts) for p in pages])),
        }


def build_raw_manifest(provider) -> Dict[str, Any]:
    """The raw-pyramid manifest of `provider` (a `RawTileProvider`), as
    `fusion_viewer_core.RawReader` takes it.

    Block tables come from the provider's own `_ChunkPlan` -- the same
    offsets the Python direct path reads -- and, for a stripped level, from
    the TIFF pages. The file identity is that of the descriptor the Python
    direct path reads from (the file those tables describe), and the path
    must still name that file; the Rust reader then refuses any other.
    `source_identity` is the provider's own `SourceIdentity`, carried for
    cache keys (no second identity scheme).
    """
    path = provider.path
    channels = provider.num_channels
    plans = [provider._direct_plan(level) for level in range(provider.num_levels)]
    if provider._chunk_fd is not None:
        file_identity = _identity(os.fstat(provider._chunk_fd))
    else:                                    # no tiled level: the strips decide
        file_identity = _identity(os.stat(path))
    if _identity(os.stat(path)) != file_identity:
        raise ValueError(f"{path} changed after this provider opened it")
    size, mtime_ns, file_number = file_identity
    # ... and it must be the file the provider's own cache keys name (its
    # `SourceIdentity` fingerprint is taken once and kept): this also holds
    # a file with no tiled level, where there is no descriptor to compare
    identity = provider.source_identity()
    if identity.dataset_fingerprint != f"{size}:{mtime_ns}":
        raise ValueError(f"{path} changed after this provider opened it")
    levels = [_level_from_plan(plan, channels) if plan is not None
              else _level_from_strips(path, level, channels, file_identity)
              for level, plan in enumerate(plans)]
    return {
        "path": path, "size": size, "mtime_ns": mtime_ns, "file_number": file_number,
        "source_identity": {"dataset_path": identity.dataset_path,
                            "dataset_fingerprint": identity.dataset_fingerprint,
                            "stage": identity.stage},
        "dtype": str(np.dtype(provider._dtype)),
        "levels": levels,
    }


# ── R1: Step1's per-channel sources (plan docs/v16_5_r1_plan.md §2, §4.5) ──

def _array_spec(array) -> Dict[str, Any]:
    """One opened zarr v2 array as the core's `ArrayMeta` + identity: the
    directory the opener resolved (not a path built here), its `.zarray`
    as written, and the identity of `.zarray` / `.zattrs`, taken before
    and after reading so a file changing meanwhile is caught."""
    import json

    store = getattr(array, "store", None)
    root = getattr(store, "path", None)
    if not root or type(store).__name__ != "DirectoryStore":
        raise ValueError(f"{array!r}: not a directory store")
    folder = os.path.join(str(root), str(getattr(array, "path", "") or ""))

    def stats():
        out = []
        for name in (".zarray", ".zattrs"):
            path = os.path.join(folder, name)
            out.append(_identity(os.stat(path)) if os.path.exists(path) else None)
        return out

    before = stats()
    with open(os.path.join(folder, ".zarray")) as fh:
        meta = json.load(fh)
    if stats() != before or before[0] is None:
        raise ValueError(f"{folder}: changed while its manifest was being built")
    compressor = meta.get("compressor") or {}
    fill = meta.get("fill_value")
    if fill is None:
        raise ValueError(f"{folder}: no fill_value")
    return {
        "dir": folder,
        "shape": [int(v) for v in meta["shape"]],
        "chunks": [int(v) for v in meta["chunks"]],
        "dtype": str(meta.get("dtype")),
        "order": str(meta.get("order")),
        "compressor": str(compressor.get("id", "")),
        "cname": str(compressor.get("cname", "")),
        "filters_none": meta.get("filters") is None,
        "fill": float(fill),                  # "NaN" / "Infinity" parse too
        "separator": str(meta.get("dimension_separator") or "."),
        "zarray": before[0],
        "zattrs": before[1],
    }


def build_corrected_manifest(table, raw_provider, strides) -> Dict[str, Any]:
    """`Step1SourceTable` -> the core's `Step1Sources` (`Step1Reader`).

    Every decision is the table's own (`source_of`, `region`,
    `coarse_plane`, `missing`): nothing is re-decided here. `strides` are
    the levels' `round(h0/hk)` values; a corrected channel lists the
    persisted coarse plane the table accepts for each of them. A store the
    core cannot take raises ValueError (the caller stays with Python)."""
    from ..viewer.step1_source import (MAX_REDUCTION_ELEMENTS, SOURCE_CORRECTED,
                                       SOURCE_MISSING)

    reasons = {m.channel: m.reason for m in table.missing()}
    channels = {}
    for name in raw_provider.channel_names:
        source = table.source_of(name)
        index = int(raw_provider.channel_index(name))
        if source == SOURCE_MISSING:
            channels[name] = {"source": "missing", "reason": reasons.get(name, ""), "index": index}
        elif source == SOURCE_CORRECTED:
            region = table.region(name)
            planes = []
            for stride in sorted({max(1, int(s)) for s in strides}):
                plane = table.coarse_plane(name, stride)
                # stride 1: only a full-resolution display layer (R7c 1A)
                if plane is not None and (stride > 1 or getattr(plane, "display", False)):
                    planes.append({"stride": stride, "array": _array_spec(plane.array),
                                   "origin": [int(plane.origin[0]), int(plane.origin[1])],
                                   "display": bool(getattr(plane, "display", False))})
            # R7c 1E: every stride drawn served by a display layer -> uint16 tiles
            wanted = sorted({max(1, int(s)) for s in strides})
            u16 = (os.environ.get("BLOCK01_STEP1_DISPLAY_U16") != "0"
                   and sorted(p["stride"] for p in planes if p["display"]) == wanted)
            channels[name] = {"source": "corrected", "product": _array_spec(region.array),
                              "bbox": [int(v) for v in region.bbox], "planes": planes,
                              "index": index, "display_u16": bool(u16)}
        else:
            channels[name] = {"source": "raw", "index": index}
    roi = table.roi_bbox()
    out = {
        "roi": None if roi is None else [int(v) for v in roi],
        "revision": table.identity_token(),
        "budget": int(MAX_REDUCTION_ELEMENTS),
        "channels": channels,
    }
    _display_tiff(table, raw_provider, out)
    if os.environ.get("BLOCK01_A9_DRAW_LOG") == "1":       # measurement evidence (R7c 1D)
        for name, entry in sorted(channels.items()):
            planes = [f"{p['stride']}{'*' if p.get('display') else ''}" for p in entry.get("planes", [])]
            print(f"[sources] {name}: {entry['source']} planes={','.join(planes)}"
                  f"{' u16' if entry.get('display_u16') else ''}", flush=True)
    return out


#: R7c 1B (measurement switch, not a product entry): a corrected channel is
#: drawn from this already-built tiled pyramid (the merged OME-TIFF) instead
#: of its float product. Only a full-slide region, only a channel the file
#: names, only a file whose every level has the slide's own shape; anything
#: else stays on its own path. The file is assumed to be the snapshot of the
#: current product (checked before measuring, `a9_bench/r7c/verify_ometiff.py`).
DISPLAY_OMETIFF_ENV = "BLOCK01_STEP1_DISPLAY_OMETIFF"


def _display_tiff(table, raw_provider, manifest) -> None:
    path = os.environ.get(DISPLAY_OMETIFF_ENV, "").strip()
    if not path:
        return
    from ..viewer.raw_tile_provider import RawTileProvider
    try:
        tiff = RawTileProvider(path)
    except Exception as exc:                                # noqa: BLE001
        print(f"[display_tiff] {path}: not opened ({exc}); every channel stays on its own path")
        return
    try:
        shapes = [tuple(raw_provider.level_shape(k)) for k in range(raw_provider.num_levels)]
        if [tuple(tiff.level_shape(k)) for k in range(tiff.num_levels)] != shapes:
            print(f"[display_tiff] {path}: its levels are not the slide's; not used")
            return
        full = [0, shapes[0][0], 0, shapes[0][1]]
        all_names = list(tiff.channel_names)
        names = {n for n in all_names if all_names.count(n) == 1}      # a repeated name is ambiguous
        used = []
        for name, entry in manifest["channels"].items():
            if entry.get("source") == "raw":
                # R7c 1F: a raw channel too, so every drawn channel comes from
                # the one file (its level 0 checked equal to the slide's,
                # `a9_bench/r7c/verify_dapi.py`)
                roi = table.roi_bbox()
                if name in names and (roi is None or [int(v) for v in roi] == full):
                    manifest["channels"][name] = {"source": "display_tiff", "index": entry["index"],
                                                  "tiff_index": int(tiff.channel_index(name))}
                    used.append(name)
                continue
            if entry.get("source") != "corrected":
                continue
            if name not in names or [int(v) for v in table.region(name).bbox] != full:
                print(f"[display_tiff] {name}: stays on its corrected product "
                      f"({'not in the file' if name not in names else 'not a full-slide region'})")
                continue
            manifest["channels"][name] = {"source": "display_tiff", "index": entry["index"],
                                          "tiff_index": int(tiff.channel_index(name))}
            used.append(name)
        if used:
            manifest["display_tiff"] = build_raw_manifest(tiff)
        print(f"[display_tiff] {path}: drawn from it: {sorted(used)}", flush=True)
    finally:
        tiff.close()
