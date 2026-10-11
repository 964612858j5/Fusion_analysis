//! `GlCore`: the probe painter inside Qt's context (rules: core gl.rs).

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::sync::Mutex;

/// unsendable: GL lives on the GUI thread only.
#[pyclass(unsendable)]
pub struct GlCore {
    inner: Mutex<crate::gl::GlCore>,
}

fn err(e: String) -> PyErr {
    PyRuntimeError::new_err(e)
}

#[pymethods]
impl GlCore {
    /// `get_proc(name: bytes) -> int`, e.g. Qt's getProcAddress; called
    /// once per GL function, now, with the context current.
    #[new]
    fn new(get_proc: Bound<'_, PyAny>) -> PyResult<Self> {
        let mut failure: Option<PyErr> = None;
        let core = crate::gl::GlCore::load(|name| {
            match get_proc.call1((pyo3::types::PyBytes::new(get_proc.py(), name.as_bytes()),))
                .and_then(|v| if v.is_none() { Ok(0usize) } else { v.extract::<usize>() }) {
                Ok(addr) => addr as *const std::ffi::c_void,
                Err(e) => {
                    failure.get_or_insert(e);
                    std::ptr::null()
                }
            }
        });
        if let Some(e) = failure {
            return Err(e);
        }
        Ok(GlCore { inner: Mutex::new(core.map_err(err)?) })
    }

    #[getter]
    fn info(&self) -> String {
        self.inner.lock().unwrap().info.clone()
    }

    #[getter]
    fn live_names(&self) -> usize {
        self.inner.lock().unwrap().live_names()
    }

    fn upload_r8(&self, pixels: &[u8], rows: i32, cols: i32) -> PyResult<()> {
        if pixels.len() != (rows * cols) as usize {
            return Err(err(format!("{} bytes for {rows}x{cols}", pixels.len())));
        }
        self.inner.lock().unwrap().upload_r8(pixels, rows, cols).map_err(err)
    }

    /// Draw into `fbo` (Qt's defaultFramebufferObject) at device size. The
    /// GIL is KEPT: a short GUI-thread call that released it could wait
    /// milliseconds to seconds to get it back behind busy Python threads
    /// (measured in R0, docs/v16_5_r0_interfaces.md §3).
    fn paint(&self, fbo: u32, width: i32, height: i32, color: [f32; 3]) -> PyResult<()> {
        self.inner.lock().unwrap().paint(fbo, width, height, color).map_err(err)
    }

    fn gl_error(&self) -> u32 {
        self.inner.lock().unwrap().error()
    }

    fn release(&self) {
        self.inner.lock().unwrap().release();
    }

    fn forget(&self) {
        self.inner.lock().unwrap().forget();
    }
}
