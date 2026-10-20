//! The fusion operators for parity tests (plan F5-1): dicts and bytes in,
//! bytes out, no logic here. `FuseJob` (F5-2) joins this file later.
//!
//! * `fuse_remap_values(window, f32 bytes)` -> f32 bytes: `apply_channel_remap`
//! * `fuse_power_values(gamma, f32 bytes)` -> f32 bytes: its `x ** f32(gamma)` step
//! * `fuse_values(plan, planes, kinds=None)` -> u16 bytes: `fuse_channels` +
//!   the x65535 cast over whole planes (an open window uses the whole plane)
//!
//! Numbers are converted with Python's own `float()`, as Python does.

use crate::fuse_plan::{Channel, Plan};
use crate::fuse_planes::{self, Plane};
use crate::fuse_window::{Params, Power, remap_plane};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

fn float(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    v.py().import("builtins")?.getattr("float")?.call1((v,))?.extract()
}

/// `channel_remap._merge_params` then `float()` of each: a missing key is
/// its default, a present None min/max is "derive", any other None raises.
fn params(d: &Bound<'_, PyDict>) -> PyResult<Params> {
    let base = Params::default();
    let num = |k: &str, dflt: f64| -> PyResult<f64> {
        match d.get_item(k)? {
            Some(v) => float(&v),
            None => Ok(dflt),
        }
    };
    let opt = |k: &str| -> PyResult<Option<f64>> {
        match d.get_item(k)? {
            Some(v) if !v.is_none() => Ok(Some(float(&v)?)),
            _ => Ok(None),
        }
    };
    Ok(Params { min: opt("min")?, max: opt("max")?, brightness: num("brightness", base.brightness)?,
                contrast: num("contrast", base.contrast)?, gamma: num("gamma", base.gamma)? })
}

fn f32s(b: &[u8]) -> PyResult<Vec<f32>> {
    if b.len() % 4 != 0 {
        return Err(PyValueError::new_err("float32 bytes expected"));
    }
    Ok(b.chunks_exact(4).map(|c| f32::from_ne_bytes([c[0], c[1], c[2], c[3]])).collect())
}

fn f32_bytes<'py>(py: Python<'py>, v: &[f32]) -> Bound<'py, PyBytes> {
    PyBytes::new_with(py, v.len() * 4, |out| {
        for (o, x) in out.chunks_exact_mut(4).zip(v) {
            o.copy_from_slice(&x.to_ne_bytes());
        }
        Ok(())
    }).expect("bytes")
}

#[pyfunction]
fn fuse_remap_values<'py>(py: Python<'py>, window: &Bound<'py, PyDict>, values: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let p = params(window)?;
    let v = f32s(values)?;
    let out = py.detach(|| remap_plane(&p, &v));
    Ok(f32_bytes(py, &out))
}

#[pyfunction]
fn fuse_power_values<'py>(py: Python<'py>, gamma: &Bound<'py, PyAny>, values: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let power = Power::of(float(gamma)?);
    let v = f32s(values)?;
    let out: Vec<f32> = py.detach(|| v.iter().map(|&x| power.apply(x)).collect());
    Ok(f32_bytes(py, &out))
}

pub(super) fn plan(d: &Bound<'_, PyDict>) -> PyResult<Plan> {
    let get = |k: &str| d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("plan needs '{k}'")));
    let mut channels = Vec::new();
    for c in get("channels")?.try_iter()? {
        let c = c?;
        let w = c.get_item("window")?;
        // `if not params`: no committed window, the channel takes no part
        let window = if w.is_truthy()? { Some(params(w.cast::<PyDict>()?)?) } else { None };
        channels.push(Channel { window });
    }
    let mut groups = Vec::new();
    for g in get("groups")?.try_iter()? {
        let g = g?;
        let mut members = Vec::new();
        for m in g.get_item(1)?.try_iter()? {
            let m = m?;
            members.push((m.get_item(0)?.extract::<usize>()?, float(&m.get_item(1)?)?));
        }
        groups.push((float(&g.get_item(0)?)?, members));
    }
    let n = get("nucleus")?;
    let nucleus = if n.is_none() { None } else { Some((n.get_item(0)?.extract::<usize>()?, float(&n.get_item(1)?)?)) };
    Plan::new(channels, groups, nucleus).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (plan_d, planes, kinds=None))]
fn fuse_values<'py>(py: Python<'py>, plan_d: &Bound<'py, PyDict>, planes: Vec<Vec<u8>>,
                    kinds: Option<Vec<String>>) -> PyResult<Bound<'py, PyBytes>> {
    let p = plan(plan_d)?;
    let kinds = kinds.unwrap_or_else(|| vec!["f32".into(); planes.len()]);
    if kinds.len() != planes.len() {
        return Err(PyValueError::new_err("one kind per plane"));
    }
    let planes: Vec<Plane> = planes.into_iter().zip(&kinds).map(|(b, k)| Ok(match k.as_str() {
        "u8" => Plane::U8(b),
        "u16" => Plane::U16(b.chunks_exact(2).map(|c| u16::from_ne_bytes([c[0], c[1]])).collect()),
        "f32" => Plane::F32(f32s(&b)?),
        _ => return Err(PyValueError::new_err(format!("kind {k}"))),
    })).collect::<PyResult<_>>()?;
    let out = py.detach(|| fuse_planes::fuse(&p, &planes)).map_err(PyValueError::new_err)?;
    PyBytes::new_with(py, out.len() * 2, |b| {
        for (o, x) in b.chunks_exact_mut(2).zip(&out) {
            o.copy_from_slice(&x.to_ne_bytes());
        }
        Ok(())
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(fuse_remap_values, m)?)?;
    m.add_function(wrap_pyfunction!(fuse_power_values, m)?)?;
    m.add_function(wrap_pyfunction!(fuse_values, m)?)?;
    Ok(())
}
