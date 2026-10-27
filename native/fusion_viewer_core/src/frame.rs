//! One paintGL: take the tiles that landed, upload within the frame's
//! budget, bring the page tables up to date and draw. Context current,
//! GUI thread, GIL held.

use crate::clock::monotonic;
use crate::viewer::ViewerCore;
use std::collections::HashSet;
use std::time::Instant;

mod compose;
mod covered;
mod drawlog;
mod pace;
pub mod phase_time;
mod shown;
pub(crate) use shown::shown_rows;
pub(crate) use covered::view_covered;
pub use compose::{draw_labels, ComposeKey};
pub use pace::Pacing;

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
    /// of `deferred`: Step3 label textures left for a later paint
    pub label_deferred: usize,
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
    /// a tile's sampler family was full with nothing to evict: the picture
    /// cannot be drawn in one pass (the binding hands it to Python)
    pub capacity_exceeded: bool,
    pub drain_ms: f64,
    pub upload_ms: f64,
    pub draw_ms: f64,
    pub total_ms: f64,
    /// any error stops the frame before the blit: it is never shown, and
    /// the binding falls back (R3 plan §2.5)
    pub error: String,
    /// the reads are given up (`viewer::failures`): why
    pub read_failure: String,
    /// a failed read is retried in this many ms: paint again then
    pub retry_ms: f64,
    /// `final` was composed again (else only masks, or nothing, changed)
    pub composed: bool,
    /// GUI-thread time on Step3's masks since the last report (R3b gate)
    pub label_ms: f64,
}

/// What each channel draws now (coarse first, then fine coarse to fine),
/// against what is on the GPU at this moment.
pub(crate) fn drawn_rows(core: &mut ViewerCore) -> Vec<(crate::plan::Ch, Vec<crate::geom::TileAt>)> {
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

/// Hold GPU blocks ahead of need (`store_plan`; docs §48, the approved
/// exception to "reserve once": the first frames, and again when other
/// channels are drawn, the canvas grows or a release dropped blocks), and
/// split the samplers between the families by need. One block per frame
/// and never while input is hot, so no frame pays more than one first use
/// (55-64 ms per 64 MiB on the server, `gpu_touch`). Tiles the frame must
/// have (complete coarse) still grow the store at once in `prepare`.
/// Returns whether more blocks are still to come.
fn top_up(core: &mut ViewerCore, w: i32, h: i32, hot: bool, base_overflow: bool) -> bool {
    let (across, down) = core.pyr.grid(core.pyr.coarsest());
    let mut channels = std::collections::HashMap::new();
    for c in core.planner.active() {
        if let Some(f) = core.format_of(*c) {
            *channels.entry(f).or_insert(0) += 1;
        }
    }
    let formats: Vec<_> = channels.keys().copied().collect();
    let drawn: std::collections::HashSet<u16> = core.planner.active().iter().copied().collect();
    let Some(gpu) = core.gpu.as_mut() else { return false };
    let store = &mut gpu.store;
    if base_overflow {
        let now: Vec<_> = crate::family::ALL.iter().map(|f| (*f, store.live_blocks(*f))).collect();
        store.targets = now.into_iter().collect();     // no speculative block then
        return false;
    }
    let need = crate::store_plan::Need { channels, coarse_slots: (across * down) as usize, w, h };
    let room = store.max_bytes().saturating_sub(store.slack_bytes(&formats));
    let plan = crate::store_plan::plan(&need, &|f| store.layers(f) as usize, &|f| store.block_bytes(f),
                                       room, gpu.render.units);
    let old = store.caps;
    if plan.caps != old && store.fit_caps(&gpu.gl, plan.caps, &drawn) {
        unsafe {
            if gpu.render.set_split(&gpu.gl, (plan.caps[0], plan.caps[1])).is_ok() {
                gpu.vt.set_caps(plan.caps);
                let all: Vec<_> = store.slots.values().copied().collect();
                store.dirty.extend(all);                // refill the meta table
            } else {
                store.caps = old;                       // renumbered blocks fit the old split too
            }
        }
    }
    store.targets = plan.targets;
    let short = crate::family::ALL.into_iter()
        .find(|f| store.live_blocks(*f) < store.targets.get(f).copied().unwrap_or(0));
    let Some(format) = short else { return false };
    if !hot {
        // a target that cannot be reached now (budget or samplers held by
        // blocks still drawn) stops here: no repaint is asked for it
        if !store.reclaim_idle(&gpu.gl, format, &drawn) || store.reserve(&gpu.gl, format, 1) == 0 {
            return false;
        }
    }
    crate::family::ALL.iter().any(|f| store.live_blocks(*f) < store.targets.get(f).copied().unwrap_or(0))
}

/// `view`: the world rect on screen now (x0, x1, y0, y1); `fbo`, `w`, `h`:
/// Qt's framebuffer and its device size.
pub fn paint(core: &mut ViewerCore, fbo: u32, w: i32, h: i32, view: [f64; 4]) -> FrameReport {
    let started = Instant::now();
    let mut rep = FrameReport { frame: core.frames + 1, ..Default::default() };
    core.frames += 1;
    let pacing = core.pacing();
    let hot = pacing.input_hot();
    let reads = core.check_reads();
    rep.retry_ms = reads.retry_ms.unwrap_or(0.0);
    if let Some(why) = reads.give_up {
        rep.read_failure = why;                // given up: this frame is not drawn either
        return rep;
    }
    if core.faults.paint_fails(rep.frame) {
        rep.error = "injected paint failure (test)".into();
        return rep;
    }
    core.drain_labels();                    // Step3's masks that landed (R3b)
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
    // R7c step 3: hybrid withdrew helpers of a target now complete
    if replanned || core.planner.take_reissue() {
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
    let deadline = Some(pacing.deadline(started, t_up));
    let may_grow = monotonic() - core.last_input >= core.cfg.growth_quiet_s;
    // R7c step 3: odon / hybrid's helper levels (between the target and the
    // coarsest) are optional -- uploaded after the rest, and a full store
    // only defers them (never "cannot draw in one pass")
    let helper = |t: &crate::geom::TileAt| core.planner.order != crate::plan::levels::Order::Cur
        && core.planner.admission_level().is_some_and(|l| t.level > l && t.level < coarsest);
    let (optional, items): (Vec<_>, Vec<_>) = items.into_iter().partition(|(k, _, _)| helper(&k.1));
    let need: HashSet<_> = required.iter().filter(|k| !helper(&k.1)).copied().collect();
    let gpu = core.gpu.as_mut().unwrap();
    let mut prep = gpu.store.prepare(&gpu.gl, &items, &mandatory, &need, deadline, may_grow);
    if !optional.is_empty() && prep.error.is_none() {
        let more = gpu.store.prepare(&gpu.gl, &optional, &HashSet::new(), &required, deadline, may_grow);
        prep.uploaded.extend(more.uploaded);
        // helpers the full store cannot take wait for a change (a landing
        // tile repaints), not for the next frame: no endless repaint
        if !more.capacity {
            prep.deferred += more.deferred;
            prep.deferred_growth += more.deferred_growth;
        }
        prep.error = more.error;
    }
    for key in &prep.uploaded {
        core.trace.stamp("gpu", Some(*key), vec![]);
    }
    rep.uploads = prep.uploaded.len();
    rep.capacity_exceeded = prep.capacity;
    rep.deferred = prep.deferred;
    rep.deferred_growth = prep.deferred_growth;
    if let Some(e) = prep.error {
        rep.error = e.clone();
        core.last_error = e;
        return rep;                          // not drawn, not shown
    }
    rep.upload_ms = t_up.elapsed().as_secs_f64() * 1000.0;
    // 4. what is drawn, again against what is on the GPU NOW (a finer
    // stand-in goes the moment its target cells are uploaded); with the
    // base layers over budget nothing new is drawn: the last frame stays
    let t_draw = Instant::now();
    rep.base_overflow = base_overflow;
    let mut pc = phase_time::PhaseClock::new(core.cfg.draw_log && core.trace.on(), rep.frame);
    let Some((rows, made)) = compose::draw(core, &mut rep, fbo, w, h, view, base_overflow, &mut pc) else { return rep };
    let gpu = core.gpu.as_ref().unwrap();
    rep.draw_ms = t_draw.elapsed().as_secs_f64() * 1000.0;
    phase_time::record(&core.trace, &pc, rep.draw_ms, rep.composed, made, rows.len(), core.attaches,
                       gpu.timers.missing);
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
    drawlog::record(core, rep.frame, view, rep.level, rep.composed, &rows);
    rep.total_ms = started.elapsed().as_secs_f64() * 1000.0;
    rep.label_ms = std::mem::take(&mut core.lab.gui_ms);
    rep
}
