//! Binding of `csv_text`: Step4's CSV row blocks as text.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// `values`: a row-major float64 table's bytes (native order), `cols`
/// columns; `integer[k]`: column k as `%d`, else `%.17g`; `extra`: text
/// columns put right after column 0; `newline`: each line's end. The lines
/// as UTF-8 bytes, formatted on `threads` threads with the GIL released.
#[pyfunction]
#[pyo3(signature = (values, cols, integer, extra, threads, newline="\n"))]
fn format_csv_rows<'py>(py: Python<'py>, values: &[u8], cols: usize, integer: Vec<bool>,
                        extra: Vec<Vec<String>>, threads: usize, newline: &str)
                        -> PyResult<Bound<'py, PyBytes>> {
    if values.len() % 8 != 0 {
        return Err(PyValueError::new_err("float64 bytes expected"));
    }
    let parts = py.detach(|| {
        let owned;
        let v: &[f64] = if (values.as_ptr() as usize) % std::mem::align_of::<f64>() == 0 {
            // SAFETY: aligned, length a multiple of 8, any bit pattern is an f64
            unsafe { std::slice::from_raw_parts(values.as_ptr() as *const f64, values.len() / 8) }
        } else {
            owned = values.chunks_exact(8)
                .map(|c| f64::from_ne_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
                .collect::<Vec<f64>>();
            &owned
        };
        crate::csv_text::format_rows(v, cols, &integer, &extra, newline, threads)
    }).map_err(PyValueError::new_err)?;
    let total = parts.iter().map(String::len).sum();
    PyBytes::new_with(py, total, |out| {
        let mut at = 0;
        for p in &parts {
            out[at..at + p.len()].copy_from_slice(p.as_bytes());
            at += p.len();
        }
        Ok(())
    })
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(format_csv_rows, m)?)
}
