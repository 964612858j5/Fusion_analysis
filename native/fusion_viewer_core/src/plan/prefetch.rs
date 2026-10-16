//! R7c step 2 (docs/v16_5_r7c_odon_alignment_plan.md §3): tiles around the
//! view read ahead into the CPU cache -- never planned, never uploaded; a
//! tile that comes into view later is a cache hit (`ViewerCore::issue`).
//!
//! `BLOCK01_STEP1_PREFETCH`:
//! * `limited` (default, user 2026-10-16): see below.
//! * `off`: nothing.
//! * `odon` -- Odon's rules on the admitted target level (`app.rs`
//!   12990-13077, 15159-15225): the visible tiles grown by one (pad1),
//!   sorted with the visible ones by distance from the view centre; then,
//!   when pad1 holds at most 16 positions, the second ring (pad2 less pad1),
//!   at most 48 positions; every position times every channel. The second
//!   ring is left out with 16 or more channels, or when pad1 times the
//!   channels exceeds the CPU cache's tile slots. No in-flight limit.
//! (The numbers are `Tune`'s defaults; R7c step 3 §6 measures others.)
//! * `limited`: one ring on the target level, at most 48 (tile x channel)
//!   keys in all, at most one being read at a time; none while the target
//!   is downgraded, the stand-in allowance is zero, or more visible tiles
//!   are missing than twice the readers.

use super::{Ch, Planner};
use crate::geom::TileAt;
use std::collections::HashSet;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Mode {
    Off,
    Odon,
    Limited,
}

impl Mode {
    pub fn from_env() -> Mode {
        match std::env::var("BLOCK01_STEP1_PREFETCH").as_deref() {
            Ok("off") => Mode::Off,
            Ok("odon") => Mode::Odon,
            _ => Mode::Limited,
        }
    }
}

/// The read-ahead's numbers. The defaults are the step-2 values (odon's
/// adapted from Odon's Balanced rules); R7c step 3 §6 measures other values
/// through `BLOCK01_R7C3_TUNE` (measurement only), e.g. "cap=128,gate=32".
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Tune {
    /// odon: at most this many NEW read-ahead keys per issue (`viewer/issue.rs`)
    pub cap: usize,
    /// odon: ring 2 only while pad1 holds at most this many positions
    pub gate: usize,
    /// odon: ring 2 at most this many positions
    pub ring2: usize,
    /// odon: no ring 2 with this many channels or more
    pub channels: usize,
    /// limited: at most this many (tile x channel) keys
    pub limited: usize,
}

impl Default for Tune {
    fn default() -> Tune {
        Tune { cap: 256, gate: 16, ring2: 48, channels: 16, limited: 48 }
    }
}

impl Tune {
    pub fn from_env() -> Tune {
        let mut t = Tune::default();
        let Ok(spec) = std::env::var("BLOCK01_R7C3_TUNE") else { return t };
        for item in spec.split(',') {
            let Some((k, v)) = item.split_once('=') else { continue };
            let Ok(v) = v.trim().parse::<usize>() else { continue };
            match k.trim() {
                "cap" => t.cap = v,
                "gate" => t.gate = v,
                "ring2" => t.ring2 = v,
                "channels" => t.channels = v,
                "limited" => t.limited = v,
                _ => {}
            }
        }
        t
    }
}

/// What to read ahead: keys merged with the visible requests (sorted with
/// them by distance; Odon's pad1) and keys after them (in order).
#[derive(Default, Debug)]
pub struct Ahead {
    pub with_visible: Vec<(Ch, TileAt)>,
    pub after: Vec<(Ch, TileAt)>,
    /// at most this many prefetch reads at once (None: no limit)
    pub in_flight: Option<usize>,
    /// positions in pad1 (counted for the report), and why ring 2 was left out
    pub pad1_positions: usize,
    pub skipped: &'static str,
    /// the same as a number for the trace: 0 none, 1 no fine target,
    /// 2 odon ring 2 off, 3 downgraded, 4 no allowance, 5 backlog
    pub skip_code: u8,
}

impl Planner {
    /// Positions of `level` around `targets` grown by `pad` tiles, minus `inner`.
    pub(super) fn ring(&self, level: u8, targets: &[(u32, u32)], pad: i64, inner: &HashSet<(u32, u32)>) -> Vec<(u32, u32)> {
        if targets.is_empty() {
            return Vec::new();
        }
        let (across, down) = self.pyr.grid(level);
        let tx0 = targets.iter().map(|t| t.0).min().unwrap() as i64 - pad;
        let tx1 = targets.iter().map(|t| t.0).max().unwrap() as i64 + pad;
        let ty0 = targets.iter().map(|t| t.1).min().unwrap() as i64 - pad;
        let ty1 = targets.iter().map(|t| t.1).max().unwrap() as i64 + pad;
        let mut out = Vec::new();
        for ty in ty0.max(0)..=ty1.min(down as i64 - 1) {
            for tx in tx0.max(0)..=tx1.min(across as i64 - 1) {
                let p = (tx as u32, ty as u32);
                if !inner.contains(&p) {
                    out.push(p);
                }
            }
        }
        out
    }

    pub(super) fn by_distance(&self, level: u8, v: &mut [(u32, u32)]) {
        let Some(view) = self.view else { return };
        let (cy, cx) = ((view.bbox_l0[0] + view.bbox_l0[2]) as f64 / 2.0, (view.bbox_l0[1] + view.bbox_l0[3]) as f64 / 2.0);
        let d = |&(tx, ty): &(u32, u32)| {
            let r = self.pyr.tile_world_rect(TileAt { level, tx, ty }).unwrap_or([0.0; 4]);
            let (dx, dy) = ((r[0] + r[1]) / 2.0 - cx, (r[2] + r[3]) / 2.0 - cy);
            ((dx * dx + dy * dy).sqrt() * 1000.0) as u64
        };
        v.sort_by_key(|p| (d(p), p.1, p.0));
    }

    /// The read-ahead for the current view; `cache_slots`: how many tiles
    /// the CPU cache holds; `readers`: reader threads.
    pub fn ahead(&mut self, mode: Mode, cache_slots: usize, readers: usize) -> Ahead {
        let mut out = Ahead::default();
        if mode == Mode::Off {
            return out;
        }
        let Some(a) = self.admission().cloned() else { return out };
        if a.coarse_only || a.base_overflow || a.tiles.is_empty() {
            out.skipped = "no fine target";
            out.skip_code = 1;
            return out;
        }
        let chans: Vec<Ch> = self.active.iter().copied()
            .filter(|c| !self.unavailable.contains(c) && self.coarse.contains_key(c)).collect();
        let level = a.level;
        let visible: HashSet<(u32, u32)> = a.tiles.iter().copied().collect();
        let mut pad1 = self.ring(level, &a.tiles, 1, &visible);
        self.by_distance(level, &mut pad1);
        out.pad1_positions = visible.len() + pad1.len();
        let keys = |pos: &[(u32, u32)]| -> Vec<(Ch, TileAt)> {
            pos.iter().flat_map(|&(tx, ty)| chans.iter().map(move |&c| (c, TileAt { level, tx, ty }))).collect()
        };
        match mode {
            Mode::Odon => {
                out.with_visible = keys(&pad1);
                if chans.len() >= self.tune.channels || out.pad1_positions * chans.len() > cache_slots {
                    out.skipped = "odon: many channels or over the cache";
                    out.skip_code = 2;
                } else if out.pad1_positions <= self.tune.gate {
                    let inner: HashSet<(u32, u32)> = visible.iter().copied().chain(pad1.iter().copied()).collect();
                    let mut ring2 = self.ring(level, &a.tiles, 2, &inner);
                    self.by_distance(level, &mut ring2);
                    ring2.truncate(self.tune.ring2);
                    out.after = keys(&ring2);
                }
            }
            Mode::Limited => {
                let missing = self.plans.values().map(|p| p.expected.len()).sum::<usize>();
                if a.limited() {
                    out.skipped = "limited: target downgraded";
                    out.skip_code = 3;
                } else if chans.iter().any(|&c| a.carry(self.texel(c)) == 0) {
                    out.skipped = "limited: no stand-in allowance";
                    out.skip_code = 4;
                } else if missing > 2 * readers {
                    out.skipped = "limited: visible tiles backlogged";
                    out.skip_code = 5;
                } else {
                    let mut k = keys(&pad1);
                    k.truncate(self.tune.limited);
                    out.after = k;
                    out.in_flight = Some(1);
                }
            }
            Mode::Off => {}
        }
        out
    }
}

#[cfg(test)]
#[path = "prefetch_tests.rs"]
mod tests;
