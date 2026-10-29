//! `ViewerCore`: the Step1 viewer core for Python (rules: core viewer.rs).

use crate::plan::View;
use crate::vt::RowParams;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::cell::RefCell;
use std::sync::Arc;

/// unsendable: lives and is called on the GUI thread only.
#[pyclass(unsendable)]
pub struct ViewerCore {
    inner: RefCell<crate::viewer::ViewerCore>,
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
        let cfg = super::config::parse(config)?;
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
    /// S0-B: `preparing` -- channels whose rows are fetched and uploaded
    /// but not composed (a source being prepared); `gains` -- per channel,
    /// the display gain of each pyramid level (missing or 0: 1).
    #[pyo3(signature = (rows, smooth, fusion=false, preparing=None, gains=None))]
    /// (An 8-tuple row, the S-Raw form without the group fields, is
    /// group 0, weight 0.)
    fn set_display(&self, rows: Vec<Bound<'_, PyAny>>, smooth: bool, fusion: bool,
                   preparing: Option<Vec<u16>>, gains: Option<std::collections::HashMap<u16, Vec<f32>>>)
                   -> PyResult<()> {
        let preparing = preparing.unwrap_or_default();
        let gains = gains.unwrap_or_default();
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let (c, lo, hi, g, r, gr, b, w, group, gw) = match row.len()? {
                8 => {
                    let (c, lo, hi, g, r, gr, b, w): (u16, f32, f32, f32, f32, f32, f32, f32) = row.extract()?;
                    (c, lo, hi, g, r, gr, b, w, 0.0, 0.0)
                }
                _ => row.extract::<(u16, f32, f32, f32, f32, f32, f32, f32, f32, f32)>()?,
            };
            let mut level_gains = [0.0f32; crate::vt::MAX_LEVELS];
            for (i, v) in gains.get(&c).map(|v| v.as_slice()).unwrap_or(&[]).iter()
                .take(crate::vt::MAX_LEVELS).enumerate() {
                level_gains[i] = *v;
            }
            out.push((c, RowParams { mapping: [lo, hi, g], color: [r, gr, b], weight: w, group, group_weight: gw,
                                     preparing: preparing.contains(&c), gains: level_gains }));
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
    /// S0-B `trial`: `(promote, yield)` channel lists -- this paint alone
    /// composes the rows being prepared of `promote` instead of `yield`'s;
    /// the report's `trial_covered` then says whether they cover the view.
    /// §14.6 `trial_level`: the report's `trial_exact` then counts only
    /// tiles of that level or of this frame's admission level, the finer
    /// (255: no picture on screen, the admission level alone), and
    /// `trial_admitted` only tiles of the admission level or finer.
    #[pyo3(signature = (fbo, w, h, view, trial=None, trial_level=None))]
    fn paint<'py>(&self, py: Python<'py>, fbo: u32, w: i32, h: i32, view: [f64; 4],
                  trial: Option<(Vec<u16>, Vec<u16>)>, trial_level: Option<u8>) -> PyResult<Bound<'py, PyDict>> {
        let trial = trial.map(|(promote, yield_)| crate::frame::Trial { promote, yield_, max_level: trial_level });
        let r = crate::frame::paint(&mut self.inner.borrow_mut(), fbo, w, h, view, trial);
        let d = PyDict::new(py);
        for (k, v) in [("frame", r.frame as f64), ("channels", r.channels as f64), ("planes", r.planes as f64),
                       ("received", r.received as f64), ("uploads", r.uploads as f64),
                       ("deferred", r.deferred as f64), ("deferred_growth", r.deferred_growth as f64),
                       ("label_deferred", r.label_deferred as f64),
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
        d.set_item("read_failure", r.read_failure)?;
        d.set_item("retry_ms", r.retry_ms)?;
        d.set_item("label_ms", r.label_ms)?;
        d.set_item("trial_covered", r.trial_covered)?;
        d.set_item("trial_exact", r.trial_exact)?;
        d.set_item("trial_admitted", r.trial_admitted)?;
        Ok(d)
    }

    /// S0-A (docs/v17_step0_engine_plan.md §10.5.2): is `view` (x0, x1,
    /// y0, y1) wholly covered, for every display channel, by the tiles the
    /// last composition would use? Read-only; asked right after a paint.
    fn view_covered(&self, view: [f64; 4]) -> bool {
        crate::frame::view_covered(&mut self.inner.borrow_mut(), view)
    }

    /// §14.6: `view_covered` counting only tiles of `max_level` or finer.
    fn view_covered_at(&self, view: [f64; 4], max_level: u8) -> bool {
        crate::frame::view_covered_at(&mut self.inner.borrow_mut(), view, max_level)
    }

    /// The slide's coarsest level (§14.7: a report's level -1 means this).
    fn coarsest_level(&self) -> u8 {
        self.inner.borrow().pyr.coarsest()
    }

    /// §15.6: the tiles `(level, tx, ty)` of `channel` a Step0 switch needs
    /// before its gate at `level` can open (see `viewer/gate_tiles.rs`).
    fn gate_tiles(&self, channel: u16, view: [f64; 4], level: u8) -> Vec<(u8, u32, u32)> {
        self.inner.borrow().gate_tiles(channel, view, level).into_iter().map(|t| (t.level, t.tx, t.ty)).collect()
    }

    // ── S0-B: Python sources (docs/v17_step0_engine_plan.md §11.3) ──
    /// Register a source whose tiles Python computes (0x8000 and above,
    /// rising, at most two at once). ValueError: refused (fall back).
    fn add_python_source(&self, channel: u16) -> PyResult<()> {
        self.inner.borrow_mut().add_python_source(channel).map_err(pyo3::exceptions::PyValueError::new_err)
    }

    /// Retire a source: everything of it goes (no GL call). False: unknown.
    fn drop_python_source(&self, channel: u16) -> bool {
        self.inner.borrow_mut().drop_python_source(channel)
    }

    /// `(new, withdrawn)` tiles `(channel, level, tx, ty)` since the last drain.
    #[allow(clippy::type_complexity)]
    fn drain_demands(&self) -> (Vec<(u16, u8, u32, u32)>, Vec<(u16, u8, u32, u32)>) {
        let (new, gone) = self.inner.borrow_mut().drain_demands();
        let f = |v: Vec<crate::cache::Key>| v.into_iter().map(|(c, t)| (c, t.level, t.tx, t.ty)).collect();
        (f(new), f(gone))
    }

    /// A computed tile: float32 native-endian bytes, the tile's real shape.
    /// True when the planner took it (paint again).
    fn complete_python_tile(&self, channel: u16, level: u8, tx: u32, ty: u32, data: Vec<u8>) -> bool {
        self.inner.borrow_mut().complete_python_tile(channel, crate::geom::TileAt { level, tx, ty }, data)
    }

    fn fail_python_tile(&self, channel: u16, level: u8, tx: u32, ty: u32) {
        self.inner.borrow_mut().fail_python_tile(channel, crate::geom::TileAt { level, tx, ty });
    }

    /// Tests (§4 item 3): the bytes the CPU cache holds for a tile, or None.
    fn cached_tile_for_test<'py>(&self, py: Python<'py>, channel: u16, level: u8, tx: u32, ty: u32)
                                 -> Option<(Bound<'py, PyBytes>, usize, usize)> {
        let core = self.inner.borrow();
        let mut cache = core.pool.shared().cache.lock().unwrap();
        let tile = cache.get(&(channel, crate::geom::TileAt { level, tx, ty }))?;
        Some((PyBytes::new(py, &tile.data), tile.rows, tile.cols))
    }

    /// Does the core hold anything of `channel` (tests)?
    fn holds_python_source(&self, channel: u16) -> bool {
        self.inner.borrow().holds_python_source(channel)
    }

    /// `{registered, retired, live, wants, handed, failures, completed,
    /// late_dropped, rejected, page_layers}` (tests, resource record).
    fn python_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let core = self.inner.borrow();
        let d = PyDict::new(py);
        let (wants, handed, failures) = core.demand.counts();
        d.set_item("registered", core.python.registered)?;
        d.set_item("retired", core.python.retired)?;
        d.set_item("live", core.python.live())?;
        d.set_item("wants", wants)?;
        d.set_item("handed", handed)?;
        d.set_item("failures", failures)?;
        d.set_item("completed", core.demand.completed)?;
        d.set_item("late_dropped", core.demand.late_dropped)?;
        d.set_item("rejected", core.demand.rejected)?;
        d.set_item("page_layers", core.gpu.as_ref().map_or(0, |g| g.vt.layers()))?;
        d.set_item("cache_tiles", core.pool.shared().cache.lock().unwrap().len())?;
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

    /// (channel, level) of every tile on the GPU (tests: what a release kept).
    fn gpu_slots(&self) -> Vec<(u16, u8)> {
        let core = self.inner.borrow();
        core.gpu.as_ref().map_or(Vec::new(), |g| g.store.slots.keys().map(|k| (k.0, k.1.level)).collect())
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        super::stats::stats(py, &mut self.inner.borrow_mut())
    }

    // ── Step3's masks (R3 plan §2.1; `super::labels`) ──
    /// The plane ids among `ids` the core holds no copy of.
    fn labels_missing(&self, ids: Vec<u64>) -> Vec<u64> {
        let core = self.inner.borrow();
        ids.into_iter().filter(|id| !core.labels.has(*id)).collect()
    }

    /// A new mask snapshot: layers of (planes, colour, alpha, radius, fill),
    /// a plane (id, world rect, ids bytes or None when already held, rows, cols).
    fn set_labels<'py>(&self, layers: Vec<super::labels::LayerIn<'py>>) -> PyResult<()> {
        super::labels::set(&mut self.inner.borrow_mut(), layers)
    }

    /// The snapshot drawn now over the picture as it stands (context
    /// current); False when no picture is composed yet.
    fn draw_labels(&self, fbo: u32, w: i32, h: i32) -> PyResult<bool> {
        crate::frame::draw_labels(&mut self.inner.borrow_mut(), fbo, w, h).map_err(PyRuntimeError::new_err)
    }

    /// R3b: the masks the core plans and reads -- `[cell, nucleus]`, each
    /// a source dict or None; None for no masks. ValueError: a layout the
    /// core does not read (the binding then keeps the masks with Python).
    #[pyo3(signature = (kinds=None))]
    fn set_label_sources(&self, kinds: Option<Vec<Option<Bound<'_, PyDict>>>>) -> PyResult<()> {
        let parsed = match kinds {
            None => None,
            Some(k) => {
                let mut out: [Option<crate::label_source::KindSource>; 2] = [None, None];
                for (i, d) in k.iter().enumerate().take(2) {
                    out[i] = d.as_ref().map(super::label_source::kind).transpose()?;
                }
                Some(out)
            }
        };
        self.inner.borrow_mut().set_label_sources(parsed);
        Ok(())
    }

    /// R3b: one mask's style (kind 0 cell, 1 nucleus).
    fn set_label_style(&self, kind: usize, visible: bool, color: [f32; 3], alpha: f32, radius: i32, fill: bool) {
        let style = crate::viewer::labels::Style { visible, color, alpha, radius: radius.clamp(0, 8), fill };
        self.inner.borrow_mut().set_label_style(kind.min(1), style);
    }

    fn core_label_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        super::label_source::stats(py, &self.inner.borrow())
    }

    fn label_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        super::labels::stats(py, &self.inner.borrow())
    }

    /// The last layer's screen id image as top-down native u32 bytes.
    fn readback_label_ids<'py>(&self, py: Python<'py>) -> Option<(Bound<'py, PyBytes>, i32, i32)> {
        let core = self.inner.borrow();
        let g = core.gpu.as_ref()?;
        let ids = unsafe { g.render.readback_ids(&g.gl) };
        let bytes: Vec<u8> = ids.iter().flat_map(|v| v.to_ne_bytes()).collect();
        Some((PyBytes::new(py, &bytes), g.render.size.0, g.render.size.1))
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

    /// `final` (with `shown`: what is presented, the masks over it) as
    /// top-down RGBA8 bytes (tests; context current).
    #[pyo3(signature = (shown=false))]
    fn readback_final<'py>(&self, py: Python<'py>, shown: bool) -> Option<(Bound<'py, PyBytes>, i32, i32)> {
        let core = self.inner.borrow();
        let g = core.gpu.as_ref()?;
        let px = unsafe { g.render.readback(&g.gl, shown && core.labels.shown) };
        Some((PyBytes::new(py, &px), g.render.size.0, g.render.size.1))
    }
}

