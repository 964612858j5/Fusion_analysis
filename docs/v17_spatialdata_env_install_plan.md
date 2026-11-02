# v17 Installer: the SpatialData export environment — plan v1.1 (2026-11-02)

v1.1: codex plan review READY (one P3 + two clarifications applied).

User (TG 2026-11-02): "对新用户，应该在配置环境时就后台默认配好，而不是在step4手动点"
— a new user's install sets up the SpatialData Python by default; Step4's
file chooser stays only for checkouts not installed by the installer
(this server's development setup). Follow-up §6 of
`docs/v17_spatialdata_export_plan.md`.

## 0. Facts (measured 2026-11-02)

* One environment is impossible: spatialdata 0.5.0 needs NumPy ≥ 2;
  `fusion_mesmer` must stay on NumPy 1.26.4 (TensorFlow: StarDist, DeepCell).
* A standalone environment works: micromamba `python=3.10 pip`
  (conda-forge) + pip `spatialdata==0.5.0` with the versions of the
  known-good Step5 environment (`fusion_test2`: anndata 0.10.9, zarr
  2.18.3, numcodecs 0.13.1, dask/distributed 2024.11.2, xarray 2025.6.1,
  ome-zarr 0.11.1, geopandas 1.1.3, shapely 2.0.7, pyarrow 17.0.0, pandas
  2.2.3, numpy 2.2.6) and **setuptools 81.0.0** (`xarray_schema` imports
  `pkg_resources`, which newer setuptools removed — the cause of
  `step5_test`'s broken import).
* Locked as `envs/block01_spatialdata/conda-linux-64.lock` (25 conda
  packages, explicit with md5) + `requirements-pip.txt` (93 pins, conda-owned
  `@ file://` lines removed). Rebuilt from the locks exactly as the
  installer does: **62 s, 1.2 GB**, `pip check` clean, spatialdata 0.5.0
  imports; with it as `BLOCK01_SPATIALDATA_PYTHON` all 11
  `tests/test_spatialdata_export.py` pass and the whole-slide export
  (2.1 M cells) takes 4.0 GB / 13 s.

## 1. Change

* `scripts/install_block01.sh`, new step **3b** after the main environment:
  `ensure_spatialdata_environment` — the same pattern as
  `ensure_environment` (prefix `$ROOT/envs/block01_spatialdata-<lock8>`,
  `<lock8>` hashed from this environment's own two lock files,
  `.owner` / env marker / ok marker, states absent / complete / built /
  interrupted / foreign, micromamba with its own HOME and package cache,
  `pip install --no-deps -r`, `pip check`), then the check
  `<python> -I -c "import spatialdata"`.
* Install record: `export BLOCK01_SPATIALDATA_PYTHON=<that python>` in
  `.block01/install.env`; the launcher already sources the record, so the
  app finds it first (`utils/spatialdata_python.find`) — Step4's tick works
  with no chooser.
* Preflight space: + 2 GiB (integer GiB as the existing rule: 1.2 GB
  environment + package / pip caches) when the environment is absent or
  interrupted (an interrupted prefix credited by its size, as the main
  one); 0 when built / complete or `--no-spatialdata`.
* Self-test (step 6): the import check above; on failure the install stops
  with the reason (it is part of a complete install).
* `--no-spatialdata`: skip the environment (the record then has no
  `BLOCK01_SPATIALDATA_PYTHON`; Step4's chooser remains as now).
* `envs/block01_spatialdata/README.md`: what it is, why separate, how the
  locks were made, how to rebuild them.
* README / user guides: one line each (the install sets it up; the chooser
  is for a checkout without the installer).

## 2. Acceptance

* `tests/test_install_scripts.py` (sourced mode, fake micromamba / pip, as
  the existing tests): the new step creates the prefix with owner and
  markers, resumes `built`, removes `interrupted`, refuses `foreign`, the
  record carries the variable, `--no-spatialdata` skips it and leaves the
  variable out.
* Clean `ubuntu:22.04` container (as the install item): full install with
  the new step; the launcher's environment has the variable; a Step4
  SpatialData export in that install on a synthetic project succeeds; a
  second run reports the environment as installed.
* Isolated install on the server (temporary root, `env -i`), as before.
* CI: `bash -n` + shellcheck already run on the scripts.

## 3. Scope (closed whitelist)

* `scripts/install_block01.sh`
* NEW `envs/block01_spatialdata/` (`conda-linux-64.lock`,
  `requirements-pip.txt`, `README.md`)
* `tests/test_install_scripts.py`
* `README.md`, `docs/user_guide.md`, `docs/用户指南.md`,
  `docs/install_with_an_ai_agent.md` (one line each; the guides' Step 4
  SpatialData sentence now says the installer sets the Python up)
* this plan; `docs/v17_install_plan.md` (a pointer to this plan)

Not in scope: the launcher (it already sources the record), Step4, the
export script, Windows (native-installer item), this server's own
`fusion_mesmer` / `fusion_test2`.

## 4. Implementation notes (2026-11-02)

* As §1: `ensure_spatialdata_environment` (step 3b, after the main
  environment), `sd_lock_id` / `sd_env_prefix`, `--no-spatialdata`, the
  space rule, the record line (an `if`, so `set -e` cannot trip on it when
  skipped); the import check runs inside step 3b (not step 6), so the
  record is written only for a working environment.
* Tests: `tests/test_install_scripts.py` 29 passed (6 new: created / owned /
  checked; interrupted redone, complete kept, foreign refused untouched; a
  non-importing environment stops the install without the ok marker;
  `--no-spatialdata`; the record with / without the variable; the space
  figures updated). `shellcheck -x` clean.
* Clean `ubuntu:22.04` container (`curl bzip2 ca-certificates` only):
  real micromamba download + this step built the environment in **78 s**
  (1.2 GB), an export of a synthetic run with it succeeded, the rerun took
  2 s, `$HOME` unchanged (with `PIP_CACHE_DIR` under the root, as `main`
  sets it). The full installer was not rerun end to end in the container
  (the other steps are unchanged since the install item's container test).
