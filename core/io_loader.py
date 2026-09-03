"""
block01/core/io_loader.py — OME-TIFF lazy loader.
"""

import os
import numpy as np
import tifffile
import zarr
import xml.etree.ElementTree as ET

from ..config import NORM_LOW, NORM_HIGH, TOPHAT_RADIUS_DEFAULT, CUCIM_SIGMA_DEFAULT
from .bg_correction import (
    CUCIM_AVAILABLE,
    _normalize_correction_config,
    _apply_background_method_tiled,
)


class OMETIFFLoader:
    """
    OME-TIFF Loader.
    Key fix: uses tifffile zarr interface for lazy loading, reading only the tiles
    covered by the ROI. The old tif.pages[idx].asarray() reads the full page (~3GB/page),
    19 channels = 57GB IO. zarr interface reads only the tiles covering the ROI,
    reducing IO by 100x or more.
    """

    def __init__(self, filepath, name_map=None, correction_config=None):
        self.filepath = filepath
        self.name_map = name_map or {}
        self.ch_map   = {}
        self.shape    = (0, 0)
        self._zarr_array = None   # lazy init, avoid resource usage at startup
        self.correction_config = _normalize_correction_config(correction_config)
        self._corrected_zarr_path = None
        self._corrected_decisions = {}
        self._corrected_store = None
        self._parse()

    def _parse(self):
        with tifffile.TiffFile(self.filepath) as tif:
            root = ET.fromstring(tif.ome_metadata)
            p    = tif.pages[0]
            self.shape = (p.imagelength, p.imagewidth)
        ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
        for i, ch in enumerate(root.findall(".//ome:Channel", ns)):
            raw  = ch.get("Name", f"ch_{i:02d}")
            disp = self.name_map.get(raw, raw)
            self.ch_map[disp] = i
        print(f"[Loader] {self.shape[0]}×{self.shape[1]} px  {len(self.ch_map)} channels")

    def channel_names(self):
        return list(self.ch_map.keys())

    def _get_zarr(self):
        """
        Lazy-initialize zarr array.
        tifffile.TiffFile.aszarr() returns a zarr store that only reads from disk
        when specific slices are accessed, without preloading entire pages.
        The store must be used within a TiffFile context, so each call reopens the file.
        (tifffile >= 2021.x supports this interface)
        """
        return None  # marker: no caching, open inside each read_region call

    def set_correction_config(self, correction_config):
        self.correction_config = _normalize_correction_config(correction_config)

    def set_corrected_zarr_store(self, zarr_path, decisions):
        self._corrected_zarr_path = zarr_path if zarr_path and os.path.exists(zarr_path) else None
        self._corrected_decisions = {
            str(ch): str(method).strip().lower()
            for ch, method in dict(decisions or {}).items()
            if str(method).strip().lower() in {"tophat", "cucim"}
        }
        self._corrected_store = None

    def read_region(self, channel_name, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        """
        Read the ROI region of the specified channel, normalized to float32 [0,1].

        Implementation: prefer zarr lazy loading (reads only ROI tiles),
        raises exception on failure.
        """
        if channel_name not in self.ch_map:
            raise KeyError(f"Channel '{channel_name}' not found")
        if channel_name in self._corrected_decisions and self._corrected_zarr_path and os.path.exists(self._corrected_zarr_path):
            if self._corrected_store is None:
                self._corrected_store = zarr.open(self._corrected_zarr_path, mode="r")
            corrected_mode = str(self._corrected_store.attrs.get("mode", "")).strip().lower()
            if corrected_mode == "roi_only":
                region = self._read_corrected_roi_only(channel_name, y0, y1, x0, x1)
                if region is not None:
                    if downsample > 1:
                        region = region[::downsample, ::downsample]
                    if normalize:
                        return self._norm(region)
                    return region.astype(np.float32, copy=False)
            elif channel_name in self._corrected_store:
                region = np.asarray(self._corrected_store[channel_name][y0:y1, x0:x1], dtype=np.float32)
                if downsample > 1:
                    region = region[::downsample, ::downsample]
                if normalize:
                    return self._norm(region)
                return region.astype(np.float32, copy=False)

        page_idx = self.ch_map[channel_name]
        region = self._read_roi_zarr(page_idx, y0, y1, x0, x1)
        cfg = self.correction_config if correction_config is None else _normalize_correction_config(correction_config)
        region = self._apply_configured_correction(channel_name, region, cfg)

        if downsample > 1:
            region = region[::downsample, ::downsample]
        if normalize:
            return self._norm(region)
        return region.astype(np.float32, copy=False)

    def read_region_lowres(self, channel_name, y0, y1, x0, x1, downsample,
                           normalize=True):
        """`read_region(..., downsample=ds)` served from the TIFF's pyramid.

        `read_region` reads the region at FULL resolution and then keeps
        every `ds`-th pixel. For a whole-slide ROI that is the entire
        channel decoded from disk -- measured 15.6 s on the GUI thread for a
        59040x35520 slide at ds=33 -- to keep 1/1089 of it. This reads the
        coarsest pyramid level whose downsample does not exceed `ds` and
        picks rows/columns from it with the same floor mapping, so the
        result has exactly the shape `read_region` would return (the
        caller's coordinate maths depends on that) and covers the same
        pixels, sampled at the pyramid's resolution instead of decoded from
        the base level.

        Correctness first: a channel that is served CORRECTED -- from the
        saved zarr store or by an on-the-fly correction config -- falls back
        to `read_region`, because the pyramid holds raw pixels. The nucleus
        channel, which every caller of this method reads, is excluded from
        correction. A TIFF without a pyramid takes the same fallback.
        """
        ds = max(1, int(downsample))
        if ds == 1 or self._channel_is_corrected(channel_name):
            return self.read_region(channel_name, y0, y1, x0, x1,
                                    downsample=ds, normalize=normalize)
        if channel_name not in self.ch_map:
            raise KeyError(f"Channel '{channel_name}' not found")
        page_idx = self.ch_map[channel_name]

        h0, w0 = self.shape
        y0, y1 = max(0, int(y0)), min(int(y1), h0)
        x0, x1 = max(0, int(x0)), min(int(x1), w0)
        # The rows/cols `read_region(...)[::ds, ::ds]` would keep, in
        # level-0 coordinates.
        rows0 = np.arange(y0, y1, ds)
        cols0 = np.arange(x0, x1, ds)
        if rows0.size == 0 or cols0.size == 0:
            return np.zeros((rows0.size, cols0.size), np.float32)

        with tifffile.TiffFile(self.filepath) as tif:
            levels = list(tif.series[0].levels)
            level_ds = [max(1.0, h0 / float(lv.shape[-2])) for lv in levels]
            chosen = 0
            for i, lds in enumerate(level_ds):
                if lds <= ds and lds >= level_ds[chosen]:
                    chosen = i
            if chosen == 0:
                # Nothing coarser than the base level: the plain path is
                # already what this would compute.
                return self.read_region(channel_name, y0, y1, x0, x1,
                                        downsample=ds, normalize=normalize)
            lv = levels[chosen]
            lds_y = h0 / float(lv.shape[-2])
            lds_x = w0 / float(lv.shape[-1])
            z = zarr.open(lv.aszarr(), mode="r")
            if not hasattr(z, "shape"):
                z = z[str(chosen)]
            lh, lw = z.shape[-2], z.shape[-1]
            rows = np.clip((rows0 / lds_y).astype(np.int64), 0, lh - 1)
            cols = np.clip((cols0 / lds_x).astype(np.int64), 0, lw - 1)
            ry0, ry1 = int(rows[0]), int(rows[-1]) + 1
            cx0, cx1 = int(cols[0]), int(cols[-1]) + 1
            if z.ndim == 3:
                block = np.asarray(z[page_idx, ry0:ry1, cx0:cx1])
            elif z.ndim == 4:
                block = np.asarray(z[0, page_idx, ry0:ry1, cx0:cx1])
            else:
                block = np.asarray(z[ry0:ry1, cx0:cx1])
        region = block[np.ix_(rows - ry0, cols - cx0)]
        if normalize:
            return self._norm(region)
        return region.astype(np.float32, copy=False)

    def _channel_is_corrected(self, channel_name):
        """True when `read_region` would return CORRECTED pixels for this
        channel -- from the saved store, or by applying the correction
        config on the fly."""
        if (channel_name in self._corrected_decisions
                and self._corrected_zarr_path
                and os.path.exists(self._corrected_zarr_path)):
            return True
        decisions = (self.correction_config or {}).get("channel_decisions") or {}
        return str(decisions.get(channel_name, "original")).strip().lower() in {"tophat", "cucim"}

    def _read_corrected_roi_only(self, channel_name, y0, y1, x0, x1):
        """
        Read a global-coordinate request from ROI-only corrected zarr when the
        request is fully contained in one stored ROI. Otherwise return None so
        callers never treat ROI-local arrays as full-WSI datasets.
        """
        store = self._corrected_store
        for group_name in store.group_keys():
            group = store[group_name]
            bbox = group.attrs.get("bbox_fullres")
            if not bbox or len(bbox) != 4 or channel_name not in group:
                continue
            ry0, ry1, rx0, rx1 = [int(v) for v in bbox]
            if ry0 <= y0 and y1 <= ry1 and rx0 <= x0 and x1 <= rx1:
                return np.asarray(
                    group[channel_name][y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0],
                    dtype=np.float32,
                )
        return None

    def _apply_configured_correction(self, channel_name, region, correction_config):
        if not correction_config:
            return region
        decisions = correction_config.get("channel_decisions") or {}
        method = str(decisions.get(channel_name, "original")).strip().lower()
        if method not in {"tophat", "cucim"}:
            return region

        params = correction_config.get("method_params") or {}
        if method == "tophat":
            radius = int(params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT))
            return _apply_background_method_tiled(region, "tophat", radius=radius)

        sigma = int(params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT))
        return _apply_background_method_tiled(
            region,
            "cucim",
            sigma=sigma,
            prefer_gpu=CUCIM_AVAILABLE,
        )

    def _read_roi_zarr(self, page_idx, y0, y1, x0, x1):
        """
        Read only the ROI region using the zarr interface.
        zarr translates the request into reads of the corresponding tiles,
        avoiding loading the full page.
        """
        try:
            with tifffile.TiffFile(self.filepath) as tif:
                store = tif.aszarr()
                z = zarr.open(store, mode='r')

                if isinstance(z, zarr.hierarchy.Group):
                    z0 = z[0] if '0' in z else next(iter(z.values()))
                else:
                    z0 = z

                if z0.ndim == 3:
                    region = np.array(z0[page_idx, y0:y1, x0:x1])
                elif z0.ndim == 4:
                    region = np.array(z0[0, page_idx, y0:y1, x0:x1])
                elif z0.ndim == 2:
                    region = np.array(z0[y0:y1, x0:x1])
                else:
                    raise ValueError(f"Unknown zarr dimensions: {z0.ndim}")

            return region.copy()

        except Exception as e:
            raise RuntimeError(
                f"zarr read failed: {e}\n"
                f"Please ensure zarr is installed: pip install zarr\n"
                f"And tifffile >= 2021.x: pip install --upgrade tifffile"
            ) from e

    @staticmethod
    def _norm(arr):
        arr = arr.astype(np.float32)
        nz  = arr[arr > 0]
        if nz.size < 100:
            return np.zeros_like(arr)
        lo, hi = np.percentile(nz, [NORM_LOW, NORM_HIGH])
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
