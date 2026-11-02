# block01_spatialdata — the SpatialData export environment

Step 4's SpatialData export (`scripts/export_spatialdata.py`) runs in this
small environment, not in `fusion_mesmer`: spatialdata 0.5.0 needs NumPy 2,
while `fusion_mesmer` stays on NumPy 1.26 for TensorFlow (StarDist, DeepCell
Mesmer). `scripts/install_block01.sh` builds it as step 3b
(`<root>/envs/block01_spatialdata-<lock8>`, about 1.2 GB) and records its
Python as `BLOCK01_SPATIALDATA_PYTHON` in `.block01/install.env`; the app
uses it without asking. Plan: `docs/v17_spatialdata_env_install_plan.md`.

* `conda-linux-64.lock` — explicit conda part (python 3.10, pip; conda-forge).
* `requirements-pip.txt` — every pip package pinned, installed with
  `--no-deps`. setuptools is pinned to 81.0.0: `xarray_schema` (used by
  spatialdata) imports `pkg_resources`, which newer setuptools removed.

The versions are those of the Step5 environment the export was first
verified with (`fusion_test2`: spatialdata 0.5.0, anndata 0.10.9, zarr
2.18.3, dask 2024.11.2).

## Rebuilding the locks

```bash
micromamba create -y -p /tmp/sd -c conda-forge python=3.10 pip
/tmp/sd/bin/python -m pip install "spatialdata==0.5.0" "anndata==0.10.9" "zarr==2.18.3" \
    "numcodecs==0.13.1" "dask[array,dataframe]==2024.11.2" "distributed==2024.11.2" \
    "xarray==2025.6.1" "ome-zarr==0.11.1" "geopandas==1.1.3" "shapely==2.0.7" \
    "pyarrow==17.0.0" "pandas==2.2.3" "numpy==2.2.6" "setuptools==81.0.0"
micromamba env export --explicit --md5 -p /tmp/sd > conda-linux-64.lock
{ echo "# pip part ..."; echo "setuptools==81.0.0"; /tmp/sd/bin/python -m pip freeze \
    | grep -v '^setuptools==' | grep -v ' @ '; } > requirements-pip.txt
```

Then run `tests/test_spatialdata_export.py` with
`BLOCK01_SPATIALDATA_PYTHON=/tmp/sd/bin/python`.
