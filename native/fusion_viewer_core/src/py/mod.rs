//! Python bindings. Each file wraps one core module; no logic lives here.

mod gil;
mod gl;
mod notify;
mod reader;
mod testing;
mod viewer;

use pyo3::prelude::*;

#[pymodule]
fn fusion_viewer_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(monotonic, m)?)?;
    m.add_class::<reader::RawReader>()?;
    m.add_class::<gl::GlCore>()?;
    m.add_class::<notify::Notifier>()?;
    m.add_class::<viewer::ViewerCore>()?;
    gil::register(m)?;
    testing::register(m)?;
    Ok(())
}

/// Seconds on the `time.monotonic()` time base.
#[pyfunction]
fn monotonic() -> f64 {
    crate::clock::monotonic()
}

pub(crate) fn to_py_err(e: crate::error::CoreError) -> PyErr {
    match e {
        crate::error::CoreError::Io(_) => pyo3::exceptions::PyOSError::new_err(e.to_string()),
        _ => pyo3::exceptions::PyValueError::new_err(e.to_string()),
    }
}
