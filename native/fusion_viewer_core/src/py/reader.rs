//! `RawReader`: a raw pyramid opened from a Python-built manifest.

use super::to_py_err;
use crate::decode::{BlockFormat, Compression};
use crate::fileid::FileId;
use crate::manifest::{LevelTable, RawManifest};
use crate::reader::{RawSource, Region};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::path::PathBuf;

#[pyclass(frozen)]
pub struct RawReader {
    source: RawSource,
}

type Job = (usize, usize, usize, usize, usize, usize);

fn region(j: &Job) -> Region {
    Region { channel: j.0, level: j.1, y0: j.2, y1: j.3, x0: j.4, x1: j.5 }
}

fn u64s(bytes: &[u8]) -> PyResult<Vec<u64>> {
    if bytes.len() % 8 != 0 {
        return Err(PyValueError::new_err("offset table is not whole u64 values"));
    }
    Ok(bytes.chunks_exact(8).map(|b| u64::from_le_bytes(b.try_into().unwrap())).collect())
}

fn level(d: &Bound<'_, PyDict>) -> PyResult<LevelTable> {
    let get = |k: &str| -> PyResult<Bound<'_, PyAny>> {
        d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("level needs '{k}'")))
    };
    let code: u32 = get("compression")?.extract()?;
    let compression = Compression::from_tiff(code)
        .ok_or_else(|| PyValueError::new_err(format!("compression {code} not supported")))?;
    let predictor: u32 = get("predictor")?.extract()?;
    let kind: String = get("kind")?.extract()?;
    if predictor == 2 && kind == "f" {
        return Err(PyValueError::new_err("predictor 2 on float samples not supported"));
    }
    if predictor != 1 && predictor != 2 {
        return Err(PyValueError::new_err(format!("predictor {predictor} not supported")));
    }
    Ok(LevelTable {
        height: get("height")?.extract()?,
        width: get("width")?.extract()?,
        block_rows: get("block_rows")?.extract()?,
        block_cols: get("block_cols")?.extract()?,
        stripped: get("stripped")?.extract()?,
        format: BlockFormat {
            compression,
            horizontal_predictor: predictor == 2,
            sample_bytes: get("sample_bytes")?.extract()?,
            big_endian: get("big_endian")?.extract()?,
        },
        channels: get("channels")?.extract()?,
        offsets: u64s(get("offsets")?.extract::<&[u8]>()?)?,
        counts: u64s(get("counts")?.extract::<&[u8]>()?)?,
    })
}

/// Open the raw source a Python manifest describes (shared with `ViewerCore`).
pub fn open_source(manifest: &Bound<'_, PyDict>) -> PyResult<RawSource> {
    let item = |k: &str| -> PyResult<Bound<'_, PyAny>> {
        manifest.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("manifest needs '{k}'")))
    };
    let levels = item("levels")?.cast_into::<PyList>()?
        .iter().map(|l| level(l.cast::<PyDict>()?)).collect::<PyResult<Vec<_>>>()?;
    let m = RawManifest {
        path: PathBuf::from(item("path")?.extract::<String>()?),
        expected: FileId {
            size: item("size")?.extract()?,
            mtime_ns: item("mtime_ns")?.extract()?,
            file_number: item("file_number")?.extract()?,
            alt_number: None,
        },
        levels,
    };
    RawSource::open(m).map_err(to_py_err)
}

#[pymethods]
impl RawReader {
    /// `manifest`: {"path", "size", "mtime_ns", "file_number", "levels": [..]};
    /// see docs/v16_5_r0_interfaces.md. Refuses a file that changed.
    #[new]
    fn new(manifest: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(RawReader { source: open_source(manifest)? })
    }

    /// One region's bytes (row-major, native-endian); the GIL is released.
    fn read_region<'py>(&self, py: Python<'py>, channel: usize, level: usize,
                        y0: usize, y1: usize, x0: usize, x1: usize) -> PyResult<Bound<'py, PyBytes>> {
        let r = Region { channel, level, y0, y1, x0, x1 };
        let out = py.detach(|| self.source.read_region(&r)).map_err(to_py_err)?;
        Ok(PyBytes::new(py, &out))
    }

    /// Many regions on `threads` workers; a list of bytes in job order.
    fn read_many<'py>(&self, py: Python<'py>, jobs: Vec<Job>, threads: usize)
                      -> PyResult<Vec<Bound<'py, PyBytes>>> {
        let regions: Vec<Region> = jobs.iter().map(region).collect();
        let done = py.detach(|| crate::batch::run(&self.source, &regions, threads,
                                                   |s, r| s.read_region(r)));
        done.into_iter().map(|t| Ok(PyBytes::new(py, &t.value.map_err(to_py_err)?))).collect()
    }

    /// Throughput run: reads every job, keeps no pixels. Returns
    /// (wall_s, [(start, end)], total_bytes, checksum) with the GIL released.
    fn bench(&self, py: Python<'_>, jobs: Vec<Job>, threads: usize)
             -> PyResult<(f64, Vec<(f64, f64)>, u64, u64)> {
        let regions: Vec<Region> = jobs.iter().map(region).collect();
        let t0 = crate::clock::monotonic();
        let done = py.detach(|| crate::batch::run(&self.source, &regions, threads, |s, r| {
            let px = s.read_region(r)?;
            Ok((px.len() as u64, px.iter().map(|&b| b as u64).sum::<u64>()))
        }));
        let wall = crate::clock::monotonic() - t0;
        let (mut total, mut sum, mut spans) = (0u64, 0u64, Vec::with_capacity(done.len()));
        for t in done {
            let (n, s) = t.value.map_err(to_py_err)?;
            total += n;
            sum = sum.wrapping_add(s);
            spans.push((t.start, t.end));
        }
        Ok((wall, spans, total, sum))
    }
}
