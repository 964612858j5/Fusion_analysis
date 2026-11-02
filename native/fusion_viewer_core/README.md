# fusion_viewer_core

The viewers' performance core in Rust: Step0's full-image engine, the Step1 /
Step3 / Regions viewers, the Rust fusion job and the exit wait use it
(`ui/step1_core_adapter.py` is the one compatibility check). Contract history:
`docs/v16_5_rust_viewer_plan.md`, `docs/v16_5_r0_interfaces.md`.

## Version and interface number (docs/v17_install_plan.md §1.1)

* `CORE_API` (`src/py/mod.rs`): raise it on ANY incompatible change of what
  Python sees (a removed or renamed class, function or argument, a changed
  meaning); `REQUIRED_CORE_API` in `ui/step1_core_adapter.py` moves with it.
* `version` (`Cargo.toml`, and `Cargo.lock` by `cargo update -p
  fusion_viewer_core --offline`): bump it for EVERY released binary, also for
  fixes that leave the interface alone; raise `MIN_CORE_VERSION` in the
  adapter when the Python side needs that build.
* A core with another `CORE_API`, without one, or older than
  `MIN_CORE_VERSION` is refused everywhere; the app then uses its Python
  paths and says why.

## Releases (prebuilt wheels)

`.github/workflows/fusion_viewer_core_release.yml` builds the Linux
(manylinux_2_28) and Windows abi3 wheels for a tag `core-v<version>`, tests
them installed, and publishes them with `SHA256SUMS` as a GitHub Release
(each release only with the owner's go). The digests are then committed to
`wheels.lock` here; `scripts/install_block01.sh` installs only a wheel whose
digest matches the checkout's `wheels.lock`.

## Build and install (server)

    export PATH=/root/.cargo/bin:$PATH
    cd native/fusion_viewer_core
    /root/.venvs/maturin/bin/maturin build --release --locked -i /root/micromamba/envs/fusion_mesmer/bin/python
    /root/micromamba/envs/fusion_mesmer/bin/python -m pip install --no-deps --force-reinstall \
        target/wheels/fusion_viewer_core-*-cp310-abi3-*.whl

- maturin lives in its own venv (`/root/.venvs/maturin`), not in fusion_mesmer.
- One abi3 wheel serves CPython 3.10+ (GIL builds only), one per OS.
- Remove with `pip uninstall fusion_viewer_core`.

Rust unit tests: `PYO3_PYTHON=/root/micromamba/envs/fusion_mesmer/bin/python
LD_LIBRARY_PATH=/root/micromamba/envs/fusion_mesmer/lib cargo test --release --lib`.
Windows type check (no linker needed): `cargo check --release --target x86_64-pc-windows-msvc`.

## Layout

Small single-purpose modules (user ruling 2026-10-11): one concern per file; the
core modules know nothing of Python, `src/py/` is a thin binding layer with no
logic. See the module table in `docs/v16_5_r0_interfaces.md` §8.

## Dependencies and licences

| crate | use | licence |
|---|---|---|
| pyo3 | Python bindings | MIT OR Apache-2.0 |
| flate2 (zlib-rs backend) | deflate | MIT OR Apache-2.0 (zlib-rs: Zlib) |
| weezl | TIFF LZW | MIT OR Apache-2.0 |
| glow | GL through Qt's getProcAddress | MIT OR Apache-2.0 OR Zlib |
| socket2 | the notice socket (fd / SOCKET) | MIT OR Apache-2.0 |
| libc (unix), windows-sys (windows) | clock, file identity | MIT OR Apache-2.0 |

Translated third-party code (not a crate): `src/fuse_powf.rs` is ARM
optimized-routines' `powf` (v23.01, `56e3bf05`), MIT option, notice in
`THIRD_PARTY_NOTICES.md` (shipped in the wheel's `.dist-info/licenses/`). Unlike
Odon below, this code and its constants are copied on purpose: the fused image
must match NumPy/glibc bit for bit (`docs/f5_rust_fusion_plan.md` §3).

`cargo tree` lists the transitive ones; all are MIT, Apache-2.0, BSD or Zlib.
One exception, at compile time only: `unicode-ident` ((MIT OR Apache-2.0) AND
Unicode-3.0, a permissive licence) is used by pyo3's procedural macros
(`cargo tree -i unicode-ident`); nothing of it is linked into the wheel.

## Design references

Odon (`/sda1/Fusion/analysis_pipline/odon`, GPL-3) is a design reference only:
no code or constants are copied. Each module's header names the design it
follows; in R0 only `decode.rs` follows one (Odon decodes the file's own tile,
`xenium/tiff_pyramid.rs`).
