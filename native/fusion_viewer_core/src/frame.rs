//! One paintGL: take the tiles that landed, upload within the frame's
//! budget, bring the page tables up to date and draw. Context current,
//! GUI thread, GIL held.

use crate::clock::monotonic;
use crate::geom::roi_scissor_box;
use crate::viewer::ViewerCore;
use std::collections::HashSet;
use std::time::{Duration, Instant};

/// What one frame did, for the Python side's measurement and pacing.
#[derive(Debug, Default)]
pub struct FrameReport {
    pub frame: u64,
    pub channels: usize,
    pub planes: usize,
    pub received: usize,
    pub uploads: usize,
    pub deferred: usize,
    pub deferred_growth: usize,
    /// (admitted level, target tiles wanted, of them on the GPU, view id)
    pub level: i64,
    pub ideal: i64,
    pub wanted: usize,
    pub shown: usize,
    pub view_id: u64,
    pub limited: bool,
    pub base_overflow: bool,
    /// blocks are still being reserved ahead (one per frame): paint again
    pub reserving: bool,
    pub drain_ms: f64,
    pub upload_ms: f64,
    pub draw_ms: f64,
    pub total_ms: f64,
    pub error: String,
}

/// What each channel draws now (coarse first, then fine coarse to fine),
/// against what is on the GPU at this moment.
fn drawn_rows(core: &mut ViewerCore) -> Vec<(crate::plan::Ch, Vec<crate::geom::TileAt>)> {
    let Some(gpu) = core.gpu.as_ref() else { return Vec::new() };
    let store = &gpu.store;
    let on_gpu = |c, t| store.is_resident(&(c, t));
    let mut rows = Vec::new();
    for c in core.planner.active().to_vec() {
        let tiles = core.planner.drawn(c, &on_gpu);
        if !tiles.is_empty() {
            rows.push((c, tiles));
        }
    }
    rows
}

/// Hold GPU blocks ahead of need (`reserve::target_blocks`; docs §48, the
/// approved exception to "reserve once": the first frames, and again when
/// more channels are drawn, the canvas grows or a release dropped blocks).
/// One block per frame and never while input is hot, so no frame pays more
/// than one first use (55-64 ms per 64 MiB on the server, `gpu_touch`). Tiles the frame
/// must have (complete coarse) still grow the store at once in `prepare`.
/// Returns whether more blocks are still to come.
fn top_up(core: &mut ViewerCore, w: i32, h: i32, hot: bool, base_overflow: bool) -> bool {
    let channels = core.planner.active().len();
    let (across, down) = core.pyr.grid(core.pyr.coarsest());
    let Some(gpu) = core.gpu.as_mut() else { return false };
    let store = &mut gpu.store;
    if base_overflow {
        store.target_blocks = store.live_blocks();   // no speculative block then
        return false;
    }
    store.target_blocks = crate::reserve::target_blocks(channels, (across * down) as usize, w, h,
                                                        store.layers_per_block as usize,
                                                        store.budget_blocks());
    if store.live_blocks() >= store.target_blocks {
        return false;
    }
    if !hot {
        store.reserve(&gpu.gl, 1);
    }
    store.live_blocks() < store.target_blocks
}

/// `view`: the world rect on screen now (x0, x1, y0, y1); `fbo`, `w`, `h`:
/// Qt's framebuffer and its device size.
pub fn paint(core: &mut ViewerCore, fbo: u32, w: i32, h: i32, view: [f64; 4]) -> FrameReport {
    let started = Instant::now();
    let mut rep = FrameReport { frame: core.frames + 1, ..Default::default() };
    core.frames += 1;
    let hot = core.input_hot();
    let budget = Duration::from_secs_f64(core.cfg.frame_budget_ms / 1000.0);
    // 1. the tiles that landed (never waiting for one that has not)
    let results = core.pool.take_results(usize::MAX);
    rep.received = results.len();
    let mut replanned = false;
    for (key, tile) in results {
        let coarse_before = core.planner.coarse_complete(key.0);
        if core.deliver(key, tile) && !coarse_before && core.planner.coarse_complete(key.0) {
            replanned = true;                // its fine was planned just now
        }
    }
    if replanned {
        core.issue();
    }
    core.prune_held();
    rep.drain_ms = started.elapsed().as_secs_f64() * 1000.0;
    if core.gpu.is_none() {
        return rep;
    }
    let base_overflow = core.planner.admission().is_some_and(|a| a.base_overflow);
    rep.reserving = top_up(core, w, h, hot, base_overflow);
    // 2. what each channel draws, coarse first then fine (coarse to fine)
    let rows = if base_overflow { Vec::new() } else { drawn_rows(core) };
    let coarsest = core.pyr.coarsest();
    // 3. upload: coarse always, the rest within the budget
    let mut items = Vec::new();
    let mut mandatory = HashSet::new();
    let mut required = HashSet::new();
    for (c, tiles) in &rows {
        for t in tiles {
            let key = (*c, *t);
            required.insert(key);
            if t.level == coarsest && !core.planner.keeps(*c, *t) {
                continue;
            }
            if let (Some(tile), Some(rect)) = (core.held.get(&key), core.pyr.tile_world_rect(*t)) {
                if t.level == coarsest {
                    mandatory.insert(key);
                }
                items.push((key, tile.clone(), rect));
            }
        }
    }
    let t_up = Instant::now();
    let deadline = if hot {
        Some(t_up + Duration::from_secs_f64(core.cfg.hot_upload_ms / 1000.0))
    } else {
        Some(started + budget)
    };
    let may_grow = monotonic() - core.last_input >= core.cfg.growth_quiet_s;
    let gpu = core.gpu.as_mut().unwrap();
    let prep = gpu.store.prepare(&gpu.gl, &items, &mandatory, &required, deadline, may_grow);
    for key in &prep.uploaded {
        core.trace.stamp("gpu", Some(*key), vec![]);
    }
    rep.uploads = prep.uploaded.len();
    rep.deferred = prep.deferred;
    rep.deferred_growth = prep.deferred_growth;
    if let Some(e) = prep.error {
        rep.error = e.clone();
        core.last_error = e;
    }
    rep.upload_ms = t_up.elapsed().as_secs_f64() * 1000.0;
    // 4. what is drawn, again against what is on the GPU NOW (a finer
    // stand-in goes the moment its target cells are uploaded); with the
    // base layers over budget nothing new is drawn: the last frame stays
    let t_draw = Instant::now();
    let rows = if base_overflow { Vec::new() } else { drawn_rows(core) };
    rep.base_overflow = base_overflow;
    let gpu = core.gpu.as_mut().unwrap();
    if !base_overflow {
        gpu.vt.sync_pages(&rows, &gpu.store);
    }
    gpu.vt.sync_meta(&mut gpu.store);
    let params_rows: Vec<_> = rows.iter().filter_map(|(c, _)| {
        core.display.iter().find(|(d, _)| d == c).map(|(_, p)| (*c, *p))
    }).collect();
    let params = if base_overflow { Vec::new() } else { gpu.vt.params(&params_rows) };
    let (gl, render) = (&gpu.gl, &mut gpu.render);
    unsafe {
        if let Err(e) = render.ensure_targets(gl, w.max(1), h.max(1)) {
            rep.error = e;
            return rep;
        }
        if !base_overflow {
            if let Err(e) = render.upload(gl, &mut gpu.vt, &params) {
                rep.error = e;
                return rep;
            }
        }
        let v32 = [view[0] as f32, view[1] as f32, view[2] as f32, view[3] as f32];
        let scissor = roi_scissor_box(view, core.roi_world, render.size);
        let rows_drawn = if base_overflow { render.last_rows } else { params_rows.len() };
        render.compose(gl, &gpu.vt, &gpu.store, rows_drawn, v32, core.smooth, scissor);
        render.last_rows = rows_drawn;
        render.blit(gl, fbo, w, h);
    }
    rep.draw_ms = t_draw.elapsed().as_secs_f64() * 1000.0;
    // 5. what this frame owes and shows (the A9 coverage record)
    rep.channels = rows.len();
    rep.planes = rows.iter().map(|r| r.1.len()).sum();
    let view_plan = core.planner.view();
    if let Some(a) = core.planner.admission().cloned() {
        rep.level = a.level as i64;
        rep.ideal = a.ideal as i64;
        rep.limited = a.limited();
        if !a.coarse_only {
            for (c, _) in &rows {
                for &(tx, ty) in &a.tiles {
                    rep.wanted += 1;
                    let key = (*c, crate::geom::TileAt { level: a.level, tx, ty });
                    if gpu.store.is_resident(&key) && core.planner.keeps(key.0, key.1) {
                        rep.shown += 1;
                    }
                }
            }
        } else {
            rep.level = -1;
        }
    }
    rep.view_id = view_plan.map_or(0, |v| v.epoch);
    rep.total_ms = started.elapsed().as_secs_f64() * 1000.0;
    rep
}
