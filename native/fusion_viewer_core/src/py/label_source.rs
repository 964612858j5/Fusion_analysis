//! Step3's masks for the core to plan and read (R3b plan §2): the sources
//! as Python describes them (`ui/step3_core_labels.py`), styles, counters;
//! and the planner alone for the parity test against `_plan`.

use crate::label_source::{KindSource, PyramidLevel};
use crate::zarr_v2::U32Array;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn item<'py>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("label source needs '{k}'")))
}

/// `{path, shape, chunks, dtype, order, compressor, cname, shuffle,
/// filters_none, fill, separator}` -> a checked array (ValueError: refused).
fn array(d: &Bound<'_, PyDict>) -> PyResult<U32Array> {
    let sep: String = item(d, "separator")?.extract()?;
    U32Array::new(std::path::PathBuf::from(item(d, "path")?.extract::<String>()?),
                  item(d, "shape")?.extract()?, item(d, "chunks")?.extract()?,
                  &item(d, "dtype")?.extract::<String>()?, &item(d, "order")?.extract::<String>()?,
                  &item(d, "compressor")?.extract::<String>()?, &item(d, "cname")?.extract::<String>()?,
                  item(d, "shuffle")?.extract()?, item(d, "filters_none")?.extract()?,
                  item(d, "fill")?.extract()?, sep.chars().next().unwrap_or('.'))
        .map_err(super::to_py_err)
}

/// Would the core read this array? ValueError says why not (R3b §2.6).
#[pyfunction]
pub fn check_label_array(d: &Bound<'_, PyDict>) -> PyResult<()> {
    array(d).map(|_| ())
}

/// `{level0, origin0, pyramid: None | [{level, origin, array}]}`.
pub(super) fn kind(d: &Bound<'_, PyDict>) -> PyResult<KindSource> {
    let level0 = array(item(d, "level0")?.cast::<PyDict>()?)?;
    let origin0: [i64; 2] = item(d, "origin0")?.extract()?;
    let pyramid = match d.get_item("pyramid")? {
        Some(v) if !v.is_none() => {
            let mut levels = Vec::new();
            for lv in v.try_iter()? {
                let lv = lv?;
                let lv = lv.cast::<PyDict>()?;
                levels.push(PyramidLevel { level: item(lv, "level")?.extract()?, origin: item(lv, "origin")?.extract()?,
                                           array: array(item(lv, "array")?.cast::<PyDict>()?)? });
            }
            Some(levels)
        }
        _ => None,
    };
    Ok(KindSource { level0, origin0, pyramid })
}

/// The core's label counters and state (`Step3LabelBinding.stats()` keys).
pub(super) fn stats<'py>(py: Python<'py>, core: &crate::viewer::ViewerCore) -> PyResult<Bound<'py, PyDict>> {
    let lab = &core.lab;
    let d = PyDict::new(py);
    let (queued, reading, pending, stale) = lab.pool.as_ref().map_or((0, 0, 0, 0), |p| p.counts());
    for (k, v) in [("requested", lab.counts[0]), ("read", lab.counts[1]), ("accepted", lab.counts[2]),
                   ("late_dropped", lab.counts[3]), ("pushes", lab.counts[4])] {
        d.set_item(k, v)?;
    }
    d.set_item("stale_dropped", stale)?;
    d.set_item("resident", lab.resident.len())?;
    d.set_item("reading", reading)?;
    d.set_item("queued", queued)?;
    d.set_item("resident_bytes", lab.resident.iter().map(|r| crate::label_plan::tile_bytes(&core.pyr, &r.0)).sum::<usize>())?;
    d.set_item("pending_bytes", pending)?;
    d.set_item("budget_bytes", crate::label_store::LABEL_BUDGET)?;
    d.set_item("target_level", lab.target)?;
    d.set_item("generation", lab.generation)?;
    d.set_item("thread_alive", lab.pool.as_ref().is_some_and(|p| p.alive()))?;
    d.set_item("note", lab.note.clone())?;
    d.set_item("last_error", lab.last_error.clone())?;
    d.set_item("wanted", lab.wanted.len())?;
    d.set_item("split_ms", lab.split_ms.to_vec())?;
    d.set_item("radius", lab.styles.iter().map(|st| st.radius).collect::<Vec<_>>())?;
    d.set_item("held_keys", lab.resident.iter().map(|r| (r.0 .0, r.0 .1, r.0 .2, r.0 .3, r.0 .4)).collect::<Vec<_>>())?;
    d.set_item("wanted_keys", lab.wanted.iter().map(|k| (k.0, k.1, k.2, k.3, k.4)).collect::<Vec<_>>())?;
    d.set_item("uploads", core.gpu.as_ref().and_then(|g| g.render.label_gpu()).map_or(0, |l| l.uploads))?;
    Ok(d)
}

/// The planner alone (tests: parity with `Step3LabelBinding._plan`).
/// `resident`: [((gen, kind, level, tx, ty), (x0, x1, y0, y1))] in arrival
/// order; `reading`: keys being read. Returns (wanted, keep, released,
/// jobs, note).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn plan_labels_for_test(shapes: Vec<(usize, usize)>, level: u8, visible: Vec<(u32, u32)>, generation: u64,
                            shown: [bool; 2], pyramid: [bool; 2],
                            resident: Vec<((u64, usize, u8, u32, u32), [f64; 4])>,
                            reading: Vec<(u64, usize, u8, u32, u32)>, budget: usize)
                            -> Option<(Vec<(u64, usize, u8, u32, u32)>, Vec<(u64, usize, u8, u32, u32)>,
                                       Vec<(u64, usize, u8, u32, u32)>, Vec<(u64, usize, u8, u32, u32)>,
                                       Option<String>)> {
    let pyr = crate::geom::Pyramid { shapes };
    let out = crate::label_plan::plan(&crate::label_plan::PlanIn {
        pyr: &pyr, level, visible: &visible, generation, shown, pyramid, resident: &resident, budget })?;
    let jobs = crate::label_plan::jobs(&out, &reading.into_iter().collect());
    Some((out.wanted, out.keep, out.released, jobs, out.note))
}

/// `read_label_tile` alone (tests: parity with Python's): (rows, cols,
/// ids as native u32 bytes, rect) or the reason it is unavailable.
#[pyfunction]
pub fn read_label_tile_for_test(py: Python<'_>, source: &Bound<'_, PyDict>, shapes: Vec<(usize, usize)>,
                                level: u8, tx: u32, ty: u32) -> PyResult<Py<PyAny>> {
    let src = kind(source)?;
    let pyr = crate::geom::Pyramid { shapes };
    match crate::label_read::read_label_tile(&src, &pyr, level, tx, ty).map_err(super::to_py_err)? {
        crate::label_read::Read::Tile(t) => {
            let bytes: Vec<u8> = t.ids.iter().flat_map(|v| v.to_ne_bytes()).collect();
            Ok((t.rows, t.cols, pyo3::types::PyBytes::new(py, &bytes), t.rect).into_pyobject(py)?.into_any().unbind())
        }
        crate::label_read::Read::Unavailable(why) => Ok(why.into_pyobject(py)?.into_any().unbind()),
    }
}
