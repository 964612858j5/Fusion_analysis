//! `ViewerCore`: the Step1 viewer core for Python (rules: core viewer.rs).

use crate::plan::View;
use crate::vt::RowParams;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::cell::RefCell;
use std::sync::Arc;

/// unsendable: lives and is called on the GUI thread only.
#[pyclass(unsendable)]
pub struct ViewerCore {
    inner: RefCell<crate::viewer::ViewerCore>,
}

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

#[pymethods]
impl ViewerCore {
    /// `manifest`: as `RawReader`; `config`: see `viewer::Config`; `roi`:
    /// `(y0, y1, x0, x1)` level 0 or None; `notify_handle`: the notice
    /// socket's write end (`fileno()`) or None; `trace`: record stages.
    #[new]
    /// `sources`: `build_corrected_manifest` (None: every channel raw,
    /// region `roi`, as the thin slice).
    #[pyo3(signature = (manifest, config, roi=None, notify_handle=None, trace=false, sources=None))]
    fn new(manifest: &Bound<'_, PyDict>, config: &Bound<'_, PyDict>, roi: Option<[i64; 4]>,
           notify_handle: Option<u64>, trace: bool, sources: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let source = super::reader::open_source(manifest)?;
        let (raw_kind, sources, budget) = match sources {
            Some(d) => super::corrected::parse(manifest, d)?,
            None => {
                let dtype: String = manifest.get_item("dtype")?.map_or(Ok("uint8".into()), |v| v.extract())?;
                let kind = crate::step1_tile::RawKind::parse(&dtype)
                    .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(format!("raw dtype {dtype} not taken")))?;
                let channels = source.manifest().levels.first().map_or(0, |l| l.channels);
                (kind, crate::corrected::Step1Sources::all_raw(channels, roi),
                 crate::reduce::MAX_REDUCTION_ELEMENTS)
            }
        };
        let cfg = crate::viewer::Config {
            max_raw_texture_bytes: cfg_u(config, "max_raw_texture_bytes")?,
            max_fine_tiles: cfg_u(config, "max_fine_tiles")?,
            max_fine_bytes_per_channel: cfg_u(config, "max_fine_bytes_per_channel")? as u64,
            max_coarse_bytes_per_channel: match config.get_item("max_coarse_bytes_per_channel")? {
                Some(v) if !v.is_none() => Some(v.extract()?),
                _ => None,
            },
            max_coarse_tiles: cfg_u(config, "max_coarse_tiles")?,
            cpu_cache_bytes: cfg_u(config, "cpu_cache_bytes")?,
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
        };
        let notifier = match notify_handle {
            Some(h) => Some(Arc::new(crate::notify::Notifier::borrow(h)?)),
            None => None,
        };
        let tr = Arc::new(crate::trace::Trace::default());
        tr.set_on(trace);
        Ok(ViewerCore { inner: RefCell::new(crate::viewer::ViewerCore::new(source, cfg, sources, raw_kind, budget, notifier, tr)) })
    }

    /// `rows`: the rows drawn, in draw order: `(channel index, lo, hi,
    /// gamma, r, g, b, weight, group, group weight)` (Overlay: group 0;
    /// Fusion: groups in order, the nucleus last with group -1); `fusion`:
    /// the mode.
    #[pyo3(signature = (rows, smooth, fusion=false))]
    /// (An 8-tuple row, the S-Raw form without the group fields, is
    /// group 0, weight 0.)
    fn set_display(&self, rows: Vec<Bound<'_, PyAny>>, smooth: bool, fusion: bool) -> PyResult<()> {
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let (c, lo, hi, g, r, gr, b, w, group, gw) = match row.len()? {
                8 => {
                    let (c, lo, hi, g, r, gr, b, w): (u16, f32, f32, f32, f32, f32, f32, f32) = row.extract()?;
                    (c, lo, hi, g, r, gr, b, w, 0.0, 0.0)
                }
                _ => row.extract::<(u16, f32, f32, f32, f32, f32, f32, f32, f32, f32)>()?,
            };
            out.push((c, RowParams { mapping: [lo, hi, g], color: [r, gr, b], weight: w, group, group_weight: gw }));
        }
        self.inner.borrow_mut().set_display(out, smooth, fusion);
        Ok(())
    }

    /// The region polygon (level-0 world (x, y) points, checked by
    /// `sanitize_roi_polygon`), or None for the rectangle alone.
    fn set_polygon(&self, points: Option<Vec<(f32, f32)>>) {
        self.inner.borrow_mut().polygon = points.map(|p| p.into_iter().map(|(x, y)| [x, y]).collect());
    }

    /// The controller's level, clamped `bbox_l0` `(y0, x0, y1, x1)` and epoch.
    fn set_view(&self, level: u8, bbox_l0: [i64; 4], epoch: u64, input: bool) {
        self.inner.borrow_mut().set_view(View { level, bbox_l0, epoch }, input);
    }

    /// `get_proc(name: bytes) -> int` (Qt's getProcAddress), context current.
    fn attach_gl(&self, get_proc: Bound<'_, PyAny>) -> PyResult<()> {
        let mut failure: Option<PyErr> = None;
        let gl = unsafe {
            glow::Context::from_loader_function(|name| {
                match get_proc.call1((PyBytes::new(get_proc.py(), name.as_bytes()),))
                    .and_then(|v| if v.is_none() { Ok(0usize) } else { v.extract::<usize>() }) {
                    Ok(a) => a as *const std::ffi::c_void,
                    Err(e) => { failure.get_or_insert(e); std::ptr::null() }
                }
            })
        };
        if let Some(e) = failure {
            return Err(e);
        }
        use glow::HasContext;
        let layers = unsafe { gl.get_parameter_i32(glow::MAX_ARRAY_TEXTURE_LAYERS) }.max(1) as usize;
        self.inner.borrow_mut().attach_gl(gl, layers).map_err(PyRuntimeError::new_err)
    }

    fn release_gl(&self) {
        self.inner.borrow_mut().release_gl();
    }

    fn forget_gl(&self) {
        self.inner.borrow_mut().forget_gl();
    }

    /// Draw into `fbo` at device size; `view` = world rect (x0, x1, y0, y1).
    fn paint<'py>(&self, py: Python<'py>, fbo: u32, w: i32, h: i32, view: [f64; 4]) -> PyResult<Bound<'py, PyDict>> {
        let r = crate::frame::paint(&mut self.inner.borrow_mut(), fbo, w, h, view);
        let d = PyDict::new(py);
        for (k, v) in [("frame", r.frame as f64), ("channels", r.channels as f64), ("planes", r.planes as f64),
                       ("received", r.received as f64), ("uploads", r.uploads as f64),
                       ("deferred", r.deferred as f64), ("deferred_growth", r.deferred_growth as f64),
                       ("level", r.level as f64), ("ideal", r.ideal as f64), ("wanted", r.wanted as f64),
                       ("shown", r.shown as f64), ("view_id", r.view_id as f64),
                       ("drain_ms", r.drain_ms), ("upload_ms", r.upload_ms), ("draw_ms", r.draw_ms),
                       ("total_ms", r.total_ms)] {
            d.set_item(k, v)?;
        }
        d.set_item("limited", r.limited)?;
        d.set_item("reserving", r.reserving)?;
        d.set_item("capacity_exceeded", r.capacity_exceeded)?;
        d.set_item("error", r.error)?;
        Ok(d)
    }

    /// The GUI side drained the notice socket (order: drain, ack, work).
    fn ack(&self) {
        if let Some(n) = &self.inner.borrow().notifier {
            n.ack();
        }
    }

    fn pause(&self) {
        self.inner.borrow_mut().pause();
    }

    fn resume(&self) {
        self.inner.borrow_mut().resume();
    }

    /// Stop notices and reads (GIL kept: it never waits for a read).
    fn close(&self) {
        self.inner.borrow_mut().close();
    }

    /// No read queued or under way, and nothing landed but not yet drawn.
    fn idle(&self) -> bool {
        let core = self.inner.borrow();
        core.pool.idle() && core.pool.pending_results() == 0
    }

    fn gpu_bytes(&self) -> usize {
        self.inner.borrow().gpu_bytes()
    }

    fn release_to_coarse(&self) -> usize {
        self.inner.borrow_mut().release_to_coarse()
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let mut core = self.inner.borrow_mut();
        let d = PyDict::new(py);
        let sh = core.pool.shared().clone();
        d.set_item("reads", sh.reads.load(std::sync::atomic::Ordering::Relaxed))?;
        d.set_item("stale", sh.stale.load(std::sync::atomic::Ordering::Relaxed))?;
        d.set_item("pending_results", core.pool.pending_results())?;
        d.set_item("read_errors", sh.errors.load(std::sync::atomic::Ordering::Relaxed))?;
        d.set_item("cache_bytes", sh.cache.lock().unwrap().bytes())?;
        d.set_item("cached_tiles", sh.cache.lock().unwrap().len())?;
        d.set_item("held_tiles", core.held.len())?;
        d.set_item("frames", core.frames)?;
        d.set_item("unavailable", core.planner.unavailable.iter().copied().collect::<Vec<_>>())?;
        // per active channel (`coarse_channels` / `coarse_pending` / `fine_channels`)
        let drawn: Vec<u16> = core.planner.active().iter().copied()
            .filter(|c| !core.planner.unavailable.contains(c)).collect();
        let (done, pending): (Vec<u16>, Vec<u16>) = drawn.iter().partition(|c| core.planner.coarse_complete(**c));
        d.set_item("coarse_complete", done)?;
        d.set_item("coarse_pending", pending)?;
        d.set_item("fine_held", drawn.iter().copied().filter(|c| core.planner.has_fine(*c)).collect::<Vec<_>>())?;
        let mut last = core.last_error.clone();
        let read_err = sh.last_error.lock().unwrap().clone();
        if last.is_empty() { last = read_err; }
        d.set_item("last_error", last)?;
        if let Some(a) = core.planner.admission().cloned() {
            d.set_item("ideal_level", a.ideal)?;
            d.set_item("admitted_level", a.level)?;
            d.set_item("resolution_limited", a.limited())?;
            d.set_item("base_overflow_channels",
                       if a.base_overflow { core.planner.active().len() } else { 0 })?;
        }
        if let Some(g) = core.gpu.as_ref() {
            d.set_item("gpu_tiles", g.store.slots.len())?;
            d.set_item("gpu_blocks", g.store.blocks.len())?;
            let fmt = |f: crate::family::Format| format!("{f:?}").to_lowercase();
            let live: std::collections::HashMap<String, usize> = crate::family::ALL.iter()
                .map(|f| (fmt(*f), g.store.live_blocks(*f))).collect();
            let targets: std::collections::HashMap<String, usize> = g.store.targets.iter()
                .map(|(f, n)| (fmt(*f), *n)).collect();
            d.set_item("gpu_live_blocks", live)?;
            d.set_item("gpu_target_blocks", targets)?;
            d.set_item("sampler_split", (g.render.u_blocks, g.render.f_blocks))?;
            d.set_item("gpu_uploads", g.store.uploads)?;
            d.set_item("gpu_evictions", g.store.evictions)?;
            d.set_item("u_blocks", g.render.u_blocks)?;
        }
        Ok(d)
    }

    /// The stage records so far, `(t, stage, (c, level, tx, ty) | None, {extra})`.
    /// GPU blocks created since the last call: (t, layers, alloc_ms,
    /// warm_ms, touch framebuffer status, first GL error).
    fn drain_growths(&self) -> Vec<(f64, u32, f64, f64, u32, u32)> {
        let mut core = self.inner.borrow_mut();
        match core.gpu.as_mut() {
            Some(g) => g.store.growths.drain(..)
                .map(|x| (x.at, x.layers, x.alloc_ms, x.warm_ms, x.touch_status, x.gl_error)).collect(),
            None => Vec::new(),
        }
    }

    fn drain_trace<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let recs = self.inner.borrow().trace.drain();
        let mut out = Vec::with_capacity(recs.len());
        for r in recs {
            let extra = PyDict::new(py);
            for (k, v) in r.extra {
                extra.set_item(k, v)?;
            }
            out.push((r.t, r.stage, r.key, extra).into_pyobject(py)?.into_any());
        }
        Ok(out)
    }

    /// `final` as top-down RGBA8 bytes (tests; context current).
    fn readback_final<'py>(&self, py: Python<'py>) -> Option<(Bound<'py, PyBytes>, i32, i32)> {
        let core = self.inner.borrow();
        let g = core.gpu.as_ref()?;
        let px = unsafe { g.render.readback(&g.gl) };
        Some((PyBytes::new(py, &px), g.render.size.0, g.render.size.1))
    }
}

