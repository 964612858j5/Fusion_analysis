# Viewer prototype benchmark

**SUPERSEDED BY** `2026-08-29_tonsil_viewer_prototype_v2.md` — v1 conclusions
overstated (order effects, pan coverage bug); kept for provenance only.

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`

| tile | level | method | param | tiles | cold s | io ms med/p90 | kernel ms med/p90 | warm ms | pan reuse% | pan ms | RSS MB | GPU MB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 256 | 0 | raw | - | 81 | 6.34 | 300.99/352.82 | n/a/n/a | 40.1 | 100% | 1.1 | 1524.7 | 0.0 |
| 256 | 0 | tophat | 25 | 81 | 3.68 | 38.12/90.63 | 0.70/1.01 | 1.3 | 100% | 1.3 | 1524.7 | 0.0 |
| 256 | 0 | tophat | 50 | 81 | 3.74 | 39.05/82.68 | 0.88/1.35 | 1.9 | 100% | 1.9 | 1524.7 | 0.0 |
| 256 | 0 | cucim | 50 | 81 | 3.73 | 40.94/86.73 | 0.92/1.42 | 2.6 | 100% | 2.6 | 1741.6 | 0.0 |
| 256 | 0 | cucim | 100 | 81 | 4.74 | 43.40/68.44 | 1.85/2.84 | 2.0 | 100% | 1.9 | 2049.1 | 0.0 |
| 256 | 1 | raw | - | 81 | 5.93 | 274.87/366.56 | n/a/n/a | 2.0 | 100% | 2.2 | 2049.1 | 0.0 |
| 256 | 1 | tophat | 25 | 81 | 4.14 | 44.20/51.66 | 0.69/0.86 | 2.3 | 100% | 2.4 | 2049.1 | 0.0 |
| 256 | 1 | tophat | 50 | 81 | 3.91 | 43.65/50.61 | 0.87/1.03 | 2.3 | 100% | 2.5 | 2217.6 | 0.0 |
| 256 | 1 | cucim | 50 | 81 | 3.72 | 43.49/47.58 | 0.93/1.13 | 2.2 | 100% | 104.0 | 2413.3 | 0.0 |
| 256 | 1 | cucim | 100 | 81 | 3.71 | 39.01/47.78 | 1.86/2.27 | 1.2 | 100% | 1.1 | 2614.1 | 0.0 |
| 256 | 2 | raw | - | 64 | 4.15 | 206.96/348.25 | n/a/n/a | 12.1 | 100% | 1.8 | 2614.1 | 0.0 |
| 256 | 2 | tophat | 25 | 64 | 3.12 | 42.84/48.09 | 0.68/0.86 | 1.7 | 100% | 1.9 | 2614.1 | 0.0 |
| 256 | 2 | tophat | 50 | 64 | 2.96 | 41.03/47.16 | 0.84/0.88 | 18.4 | 100% | 2.1 | 2614.1 | 0.0 |
| 256 | 2 | cucim | 50 | 64 | 2.72 | 38.35/46.07 | 0.90/1.09 | 12.6 | 100% | 2.2 | 2614.1 | 0.0 |
| 256 | 2 | cucim | 100 | 64 | 2.77 | 34.44/49.11 | 1.81/2.21 | 1.8 | 100% | 1.9 | 2614.1 | 0.0 |
| 512 | 0 | raw | - | 25 | 1.59 | 262.54/299.53 | n/a/n/a | 0.7 | 100% | 0.7 | 2635.4 | 0.0 |
| 512 | 0 | tophat | 25 | 25 | 1.42 | 47.19/110.74 | 0.94/1.23 | 0.9 | 100% | 0.8 | 2775.5 | 0.0 |
| 512 | 0 | tophat | 50 | 25 | 1.72 | 43.51/115.46 | 1.48/1.90 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 0 | cucim | 50 | 25 | 0.97 | 31.76/68.35 | 1.44/1.68 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 0 | cucim | 100 | 25 | 1.06 | 32.31/75.29 | 2.93/3.40 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 1 | raw | - | 25 | 1.41 | 227.48/297.34 | n/a/n/a | 0.7 | 100% | 0.7 | 2834.6 | 0.0 |
| 512 | 1 | tophat | 25 | 25 | 1.34 | 49.06/55.19 | 1.07/2.07 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 1 | tophat | 50 | 25 | 1.34 | 47.50/51.84 | 1.49/1.76 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 1 | cucim | 50 | 25 | 1.34 | 47.18/53.68 | 1.47/1.86 | 0.9 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 1 | cucim | 100 | 25 | 1.39 | 48.46/55.31 | 2.97/3.52 | 0.8 | 100% | 0.8 | 2834.6 | 0.0 |
| 512 | 2 | raw | - | 16 | 1.06 | 260.70/330.09 | n/a/n/a | 0.4 | 100% | 0.4 | 2834.6 | 0.0 |
| 512 | 2 | tophat | 25 | 16 | 0.94 | 46.10/136.39 | 0.94/1.85 | 0.4 | 100% | 0.3 | 2834.6 | 0.0 |
| 512 | 2 | tophat | 50 | 16 | 0.79 | 42.88/50.85 | 1.34/1.49 | 0.6 | 100% | 0.6 | 2834.6 | 0.0 |
| 512 | 2 | cucim | 50 | 16 | 0.80 | 44.19/49.14 | 1.32/1.50 | 0.6 | 100% | 0.5 | 2834.6 | 0.0 |
| 512 | 2 | cucim | 100 | 16 | 0.94 | 45.80/133.79 | 2.48/3.70 | 0.4 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 0 | raw | - | 9 | 0.66 | 258.00/282.90 | n/a/n/a | 0.3 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 0 | tophat | 25 | 9 | 0.60 | 56.82/69.28 | 2.29/2.31 | 0.3 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 0 | tophat | 50 | 9 | 0.59 | 56.48/59.22 | 3.79/4.42 | 0.3 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 0 | cucim | 50 | 9 | 0.58 | 57.32/59.00 | 3.29/3.53 | 0.3 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 0 | cucim | 100 | 9 | 0.61 | 54.84/59.99 | 6.49/7.35 | 0.4 | 100% | 0.3 | 2834.6 | 0.0 |
| 1024 | 1 | raw | - | 9 | 0.53 | 225.20/247.22 | n/a/n/a | 0.2 | 67% | 234.7 | 2834.6 | 0.0 |
| 1024 | 1 | tophat | 25 | 9 | 0.63 | 58.74/64.33 | 2.30/3.13 | 0.4 | 67% | 191.3 | 2834.6 | 0.0 |
| 1024 | 1 | tophat | 50 | 9 | 0.62 | 57.91/60.86 | 4.27/4.51 | 0.3 | 67% | 196.2 | 2834.6 | 0.0 |
| 1024 | 1 | cucim | 50 | 9 | 0.59 | 57.94/61.35 | 3.21/3.74 | 0.4 | 67% | 186.9 | 2834.6 | 0.0 |
| 1024 | 1 | cucim | 100 | 9 | 0.62 | 55.52/61.29 | 7.30/7.70 | 0.4 | 67% | 205.7 | 2834.6 | 0.0 |
| 1024 | 2 | raw | - | 4 | 0.27 | 216.33/266.06 | n/a/n/a | 0.2 | 100% | 0.1 | 2834.6 | 0.0 |
| 1024 | 2 | tophat | 25 | 4 | 0.19 | 46.52/50.82 | 1.80/2.16 | 0.2 | 100% | 0.2 | 2834.6 | 0.0 |
| 1024 | 2 | tophat | 50 | 4 | 0.21 | 48.75/50.72 | 2.74/3.22 | 0.2 | 100% | 0.2 | 2834.6 | 0.0 |
| 1024 | 2 | cucim | 50 | 4 | 0.71 | 87.39/493.78 | 2.44/2.93 | 0.3 | 100% | 0.2 | 2834.6 | 0.0 |
| 1024 | 2 | cucim | 100 | 4 | 0.15 | 31.84/35.15 | 4.98/6.21 | 0.3 | 100% | 0.2 | 2834.6 | 0.0 |

## Conclusions (2026-08-29, this machine, tonsil 29ch 31416x28800 uint8)

1. **GPU kernels are NOT the bottleneck.** Tophat/cuCIM per tile (incl.
   H2D/D2H) = 0.7–7 ms across all tile sizes and params. The feared
   heavy-compute problem does not materialize at viewport scale.
2. **I/O dominates**: ~40–60 ms per tile warm-OS-cache, ~250–300 ms first
   decode of a region. Cold 2048² viewport fill (25×512² tiles) ≈ 1.0–1.7 s,
   and that is SERIALIZED because the correction path does its halo read
   inside the single compute worker. Next optimization: stage the halo read
   through the I/O pool (or raise compute workers for I/O overlap only) —
   expected ÷3–4 on cold fill. Gate G2 measured, not promised.
3. **Cache-hit path (G1)**: warm re-request and half-tile pan fill are
   ≤3 ms for a full viewport — 60 FPS drag budget is trivially met at the
   data layer; rendering is the remaining variable.
4. **Tile size**: 512 confirmed as default (25 requests/viewport, kernels
   tiny). 256 wastes ~3–4× wall time on request/IO overhead (81 tiles);
   1024 is fine too (9 tiles) but pan refills cost ~200 ms when 1/3 of
   tiles are new — coarser granularity, lumpier refills.
5. **Memory**: RSS grew to ~2.8 GB across the full matrix (caches at
   default budgets, never evicting here); GPU pool reports ~0 after frees.
   Byte budgets must be enforced in the real integration.
6. **Per-sigma background intermediate cache**: deprioritized — kernels are
   milliseconds; not worth the complexity until profiling says otherwise.
