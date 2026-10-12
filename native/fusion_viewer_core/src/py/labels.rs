//! Step3's masks handed over by Python (R3 plan §2.1, L1): the snapshot's
//! layers, and the bytes of the planes the core holds no copy of yet
//! (`labels_missing`); checked here, kept by `label_store`, drawn by
//! `render::labels` on the next paint.

use crate::label_store::{Layer, Plane, LABEL_BUDGET};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// (id, world rect x0 x1 y0 y1, ids as native u32 bytes or None, rows, cols)
pub type PlaneIn<'py> = (u64, [f64; 4], Option<Bound<'py, PyBytes>>, usize, usize);
/// (planes, colour, alpha, outline radius in screen pixels, fill)
pub type LayerIn<'py> = (Vec<PlaneIn<'py>>, [f32; 3], f32, i32, bool);

pub(super) fn set(core: &mut crate::viewer::ViewerCore, layers: Vec<LayerIn<'_>>) -> PyResult<()> {
    let keep: std::collections::HashSet<u64> = layers.iter().flat_map(|l| l.0.iter().map(|p| p.0)).collect();
    let mut total = 0usize;
    let mut out = Vec::with_capacity(layers.len());
    for (planes, color, alpha, radius, fill) in layers {
        let mut kept = Vec::with_capacity(planes.len());
        for (id, rect, data, rows, cols) in planes {
            if !(rect[1] > rect[0] && rect[3] > rect[2]) || rows == 0 || cols == 0 {
                return Err(PyValueError::new_err(format!("label plane {id}: empty rect or shape")));
            }
            total += rows * cols * 4;
            match data {
                Some(bytes) => {
                    let raw = bytes.as_bytes();
                    if raw.len() != rows * cols * 4 {
                        return Err(PyValueError::new_err(format!("label plane {id}: {} bytes for {rows}x{cols}", raw.len())));
                    }
                    let mut ids = vec![0u32; rows * cols];      // one copy, bytes as they are (native order)
                    unsafe { std::ptr::copy_nonoverlapping(raw.as_ptr(), ids.as_mut_ptr() as *mut u8, raw.len()) };
                    core.labels.insert(id, Plane { rows, cols, ids }, &keep);
                }
                None if core.labels.has(id) => {}
                None => return Err(PyValueError::new_err(format!("label plane {id} is not held: send its ids"))),
            }
            kept.push((id, rect));
        }
        out.push(Layer { planes: kept, color, alpha, radius: radius.clamp(0, 8), fill });
    }
    if total > LABEL_BUDGET {
        return Err(PyValueError::new_err(format!(
            "label working set {total} bytes exceeds the label budget {LABEL_BUDGET}")));
    }
    core.labels.set(out);
    Ok(())
}

/// `Step1GpuLayer.label_stats()`'s keys, the cache's included.
pub(super) fn stats<'py>(py: Python<'py>, core: &crate::viewer::ViewerCore) -> PyResult<Bound<'py, PyDict>> {
    let l = &core.labels;
    let d = PyDict::new(py);
    d.set_item("image_submissions", l.image_submissions)?;
    d.set_item("label_compositions", l.label_compositions)?;
    d.set_item("label_passes", l.label_passes)?;
    d.set_item("shown", l.shown)?;
    let cache = PyDict::new(py);
    cache.set_item("budget_bytes", LABEL_BUDGET)?;
    cache.set_item("copy_bytes", l.copy_bytes())?;
    if let Some(lg) = core.gpu.as_ref().and_then(|g| g.render.label_gpu()) {
        cache.set_item("bytes", lg.bytes)?;
        cache.set_item("uploads", lg.uploads)?;
        cache.set_item("evictions", lg.evictions)?;
    }
    d.set_item("cache", cache)?;
    Ok(d)
}
