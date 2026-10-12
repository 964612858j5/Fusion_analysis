//! Python bindings. Each file wraps one core module; no logic lives here.

pub(crate) mod corrected;
mod gil;
mod gl;
mod labels;
mod notify;
pub(crate) mod reader;
mod stats;
mod testing;
mod viewer;

use pyo3::prelude::*;

#[pymodule]
fn fusion_viewer_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(monotonic, m)?)?;
    m.add_function(wrap_pyfunction!(wait_closing, m)?)?;
    m.add_class::<reader::RawReader>()?;
    m.add_class::<gl::GlCore>()?;
    m.add_class::<notify::Notifier>()?;
    m.add_class::<viewer::ViewerCore>()?;
    m.add_class::<corrected::Step1Reader>()?;
    m.add_class::<corrected::ZarrArray>()?;
    m.add_class::<corrected::CancelToken>()?;
    gil::register(m)?;
    testing::register(m)?;
    Ok(())
}

/// Seconds on the `time.monotonic()` time base.
#[pyfunction]
fn monotonic() -> f64 {
    crate::clock::monotonic()
}

/// The program's exit: wait at most `timeout_s` for the readers of closed
/// viewers (GIL released); how many are still running then.
#[pyfunction]
fn wait_closing(py: Python<'_>, timeout_s: f64) -> usize {
    py.detach(|| crate::pool::wait_closing(timeout_s))
}

pub(crate) fn to_py_err(e: crate::error::CoreError) -> PyErr {
    match e {
        crate::error::CoreError::Io(_) => pyo3::exceptions::PyOSError::new_err(e.to_string()),
        _ => pyo3::exceptions::PyValueError::new_err(e.to_string()),
    }
}
