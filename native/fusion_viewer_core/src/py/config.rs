//! `ViewerCore`'s config dict (`step1_core_binding.core_config`) parsed
//! into `viewer::Config`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn cfg_item<'py>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("config needs '{k}'")))
}

fn cfg_u(d: &Bound<'_, PyDict>, k: &str) -> PyResult<usize> {
    cfg_item(d, k)?.extract::<usize>()
}

fn cfg_f(d: &Bound<'_, PyDict>, k: &str) -> PyResult<f64> {
    cfg_item(d, k)?.extract::<f64>()
}

fn cfg_b(d: &Bound<'_, PyDict>, k: &str) -> PyResult<bool> {
    cfg_item(d, k)?.extract::<bool>()
}


pub(super) fn parse(config: &Bound<'_, PyDict>) -> PyResult<crate::viewer::Config> {
    Ok(crate::viewer::Config {
        max_raw_texture_bytes: cfg_u(config, "max_raw_texture_bytes")?,
        max_fine_tiles: cfg_u(config, "max_fine_tiles")?,
        max_fine_bytes_per_channel: cfg_u(config, "max_fine_bytes_per_channel")? as u64,
        max_coarse_bytes_per_channel: match config.get_item("max_coarse_bytes_per_channel")? {
            Some(v) if !v.is_none() => Some(v.extract()?),
            _ => None,
        },
        max_coarse_tiles: cfg_u(config, "max_coarse_tiles")?,
        cpu_cache_bytes: cfg_u(config, "cpu_cache_bytes")?,
        corrected_cache_bytes: match config.get_item("corrected_cache_bytes")? {
            Some(v) => v.extract()?,
            None => 2048 * 1024 * 1024,      // STEP1_VIEWER_CORRECTED_CACHE_BYTES
        },
        threads: cfg_u(config, "threads")?,
        input_hot_s: cfg_f(config, "input_hot_s")?,
        hot_upload_ms: cfg_f(config, "hot_upload_ms")?,
        frame_budget_ms: cfg_f(config, "frame_budget_ms")?,
        growth_quiet_s: cfg_f(config, "growth_quiet_s")?,
        legacy_order: cfg_b(config, "legacy_order")?,
        result_bytes: match config.get_item("result_bytes")? {
            Some(v) => v.extract()?,
            None => crate::pool::RESULT_BYTES,
        },
        test_read_delay_ms: match config.get_item("test_read_delay_ms")? {
            Some(v) => v.extract()?,
            None => 0.0,
        },
        test_block_units: match config.get_item("test_block_units")? {
            Some(v) if !v.is_none() => Some(v.extract()?),
            _ => None,
        },
    })
}
