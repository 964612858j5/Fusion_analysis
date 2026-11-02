# v17 Step4: h5ad X by row blocks, CSV text in Rust — plan v2 (2026-11-02)

Source: whole-code review 2026-10-31, codex finding 5 (P2, VERIFIED): "Step4's
memory bounds do not extend through finalization and H5AD export";
recommendation 5 "Stream the H5AD primary matrix instead of loading it
whole" (M) and 6 "Budget Step4's full stage memory …" (L). User (TG
2026-11-02): HDD reads set aside, next review item.

v2 (user, TG 2026-11-02): "同意分块写入，包括h5ad\csv … 尽可能的快，但是必须
和之前的内容保持一致 … 如果能用rust就尽可能用rust"; then "你先完成h5ad和csv改造，
然后接着就做spatialdata输出" (SpatialData export: its own plan, next). Codex
plan review of v1: READY (advisories applied below).

This plan takes recommendation 5 only. Recommendation 6 (geometry ~152 B
per label, the obs table, finalization temporaries, distribution frames)
is the larger draft `docs/step4_20m_laptop_plan.md` (E0–E4, never
authorized, untracked); it stays a separate decision.

## 0. What happens now

`workers/feature_extract_worker.py:_write_h5ad`:

```python
adata = ad.AnnData(X=result.sink.read(result.x_layer).astype(np.float32), obs=obs, var=var)
...
adata.write_h5ad(path)
```

* `result.sink.read(layer)` with no row bounds reads every row of the
  disk sink (`core/quant_engine.py:415`), in the sink dtype — float32, or
  float64 when the CSV is on (`run_extraction` switches the sink to `f8`).
* `.astype(np.float32)` makes a second full copy (the float64 one is freed
  only after the AnnData exists; with an f4 sink astype still copies).
* Peak for X alone, n cells × c channels: 2 × 4·n·c bytes (f4 sink) or
  (8 + 4)·n·c (CSV on). 20 M × 69: **11.0 GB** (f4) / **16.6 GB** (CSV on);
  2 M × 50: 0.8 / 1.2 GB.
* Every other layer is already written a row block at a time with h5py
  (`H5AD_BLOCK_ROWS = 65536`, encoding-type `array`, version `0.2.0`) after
  the skeleton — the same on-disk layout anndata reads for X.

## 1. Change

### A. X by row blocks

* The skeleton AnnData is built with `X=None` (shape from obs/var;
  anndata 0.11.4 writes no `X` then). Right after `write_h5ad`, in the same
  `h5py.File(path, "a")` block that appends the layers, `X` is created as a
  dataset at the root — shape (n, c), float32, the layers' chunking — and
  filled a row block at a time from the sink:
  `ds[r0:r1] = result.sink.read(result.x_layer, r0, r1).astype(np.float32)`,
  attrs `encoding-type = "array"`, `encoding-version = "0.2.0"`.
* Shared with the layers through one small local helper
  (`_write_rows(group, name, n, c, dtype, read)`) so X and the layers cannot
  drift apart; the layers' loop becomes a call of it. No new module.
* Nothing else changes: obs, var, uns, layer names and order, dtypes, the
  QC layer, obsm distribution frames, the CSV, `.partial` publishing and
  cleanup, provenance. The file is read back by anndata exactly as before
  (X float32 dense, same values).
* Zero-object result: X is (0, c) with no chunking, like the layers.

### B. The CSV text in Rust (v2)

Measured first: h5ad writing is disk-bound (2 M × 50 float32 by row blocks
with h5py: 0.2 s), the CSV is CPU-bound (`np.savetxt` with `%.17g`: 5.5 s
per 65,536 rows × 176 columns, ~2.8 min per 2 M cells). DuckDB: no h5ad
writer, and its CSV number text differs from `%.17g` (not byte-identical).

* NEW `native/fusion_viewer_core/src/csv_text.rs` (one job: table rows →
  CSV text): `%.17g` from the double's exact value — 17 correctly rounded
  significant digits by u128 integer arithmetic (ties to even), the
  exponent chosen from the unrounded quotient, C's `%g` layout (fixed for
  exponent −4..16, else `d.ddde±XX`, trailing zeros dropped); values whose
  arithmetic does not fit 128 bits take Rust's `{:.16e}` (slow, equal);
  `nan` / `inf` / `-inf` / `-0` as Python; `%d` = truncation toward zero,
  NaN / inf refused (Python raises too). Platform printf is not used, so
  the text is the same on Windows. Rows formatted on several threads.
* Binding `src/py/csv.rs` `format_csv_rows(values, cols, integer, extra,
  threads, newline)`; core 0.2.1 (`CORE_API` unchanged: an addition).
* `_write_csv`: with the function present, the rows go through
  `_write_csv_rows_rust`: the sink read by `CSV_BLOCK_ROWS` (its chunk
  rows), formatted in parts of 16,384 rows on `CSV_THREADS` = min(16,
  cores) threads, written as bytes to the file's buffer after the header
  (line end `os.linesep`, as the text file writes "\n"); one helper thread
  formats/writes while the next block is read (two blocks at most; its
  error is re-raised in order). Regions text columns (F4-4) are formatted
  in Python exactly as before and passed in. Without the function (an
  older or missing core) the NumPy branch runs unchanged.
* Not a new mechanism: a local two-stage overlap inside one function, no
  queue beyond one pending part.

Peak for X after: one row block — 65,536 × c × (sink bytes + 4): 69
channels f4 ≈ 36 MB, f8 ≈ 54 MB, independent of n — read-buffer arithmetic,
not a ceiling (zarr decode buffers add; codex). The function's other full
peaks stay (outside this plan): the obs DataFrame / string index and the
Regions concatenation (`_write_h5ad`), TMA indexing temporaries
(`core/regions/quant_apply.py`), the distribution frames read whole.
Before the change, the read and the cast copy overlap during `astype`
(the source is released when it returns, before `AnnData` runs).

## 2. Acceptance

* v2 (codex P3): `backed='r'` read, `/X` attrs and chunking `(min(n,
  block), c)` asserted (the layout changes from contiguous to chunked;
  values and reads do not).
* Unit (new `tests/test_step4_stream_export.py`, synthetic only):
  - a small run's h5ad read back with `anndata.read_h5ad`: X equals the
    sink's x layer cast to float32, dtype float32, shape, obs / var / uns /
    layers / obsm identical to the file the current code writes (built in
    the test from the same result through the old one-shot path as oracle,
    or compared to a stored reference of the existing test fixtures);
  - f4 and f8 sinks; n = 0; n not a multiple of the block, n > one block
    (block size patched small);
  - with Regions (QC layer present) and with distribution markers;
  - X is written through row-bounded reads only (the sink's `read` called
    with `r1 - r0 <= block` for the X layer — no full read).
* Existing Step4 tests unchanged and passing: `tests/test_step4_worker.py`,
  `tests/test_f4_step4.py`, `tests/test_step4_page.py` (DISPLAY=:1).
* Memory check (session scratchpad script, not a repository file): a
  synthetic disk sink of 2 M × 50 driving `_write_h5ad` in its own process,
  peak RSS of the export before vs after (expected: drop by about 2 × 0.4 GB
  for f4, ~1.2 GB with f8); file read back and X compared block by block.
* Real projects are not used: a Step4 run on a project writes a quant run
  into it, and the user's projects are read only. The synthetic fixtures
  of the existing Step4 tests (full `run_extraction`) cover the product
  path; real-machine acceptance (user): one normal Step4 run, its h5ad
  opened as usual.
* Checked already (2026-11-02, anndata 0.11.4 / h5py 3.16.0): an AnnData
  written with `X=None` has no `X`; an `X` dataset added afterwards with
  the attrs above reads back as dense float32.

## 2b. Results (2026-11-02)

Synthetic disk sink 2 M × 50 (3 layers, 22 obs columns), scratch script
`s4exp/export_bench.py`, own process each, old = HEAD 701258c worktree:

| | before | after |
|---|---|---|
| h5ad (f4 sink): time / peak RSS over start | 5.4 s / 1,961 MB | 5.4 s / 1,016 MB |
| CSV (f8 sink, 6.5 GB file): time / peak RSS over start | 173.5 s / 275 MB | **14.5–15.0 s** / 385 MB |
| CSV bytes | | **identical** (`cmp`) |
| h5ad read back | | X, layers, obs, var, uns equal |

The h5ad's remaining ~1 GB is the obs table (above). The CSV's extra
~110 MB is bounded (two sink blocks + one part's text), not growing with n.
Rust `%.17g` vs `np.savetxt`: identical on 7.2 M values (random bit
patterns over all magnitudes, normals ×10^±30, edge values) in three seeds.
Tests: new `tests/test_step4_stream_export.py` (7) + Step4 / core / install
suites 182 passed; Rust 113 passed.

## 3. Scope (closed whitelist)

* `workers/feature_extract_worker.py` (`_write_h5ad`, `_write_csv` and
  their new local helpers only)
* NEW `tests/test_step4_stream_export.py`
* v2: NEW `native/fusion_viewer_core/src/csv_text.rs`, NEW `src/py/csv.rs`,
  `src/py/mod.rs`, `src/lib.rs` (module lines), `Cargo.toml` /
  `Cargo.lock` 0.2.1
* v2: `tests/test_step4_worker.py` (its CSV fault injection also covers the
  Rust path: one line); `tests/test_core_api_check.py`,
  `tests/test_install_scripts.py` (`read_text(encoding="utf-8")`: they
  failed with a C locale set by another module in the same run)
* this plan; `KNOWN_ISSUES.md` (one line: the remaining Step4 memory items
  of recommendation 6 point to `docs/step4_20m_laptop_plan.md`; v2: the
  Step0 compare line corrected to the measured HDD finding)

Not in scope: `core/quant_engine.py`, the obs table, geometry, distribution
frames, the 20 M plan's E0–E4, any new mechanism.

## 4. Questions for the user

1. Only the X export now (this plan), and the larger 20-million-cell plan
   (`docs/step4_20m_laptop_plan.md`) later as its own decision — agreed?
   (User: X + CSV now, SpatialData export next.)
