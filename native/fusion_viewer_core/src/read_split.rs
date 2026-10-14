//! R7a (docs/v16_5_r7a_timing_plan.md §2), measurement only: where one
//! tile read's time goes, per reader thread. Off unless a viewer was built
//! with the trace and `Config::draw_log` on (`set_on`); then each stage
//! adds its wall and thread-CPU time to this thread's tally, and the worker
//! takes the tally into the tile's `read` record. Stages:
//!   file    reading the stored bytes (`std::fs::read` / `read_exact_at`)
//!   lz4     Blosc's LZ4 streams          unshuffle  Blosc's byte unshuffle
//!   tiffdec a TIFF block's decompression copy       a block's overlap copied out
//!   pack    the tile's values built from what was read
//! `wall - cpu` also holds time the thread was not scheduled, not only I/O.

use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, Ordering};

static ON: AtomicBool = AtomicBool::new(false);
const STAGES: [&str; 6] = ["file", "lz4", "unshuffle", "tiffdec", "copy", "pack"];

#[derive(Default, Clone, Copy)]
struct Tally {
    wall: [f64; 6],
    cpu: [f64; 6],
    bytes: f64,
    chunks: f64,
    /// where the read went: 1 a zarr chunk (corrected), 2 a TIFF block
    src: f64,
}

thread_local! {
    static TALLY: RefCell<Tally> = RefCell::new(Tally::default());
}

pub fn set_on(on: bool) {
    if on {
        ON.store(true, Ordering::Relaxed);
    }
}

pub fn on() -> bool {
    ON.load(Ordering::Relaxed)
}

/// A stage being timed; adds itself to the thread's tally when dropped.
pub struct Span {
    stage: usize,
    t: f64,
    c: f64,
}

pub fn span(stage: &str) -> Option<Span> {
    if !on() {
        return None;
    }
    let i = STAGES.iter().position(|s| *s == stage)?;
    Some(Span { stage: i, t: crate::clock::monotonic(), c: crate::clock::thread_cpu() })
}

impl Drop for Span {
    fn drop(&mut self) {
        let (w, c) = (crate::clock::monotonic() - self.t, crate::clock::thread_cpu() - self.c);
        TALLY.with(|t| {
            let mut t = t.borrow_mut();
            t.wall[self.stage] += w * 1000.0;
            t.cpu[self.stage] += c * 1000.0;
        });
    }
}

pub const SRC_ZARR: u8 = 1;
pub const SRC_TIFF: u8 = 2;

/// Bytes read from storage and stored chunks/blocks decoded, and from
/// which kind of source (`SRC_*`; the last one counted wins).
pub fn count(bytes: usize, chunks: usize, src: u8) {
    if on() {
        TALLY.with(|t| {
            let mut t = t.borrow_mut();
            t.bytes += bytes as f64;
            t.chunks += chunks as f64;
            t.src = src as f64;
        });
    }
}

pub fn reset() {
    if on() {
        TALLY.with(|t| *t.borrow_mut() = Tally::default());
    }
}

/// The tally as record fields (`<stage>_ms`, `<stage>_cpu`, bytes,
/// chunks, src, and `rest_ms` = `wall_ms` minus the classified stages).
pub fn take(wall_ms: f64) -> Vec<(&'static str, f64)> {
    if !on() {
        return Vec::new();
    }
    let t = TALLY.with(|t| std::mem::take(&mut *t.borrow_mut()));
    const WALL: [&str; 6] = ["file_ms", "lz4_ms", "unshuffle_ms", "tiffdec_ms", "copy_ms", "pack_ms"];
    const CPU: [&str; 6] = ["file_cpu", "lz4_cpu", "unshuffle_cpu", "tiffdec_cpu", "copy_cpu", "pack_cpu"];
    let mut out = Vec::with_capacity(16);
    for i in 0..6 {
        out.push((WALL[i], t.wall[i]));
        out.push((CPU[i], t.cpu[i]));
    }
    out.push(("bytes", t.bytes));
    out.push(("chunks", t.chunks));
    out.push(("src", t.src));
    out.push(("rest_ms", wall_ms - t.wall.iter().sum::<f64>()));
    out
}
