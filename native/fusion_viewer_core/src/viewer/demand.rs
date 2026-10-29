//! S0-B (docs/v17_step0_engine_plan.md §11.3): the tiles of Python sources
//! the planner wants and the cache does not hold, handed to Python instead
//! of the reader pool, and what comes back -- a computed tile, a failure.
//!
//! `Demand` is pure state: the wants of the last issue, the keys handed to
//! Python and not yet answered, and the failures (a backoff of `RETRY_S`,
//! given up after `KEY_LIMIT` failures of a key or `RUN_LIMIT` in a row --
//! the reader pool's numbers, kept here because these keys never reach the
//! pool). The `ViewerCore` methods below wire it to the planner and cache.

use super::failures::{ReadCheck, RUN_LIMIT};
use super::ViewerCore;
use crate::cache::{Key, Tile};
use crate::clock::monotonic;
use crate::geom::TileAt;
use crate::plan::Ch;
use crate::pool::{KEY_LIMIT, RETRY_S};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

#[derive(Default)]
pub struct Demand {
    /// what the last issue wanted of the Python sources (not in the cache)
    wants: HashSet<Key>,
    /// handed to Python by `drain` and not yet completed, failed or withdrawn
    handed: HashSet<Key>,
    /// per key: failures since its last success, and when the latest was
    failures: HashMap<Key, (u32, f64)>,
    run: u32,
    longest_run: u32,
    pub completed: u64,
    pub late_dropped: u64,
    pub rejected: u64,
}

impl Demand {
    /// The wants of this issue. True when one of them is new to Python
    /// (the GUI is then woken to drain).
    pub fn set_wants(&mut self, wants: HashSet<Key>) -> bool {
        let fresh = wants.iter().any(|k| !self.handed.contains(k));
        self.wants = wants;
        fresh
    }

    fn eligible(&self, key: &Key, now: f64) -> bool {
        match self.failures.get(key) {
            None => true,
            Some((n, at)) => *n < KEY_LIMIT && now - at >= RETRY_S,
        }
    }

    /// `(new, withdrawn)`: new = wanted, not handed, not backing off;
    /// withdrawn = handed, no longer wanted. Both sorted.
    pub fn drain(&mut self, now: f64) -> (Vec<Key>, Vec<Key>) {
        let mut new: Vec<Key> = self.wants.iter()
            .filter(|k| !self.handed.contains(k) && self.eligible(k, now)).copied().collect();
        let mut withdrawn: Vec<Key> = self.handed.iter().filter(|k| !self.wants.contains(k)).copied().collect();
        new.sort();
        withdrawn.sort();
        for k in &withdrawn {
            self.handed.remove(k);
        }
        self.handed.extend(new.iter().copied());
        (new, withdrawn)
    }

    /// An answer for `key` (a tile or a failure) ends its hand-over.
    fn answered(&mut self, key: &Key) {
        self.handed.remove(key);
    }

    /// The tile is in the cache now: no longer a want until the next issue
    /// asks for it again (it would, were it evicted) -- else the next drain
    /// would hand it to Python once more, and again after that.
    pub fn succeeded(&mut self, key: &Key) {
        self.answered(key);
        self.wants.remove(key);
        self.failures.remove(key);
        self.run = 0;
        self.completed += 1;
    }

    pub fn failed(&mut self, key: &Key, now: f64) {
        self.answered(key);
        let e = self.failures.entry(*key).or_insert((0, now));
        e.0 += 1;
        e.1 = now;
        self.run += 1;
        self.longest_run = self.longest_run.max(self.run);
    }

    /// Everything of a retired source goes.
    pub fn drop_channel(&mut self, c: Ch) {
        self.wants.retain(|k| k.0 != c);
        self.handed.retain(|k| k.0 != c);
        self.failures.retain(|k, _| k.0 != c);
    }

    pub fn holds_channel(&self, c: Ch) -> bool {
        self.wants.iter().chain(&self.handed).chain(self.failures.keys()).any(|k| k.0 == c)
    }

    pub fn counts(&self) -> (usize, usize, usize) {
        (self.wants.len(), self.handed.len(), self.failures.len())
    }

    /// The pool's rules for the Python keys still wanted: given up, or a
    /// retry due in so many ms.
    pub fn check(&self, now: f64) -> ReadCheck {
        if self.longest_run >= RUN_LIMIT {
            return ReadCheck { give_up: Some(format!("{} Python tiles in a row failed", self.longest_run)),
                               retry_ms: None };
        }
        let wanted: Vec<(&Key, &(u32, f64))> = self.failures.iter().filter(|(k, _)| self.wants.contains(k)).collect();
        if let Some((k, (n, _))) = wanted.iter().find(|(_, (n, _))| *n >= KEY_LIMIT) {
            return ReadCheck { give_up: Some(format!("Python tile {k:?} failed {n} times")), retry_ms: None };
        }
        let left = wanted.iter().filter(|(k, _)| !self.handed.contains(k))
            .map(|(_, (_, at))| RETRY_S - (now - at)).filter(|l| *l > 0.0).reduce(f64::min);
        ReadCheck { give_up: None, retry_ms: left.map(|l| l * 1000.0) }
    }
}

impl ViewerCore {
    /// Register a Python source (§11.3): float32, corrected allowance.
    pub fn add_python_source(&mut self, c: Ch) -> Result<(), String> {
        self.python.add(c)?;
        self.pool.shared().cache.lock().unwrap().add_corrected(c);
        Ok(())
    }

    /// Retire a Python source: every trace of it goes -- planner, held
    /// tiles, wants and failures, CPU cache, GPU slots (bookkeeping only,
    /// no GL: blocks stay) and its page layer. Its rows leave the display.
    pub fn drop_python_source(&mut self, c: Ch) -> bool {
        if !self.python.remove(c) {
            return false;
        }
        self.display.retain(|(d, _)| *d != c);
        self.planner.drop_channel(c);
        self.held.retain(|k, _| k.0 != c);
        self.issued.retain(|k| k.0 != c);
        self.issued_ahead.retain(|k| k.0 != c);
        self.demand.drop_channel(c);
        self.pool.shared().cache.lock().unwrap().remove_channel(c);
        if let Some(g) = self.gpu.as_mut() {
            g.store.evict_channel(c);
            g.vt.release_channel(c);
        }
        self.update_total();
        true
    }

    /// Does the core hold anything of `c` (tests: a retirement is complete)?
    pub fn holds_python_source(&self, c: Ch) -> bool {
        self.python.is_live(c) || self.planner.holds_channel(c) || self.held.keys().any(|k| k.0 == c)
            || self.demand.holds_channel(c) || self.display.iter().any(|(d, _)| *d == c)
            || self.gpu.as_ref().is_some_and(|g| g.store.slots.keys().any(|k| k.0 == c)
                                              || g.vt.channel_slots.contains_key(&c))
    }

    /// `(new, withdrawn)` tiles of the Python sources since the last drain.
    pub fn drain_demands(&mut self) -> (Vec<Key>, Vec<Key>) {
        self.demand.drain(monotonic())
    }

    /// A computed tile (§11.3): float32 native-endian bytes of the tile's
    /// real shape. Returns whether the planner took it (a repaint is then
    /// due). A retired source's tile is dropped; a wrong shape is a failure.
    pub fn complete_python_tile(&mut self, c: Ch, t: TileAt, data: Vec<u8>) -> bool {
        let key = (c, t);
        if !self.python.is_live(c) {
            self.demand.late_dropped += 1;
            return false;
        }
        let (rows, cols) = self.pyr.tile_shape(t);
        if rows == 0 || data.len() != rows * cols * 4 {
            self.demand.rejected += 1;
            self.demand.failed(&key, monotonic());
            return false;
        }
        let tile = Arc::new(Tile { rows, cols, format: crate::family::Format::F32, data,
                                   valid: Some([0, rows as u32, 0, cols as u32]) });
        self.pool.shared().cache.lock().unwrap().insert(key, Arc::clone(&tile));
        self.demand.succeeded(&key);
        let ok = self.deliver(key, tile);
        // paused (Step0 left, a standby): cached and delivered, but no new
        // read starts -- `resume` issues again (codex S0-B code review 3)
        if ok && !self.paused && self.planner.take_reissue() {
            self.issue();
        }
        ok
    }

    /// Python could not compute the tile: backoff, then retried while wanted.
    pub fn fail_python_tile(&mut self, c: Ch, t: TileAt) {
        if self.python.is_live(c) {
            self.demand.failed(&(c, t), monotonic());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(c: Ch, tx: u32) -> Key {
        (c, TileAt { level: 0, tx, ty: 0 })
    }

    #[test]
    fn a_drain_hands_over_new_wants_once_and_withdraws_what_is_no_longer_wanted() {
        let mut d = Demand::default();
        assert!(d.set_wants([k(0x8000, 0), k(0x8000, 1)].into()));
        assert_eq!(d.drain(0.0), (vec![k(0x8000, 0), k(0x8000, 1)], vec![]));
        assert!(!d.set_wants([k(0x8000, 1), k(0x8000, 0)].into()), "nothing new to wake for");
        assert_eq!(d.drain(0.0), (vec![], vec![]));
        assert!(d.set_wants([k(0x8000, 1), k(0x8000, 2)].into()));
        assert_eq!(d.drain(0.0), (vec![k(0x8000, 2)], vec![k(0x8000, 0)]));
        d.succeeded(&k(0x8000, 1));
        assert_eq!(d.drain(0.0), (vec![], vec![]), "a completed tile is never handed again by itself");
        assert!(d.set_wants([k(0x8000, 1), k(0x8000, 2)].into()), "evicted, wanted again by an issue");
        assert_eq!(d.drain(0.0), (vec![k(0x8000, 1)], vec![]));
    }

    #[test]
    fn a_failure_backs_off_then_retries_and_gives_up_at_the_limit() {
        let mut d = Demand::default();
        d.set_wants([k(0x8000, 0)].into());
        d.drain(0.0);
        d.failed(&k(0x8000, 0), 1.0);
        assert_eq!(d.drain(1.1), (vec![], vec![]), "inside the backoff");
        assert!(d.check(1.1).retry_ms.is_some_and(|ms| ms > 0.0));
        assert_eq!(d.drain(1.3).0, vec![k(0x8000, 0)]);
        d.failed(&k(0x8000, 0), 1.3);
        d.drain(1.6);
        d.failed(&k(0x8000, 0), 1.6);
        assert!(d.check(2.0).give_up.is_some());
        assert_eq!(d.drain(9.0).0, vec![], "given up: never handed again");
    }

    #[test]
    fn a_success_clears_the_key_and_the_run() {
        let mut d = Demand::default();
        d.set_wants([k(0x8000, 0)].into());
        d.drain(0.0);
        d.failed(&k(0x8000, 0), 0.0);
        d.drain(1.0);
        d.succeeded(&k(0x8000, 0));
        assert_eq!(d.check(1.0), ReadCheck::default());
        assert_eq!(d.counts().2, 0);
    }

    #[test]
    fn a_dropped_channel_leaves_nothing() {
        let mut d = Demand::default();
        d.set_wants([k(0x8000, 0), k(0x8001, 0)].into());
        d.drain(0.0);
        d.failed(&k(0x8000, 0), 0.0);
        d.drop_channel(0x8000);
        assert!(!d.holds_channel(0x8000) && d.holds_channel(0x8001));
    }
}
