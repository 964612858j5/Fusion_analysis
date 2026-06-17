# 12 — Small-ROI Step2 Remap Smoke Test (Phase 5c)

Validate that Step2 actually runs with a source-aligned manual remap config on a
**small ROI / patch**, region-stratified (clean vs AF-heavy), comparing gate
modes. No full WSI. No segmentation algorithm / `_block_gi` / camp / h5ad change.

Runner: `scripts/smoke_test_step2_remap_small_roi.py` (run as a module so its
package imports resolve):

```bash
python -m block01.scripts.smoke_test_step2_remap_small_roi --help
```

Default is a **DRY RUN**: it validates the config, prints the exact seg_config
keys for the chosen mode, and checks provenance in `--out` if present. Pass
`--execute` with a real `--input` zarr to drive the existing
`SegmentMergeWorker` (no parallel pipeline is invented).

---

## Questions this answers

1. Does Step2 run without crash with a remap config?
2. Does the remap config load + validate once per run?
3. Do HQ2 / CDS2 use remap on the segmentation signal path?
4. Does CDS2 still compute `_block_gi` on the native/corrected raw for camp?
5. Does provenance record the config + gate mode?
6. Does feature extraction / h5ad stay on native intensity (remapped 0–1 never
   becomes `adata.X`)?
7. Does an AF-heavy region behave differently from a clean region?
8. Which gate mode looks safer: `gi`, `remap`, or `remap_and_gi`?

---

## Regions (manual, not auto-detected in 5c)

Provide at least two small ROIs by name/coords/crop:

```text
clean_region      — well-separated cells, low background
AF_heavy_region   — spatially-uneven autofluorescence over weak real signal
```

Optional: RBC/necrosis, tumor–immune boundary, endothelial-rich.

---

## Gate modes compared

| mode | seg_config | meaning |
|---|---|---|
| `baseline` | no remap config | legacy v13 path |
| `gi` | remap config + `remap_gate_mode=gi` | local-z gate (config loaded but gate unchanged) |
| `remap` | remap config + `remap_gate_mode=remap` | manual Min/Max/Gamma replaces the signal gate |
| `remap_and_gi` | remap config + `remap_gate_mode=remap_and_gi` | remap gate AND local-z gate |

CDS2 is the **primary** target. HQ2 is a simpler secondary smoke. lean_carve is
optional / lower priority. For CDS2, `_block_gi`/`camp_z` are computed on native
raw in **all** modes — only the signal gate changes.

---

## Commands (per ROI × mode)

Dry-run validation + settings (safe, no run):

```bash
python -m block01.scripts.smoke_test_step2_remap_small_roi \
  --roi clean_region --method cds2 \
  --remap-config /path/channel_remap_config.json \
  --channels "CK19;CD68;CD3D" \
  --gate-mode remap --allow-preview-remap
```

Execute on a small zarr ROI (reuses SegmentMergeWorker):

```bash
python -m block01.scripts.smoke_test_step2_remap_small_roi \
  --input /path/small_roi.zarr --roi clean_region --method cds2 \
  --remap-config /path/channel_remap_config.json \
  --channels "CK19;CD68;CD3D" \
  --gate-mode remap_and_gi --allow-preview-remap \
  --out /path/smoke_out/clean_remap_and_gi --execute
```

Run the full grid: `{clean_region, AF_heavy_region} × {baseline, gi, remap,
remap_and_gi}` (8 runs). Use distinct `--out` per cell.

`baseline` omits `--remap-config`; `gi`/`remap`/`remap_and_gi` require it +
`--allow-preview-remap` (Step3 configs are preview_only until Step2 promotion).

---

## Metrics to collect (per ROI × mode)

```text
status (success/fail), runtime
cell count, total mask area, mean cell area, median cell area
large-cell outlier count, empty/failed tile count
provenance: channel_remap_config.used.json + channel_remap_provenance.json exist
```

Optional if easy: per-channel positive area after gate, mask∩nuclei territory,
boundary expansion area. Do not overbuild.

Summary table (produced by `format_smoke_summary_table`):

```text
roi             mode           status   runtime    cells     mask_area    mean_area   provenance
--------------  -------------  -------  ---------  --------  -----------  ----------  -----------
clean_region    baseline       ok       ...        ...       ...          ...         n/a
clean_region    gi             ok       ...        ...       ...          ...         yes
clean_region    remap          ok       ...        ...       ...          ...         yes
clean_region    remap_and_gi   ok       ...        ...       ...          ...         yes
AF_heavy_region baseline       ok       ...        ...       ...          ...         n/a
AF_heavy_region gi             ok       ...        ...       ...          ...         yes
AF_heavy_region remap          ok       ...        ...       ...          ...         yes
AF_heavy_region remap_and_gi   ok       ...        ...       ...          ...         yes
```

---

## Visual QC

If the project already has overlay utilities (e.g. `utils/mask_renderer.py`),
save quick PNGs per ROI×mode: DAPI+mask overlay, remapped conditioning+mask
overlay, AF raw+mask overlay. No new viewer is built in 5c.

---

## Provenance checks (every remap run)

`check_remap_provenance(out_dir)` asserts the run output dir contains:

```text
channel_remap_config.used.json
channel_remap_provenance.json
```

and that provenance records:

```text
manual_remap_enabled = true
allow_preview_remap = true
gate_mode (remap / remap_and_gi / gi)
channel_remap_config_hash
used_for = segmentation_only
source_policy present
```

Baseline run: no remap provenance (or `manual_remap_enabled` false).

---

## h5ad / feature-extraction guard

Feature extraction is NOT modified. If the smoke run triggers feature
extraction, verify:

```text
feature_extract_worker reads native/corrected channels (read_region normalize=False)
remapped 0–1 arrays are never passed to feature_extract_worker
adata.X is not remapped intensity
```

If no h5ad is produced in the smoke run, record that the h5ad guard was **not
runtime-tested** in Phase 5c (it remains structurally guaranteed: the remap path
only touches the segmentation signal gate, never the feature-extraction source).

---

## Interpretation

```text
remap         — can manual Min/Max/Gamma alone replace the signal gate?
remap_and_gi  — does local contrast still suppress AF-heavy false expansion?
gi            — legacy local-z baseline
```

Key decision:

```text
If `remap` over-expands cells in the AF-heavy region but `remap_and_gi`
suppresses that over-expansion, the HCC default gate mode should likely be
remap_and_gi rather than remap.
```

Compare cell count / mean+median area / large-cell outliers between modes within
each region. Over-expansion shows as inflated mean/median area and more
large-cell outliers in AF-heavy `remap` runs.

---

## Limitations (Phase 5c)

- Regions are manual; no auto-detection.
- The runner's `--execute` path drives `SegmentMergeWorker` synchronously and is
  environment/data dependent; the default dry run validates + prints only.
- Quantitative metric extraction from run outputs (cells/area) is left to the
  operator / existing QC tables; the script focuses on config validation,
  settings, and provenance.
- h5ad guard is structural, not necessarily runtime-exercised here.
