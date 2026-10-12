//! Reads that keep failing (R3 plan §2.5). A failed tile is not queued
//! again by anything until `RETRY_S` after its failure (`Pool::set_jobs`,
//! under the queue lock); then, while the
//! picture still needs it, the paint asks again -- so a still view retries
//! too -- up to `KEY_LIMIT` reads in all. The reads are given up when a
//! tile the picture needs has failed `KEY_LIMIT` times, or `RUN_LIMIT`
//! reads in a row failed (a run that reached it counts even if a success
//! came before this check). Any success clears its tile's count and the
//! current run (`pool::worker`). Checked by each paint.

use super::ViewerCore;
use crate::cache::Key;
use crate::clock::monotonic;

pub use crate::pool::{KEY_LIMIT, RETRY_S};
pub const RUN_LIMIT: u32 = 8;

/// What the paint does about failed reads.
#[derive(Debug, Default, PartialEq)]
pub struct ReadCheck {
    /// the reads are given up: why (the binding falls back)
    pub give_up: Option<String>,
    /// a retry is due in this many ms (paint again then)
    pub retry_ms: Option<f64>,
}

impl ViewerCore {
    pub fn check_reads(&mut self) -> ReadCheck {
        let shared = self.pool.shared().clone();
        let (run, needed) = {
            let f = shared.failures.lock().unwrap();
            let needed: Vec<(Key, u32, f64)> = f.per_key.iter().filter(|(k, _)| self.issued.contains(k))
                .map(|(k, (n, at))| (*k, *n, *at)).collect();
            (f.longest_run, needed)
        };
        let why = || shared.last_error.lock().unwrap().clone();
        if run >= RUN_LIMIT {
            return ReadCheck { give_up: Some(format!("{run} reads in a row failed: {}", why())), retry_ms: None };
        }
        if let Some((key, n, _)) = needed.iter().find(|(_, n, _)| *n >= KEY_LIMIT) {
            return ReadCheck { give_up: Some(format!("tile {key:?} failed {n} times: {}", why())), retry_ms: None };
        }
        // the queue first, then the cache: never both locks at once
        let idle: Vec<(Key, f64)> = needed.iter().filter(|(k, _, _)| !self.pool.queued_or_reading(k))
            .map(|(k, _, at)| (*k, *at)).collect();
        let waiting: Vec<f64> = {
            let cache = shared.cache.lock().unwrap();
            idle.iter().filter(|(k, _)| !cache.contains(k)).map(|(_, at)| *at).collect()
        };
        let Some(earliest) = waiting.iter().copied().reduce(f64::min) else { return ReadCheck::default() };
        let left = RETRY_S - (monotonic() - earliest);
        if left > 0.0 {
            return ReadCheck { give_up: None, retry_ms: Some(left * 1000.0) };
        }
        self.issue();                                  // the plan's wants, the due ones again
        ReadCheck::default()
    }
}
