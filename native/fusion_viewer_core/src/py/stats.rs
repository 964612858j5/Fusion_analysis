//! `ViewerCore.stats()`: the counters and states the binding, the mount
//! and the A9 tools read.

use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(super) fn stats<'py>(py: Python<'py>, core: &mut crate::viewer::ViewerCore) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    let sh = core.pool.shared().clone();
    d.set_item("reads", sh.reads.load(std::sync::atomic::Ordering::Relaxed))?;
    d.set_item("stale", sh.stale.load(std::sync::atomic::Ordering::Relaxed))?;
    d.set_item("pending_results", core.pool.pending_results())?;
    d.set_item("read_errors", sh.errors.load(std::sync::atomic::Ordering::Relaxed))?;
    let m = core.memory();
    for (k, v) in [("cache_bytes", m.cache_raw + m.cache_corrected), ("cache_raw_bytes", m.cache_raw),
                   ("cache_corrected_bytes", m.cache_corrected), ("cached_tiles", m.cached_tiles),
                   ("queue_extra_bytes", m.queue_extra), ("held_extra_bytes", m.held_extra),
                   ("label_copy_bytes", m.label_copies), ("worker_allowance_bytes", m.worker_allowance),
                   ("cpu_bytes", m.total), ("cpu_bound_bytes", m.bound)] {
        d.set_item(k, v)?;
    }
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
