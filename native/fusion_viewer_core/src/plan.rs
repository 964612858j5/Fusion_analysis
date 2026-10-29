//! The planner: what each channel needs and what it may keep
//! (`Step1GpuBinding`'s coarse / fine tiers, raw uint8 channels).
//!
//! * COARSE IS ATOMIC: a channel's whole coarsest level is asked for, and
//!   it counts as delivered only when every tile has landed.
//! * FINE IS INCREMENTAL AND KEPT ACROSS CAMERA MOVES: a new view keeps
//!   the delivered tiles that still matter (target tiles always; others
//!   as stand-ins within the carry allowance, nearest level first) and
//!   asks only for the missing target tiles.
//! Pure state, no threads, no GL: the viewer feeds it views, channel sets
//! and deliveries, and turns its answers into reads and draws.

use crate::admit::{admit, Admission, Budgets};
use crate::geom::{overlaps, Pyramid, TileAt};
use std::collections::{BTreeMap, HashMap, HashSet};

pub type Ch = u16;

/// What the controller decided for one camera position.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct View {
    pub level: u8,
    /// `(y0, x0, y1, x1)` in level-0 pixels, clamped and truncated.
    pub bbox_l0: [i64; 4],
    pub epoch: u64,
}

struct Coarse {
    expected: usize,
    got: HashSet<TileAt>,
}

struct FinePlan {
    epoch: u64,
    expected: HashSet<TileAt>,
    /// R7c step 3: helper-level tiles asked for (reserved), odon / hybrid
    aux: HashSet<TileAt>,
}

pub struct Planner {
    pub pyr: Pyramid,
    budgets: Budgets,
    max_coarse_tiles: usize,
    active: Vec<Ch>,
    coarse: HashMap<Ch, Coarse>,
    fine: HashMap<Ch, BTreeMap<TileAt, ()>>,
    plans: HashMap<Ch, FinePlan>,
    view: Option<View>,
    consumed: Option<u64>,
    epoch: u64,
    admission: Option<(Option<View>, Vec<u64>, Admission)>,
    /// bytes per texel per channel (`set_texels`)
    texels: HashMap<Ch, u64>,
    /// channels refused (coarse over budget); never drawn
    pub unavailable: HashSet<Ch>,
    /// R7c step 2: read-ahead (`prefetch`) and its numbers
    pub prefetch: prefetch::Mode,
    pub tune: prefetch::Tune,
    /// R7c step 3: request order and helper levels (`levels`), the sticky
    /// ceiling, hybrid's opened channels (`gate`), a withdrawal to issue
    pub order: levels::Order,
    prev_target: Option<u8>,
    ceiling: Option<u8>,
    opened: HashSet<Ch>,
    reissue: bool,
}

impl Planner {
    pub fn new(pyr: Pyramid, budgets: Budgets, max_coarse_tiles: usize) -> Self {
        Planner { pyr, budgets, max_coarse_tiles, active: Vec::new(), coarse: HashMap::new(),
                  fine: HashMap::new(), plans: HashMap::new(), view: None, consumed: None,
                  epoch: 0, admission: None, texels: HashMap::new(), unavailable: HashSet::new(),
                  prefetch: prefetch::Mode::Off, tune: prefetch::Tune::default(),
                  order: levels::Order::Cur, prev_target: None, ceiling: None, opened: HashSet::new(),
                  reissue: false }
    }

    /// The total budget admission uses (the raw budget less the GPU
    /// store's block slack, known once GL is up).
    pub fn set_total(&mut self, total: Option<u64>) {
        self.budgets.total = total;
        self.admission = None;
    }

    /// Bytes per texel of each channel (`_pixel_bytes`; 1 when not told).
    pub fn set_texels(&mut self, texels: HashMap<Ch, u64>) {
        if texels != self.texels {
            self.texels = texels;
            self.admission = None;
        }
    }

    fn texel(&self, c: Ch) -> u64 {
        self.texels.get(&c).copied().unwrap_or(1)
    }

    pub fn active(&self) -> &[Ch] {
        &self.active
    }

    /// The admission for the current view and active set (memoised).
    pub fn admission(&mut self) -> Option<&Admission> {
        let view = self.view?;
        let texels: Vec<u64> = self.active.iter().filter(|c| !self.unavailable.contains(c))
            .map(|c| self.texel(*c)).collect();
        let stale = !matches!(&self.admission, Some((v, k, _)) if *v == Some(view) && *k == texels);
        if stale {
            let a = admit(&self.pyr, view.level, view.bbox_l0, &texels, &self.budgets);
            self.admission = Some((Some(view), texels, a));
        }
        self.admission.as_ref().map(|(_, _, a)| a)
    }

    /// The drawn channels changed (`refresh_display`). `active` is sorted
    /// by channel name. Returns the channels whose plans may have changed.
    pub fn set_active(&mut self, active: Vec<Ch>) {
        let keep: HashSet<Ch> = active.iter().copied().collect();
        self.fine.retain(|c, _| keep.contains(c));
        self.plans.retain(|c, _| keep.contains(c));
        self.opened.retain(|c| keep.contains(c));
        let changed = active != self.active;
        self.active = active;
        for c in self.active.clone() {
            self.start_coarse(c);
        }
        if changed && self.view.is_some() {
            self.track_target();               // R7c step 3: admission may move with the channels
            for c in self.active.clone() {
                self.plan_channel(c);
            }
        }
    }

    fn start_coarse(&mut self, c: Ch) {
        if self.coarse.contains_key(&c) || self.unavailable.contains(&c) {
            return;
        }
        let (across, down) = self.pyr.grid(self.pyr.coarsest());
        let n = (across * down) as usize;
        let bytes = n as u64 * (crate::geom::TILE * crate::geom::TILE) as u64 * self.texel(c);
        if n > self.max_coarse_tiles || self.budgets.max_coarse_bytes.is_some_and(|m| bytes > m) {
            self.unavailable.insert(c);
            return;
        }
        self.coarse.insert(c, Coarse { expected: n, got: HashSet::new() });
    }

    /// A new camera position (`update_viewport`); the same epoch twice is
    /// planned once.
    pub fn set_view(&mut self, view: View) {
        self.view = Some(view);
        if self.consumed == Some(view.epoch) {
            return;
        }
        self.consumed = Some(view.epoch);
        self.plans.clear();
        self.epoch += 1;
        self.track_target();
        for c in self.active.clone() {
            self.plan_channel(c);
        }
    }

    pub fn coarse_complete(&self, c: Ch) -> bool {
        self.coarse.get(&c).is_some_and(|k| k.got.len() == k.expected)
    }

    /// Has the coarse tile `t` of `c` been delivered (§15.6: read-only)?
    pub fn coarse_has(&self, c: Ch, t: TileAt) -> bool {
        self.coarse.get(&c).is_some_and(|k| k.got.contains(&t))
    }

    /// Does `c` hold fine tiles (delivered and kept for the current view)?
    pub fn has_fine(&self, c: Ch) -> bool {
        self.fine.get(&c).is_some_and(|m| !m.is_empty())
    }

    /// One channel's fine for the current view (`_plan_fine_for_channel`).
    fn plan_channel(&mut self, c: Ch) {
        if self.unavailable.contains(&c) || !self.coarse.contains_key(&c) || self.view.is_none() {
            self.fine.remove(&c);
            return;
        }
        let view = self.view.unwrap();
        let a = self.admission().unwrap().clone();
        let level = a.level;
        let keys: Vec<TileAt> = a.tiles.iter().map(|&(tx, ty)| TileAt { level, tx, ty }).collect();
        let viewport = if !keys.is_empty() { a.rect } else {
            let ideal = self.pyr.cover(view.bbox_l0, view.level);
            self.pyr.tiles_world_rect(view.level, &ideal)
        };
        let carry = a.carry(self.texel(c));
        let target: HashSet<TileAt> = keys.iter().copied().collect();
        let kept_bytes = self.retain(c, &target, viewport, level, carry);
        let resident = self.fine.get(&c);
        let missing: HashSet<TileAt> = keys.iter().copied()
            .filter(|k| resident.is_none_or(|r| !r.contains_key(k))).collect();
        // R7c step 3: helper levels from what the kept stand-ins leave
        let aux = if a.coarse_only || a.base_overflow || keys.is_empty() { HashSet::new() } else {
            self.helpers(c, level, !missing.is_empty(), carry.saturating_sub(kept_bytes))
        };
        if missing.is_empty() && aux.is_empty() {
            self.plans.remove(&c);
            return;
        }
        if let Some(p) = self.plans.get(&c) {
            if p.epoch == self.epoch && p.expected == missing && p.aux == aux {
                return;                    // the same request is under way
            }
        }
        self.plans.insert(c, FinePlan { epoch: self.epoch, expected: missing, aux });
    }

    /// Keep the target tiles and, within `carry`, the nearest-level
    /// stand-ins that still meet `viewport` (`_retain_fine`; sizes are the
    /// tiles' real bytes, not slots).
    fn retain(&mut self, c: Ch, target: &HashSet<TileAt>, viewport: Option<[f64; 4]>,
              level: u8, carry: u64) -> u64 {
        let Some(planes) = self.fine.get(&c) else { return 0 };
        let mut kept = BTreeMap::new();
        let mut carried = Vec::new();
        for &t in planes.keys() {
            if target.contains(&t) {
                kept.insert(t, ());
            } else if self.pyr.tile_world_rect(t).is_some_and(|r| overlaps(&r, &viewport)) {
                carried.push(t);
            }
        }
        carried.sort_by_key(|t| ((t.level as i32 - level as i32).abs(), t.level, t.ty, t.tx));
        let mut total = 0u64;
        for t in carried {
            let (rows, cols) = self.pyr.tile_shape(t);
            let size = (rows * cols) as u64 * self.texel(c);
            if total + size > carry {
                break;
            }
            kept.insert(t, ());
            total += size;
        }
        if kept.is_empty() { self.fine.remove(&c); } else { self.fine.insert(c, kept); }
        total                              // the kept stand-ins' bytes
    }

    /// A decoded tile is in hand. True when this planner wanted it (it is
    /// then delivered; a coarse that completes plans its channel's fine).
    pub fn deliver(&mut self, c: Ch, t: TileAt) -> bool {
        if !self.active.contains(&c) {
            return false;
        }
        // the same tile may be owed to both tiers (a coarsest-level fine
        // target while the coarse is still loading): both take it, as both
        // scheduler waiters are called back in Python
        let mut taken = false;
        let mut completed = false;
        if t.level == self.pyr.coarsest() {
            if let Some(k) = self.coarse.get_mut(&c) {
                if k.got.len() < k.expected && k.got.insert(t) {
                    taken = true;
                    completed = k.got.len() == k.expected;
                }
            }
        }
        if let Some(p) = self.plans.get_mut(&c) {
            if p.epoch == self.epoch && (p.expected.remove(&t) || p.aux.remove(&t)) {
                // hybrid: the target complete, its helpers are withdrawn
                if p.expected.is_empty() && !p.aux.is_empty() && self.order == levels::Order::Hybrid {
                    p.aux.clear();
                    self.reissue = true;
                }
                if p.expected.is_empty() && p.aux.is_empty() {
                    self.plans.remove(&c);
                }
                self.fine.entry(c).or_default().insert(t, ());
                taken = true;
            }
        }
        if completed {
            self.plan_channel(c);
        }
        taken
    }

    /// S0-B: a retired channel (a Python source) leaves for good -- its
    /// coarse state, fine tiles, plan, hybrid gate, texel size and refusal.
    /// `set_active` alone keeps a dropped channel's coarse state (a channel
    /// may come back); a retired number never does.
    pub fn drop_channel(&mut self, c: Ch) {
        self.active.retain(|a| *a != c);
        self.coarse.remove(&c);
        self.fine.remove(&c);
        self.plans.remove(&c);
        self.opened.remove(&c);
        self.unavailable.remove(&c);
        if self.texels.remove(&c).is_some() {
            self.admission = None;
        }
    }

    /// Does the planner hold anything at all for `c` (tests: a retirement
    /// leaves nothing behind)?
    pub fn holds_channel(&self, c: Ch) -> bool {
        self.active.contains(&c) || self.coarse.contains_key(&c) || self.fine.contains_key(&c)
            || self.plans.contains_key(&c) || self.opened.contains(&c) || self.unavailable.contains(&c)
            || self.texels.contains_key(&c)
    }

    /// Does the planner still hold this delivered tile (coarse or kept fine)?
    pub fn keeps(&self, c: Ch, t: TileAt) -> bool {
        self.coarse.get(&c).is_some_and(|k| k.got.contains(&t))
            || self.fine.get(&c).is_some_and(|f| f.contains_key(&t))
    }

    /// The view last set, if any.
    pub fn view(&self) -> Option<View> {
        self.view
    }
}

mod draw;
pub mod gate;
pub mod levels;
pub mod prefetch;

#[cfg(test)]
#[path = "plan_tests.rs"]
mod tests;
