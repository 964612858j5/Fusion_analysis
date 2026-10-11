//! Test entry points: pure functions exposed for parity tests against the
//! Python binding. No product code calls them.

use crate::admit::{admit, Budgets};
use crate::geom::Pyramid;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// `admit` for `shapes` ((h, w) per level), as a dict.
#[pyfunction]
#[pyo3(signature = (shapes, ideal, bbox_l0, channels, max_fine_tiles, max_fine_bytes, total))]
fn admit_for_test<'py>(py: Python<'py>, shapes: Vec<(usize, usize)>, ideal: u8, bbox_l0: [i64; 4],
                       channels: usize, max_fine_tiles: usize, max_fine_bytes: u64,
                       total: Option<u64>) -> PyResult<Bound<'py, PyDict>> {
    let p = Pyramid { shapes };
    let b = Budgets { max_fine_tiles, max_fine_bytes_per_channel: max_fine_bytes, total, texel_bytes: 1 };
    let a = admit(&p, ideal, bbox_l0, channels, &b);
    let d = PyDict::new(py);
    d.set_item("ideal", a.ideal)?;
    d.set_item("level", a.level)?;
    d.set_item("tiles", a.tiles.clone())?;
    d.set_item("coarse_only", a.coarse_only)?;
    d.set_item("fine_bytes", a.fine_bytes)?;
    d.set_item("carry", a.carry)?;
    d.set_item("rect", a.rect.map(|r| (r[0], r[1], r[2], r[3])))?;
    Ok(d)
}

/// `Pyramid::cover` (sorted (tx, ty)).
#[pyfunction]
fn cover_for_test(shapes: Vec<(usize, usize)>, bbox_l0: [i64; 4], level: u8) -> Vec<(u32, u32)> {
    let mut c = Pyramid { shapes }.cover(bbox_l0, level);
    c.sort();
    c
}

/// `grow_probe::run` in the caller's current context (`get_proc`: Qt's
/// getProcAddress), `warm` none | old | touch: per block [drain,
/// allocate, its finish, warm, upload+draw of layer 0, the middle, the
/// last, the middle again, the layer after it] ms.
#[pyfunction]
fn grow_probe_for_test(get_proc: Bound<'_, PyAny>, layers: u32, storage: bool, warm: &str,
                       count: usize) -> PyResult<Vec<[f64; 9]>> {
    let mut failure: Option<PyErr> = None;
    let gl = unsafe {
        glow::Context::from_loader_function(|name| {
            match get_proc.call1((pyo3::types::PyBytes::new(get_proc.py(), name.as_bytes()),))
                .and_then(|v| if v.is_none() { Ok(0usize) } else { v.extract::<usize>() }) {
                Ok(a) => a as *const std::ffi::c_void,
                Err(e) => { failure.get_or_insert(e); std::ptr::null() }
            }
        })
    };
    if let Some(e) = failure {
        return Err(e);
    }
    use crate::grow_probe::Warm;
    let warm = match warm { "old" => Warm::Old, "touch" => Warm::Touch, _ => Warm::None };
    Ok(unsafe { crate::grow_probe::run(&gl, layers, storage, warm, count) })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(admit_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(cover_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(grow_probe_for_test, m)?)?;
    Ok(())
}
