//! R6 (docs/v16_5_r6_motion_clarity_plan.md §5), measurement only and off
//! unless both the trace and `Config::draw_log` are on: per paint, which
//! tiles each channel drew and which tiles of the view were in hand but not
//! drawn -- so a blurry spot on a screenshot can be traced to "never read",
//! "read but not uploaded", "uploaded but not drawn" or "a sharper tile was
//! in hand". Records only; nothing here changes what is drawn.
//!
//! Stages (the trace's names and fields, `trace.rs`):
//!   fview  no key; frame, the view (x0, x1, y0, y1, level 0), the admitted
//!          level (-1: none), whether `final` was composed again
//!   draw   (channel, tile) drawn this frame; `order`: its place in the
//!          channel's row (coarse first, then fine coarse to fine: a later
//!          tile covers an earlier one)
//!   avail  (channel, tile) in view, held by the planner or on the GPU, not
//!          drawn; `held`, `on_gpu` (0/1)

use crate::geom::TileAt;
use crate::plan::Ch;
use crate::viewer::ViewerCore;
use std::collections::HashSet;

fn overlaps(r: [f64; 4], view: [f64; 4]) -> bool {
    r[0] < view[1] && view[0] < r[1] && r[2] < view[3] && view[2] < r[3]
}

pub(super) fn record(core: &ViewerCore, frame: u64, view: [f64; 4], level: i64, composed: bool,
                     rows: &[(Ch, Vec<TileAt>)]) {
    if !core.cfg.draw_log || !core.trace.on() {
        return;
    }
    let f = frame as f64;
    core.trace.stamp("fview", None, vec![("frame", f), ("x0", view[0]), ("x1", view[1]), ("y0", view[2]),
                                          ("y1", view[3]), ("level", level as f64),
                                          ("composed", composed as i32 as f64)]);
    let mut drawn = HashSet::new();
    for (c, tiles) in rows {
        for (k, t) in tiles.iter().enumerate() {
            drawn.insert((*c, *t));
            core.trace.stamp("draw", Some((*c, *t)), vec![("frame", f), ("order", k as f64)]);
        }
    }
    let channels: HashSet<Ch> = rows.iter().map(|r| r.0).collect();
    let store = core.gpu.as_ref().map(|g| &g.store);
    let mut seen = HashSet::new();
    let held = core.held.keys().copied();
    let resident = store.into_iter().flat_map(|s| s.slots.keys().copied());
    for key in held.chain(resident) {
        if !channels.contains(&key.0) || drawn.contains(&key) || !seen.insert(key) {
            continue;
        }
        let Some(rect) = core.pyr.tile_world_rect(key.1) else { continue };
        if !overlaps(rect, view) {
            continue;
        }
        let on_gpu = store.is_some_and(|s| s.is_resident(&key));
        core.trace.stamp("avail", Some(key), vec![("frame", f),
                                                   ("held", core.held.contains_key(&key) as i32 as f64),
                                                   ("on_gpu", on_gpu as i32 as f64)]);
    }
}
