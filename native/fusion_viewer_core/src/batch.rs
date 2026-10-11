//! Many region reads on a fixed number of threads.
//!
//! Workers take the next job from a shared atomic index and decode with
//! no lock held. Used by the probes for byte comparison and throughput;
//! the product scheduler (request sets, cancellation, bounded result
//! queue) is a later, separate module.

use crate::clock::monotonic;
use crate::error::Result;
use crate::reader::{RawSource, Region};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// Per job: start and end on the `time.monotonic()` base, and the bytes.
pub struct Timed<T> {
    pub start: f64,
    pub end: f64,
    pub value: Result<T>,
}

/// Run `f` on every region with `threads` workers; results keep job order.
pub fn run<T: Send>(source: &RawSource, jobs: &[Region], threads: usize,
                    f: impl Fn(&RawSource, &Region) -> Result<T> + Sync) -> Vec<Timed<T>> {
    let next = AtomicUsize::new(0);
    let slots: Vec<Mutex<Option<Timed<T>>>> = jobs.iter().map(|_| Mutex::new(None)).collect();
    std::thread::scope(|scope| {
        for _ in 0..threads.max(1) {
            scope.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                let Some(job) = jobs.get(i) else { break };
                let start = monotonic();
                let value = f(source, job);
                *slots[i].lock().unwrap() = Some(Timed { start, end: monotonic(), value });
            });
        }
    });
    slots.into_iter().map(|s| s.into_inner().unwrap().unwrap()).collect()
}
