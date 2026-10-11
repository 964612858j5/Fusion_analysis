# fusion_viewer_core

The Step1 viewer's performance core in Rust (branch v16_5). The contract is in
`docs/v16_5_rust_viewer_plan.md` (plan) and `docs/v16_5_r0_interfaces.md`
(interfaces). Status: R0 (skeleton and probes); nothing in the product imports
it yet.

## Build and install (server)

    export PATH=/root/.cargo/bin:$PATH
    cd native/fusion_viewer_core
    /root/.venvs/maturin/bin/maturin build --release -i /root/micromamba/envs/fusion_mesmer/bin/python
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

`cargo tree` lists the transitive ones; all are MIT, Apache-2.0, BSD or Zlib.
One exception, at compile time only: `unicode-ident` ((MIT OR Apache-2.0) AND
Unicode-3.0, a permissive licence) is used by pyo3's procedural macros
(`cargo tree -i unicode-ident`); nothing of it is linked into the wheel.

## Design references

Odon (`/sda1/Fusion/analysis_pipline/odon`, GPL-3) is a design reference only:
no code or constants are copied. Each module's header names the design it
follows; in R0 only `decode.rs` follows one (Odon decodes the file's own tile,
`xenium/tiff_pyramid.rs`).
