//! R7c step 3 (docs/v16_5_r7c3_order_plan.md §2): which levels a channel
//! asks for besides its target, `BLOCK01_STEP1_ORDER=cur|odon|hybrid`.
//!
//! * `cur`: the coarsest level and the target only -- the paths below are
//!   never taken.
//! * `odon`: also target+1 and, after zooming in, every level up to the
//!   sticky ceiling (Odon's design, `app.rs` 12951-12978: the ceiling
//!   rises to the previous target when the target gets finer, drops to the
//!   target when it gets coarser); each such level's visible tiles grown
//!   by one.
//! * `hybrid` (the default, user 2026-10-16 after R7c3 measurement and a
//!   real-machine check): target+1 only while the channel's target tiles
//!   are missing; a channel first drawn once the view is covered.
//!
//! The helper levels' tiles are paid from the channel's stand-in allowance
//! (`Admission::carry`): what the kept stand-ins leave, nearest level
//! first, then nearest the view centre. A helper tile is RESERVED when
//! asked for and only changes from reserved to kept when it lands, so a
//! delivery frees nothing to hand out again; the allowance is shared out
//! only when a channel is planned (`plan_channel`).

use super::{Ch, Planner};
use crate::geom::TileAt;
use std::collections::HashSet;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Order {
    Cur,
    Odon,
    Hybrid,
}

impl Order {
    pub fn from_env() -> Order {
        match std::env::var("BLOCK01_STEP1_ORDER").as_deref() {
            Ok("cur") => Order::Cur,
            Ok("odon") => Order::Odon,
            _ => Order::Hybrid,
        }
    }

    /// The trace's number for it (0 cur, 1 odon, 2 hybrid).
    pub fn code(self) -> f64 {
        match self { Order::Cur => 0.0, Order::Odon => 1.0, Order::Hybrid => 2.0 }
    }
}

impl Planner {
    /// odon: follow the admitted target wherever admission is redone (a new
    /// view, other channels), moving the sticky ceiling.
    pub(super) fn track_target(&mut self) {
        if self.order != Order::Odon {
            return;
        }
        if let Some(level) = self.admission().filter(|a| !a.coarse_only).map(|a| a.level) {
            self.update_ceiling(level);
        }
    }

    /// The admitted target changed: move the sticky ceiling (odon).
    pub(super) fn update_ceiling(&mut self, target: u8) {
        let coarsest = self.pyr.coarsest();
        self.ceiling = Some(match self.prev_target {
            Some(prev) if target < prev => self.ceiling.unwrap_or(target).max(prev),
            Some(prev) if target > prev => target,
            Some(_) => self.ceiling.unwrap_or(target),
            None => target,
        }.min(coarsest));
        self.prev_target = Some(target);
    }

    /// The helper levels of a channel whose target is `target`, nearest
    /// first: finer than the coarsest level only.
    pub(super) fn helper_levels(&self, target: u8, target_missing: bool) -> Vec<u8> {
        let coarsest = self.pyr.coarsest();
        let top = match self.order {
            Order::Cur => return Vec::new(),
            Order::Hybrid if !target_missing => return Vec::new(),
            Order::Hybrid => target + 1,
            Order::Odon => self.ceiling.unwrap_or(target).max(target + 1),
        };
        (target + 1..=top).filter(|&l| l < coarsest).collect()
    }

    /// `level`'s tiles covering the view, grown by `pad`, nearest the view
    /// centre first.
    pub(super) fn padded(&self, level: u8, pad: i64) -> Vec<(u32, u32)> {
        let Some(view) = self.view else { return Vec::new() };
        let cover = self.pyr.cover(view.bbox_l0, level);
        let mut out = self.ring(level, &cover, pad, &HashSet::new());
        self.by_distance(level, &mut out);
        out
    }

    /// The helper tiles `c` may ask for now, within `room` bytes (what its
    /// kept stand-ins leave of its allowance), skipping those it keeps.
    pub(super) fn helpers(&self, c: Ch, target: u8, target_missing: bool, mut room: u64) -> HashSet<TileAt> {
        let mut out = HashSet::new();
        let kept = self.fine.get(&c);
        for level in self.helper_levels(target, target_missing) {
            for (tx, ty) in self.padded(level, 1) {
                let t = TileAt { level, tx, ty };
                if kept.is_some_and(|k| k.contains_key(&t)) {
                    continue;
                }
                let (rows, cols) = self.pyr.tile_shape(t);
                let size = (rows * cols) as u64 * self.texel(c);
                if size == 0 {
                    continue;
                }
                if size > room {
                    return out;              // nearest first: what is left is farther
                }
                room -= size;
                out.insert(t);
            }
        }
        out
    }

    /// The coarsest level's tiles around the view (asked for before the
    /// rest of that level, odon / hybrid).
    pub fn coarse_near(&self) -> HashSet<(u32, u32)> {
        self.padded(self.pyr.coarsest(), 1).into_iter().collect()
    }

    /// The admitted fine level as last computed (no admission redone).
    pub fn admission_level(&self) -> Option<u8> {
        self.admission.as_ref().map(|(_, _, a)| a).filter(|a| !a.coarse_only).map(|a| a.level)
    }

    /// Something was withdrawn: `issue()` again (taken once).
    pub fn take_reissue(&mut self) -> bool {
        std::mem::take(&mut self.reissue)
    }
}

#[cfg(test)]
#[path = "levels_tests.rs"]
mod tests;
