"""Block A2c: Step4 and Step1 fusion read their image data through the
PixelSource contract (docs/v16_A2c_application.md).

The pixels themselves are proven bitwise against the pre-migration path by
`scripts/diagnose_v16_a2c_oracle.py` on the representative dataset and by the
existing JobReader / fusion tests; these tests pin what is NEW: which
sources the consumers use, and that a corrected read is fail-closed.
"""

import os

import numpy as np
import pytest

pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core import quant_sources as qs  # noqa: E402
from block01.sources import CorrectedZarrSource, OmeTiffSource  # noqa: E402
from test_quant_sources import ROI_BBOX, build_project  # noqa: E402


# ── 2/3 Step4 ────────────────────────────────────────────────────────────

def test_step4_reads_through_the_contract(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        assert isinstance(r._raw, OmeTiffSource)
        assert isinstance(r._corrected, CorrectedZarrSource)
        assert r.raw_mode == "tiff_tiles"          # the provenance keeps its meaning
        raw = [c for c in job.channels if c.kind == "raw"]
        assert r.channel_dtype(raw[0]) == np.uint8
    finally:
        r.close()


def test_a_corrected_tile_the_product_does_not_cover_is_refused_not_read_raw(tmp_path):
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"])
    cd3 = next(c for c in job.channels if c.name == "CD3")
    r = qs.JobReader(job, read_threads=2)
    try:
        # a tile running past the region's (and the group's) edge
        h = ROI_BBOX[1] - ROI_BBOX[0]
        with pytest.raises(qs.QuantSourceError):
            r.channels([cd3], h - 5, h + 5, 0, 10)
    finally:
        r.close()


def test_a_project_without_corrected_channels_opens_no_corrected_source(tmp_path):
    p = build_project(tmp_path, decisions={"CD3": "original", "CD8": "original",
                                           "PanCK": "original"})
    job = qs.resolve_quant_job(p["run_dir"])
    r = qs.JobReader(job, read_threads=2)
    try:
        assert r._corrected is None
    finally:
        r.close()
