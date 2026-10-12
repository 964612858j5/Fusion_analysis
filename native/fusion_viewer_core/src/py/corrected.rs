//! `Step1Reader`, `ZarrArray`, `CancelToken`: Step1's per-channel tile
//! rule (core `step1_tile`) for parity tests and probes. No logic here:
//! dicts in, core types out. Manifest shape: `step1_core_adapter
//! .build_corrected_manifest`.

use crate::cancel::Cancel;
use crate::corrected::{ArraySpec, Source, Step1Sources};
use crate::fileid::FileId;
use crate::py::to_py_err;
use crate::reader::RawSource;
use crate::step1_tile::{RawKind, Valid, Values};
use crate::zarr_v2::ArrayMeta;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::HashMap;
use std::path::PathBuf;

fn item<'py>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("manifest needs '{k}'")))
}

fn file_id(v: &Bound<'_, PyAny>) -> PyResult<FileId> {
    let (size, mtime_ns, file_number): (u64, i128, u128) = v.extract()?;
    Ok(FileId { size, mtime_ns, file_number, alt_number: None })
}

fn meta(d: &Bound<'_, PyDict>) -> PyResult<ArrayMeta> {
    let sep: String = item(d, "separator")?.extract()?;
    ArrayMeta::new(PathBuf::from(item(d, "dir")?.extract::<String>()?), item(d, "shape")?.extract()?,
                   item(d, "chunks")?.extract()?, &item(d, "dtype")?.extract::<String>()?,
                   &item(d, "order")?.extract::<String>()?, &item(d, "compressor")?.extract::<String>()?,
                   &item(d, "cname")?.extract::<String>()?, item(d, "filters_none")?.extract()?,
                   item(d, "fill")?.extract()?, sep.chars().next().unwrap_or('.'))
        .map_err(to_py_err)
}

fn spec(d: &Bound<'_, PyDict>) -> PyResult<ArraySpec> {
    let zattrs = item(d, "zattrs")?;
    Ok(ArraySpec { meta: meta(d)?, zarray: file_id(&item(d, "zarray")?)?,
                   zattrs: if zattrs.is_none() { None } else { Some(file_id(&zattrs)?) } })
}

fn source(d: &Bound<'_, PyDict>) -> PyResult<Source> {
    let kind: String = item(d, "source")?.extract()?;
    match kind.as_str() {
        "raw" => Ok(Source::Raw { index: item(d, "index")?.extract()? }),
        "missing" => Ok(Source::Missing { reason: item(d, "reason")?.extract()? }),
        "corrected" => {
            let product = spec(item(d, "product")?.cast::<PyDict>()?)?;
            let mut planes = Vec::new();
            for p in item(d, "planes")?.cast_into::<PyList>()?.iter() {
                let p = p.cast::<PyDict>()?;
                planes.push((item(p, "stride")?.extract()?, spec(item(p, "array")?.cast::<PyDict>()?)?,
                             item(p, "origin")?.extract()?));
            }
            crate::corrected::corrected(product, item(d, "bbox")?.extract()?, planes).map_err(to_py_err)
        }
        other => Err(PyValueError::new_err(format!("source {other:?}"))),
    }
}

/// `build_raw_manifest`'s dtype and `build_corrected_manifest` -> the
/// raw sample kind, the verified per-channel sources and the slab budget
/// (shared with `ViewerCore`).
pub(crate) fn parse(raw: &Bound<'_, PyDict>, sources: &Bound<'_, PyDict>)
                    -> PyResult<(RawKind, Step1Sources, usize)> {
    let dtype: String = item(raw, "dtype")?.extract()?;
    let raw_kind = RawKind::parse(&dtype)
        .ok_or_else(|| PyValueError::new_err(format!("raw dtype {dtype} not taken")))?;
    let (mut channels, mut names) = (HashMap::new(), HashMap::new());
    for (name, d) in item(sources, "channels")?.cast_into::<PyDict>()?.iter() {
        let (name, d) = (name.extract::<String>()?, d.cast::<PyDict>()?.clone());
        names.insert(item(&d, "index")?.extract::<u16>()?, name.clone());
        channels.insert(name, source(&d)?);
    }
    let roi = item(sources, "roi")?;
    Ok((raw_kind,
        Step1Sources { channels, names, roi: if roi.is_none() { None } else { Some(roi.extract()?) },
                       revision: item(sources, "revision")?.extract()? },
        item(sources, "budget")?.extract()?))
}

#[pyclass]
pub struct CancelToken {
    inner: Cancel,
}

#[pymethods]
impl CancelToken {
    #[new]
    fn new() -> Self {
        CancelToken { inner: Cancel::new() }
    }

    fn cancel(&self) {
        self.inner.cancel();
    }
}

#[pyclass]
pub struct ZarrArray {
    meta: ArrayMeta,
}

#[pymethods]
impl ZarrArray {
    #[new]
    fn new(array: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(ZarrArray { meta: meta(array)? })
    }

    /// Rows y0..y1, columns x0..x1 as little-endian f32 bytes; GIL released.
    fn read_rect<'py>(&self, py: Python<'py>, y0: usize, y1: usize, x0: usize, x1: usize)
                      -> PyResult<Bound<'py, PyBytes>> {
        let v = py.detach(|| self.meta.read_rect(y0, y1, x0, x1, &|| true)).map_err(to_py_err)?
            .unwrap_or_default();
        let bytes: Vec<u8> = v.iter().flat_map(|f| f.to_le_bytes()).collect();
        Ok(PyBytes::new(py, &bytes))
    }
}

#[pyclass]
pub struct Step1Reader {
    raw: RawSource,
    raw_kind: RawKind,
    sources: Step1Sources,
    budget: usize,
}

#[pymethods]
impl Step1Reader {
    /// `raw`: `build_raw_manifest`; `sources`: `build_corrected_manifest`.
    #[new]
    fn new(raw: &Bound<'_, PyDict>, sources: &Bound<'_, PyDict>) -> PyResult<Self> {
        let (raw_kind, sources, budget) = parse(raw, sources)?;
        Ok(Step1Reader { raw: crate::py::reader::open_source(raw)?, raw_kind, sources, budget })
    }

    /// Throughput run on `threads` Rust threads (no Python between tiles,
    /// as the reader pool): `jobs` = [(channel, level, y0, y1, x0, x1,
    /// stride, tile_size)]. Returns (wall_s, tiles read). GIL released.
    fn read_many(&self, py: Python<'_>, jobs: Vec<(String, usize, i64, i64, i64, i64, usize, usize)>,
                 threads: usize) -> PyResult<(f64, usize)> {
        for j in &jobs {
            if !self.sources.channels.contains_key(&j.0) {
                return Err(PyValueError::new_err(format!("unknown channel {:?}", j.0)));
            }
        }
        let next = std::sync::atomic::AtomicUsize::new(0);
        let done = std::sync::atomic::AtomicUsize::new(0);
        let failure = std::sync::Mutex::new(None);
        let t0 = crate::clock::monotonic();
        py.detach(|| std::thread::scope(|scope| {
            for _ in 0..threads.max(1) {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    let Some(j) = jobs.get(i) else { return };
                    let src = &self.sources.channels[&j.0];
                    match crate::step1_tile::read(src, &self.raw, self.raw_kind, self.sources.roi, j.1,
                                                  [j.2, j.3, j.4, j.5], j.6, j.7, self.budget, &|| true) {
                        Ok(_) => { done.fetch_add(1, std::sync::atomic::Ordering::Relaxed); }
                        Err(e) => { failure.lock().unwrap().get_or_insert(e); }
                    }
                });
            }
        }));
        if let Some(e) = failure.into_inner().unwrap() {
            return Err(to_py_err(e));
        }
        Ok((crate::clock::monotonic() - t0, done.into_inner()))
    }

    #[getter]
    fn revision(&self) -> String {
        self.sources.revision.clone()
    }

    /// One tile: {"rows", "cols", "dtype", "data", "valid": "nothing" |
    /// "rect" | "finite", "rect"} or None when `cancel` fired. GIL released.
    #[pyo3(signature = (channel, level, y0, y1, x0, x1, stride, tile_size, cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn read_tile<'py>(&self, py: Python<'py>, channel: &str, level: usize, y0: i64, y1: i64,
                      x0: i64, x1: i64, stride: usize, tile_size: usize,
                      cancel: Option<PyRef<'_, CancelToken>>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let src = self.sources.channels.get(channel)
            .ok_or_else(|| PyValueError::new_err(format!("unknown channel {channel:?}")))?;
        let token = cancel.map(|c| c.inner.clone()).unwrap_or_default();
        let tile = py.detach(|| crate::step1_tile::read(src, &self.raw, self.raw_kind, self.sources.roi, level,
                                                        [y0, y1, x0, x1], stride, tile_size, self.budget,
                                                        &|| token.wanted()))
            .map_err(to_py_err)?;
        let Some(t) = tile else { return Ok(None) };
        let d = PyDict::new(py);
        d.set_item("rows", t.rows)?;
        d.set_item("cols", t.cols)?;
        let (dtype, data): (&str, Vec<u8>) = match &t.values {
            Values::U8(v) => ("uint8", v.clone()),
            Values::U16(v) => ("uint16", v.iter().flat_map(|x| x.to_le_bytes()).collect()),
            Values::F32(v) => ("float32", v.iter().flat_map(|x| x.to_le_bytes()).collect()),
        };
        d.set_item("dtype", dtype)?;
        d.set_item("data", PyBytes::new(py, &data))?;
        match t.valid {
            Valid::Nothing => d.set_item("valid", "nothing")?,
            Valid::Finite => d.set_item("valid", "finite")?,
            Valid::Rect(r) => {
                d.set_item("valid", "rect")?;
                d.set_item("rect", (r[0], r[1], r[2], r[3]))?;
            }
        }
        Ok(Some(d))
    }
}
