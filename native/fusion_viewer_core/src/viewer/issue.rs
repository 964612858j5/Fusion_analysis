//! `ViewerCore::issue`: the planner's wants, and the read-ahead (R7c step 2),
//! handed to the reader pool -- in R7c step 3's order (`plan::levels`).

use super::ViewerCore;
use crate::geom::TileAt;
use crate::plan::Ch;
use std::collections::HashMap;

impl ViewerCore {
    /// Hand the planner's wants to the pool: cache hits are delivered at
    /// once (one picture, as the binding's `immediate` batch), the rest are
    /// queued coarse first, then fine near-to-far, all channels of one
    /// position together. Under odon / hybrid (R7c step 3) the queue is
    /// target first, then the helper levels finer to coarser, the coarsest
    /// level around the view, the read-ahead, the rest of the coarsest
    /// level -- a queueing priority, not a barrier. A cache hit that
    /// completes a target withdraws its helpers: the wants are then taken
    /// once more before the queue is handed over.
    pub fn issue(&mut self) {
        let mut new_ahead = 0usize;            // odon's cap holds for the whole issue
        for pass in 0..2 {
            let (jobs, ahead_jobs, in_flight) = self.issue_once(&mut new_ahead);
            // a withdrawal on the second pass leaves the flag set: the next
            // paint issues again (the queue handed over may still hold it)
            if pass == 1 || !self.planner.take_reissue() {
                self.pool.set_jobs_ahead(jobs, ahead_jobs, in_flight);
                return;
            }
        }
    }

    /// One pass: the cache hits delivered, the queue to hand over.
    #[allow(clippy::type_complexity)]
    fn issue_once(&mut self, new_ahead: &mut usize) -> (Vec<(Ch, TileAt)>, std::collections::HashSet<(Ch, TileAt)>, Option<usize>) {
        let (coarse, mut fine) = self.planner.wanted();
        // R7c step 2: read-ahead keys (cache only); Odon's pad1 is sorted
        // with the visible tiles, the rest follows them. A key the picture
        // wants (e.g. the coarse level's, when the target is the coarsest)
        // stays the picture's: it is never read ahead (codex review)
        let picture: std::collections::HashSet<(Ch, TileAt)> = coarse.iter().chain(&fine).copied().collect();
        let mut ahead = self.planner.ahead(self.planner.prefetch, self.cache_slots(), self.cfg.threads);
        ahead.with_visible.retain(|k| !picture.contains(k));
        ahead.after.retain(|k| !picture.contains(k));
        // S0-B: Python sources are never read ahead (their tiles are computed)
        ahead.with_visible.retain(|k| !crate::python_source::PythonSources::is_python(k.0));
        ahead.after.retain(|k| !crate::python_source::PythonSources::is_python(k.0));
        let pf: std::collections::HashSet<(Ch, TileAt)> = ahead.with_visible.iter().chain(&ahead.after).copied().collect();
        fine.extend(ahead.with_visible.iter().copied());
        let order: HashMap<Ch, usize> = self.planner.active().iter().enumerate().map(|(i, c)| (*c, i)).collect();
        let tiers: Vec<((Ch, TileAt), f64)> = if self.planner.order != crate::plan::levels::Order::Cur {
            self.ordered(coarse, fine, &ahead, &pf, &order)
        } else {
        // (cur only: `legacy_order`, BLOCK01_A9_RUST_LEGACY_ORDER, has no effect under odon / hybrid)
        if self.cfg.legacy_order {
            fine.sort_by_key(|(c, t)| (order.get(c).copied(), t.level, t.ty, t.tx));
        } else if let Some(v) = self.planner.view() {
            let cy = (v.bbox_l0[0] + v.bbox_l0[2]) as f64 / 2.0;
            let cx = (v.bbox_l0[1] + v.bbox_l0[3]) as f64 / 2.0;
            let pyr = &self.pyr;
            let dist = |t: &TileAt| {
                let r = pyr.tile_world_rect(*t).unwrap_or([0.0; 4]);
                let (dx, dy) = ((r[0] + r[1]) / 2.0 - cx, (r[2] + r[3]) / 2.0 - cy);
                ((dx * dx + dy * dy).sqrt() * 1000.0) as u64
            };
            fine.sort_by_key(|(c, t)| (dist(t), t.ty, t.tx, order.get(c).copied()));
        }
        coarse.into_iter().map(|k| (k, 0.0)).chain(fine.into_iter().map(|k| (k, 1.0)))
            .chain(ahead.after.iter().map(|k| (*k, 2.0))).collect()
        };
        let mut jobs = Vec::with_capacity(tiers.len());
        let mut hits = Vec::new();
        let mut now_issued = std::collections::HashSet::with_capacity(tiers.len());
        let mut ahead_jobs = std::collections::HashSet::new();
        let mut now_ahead = std::collections::HashSet::new();
        let mut python_wants = std::collections::HashSet::new();
        {
            let mut cache = self.pool.shared().cache.lock().unwrap();
            for (key, fine) in tiers {
                if pf.contains(&key) {
                    // read-ahead: nothing to do when cached; adapted from
                    // Odon's 256 new requests per frame: here at most 256
                    // NEW read-ahead keys per issue (the visible ones are
                    // never cut)
                    let new = !self.issued_ahead.contains(&key);
                    if cache.contains(&key) || (self.planner.prefetch == crate::plan::prefetch::Mode::Odon
                                                 && new && *new_ahead >= self.planner.tune.cap) {
                        continue;
                    }
                    if new {
                        *new_ahead += 1;
                        // `ahead` survives the layer's tier mapping (fine 2 reads as "fine")
                        self.trace.stamp("req", Some(key), vec![("fine", 2.0), ("ahead", 1.0)]);
                    }
                    now_ahead.insert(key);
                    ahead_jobs.insert(key);
                    jobs.push(key);
                    continue;
                }
                // S0-B: a Python source's tile is the cache's or Python's to
                // make -- never the reader pool's
                if crate::python_source::PythonSources::is_python(key.0) {
                    match cache.get(&key) {
                        Some(tile) => hits.push((key, tile)),
                        None => { python_wants.insert(key); }
                    }
                    continue;
                }
                // a key newly the picture's (also one read ahead before) is a new "req"
                if !self.issued.contains(&key) {
                    self.trace.stamp("req", Some(key), vec![("fine", fine)]);
                }
                now_issued.insert(key);
                match cache.get(&key) {
                    Some(tile) => hits.push((key, tile)),
                    None => jobs.push(key),
                }
            }
        }
        self.issued = now_issued;
        self.issued_ahead = now_ahead;
        if self.demand.set_wants(python_wants) {
            if let Some(n) = &self.notifier {
                n.notify();                        // Python drains the new wants
            }
        }
        if self.planner.prefetch != crate::plan::prefetch::Mode::Off {
            // measurement evidence: the mode, the rings, why a ring was left out
            let mode = match self.planner.prefetch { crate::plan::prefetch::Mode::Odon => 1.0, _ => 2.0 };
            let cached = self.pool.shared().cache.lock().unwrap().bytes() as f64;
            self.trace.stamp("ahead", None, vec![("mode", mode), ("pad1", ahead.pad1_positions as f64),
                                                 ("with", ahead.with_visible.len() as f64),
                                                 ("after", ahead.after.len() as f64),
                                                 ("skip", ahead.skip_code as f64), ("cache_bytes", cached)]);
        }
        for (key, tile) in hits {
            self.deliver(key, tile);
        }
        (jobs, ahead_jobs, ahead.in_flight)
    }

    /// odon / hybrid's queue (R7c step 3 §2.1): ranked by (tier, distance
    /// from the view centre, position, channel); the second value is the
    /// trace's tier (0 coarse, 1 fine, 2 read-ahead after the visible).
    fn ordered(&mut self, coarse: Vec<(Ch, TileAt)>, fine: Vec<(Ch, TileAt)>, ahead: &crate::plan::prefetch::Ahead,
               pf: &std::collections::HashSet<(Ch, TileAt)>, order: &HashMap<Ch, usize>) -> Vec<((Ch, TileAt), f64)> {
        let coarsest = self.pyr.coarsest();
        let target = self.planner.admission().map_or(coarsest, |a| a.level);
        let near = self.planner.coarse_near();
        let after: std::collections::HashSet<(Ch, TileAt)> = ahead.after.iter().copied().collect();
        let (cy, cx) = self.planner.view().map_or((0.0, 0.0), |v| {
            ((v.bbox_l0[0] + v.bbox_l0[2]) as f64 / 2.0, (v.bbox_l0[1] + v.bbox_l0[3]) as f64 / 2.0)
        });
        let pyr = &self.pyr;
        let dist = |t: &TileAt| {
            let r = pyr.tile_world_rect(*t).unwrap_or([0.0; 4]);
            let (dx, dy) = ((r[0] + r[1]) / 2.0 - cx, (r[2] + r[3]) / 2.0 - cy);
            ((dx * dx + dy * dy).sqrt() * 1000.0) as u64
        };
        let tier = |k: &(Ch, TileAt)| -> u32 {
            let t = k.1;
            if after.contains(k) {
                60                                         // read-ahead after the visible
            } else if pf.contains(k) || t.level == target {
                0                                          // the target (and Odon's pad1)
            } else if t.level == coarsest {
                if near.contains(&(t.tx, t.ty)) { 50 } else { 70 }
            } else {
                1 + t.level.saturating_sub(target) as u32  // helper levels, finer first
            }
        };
        let mut all: Vec<((Ch, TileAt), f64)> = coarse.into_iter().map(|k| (k, 0.0))
            .chain(fine.into_iter().map(|k| (k, 1.0))).chain(ahead.after.iter().map(|k| (*k, 2.0))).collect();
        all.sort_by_key(|(k, _)| (tier(k), dist(&k.1), k.1.level, k.1.ty, k.1.tx, order.get(&k.0).copied()));
        let aux = all.iter().filter(|(k, _)| k.1.level != target && k.1.level != coarsest).count();
        self.trace.stamp("order", None, vec![("order", self.planner.order.code()), ("aux", aux as f64),
                                             ("target", target as f64)]);
        all
    }


    /// How many tiles the CPU cache holds for the drawn channels: the
    /// smallest of each channel's allowance (raw / corrected, less the
    /// chunk cache) over its tile size (Odon's cache-capacity test, adapted).
    fn cache_slots(&self) -> usize {
        let corrected = self.cfg.corrected_cache_bytes - self.cfg.chunk_cache_bytes.min(self.cfg.corrected_cache_bytes);
        self.planner.active().iter().filter_map(|&c| {
            let bytes = crate::geom::TILE * crate::geom::TILE * self.format_of(c)?.bytes();
            let allowance = match self.sources.of(c) {
                Some(crate::corrected::Source::Raw { .. }) => self.cfg.cpu_cache_bytes,
                _ => corrected,
            };
            Some(allowance / bytes.max(1))
        }).min().unwrap_or(usize::MAX)
    }
}
