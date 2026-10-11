//! GIL probes: what releasing and re-taking the GIL costs.

use pyo3::prelude::*;
use std::time::{Duration, Instant};

fn spin(us: u64) {
    let end = Instant::now() + Duration::from_micros(us);
    while Instant::now() < end {
        std::hint::spin_loop();
    }
}

/// Release the GIL, spin `us` microseconds, take it back.
#[pyfunction]
fn detached_spin(py: Python<'_>, us: u64) {
    py.detach(|| spin(us));
}

/// Spin `us` microseconds WITH the GIL held (a busy Python-side caller).
#[pyfunction]
fn held_spin(us: u64) {
    spin(us);
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(detached_spin, m)?)?;
    m.add_function(wrap_pyfunction!(held_spin, m)?)?;
    Ok(())
}
