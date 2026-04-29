"""
block01/workers/feature_extract_worker.py — Per-cell feature extraction worker.
"""

import os
import gc
import json
import traceback

import numpy as np
import tifffile
import zarr

from PyQt5.QtCore import QThread, pyqtSignal

from ..core.io_loader import OMETIFFLoader
from ..core.bg_correction import _normalize_correction_config


class FeatureExtractWorker(QThread):
    """
    Extract per-cell intensity + morphology features from:
      • global_mask  (uint32 memmap or OME-TIFF)
      • original OME-TIFF (all channels, lazy region read via zarr)

    Strategy: full-image pass.
      1. Load global_mask as memmap (uint32, ~8 GB for 59k×35k).
      2. Use skimage.measure.regionprops on the mask once to get
         morphology features + per-cell bounding boxes.
      3. For each channel, read the full page via zarr, then use
         scipy.ndimage.mean / median / sum with the label array.
         Peak memory ≈ mask (8 GB) + one channel (4 GB) = 12 GB.
      4. Write cell_features.csv + cell_features.h5ad.

    Signals:
        progress(done_channels, total_channels, msg)
        finished(output_dir)
        error(traceback_str)
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str, str)   # (output_dir, base_name)
    error    = pyqtSignal(str)

    def __init__(self, mask_path, ome_tiff_path, output_dir,
                 channel_names=None, statistics=None, file_prefix=None,
                 correction_config=None):
        super().__init__()
        self.mask_path     = mask_path
        self.ome_tiff_path = ome_tiff_path
        self.output_dir    = output_dir
        self.channel_names = channel_names
        self.statistics    = statistics if statistics else ['mean']
        self.correction_config = _normalize_correction_config(correction_config)
        p = file_prefix.strip() if file_prefix else ''
        self.base_name = f'{p}_cell_features' if p else 'cell_features'
        self._stop     = False

    def stop(self):
        self._stop = True

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _load_mask(mask_path, full_h, full_w):
        """Load mask as read-only uint32 memmap (flat .dat) or via OME-TIFF."""
        if mask_path.endswith('.dat'):
            return np.memmap(mask_path, dtype='uint32', mode='r',
                             shape=(full_h, full_w))
        tif   = tifffile.TiffFile(mask_path)
        store = tif.aszarr()
        try:
            z  = zarr.open(store, mode='r')
            z0 = z[0] if isinstance(z, zarr.hierarchy.Group) else z
            if z0.ndim == 3:
                arr = np.array(z0[0], dtype='uint32')
            else:
                arr = np.array(z0, dtype='uint32')
        finally:
            store.close()
            tif.close()
        return arr

    # ── main ──────────────────────────────────────────────────────────

    def run(self):
        try:
            import xml.etree.ElementTree as ET
            from scipy.ndimage import (
                mean               as nd_mean,
                sum                as nd_sum,
                median             as nd_median,
                standard_deviation as nd_std,
                minimum            as nd_minimum,
                maximum            as nd_maximum,
                labeled_comprehension as nd_lc,
            )
            import pandas as pd
            _ = pd

            self.progress.emit(0, 1, 'Parsing OME-TIFF metadata…')

            with tifffile.TiffFile(self.ome_tiff_path) as tif:
                root  = ET.fromstring(tif.ome_metadata)
                page0 = tif.pages[0]
                full_h = page0.imagelength
                full_w = page0.imagewidth

            ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
            ch_nodes = root.findall('.//ome:Channel', ns)
            if self.channel_names and len(self.channel_names) == len(ch_nodes):
                ch_names = list(self.channel_names)
            else:
                ch_names = [ch.get('Name', f'ch_{i:02d}')
                            for i, ch in enumerate(ch_nodes)]
            n_ch = len(ch_names)

            self.progress.emit(0, n_ch + 1, 'Loading mask…')
            mask = self._load_mask(self.mask_path, full_h, full_w)
            mask_h, mask_w = mask.shape

            bbox_y0, bbox_y1, bbox_x0, bbox_x1 = 0, mask_h, 0, mask_w
            mask_dir = os.path.dirname(self.mask_path)

            import glob as _glob
            meta_candidates = (
                _glob.glob(os.path.join(mask_dir, 'segmentation_meta*.json')) +
                _glob.glob(os.path.join(mask_dir, '*_segmentation_meta.json'))
            )
            bbox_found = False
            for mc in meta_candidates:
                try:
                    with open(mc) as _f:
                        _m = json.load(_f)
                    bb = _m.get('bbox') or _m.get('bbox_fullres')
                    if bb and len(bb) >= 4:
                        bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                        bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                        bbox_found = True
                        break
                except Exception:
                    pass

            if not bbox_found:
                for zarr_name in _glob.glob(os.path.join(mask_dir, 'fused*.zarr')):
                    try:
                        _z = zarr.open(zarr_name, mode='r')
                        bb = _z.attrs.get('bbox_fullres')
                        if bb and len(bb) >= 4:
                            bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                            bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                            bbox_found = True
                            break
                    except Exception:
                        pass

            if not bbox_found:
                mask_zarr = self.mask_path.replace('.ome.tiff', '.zarr').replace('.dat', '.zarr')
                if os.path.exists(mask_zarr):
                    try:
                        _z = zarr.open(mask_zarr, mode='r')
                        bb = _z.attrs.get('bbox_fullres') or _z.attrs.get('bbox')
                        if bb and len(bb) >= 4:
                            bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                            bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                            bbox_found = True
                    except Exception:
                        pass

            if bbox_found:
                self.progress.emit(0, n_ch + 1,
                    f'ROI bbox: y=[{bbox_y0},{bbox_y1}) x=[{bbox_x0},{bbox_x1})  '
                    f'mask: {mask_h}×{mask_w} px')
            else:
                self.progress.emit(0, n_ch + 1,
                    f'No bbox found — assuming full-WSI mode  '
                    f'mask: {mask_h}×{mask_w} px')

            n_cells = int(mask.max())
            if n_cells == 0:
                self.error.emit('Mask is empty — no cells found.')
                return
            labels = np.arange(1, n_cells + 1)

            self.progress.emit(0, n_ch + 1,
                               f'Computing morphology for {n_cells:,} cells…')

            ys = np.repeat(
                np.arange(mask_h, dtype=np.float32), mask_w
            ).reshape(mask_h, mask_w)
            xs = np.tile(
                np.arange(mask_w, dtype=np.float32), mask_h
            ).reshape(mask_h, mask_w)

            ones  = (mask > 0).astype(np.float32)
            area  = nd_sum(ones, mask, labels)

            cy_local = nd_mean(ys, mask, labels)
            cx_local = nd_mean(xs, mask, labels)
            cy = cy_local + bbox_y0
            cx = cx_local + bbox_x0

            m20 = nd_mean(xs * xs, mask, labels)
            m02 = nd_mean(ys * ys, mask, labels)
            m11 = nd_mean(xs * ys, mask, labels)
            del ys, xs, ones
            gc.collect()

            mu20 = m20 - cx * cx
            mu02 = m02 - cy * cy
            mu11 = m11 - cx * cy
            del m20, m02, m11

            tmp   = np.sqrt(np.maximum(0.0, (mu20 - mu02)**2 + 4.0 * mu11**2))
            lam1  = 0.5 * (mu20 + mu02 + tmp)
            lam2  = 0.5 * (mu20 + mu02 - tmp)
            lam2  = np.maximum(lam2, 0.0)
            del tmp, mu20, mu02, mu11

            major_axis = 4.0 * np.sqrt(lam1)
            minor_axis = 4.0 * np.sqrt(lam2)

            with np.errstate(invalid='ignore', divide='ignore'):
                ecc = np.where(
                    lam1 > 0,
                    np.sqrt(np.maximum(0.0, 1.0 - lam2 / lam1)),
                    0.0,
                )
            del lam1, lam2

            from scipy.ndimage import binary_erosion as _bin_erode
            bin_mask = (mask > 0)
            eroded   = _bin_erode(bin_mask, structure=np.ones((3, 3), dtype=bool))
            boundary = (bin_mask & ~eroded).astype(np.float32)
            del bin_mask, eroded
            gc.collect()
            perimeter = nd_sum(boundary.astype(np.float32), mask, labels)
            del boundary

            if self._stop:
                self.error.emit('Stopped by user.')
                return

            stats   = self.statistics
            intensity_cols = {}
            loader = OMETIFFLoader(
                self.ome_tiff_path,
                correction_config=self.correction_config,
            )

            for ci, ch_name in enumerate(ch_names):
                if self._stop:
                    self.error.emit('Stopped by user.')
                    return

                stat_str = ' / '.join(stats)
                self.progress.emit(
                    ci + 1, n_ch + 1,
                    f'[{ci+1}/{n_ch}]  {ch_name}  —  {stat_str}…'
                )

                ch_data = loader.read_region(
                    ch_name,
                    bbox_y0, bbox_y0 + mask_h,
                    bbox_x0, bbox_x0 + mask_w,
                    downsample=1,
                    normalize=False,
                )
                safe = ch_name.replace('/', '_').replace(' ', '_')

                def _f64(arr):
                    return np.asarray(arr, dtype=np.float64)

                if 'mean'   in stats:
                    intensity_cols[f'{safe}_mean']   = _f64(nd_mean(ch_data, mask, labels))
                if 'sum'    in stats:
                    intensity_cols[f'{safe}_sum']    = _f64(nd_sum(ch_data, mask, labels))
                if 'median' in stats:
                    intensity_cols[f'{safe}_median'] = _f64(nd_median(ch_data, mask, labels))
                if 'std'    in stats:
                    intensity_cols[f'{safe}_std']    = _f64(nd_std(ch_data, mask, labels))
                if 'min'    in stats:
                    intensity_cols[f'{safe}_min']    = _f64(nd_minimum(ch_data, mask, labels))
                if 'max'    in stats:
                    intensity_cols[f'{safe}_max']    = _f64(nd_maximum(ch_data, mask, labels))
                if 'p90'    in stats:
                    intensity_cols[f'{safe}_p90']    = _f64(nd_lc(
                        ch_data, mask, labels,
                        lambda v: float(np.percentile(v, 90)),
                        float, default=0.0,
                    ))

                del ch_data
                gc.collect()

            if self._stop:
                self.error.emit('Stopped by user.')
                return

            self.progress.emit(n_ch + 1, n_ch + 1, 'Writing outputs…')
            os.makedirs(self.output_dir, exist_ok=True)

            morph_cols = ['cell_id', 'area', 'centroid_y', 'centroid_x',
                          'perimeter', 'major_axis', 'minor_axis', 'eccentricity']
            morph_arrays = [
                labels.astype(np.float64),
                np.asarray(area,       dtype=np.float64),
                np.asarray(cy,         dtype=np.float64),
                np.asarray(cx,         dtype=np.float64),
                np.asarray(perimeter,  dtype=np.float64),
                np.asarray(major_axis, dtype=np.float64),
                np.asarray(minor_axis, dtype=np.float64),
                np.asarray(ecc,        dtype=np.float64),
            ]
            del area, cy, cx, perimeter, major_axis, minor_axis, ecc
            gc.collect()

            intens_col_names = list(intensity_cols.keys())
            intens_arrays = [np.asarray(v, dtype=np.float64)
                             for v in intensity_cols.values()]
            del intensity_cols
            gc.collect()

            all_cols   = morph_cols + intens_col_names
            all_arrays = morph_arrays + intens_arrays

            data_matrix = np.column_stack(all_arrays)
            del morph_arrays, intens_arrays, all_arrays
            gc.collect()

            csv_path = os.path.join(self.output_dir, f'{self.base_name}.csv')
            header = ','.join(all_cols)
            np.savetxt(csv_path, data_matrix, delimiter=',',
                       header=header, comments='', fmt='%.6g')
            self.progress.emit(n_ch + 1, n_ch + 1,
                               f'CSV written  ({n_cells:,} cells × '
                               f'{len(all_cols)} features)  →  {csv_path}')

            try:
                import anndata as ad

                primary_stat = stats[0]
                ch_safe = [ch.replace('/', '_').replace(' ', '_') for ch in ch_names]

                x_cols_idx = [all_cols.index(f'{s}_{primary_stat}')
                              for s in ch_safe
                              if f'{s}_{primary_stat}' in all_cols]
                X = data_matrix[:, x_cols_idx].astype(np.float32)

                morph_idx = {c: all_cols.index(c) for c in morph_cols}
                import pandas as _pd
                obs = _pd.DataFrame({
                    'cell_id':      data_matrix[:, morph_idx['cell_id']].astype(int).tolist(),
                    'centroid_y':   data_matrix[:, morph_idx['centroid_y']].tolist(),
                    'centroid_x':   data_matrix[:, morph_idx['centroid_x']].tolist(),
                    'area':         data_matrix[:, morph_idx['area']].tolist(),
                    'perimeter':    data_matrix[:, morph_idx['perimeter']].tolist(),
                    'major_axis':   data_matrix[:, morph_idx['major_axis']].tolist(),
                    'minor_axis':   data_matrix[:, morph_idx['minor_axis']].tolist(),
                    'eccentricity': data_matrix[:, morph_idx['eccentricity']].tolist(),
                })
                obs.index = obs['cell_id'].astype(str)
                obs.index.name = None

                import pandas as _pd2
                adata = ad.AnnData(
                    X   = X,
                    obs = obs,
                    var = _pd2.DataFrame(index=ch_names),
                )
                for s in stats:
                    if s == primary_stat:
                        continue
                    s_idx = [all_cols.index(f'{c}_{s}')
                             for c in ch_safe
                             if f'{c}_{s}' in all_cols]
                    if s_idx:
                        adata.obsm[s] = data_matrix[:, s_idx].astype(np.float32)

                adata.uns['ome_tiff']    = self.ome_tiff_path
                adata.uns['mask_path']   = self.mask_path
                adata.uns['n_cells']     = int(n_cells)
                adata.uns['statistics']  = stats
                adata.uns['x_statistic'] = primary_stat

                h5ad_path = os.path.join(self.output_dir, f'{self.base_name}.h5ad')
                adata.write_h5ad(h5ad_path)
                self.progress.emit(n_ch + 1, n_ch + 1,
                                   f'h5ad written  →  {h5ad_path}')
            except ImportError:
                self.progress.emit(n_ch + 1, n_ch + 1,
                                   '⚠  anndata not installed — h5ad skipped. '
                                   'Run: pip install anndata')

            del data_matrix

            del mask
            gc.collect()

            self.finished.emit(self.output_dir, self.base_name)

        except Exception:
            self.error.emit(traceback.format_exc())
