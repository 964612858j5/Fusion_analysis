"""Step4's h5ad X by row blocks and the Rust CSV text
(docs/v17_step4_x_stream_plan.md §2): the files read back as before, X is
never read whole, and the CSV is byte for byte NumPy's `savetxt` text.

Synthetic projects in the test's temporary directory only.
"""

import os
import sys

import h5py
import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from test_quant_sources import build_project  # noqa: E402
from test_f4_step4 import _add_regions, _facts, _snapshot  # noqa: E402

import anndata as ad  # noqa: E402

from block01.core import quant_engine as qe  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

_app = QApplication.instance() or QApplication([])
fvc = pytest.importorskip("fusion_viewer_core")
needs_rust_csv = pytest.mark.skipif(not hasattr(fvc, "format_csv_rows"),
                                    reason="viewer core without format_csv_rows")


@pytest.fixture
def small_blocks(monkeypatch):
    """Blocks of 7 rows: every output spans several blocks and a ragged end."""
    monkeypatch.setattr(few, "CSV_BLOCK_ROWS", 7)
    monkeypatch.setattr(few, "H5AD_BLOCK_ROWS", 7)


def _project(tmp_path, name, tma=False, **kw):
    (tmp_path / name).mkdir()
    p = build_project(tmp_path / name, **kw)
    if tma:
        _add_regions(p, _snapshot(), _facts(p["labels"]["Full WSI"], 40))
    return p


@pytest.mark.parametrize("csv", [False, True], ids=["f4-sink", "f8-sink"])
def test_x_is_the_x_layer_written_by_row_blocks(tmp_path, monkeypatch, small_blocks, csv):
    reads = []
    real = qe.FeatureMatrixSink.read

    def spy(self, layer, r0=0, r1=None):
        reads.append((layer, r0, r1))
        return real(self, layer, r0, r1)
    monkeypatch.setattr(qe.FeatureMatrixSink, "read", spy)
    p = _project(tmp_path, "p", nuclei=True)
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), statistics=["mean", "max"],
                             regions=["nucleus"], write_csv=csv)
    a = ad.read_h5ad(res["h5ad"])
    x_layer = "cell_mean"
    assert a.X.dtype == np.float32
    assert np.array_equal(a.X, np.asarray(a.layers[x_layer]), equal_nan=True)
    assert a.n_obs > 7
    x_reads = [(r0, r1) for layer, r0, r1 in reads if layer == x_layer]
    assert x_reads and all(r1 is not None and r1 - r0 <= 7 for r0, r1 in x_reads)
    with h5py.File(res["h5ad"], "r") as f:
        x = f["X"]
        assert dict(x.attrs) == {"encoding-type": "array", "encoding-version": "0.2.0"}
        assert x.chunks == (7, a.n_vars) and x.dtype == np.float32
    b = ad.read_h5ad(res["h5ad"], backed="r")
    try:
        assert np.array_equal(np.asarray(b.X[:]), a.X, equal_nan=True)
    finally:
        b.file.close()


def test_a_tma_run_with_distribution_reads_back_whole(tmp_path, small_blocks):
    p = _project(tmp_path, "p", tma=True, nuclei=True)
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), regions=["nucleus"],
                             distribution=["median"], markers=["CD8"])
    a = ad.read_h5ad(res["h5ad"])
    assert np.array_equal(a.X, np.asarray(a.layers["cell_mean"]), equal_nan=True)
    assert "measurement_qc" in a.layers and sorted(a.obsm.keys()) == ["cell_median",
                                                                       "nucleus_median"]
    assert a.uns["X_statistic"] == "mean" and "regions" in a.uns


def test_a_zero_object_x(tmp_path):
    with h5py.File(tmp_path / "z.h5", "w") as f:
        few._write_rows(f, "X", 0, 3, np.float32, lambda r0, r1: np.zeros((r1 - r0, 3)))
        assert f["X"].shape == (0, 3) and f["X"].chunks is None


@needs_rust_csv
@pytest.mark.parametrize("tma", [False, True], ids=["plain", "tma-text-columns"])
def test_the_rust_csv_is_savetxts_text(tmp_path, monkeypatch, small_blocks, tma):
    p = _project(tmp_path, "p", tma=tma, nuclei=True)
    kw = dict(statistics=["mean", "std", "max"], regions=["nucleus", "cytoplasm"], write_csv=True)
    rust = few.run_extraction(p["run_dir"], str(tmp_path / "rust"), **kw)
    monkeypatch.setattr(few, "_csv_formatter", lambda: None)
    numpy = few.run_extraction(p["run_dir"], str(tmp_path / "numpy"), **kw)
    with open(rust["csv"], "rb") as f1, open(numpy["csv"], "rb") as f2:
        a, b = f1.read(), f2.read()
    assert a == b and a.count(b"\n") > 8


@needs_rust_csv
def test_the_rust_text_matches_savetxt_on_hard_values():
    import io
    rng = np.random.default_rng(7)
    bits = rng.integers(0, 2**64, size=40000, dtype=np.uint64).view(np.float64)
    special = [0.0, -0.0, np.nan, np.inf, -np.inf, 1e-6, 1e-4, 1e16, 1e17, 1e21, 0.1, 5e-324,
               2.2250738585072014e-308, 1.7976931348623157e308, 99999999999999999.0, 2.5]
    values = np.concatenate([bits, special, rng.normal(0, 1, 40000) * 10.0 ** rng.integers(-30, 30, 40000)])
    values = values[: values.size // 4 * 4].reshape(-1, 4)
    ids = np.arange(values.shape[0], dtype=np.float64) * 3.5
    table = np.column_stack([ids, values])
    integer = [True, False, False, False, False]
    buf = io.StringIO()
    np.savetxt(buf, table, delimiter=",", fmt=["%d" if i else "%.17g" for i in integer])
    got = fvc.format_csv_rows(table.tobytes(), 5, integer, [], 4).decode()
    assert got == buf.getvalue()
    with pytest.raises(ValueError):
        fvc.format_csv_rows(np.array([[np.nan, 1.0]]).tobytes(), 2, [True, False], [], 1)


@needs_rust_csv
@pytest.mark.parametrize("linesep", ["\n", "\r\n"], ids=["posix", "windows"])
def test_text_columns_with_line_breaks_match_the_text_file(monkeypatch, linesep):
    """Windows: the text file turns EVERY "\\n" into "\\r\\n", inside a Regions
    field too; the Rust branch must write the same bytes (codex review)."""
    import io
    import types
    import pandas as pd
    table = np.array([[1.0, 2.5], [2.0, 0.1], [3.0, -0.0]])
    frame = pd.DataFrame({"note": ['a\nb', 'plain', 'x\n\ny']})
    result = types.SimpleNamespace(columns=["cell_id", "v"], ids=np.arange(3),
                                   block=lambda r0, r1: table[r0:r1])
    extra = few._region_csv_columns(frame)
    monkeypatch.setattr(few.os, "linesep", linesep)
    rust = io.BytesIO()
    few._write_csv_rows_rust(rust, result, 3, 2, [True, False], extra, frame,
                             fvc.format_csv_rows)
    raw = io.BytesIO()
    text = io.TextIOWrapper(raw, encoding="utf-8", newline=linesep)
    monkeypatch.setattr(few, "_csv_formatter", lambda: None)
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: text)
    text.close = lambda: None
    few._write_csv("ignored", result, frame)
    monkeypatch.setattr("builtins.open", real_open)
    text.flush()
    header_end = raw.getvalue().index(linesep.encode()) + len(linesep)
    assert raw.getvalue()[header_end:] == rust.getvalue()
