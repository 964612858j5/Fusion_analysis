//! `FuseJob`: one region's fusion, unit by unit (plan F5-2, §5.1). Dicts in,
//! core types out; no logic here.
//!
//!   FuseJob(raw_manifest, plan)
//!     raw_manifest: `step1_core_adapter.build_raw_manifest` (file identity
//!                   checked on open), or None when no channel is raw
//!     plan = {"channels": [{"name": str,
//!                           "source": {"raw": index}
//!                                   | {"corrected": <_array_spec dict>, "bbox": [y0, y1, x0, x1]},
//!                           "window": {min, max, brightness, contrast, gamma}}],
//!             "groups":  [(group_weight, [(slot, weight), ...]), ...],
//!             "nucleus": (slot, weight) | None}
//!   .fuse_unit(y0, y1, x0, x1, mask=None) -> bytes | None   (GIL released)
//!   .cancel()

use super::fuse::plan;
use super::to_py_err;
use crate::fuse_source::Source;
use crate::fuse_unit::UnitFuser;
use crate::reduce::CorrectedRegion;
use crate::step1_tile::RawKind;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::sync::Arc;

fn source(d: &Bound<'_, PyAny>, raw: &Option<(Arc<crate::reader::RawSource>, RawKind)>) -> PyResult<Source> {
    let d = d.cast::<PyDict>()?;
    if let Some(index) = d.get_item("raw")? {
        let (src, kind) = raw.as_ref().ok_or_else(|| PyValueError::new_err("a raw channel without a raw manifest"))?;
        return Ok(Source::Raw { src: Arc::clone(src), kind: *kind, index: index.extract()? });
    }
    if let Some(spec) = d.get_item("corrected")? {
        let spec = super::corrected::spec(spec.cast::<PyDict>()?)?;
        crate::corrected::verify(&spec).map_err(to_py_err)?;
        let bbox: [i64; 4] = d.get_item("bbox")?.ok_or_else(|| PyValueError::new_err("corrected needs 'bbox'"))?.extract()?;
        return Ok(Source::Corrected { region: CorrectedRegion { array: spec.meta, bbox } });
    }
    Err(PyValueError::new_err("source needs 'raw' or 'corrected'"))
}

#[pyclass(frozen)]
pub struct FuseJob {
    core: UnitFuser,
}

#[pymethods]
impl FuseJob {
    #[new]
    fn new(raw_manifest: &Bound<'_, PyAny>, plan_d: &Bound<'_, PyDict>) -> PyResult<Self> {
        let raw = if raw_manifest.is_none() {
            None
        } else {
            let m = raw_manifest.cast::<PyDict>()?;
            let dtype: String = m.get_item("dtype")?.ok_or_else(|| PyValueError::new_err("manifest needs 'dtype'"))?.extract()?;
            let kind = RawKind::parse(&dtype).ok_or_else(|| PyValueError::new_err(format!("raw dtype {dtype} not taken")))?;
            Some((Arc::new(super::reader::open_source(m)?), kind))
        };
        let p = plan(plan_d)?;
        let (mut sources, mut names) = (Vec::new(), Vec::new());
        for c in plan_d.get_item("channels")?.ok_or_else(|| PyValueError::new_err("plan needs 'channels'"))?.try_iter()? {
            let c = c?;
            sources.push(Some(source(&c.get_item("source")?, &raw)?));
            names.push(match c.get_item("name") { Ok(n) => n.extract::<String>()?, Err(_) => String::new() });
        }
        Ok(FuseJob { core: UnitFuser::new(p, sources, names).map_err(to_py_err)? })
    }

    fn cancel(&self) {
        self.core.cancel();
    }

    #[pyo3(signature = (y0, y1, x0, x1, mask=None))]
    fn fuse_unit<'py>(&self, py: Python<'py>, y0: usize, y1: usize, x0: usize, x1: usize,
                      mask: Option<&[u8]>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let out = py.detach(|| self.core.fuse_unit(y0, y1, x0, x1, mask)).map_err(to_py_err)?;
        Ok(match out {
            None => None,
            Some(v) => Some(PyBytes::new_with(py, v.len() * 2, |b| {
                for (o, x) in b.chunks_exact_mut(2).zip(&v) {
                    o.copy_from_slice(&x.to_le_bytes());
                }
                Ok(())
            })?),
        })
    }
}
