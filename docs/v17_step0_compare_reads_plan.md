# v17 Step0 compare / slide reads on HDD — plan v2.1 (2026-11-01, codex: READY; A built, measured and WITHDRAWN 2026-11-02, §6)

v2.1: codex round 2 READY; its P3 wording applied (§5).
v2: codex plan review round 1 (4 P2 + P3, no P1) — see §5.

Source: whole-code review 2026-10-31, Fable P1 ("each Rust core opens, reads
and decodes the slide independently … up to 48 reader threads on one file;
one shared raw source and raw-tile cache per file") and P4 ("read order
seeks per tile … add an HDD profile"). User (TG 2026-11-01): measure first
(authorized), agree a design with codex, then the user reviews. Deployment
rule: servers = HDD, edge = SSD; measure both (`docs/deployment_targets.md`).

## 0. Measured (2026-11-01; scripts and logs in the session scratchpad `s0cmp/`)

Slide `FINALRUN_updated_TMA4_Scan1.qptiff` (41.8 GB, uint8, 69 channels,
46800×26880, 6 levels, 512² LZW tiles), two identical copies: SSD
`/dev/nvme0n1p1` (read-ahead 128 KB), HDD `/dev/sdb1` (rotational,
read-ahead **8192 KB**). The real Step0 tab + compare strip (CD45RO; TopHat
15 and cuCIM 50 live, no saved product), display :1 at 60 Hz; per disk two
cold runs (page-cache eviction requested: `sync` + file `DONTNEED`, not
verified) and one warm; sequence: whole slide, enter
compare, 8 pans / zooms, 4 channel switches.

**The review's premise does not hold in this (live-method) scenario.** It
does not disprove the maximum-core count; with saved products it is
unmeasured (§1 C).

| | value |
|---|---|
| Rust cores in compare / reader threads | 4 (full image paused + 3 panels) / 24 |
| cores that actually read the slide | **1** (Original); TopHat / cuCIM are computed in Python from raw tiles Python reads itself |
| Rust tile reads / distinct tiles | 357 / 349 (1.02×) |
| Python native blocks covered by its region reads / distinct | **3070 / 1318 (2.33×)** — INFERRED by expanding completed `read_region` calls into the file's blocks (512² tiles; level 5 of this slide is stripped, 16 × 840), not counted at the decoder; region reads not aligned to tiles decode the same block again; two Python providers (full image's, strip's) |
| tiles read by both Rust and Python | ~210 (indicative: level 5's Python strips and Rust tiles are different grids) |

| | SSD | HDD |
|---|---|---|
| first picture: full image / compare | 4.0 / 2.5 s | 4.1 / 2.5 s (same warm: probably CPU/GL rather than I/O — not isolated) |
| process bytes read (pans + switches) / distinct compressed tile bytes of the whole run (levels with a direct plan only) | 1.2× | **2.9×** (indicative: the windows and units differ, codex; §2 defines the proper gate) |
| channel switch, last read settled (median / max) | 0.58 / 0.63 s | **0.97 / 1.16 s** (warm 0.59) |
| pan / zoom, last read settled (median / max) | 0.21 / 0.63 s | 0.26 / 0.83 s |

**Read-ahead probe** (a MECHANISM test, not a replay of the product: one
fd, serial `pread` of the same tile byte ranges, cold (`sync` + file
`DONTNEED`, eviction not verified), 600 tiles in 40 randomly placed runs of
15 tiles adjacent in file order; script `s0cmp/fadv_probe_seq.py`, output
`s0cmp/fadv_probe.txt`):

| | default | `POSIX_FADV_RANDOM` |
|---|---|---|
| HDD: disk bytes / needed, time | 170.8 / 46.6 MB (3.7×), **1.04 s** | 46.8 / 46.6 MB (1.0×), **0.34 s** |
| SSD: disk bytes / needed, time | 54.8 / 46.6 MB, 0.04–0.05 s | 46.8 / 46.6 MB, 0.08 s |

Scattered single tiles show no amplification on either disk; the 8 MB
read-ahead fires on adjacent reads. On the SSD read-ahead helps a little,
so the hint belongs to rotational disks only.

Odon (design reference, nothing copied): one `TiffPyramid` per file shared by
`Arc`; two worker pools per file (display, raw), min(cores, 6) threads
(`render/tiles.rs:129-134`, `app.rs:1428-1433`), one handle + decoder per
worker (`xenium/tiff_pyramid.rs:1487`), stale requests skipped
(`active_keys`, `:1413-1418`), no read-ahead control, no ordering by file
offset, no side-by-side view of one file.

## 1. Proposal

### A. HDD read profile for the interactive viewers: no read-ahead on a rotational disk (proposed now)

Only the INTERACTIVE readers get the advice; Generate (`FuseJob`, the Python
fusion fallback), Step4 and the other whole-image passes keep the default,
since a sequential pass can benefit from read-ahead (codex P2). Linux keeps
the advice per open file description, so separate opens are unaffected.

* Rust: a new `RawSource::advise_random_access()` (not inside
  `RawSource::open`), called by the viewer constructors only —
  `ViewerCore::new` (`src/py/viewer.rs:28`; product constructors
  `ui/step0/engine_mount.py:395`, `ui/step1_core_binding.py:406`) and the
  display-TIFF source it parses (`src/py/corrected.rs:88`). `Step1Reader`
  (`:158`, used only by tests and probes) is left as it is.
  `FuseJob` (`src/py/fuse_job.rs:57`) does not call it. Corrected-zarr
  chunk files (whole-file reads, `zarr_v2.rs:69`) are left alone.
* Python: `RawTileProvider(path, random_access=False)`; the viewer's
  provider (`viewer/source_tile_provider.py:120`, `open_viewer_source`)
  passes `True`. With it, every pixel-reading handle the provider itself
  opens is advised (companion files tifffile might open on its own for a
  multi-file OME set are not covered; no product case needs them): the shared direct-read descriptor (`_chunk_fd`) and each
  `tifffile.TiffFile` it opens (all through `_counted_tifffile`: the zarr /
  stripped-level path, e.g. level 5 of the measured slide). Generate's and
  Step4's `OmeTiffSource` do not pass it.
* Detection (Linux only; Rust `#[cfg(target_os = "linux")]`, elsewhere a
  no-op): from the opened file's `st_dev` → `/sys/dev/block/<maj>:<min>`
  (symlinks resolved; a partition → its parent device) →
  `queue/rotational` == 1. dm / LVM: their own advertised `queue/rotational`
  when present; anything absent, ambiguous, network or FUSE → unknown →
  no advice (never guessed from a path).
* Switch `BLOCK01_READ_ADVICE`: `auto` (default: rotational → random),
  `random` (always on Linux), `default` (never advise); read when a handle
  is opened, so a change takes effect on reopen / restart; an invalid value
  → `auto` with one log line. Detection or advice failures are logged once
  and never fail a read.
* No cache, scheduler, thread or state machine; read order, thread counts
  and pixels unchanged.

### B. Python live-method reads: decode each raw tile once (later, separate authorization)

The real duplication is Python's: unaligned region reads re-decode tiles
(2.33×) and two providers read the same file. Option: tile-aligned reads
through one small decoded-tile LRU per slide file shared by the Python
providers (a new cache → its own plan and authorization, AGENTS rule 4).
Decide after A is measured.

### C. Not proposed: one cache shared by the Rust cores

Measured Rust duplication is 1.02×: only one compare core reads the slide.
With SAVED TopHat / cuCIM products each panel's core reads its own corrected
product (different files), so cross-core sharing would still only cover the
paused full image vs the Original panel. To be re-measured on a project with
saved products before anyone proposes it again.

### D. Deferred: fewer threads / offset-sorted batches on HDD

Re-measure after A; whether ordering still matters on HDD once
read-ahead is off is a hypothesis to test then.

## 2. Acceptance (A)

* Unit: detection on a fake sysfs tree (partition → parent, dm with and
  without `rotational`, missing → unknown); the switch values (invalid →
  auto); which handles get advice (viewer provider yes, default provider
  no; tifffile handles included); Rust `cargo test` incl. `FuseJob` not
  advised.
* Pixels: synthetic slides read through the viewer provider and the Rust
  reader with `auto` / `random` / `default` are byte-identical.
* Measurement harness corrected first (codex P2): both readers counted in
  the file's native blocks / byte ranges (stripped levels included),
  Python instrumented at its actual reads (direct `pread` and the tifffile
  path), Rust read records with their outcome (error / stale kept apart),
  the same time window for numerator and denominator, eviction success,
  the runtime core version and probe output recorded.
* Product series on both copies (cold ×2 + warm), `auto` vs `default`:
  HDD disk bytes / distinct native-block bytes in the same window close to
  the SSD's (target ≤ 1.3×); channel-switch and pan/zoom READ completion
  and VISIBLE completion (the sampled screen region stable,
  `scripts/a9_pixwatch.py`, its sampling coverage reported — not proof of
  every pixel) both reported, HDD better than `default`; SSD within noise; first
  picture (startup / coarse levels) no worse.
* Non-regression: Generate on the HDD copy (a fixed region) takes no
  longer than before (it is not advised; checked anyway).
* Real-machine acceptance (user): Step0 compare on the HDD copy, channel
  switches feel close to the SSD.

## 3. Scope (closed whitelist, for A)

* NEW `native/fusion_viewer_core/src/read_advice.rs` (detect + advise);
  `src/reader.rs` (`advise_random_access`); `src/py/viewer.rs`,
  `src/py/corrected.rs` (the three viewer call sites); `src/lib.rs` (module
  line); crate version 0.2.1 (`Cargo.toml` / `Cargo.lock`, install plan
  rule; no `CORE_API` change)
* `viewer/raw_tile_provider.py` (`random_access`: advise `_chunk_fd` and the
  handles from `_counted_tifffile`), `viewer/source_tile_provider.py`
  (the viewer passes `random_access=True`)
* NEW `utils/read_advice.py` (the Python detection + switch)
* tests: NEW `tests/test_read_advice.py`; Rust unit tests in the module
* this plan; `docs/deployment_targets.md` (one paragraph: HDD read-ahead)
* measurement scripts in the session scratchpad (`s0cmp/`), corrected as §2
  (not repository files)

## 4. Questions for the user

1. A now, B and C only after re-measuring — agreed?
2. Should the server's HDD read-ahead (8 MB, a system setting) also be
   lowered system-wide (`blockdev --setra`)? Not proposed: it affects every
   program on that disk; A fixes Block01's reads only.

## 5. Codex plan review round 1 — answers

* P2 Python handles missed → every pixel-reading handle of the viewer's
  provider, incl. the tifffile / zarr path (level 5 of the measured slide
  is stripped and goes that way).
* P2 Generate shares the open route → advice is called by the viewer
  constructors only, not in `RawSource::open`; Python by `random_access`
  on the viewer's provider only; Generate non-regression gate added.
* P2 measurement units / windows → §0 relabelled (inferred, indicative);
  §2 corrects the harness before any pass / fail ratio.
* P2 probe ≠ product → called a mechanism test; read vs visible completion
  reported separately; first picture, Generate and pixel gates added.
* P3 detection / switch / platform / cold-cache details → §1 A, §2.
* §4: agreed (A first; B, C, D after product measurements; no system-wide
  read-ahead change).
* Round 2: READY. P3 applied: `Step1Reader` (tests / probes only) not
  advised; tifffile's own companion-file opens not claimed; visible
  completion = a sampled region; eviction "requested", the Rust/Python
  overlap and the CPU/GL attribution marked indicative; probe numbers as in
  `fadv_probe.txt`.

## 6. Outcome (2026-11-02): A measured, failed its gate, withdrawn

User approved A (TG 2026-11-02). A was built as §1 (Rust `read_advice.rs`
+ viewer call sites, Python twin, core 0.2.1, 26 unit tests passing;
strace showed the advice set in the product, none in Generate). Measured
with the corrected harness of §2 (session scratchpad `s0cmp/measure2.py`,
results `r2_*.json`; every cold run checked 0.000 % resident by mincore
before start), slide as §0, `auto` vs `default`, cold ×2 + warm:

| | HDD auto | HDD default | SSD auto | SSD default |
|---|---|---|---|---|
| disk MB, pans + switches window | **111** | 318–329 | 131–134 | 134 |
| same / new distinct native-block bytes | **1.03–1.05×** | 2.97–3.04× | 1.24× | 1.24× |
| channel switch, visible (median) | 1.11–1.13 s | **1.06 s** | 0.65 s | 0.63–0.65 s |
| channel switch, last read (median) | 1.05–1.08 s | 1.00 s | 0.59–0.60 s | 0.59–0.60 s |
| pan / zoom, visible (median) | 0.38–0.39 s | 0.37–0.38 s | 0.31–0.34 s | 0.32–0.34 s |
| warm channel switch, visible | 0.61 s | 0.64 s | 0.64 s | 0.64 s |

Generate (Rust FuseJob, 8192² region, 6 channels, pool of 8, HDD cold):
4.7–5.3 s either way. Python decode multiplicity, now counted at the
actual reads: 1.51× (all through the direct `pread` path in this scenario).

**Reading:** A removes the read-ahead bytes (3× → 1.03×) but not the time:
channel switches got ~5 % SLOWER on the HDD (read-ahead was also fetching
neighbours used next), pans unchanged, SSD unchanged. The HDD's extra
~0.5 s over warm is seek latency: a switch reads ~90 scattered blocks
(~30 Rust + ~60 Python) at ~10 ms each. The probe's 3× (§0) came from
serial reads; the product's concurrent reads let the disk sort them.
Gate "HDD better than default" failed → A reverted (user, TG 2026-11-02),
core back to 0.2.0 on the server. Not committed.

Left for later (user: set aside, next review item): fewer seeks, not
fewer bytes — e.g. the Python live-method panels reusing the tiles the
Rust Original panel already read (B-like; a new cache, own authorization).
