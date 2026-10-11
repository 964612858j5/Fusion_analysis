//! The reader threads: read and decode the tiles the planner wants, put
//! them in the CPU cache and in a bounded result queue, and ask the GUI
//! for a redraw.
//!
//! The job list is replaced whole whenever the plan changes; a job taken
//! before that still finishes (its tile lands in the cache, nothing is
//! wasted). Workers hold no lock while reading or decoding. When the
//! result queue is full a worker waits (a target tile is never dropped);
//! `close()` wakes everyone and nobody waits for a read under way.

use crate::cache::{Cache, Key, Tile};
use crate::geom::{Pyramid, TILE};
use crate::notify::Notifier;
use crate::reader::{RawSource, Region};
use crate::trace::Trace;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};

pub const RESULT_CAPACITY: usize = 1024;

struct Queue {
    jobs: VecDeque<Key>,
    /// keys a worker is reading now: never queued a second time
    reading: std::collections::HashSet<Key>,
    results: VecDeque<(Key, Arc<Tile>)>,
    closed: bool,
}

pub struct Shared {
    queue: Mutex<Queue>,
    work: Condvar,
    room: Condvar,
    busy: AtomicUsize,
    pub reads: AtomicUsize,
    pub errors: AtomicUsize,
    pub last_error: Mutex<String>,
    source: Arc<RawSource>,
    pyr: Pyramid,
    /// the analysis region `(y0, y1, x0, x1)` in level-0 pixels
    roi: Option<[i64; 4]>,
    pub cache: Mutex<Cache>,
    notifier: Option<Arc<Notifier>>,
    trace: Arc<Trace>,
}

pub struct Pool {
    shared: Arc<Shared>,
}

impl Pool {
    pub fn start(source: Arc<RawSource>, pyr: Pyramid, roi: Option<[i64; 4]>, cache_bytes: usize,
                 threads: usize, notifier: Option<Arc<Notifier>>, trace: Arc<Trace>) -> Pool {
        let shared = Arc::new(Shared {
            queue: Mutex::new(Queue { jobs: VecDeque::new(), reading: Default::default(),
                                     results: VecDeque::new(), closed: false }),
            work: Condvar::new(), room: Condvar::new(), busy: AtomicUsize::new(0),
            reads: AtomicUsize::new(0), errors: AtomicUsize::new(0),
            last_error: Mutex::new(String::new()), source, pyr, roi,
            cache: Mutex::new(Cache::new(cache_bytes)), notifier, trace,
        });
        for i in 0..threads.clamp(1, 6) {
            let s = Arc::clone(&shared);
            std::thread::Builder::new().name(format!("fvc-read-{i}"))
                .spawn(move || worker(s)).expect("spawn reader");
        }
        Pool { shared }
    }

    pub fn shared(&self) -> &Arc<Shared> {
        &self.shared
    }

    /// Replace the jobs (the planner's current wants, in order).
    pub fn set_jobs(&self, jobs: Vec<Key>) {
        let mut q = self.shared.queue.lock().unwrap();
        q.jobs = jobs.into_iter().filter(|k| !q.reading.contains(k)).collect();
        drop(q);
        self.shared.work.notify_all();
    }

    /// Every result in hand now, without waiting.
    pub fn take_results(&self, max: usize) -> Vec<(Key, Arc<Tile>)> {
        let mut q = self.shared.queue.lock().unwrap();
        let n = q.results.len().min(max);
        let out: Vec<_> = q.results.drain(..n).collect();
        drop(q);
        if n > 0 {
            self.shared.room.notify_all();
        }
        out
    }

    pub fn pending_results(&self) -> usize {
        self.shared.queue.lock().unwrap().results.len()
    }

    /// No job queued and no read under way.
    pub fn idle(&self) -> bool {
        self.shared.queue.lock().unwrap().jobs.is_empty() && self.shared.busy.load(Ordering::SeqCst) == 0
    }

    /// Stop: workers exit after the tile in hand; nobody waits for them.
    pub fn close(&self) {
        let mut q = self.shared.queue.lock().unwrap();
        q.closed = true;
        q.jobs.clear();
        drop(q);
        self.shared.work.notify_all();
        self.shared.room.notify_all();
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        self.close();
    }
}

fn worker(s: Arc<Shared>) {
    loop {
        let key = {
            let mut q = s.queue.lock().unwrap();
            loop {
                if q.closed {
                    return;
                }
                if let Some(k) = q.jobs.pop_front() {
                    s.busy.fetch_add(1, Ordering::SeqCst);
                    q.reading.insert(k);
                    break k;
                }
                q = s.work.wait(q).unwrap();
            }
        };
        s.trace.stamp("start", Some(key), vec![]);
        let cached = s.cache.lock().unwrap().get(&key);
        let tile = match cached {
            Some(t) => Some(t),
            None => {
                let (w0, c0) = (crate::clock::monotonic(), crate::clock::thread_cpu());
                let got = read_tile(&s, key);
                let timing = |err: bool| vec![
                    ("wall_ms", (crate::clock::monotonic() - w0) * 1000.0),
                    ("cpu_ms", (crate::clock::thread_cpu() - c0) * 1000.0),
                    ("err", err as i32 as f64)];
                match got {
                    Ok(t) => {
                        let t = Arc::new(t);
                        s.reads.fetch_add(1, Ordering::Relaxed);
                        s.cache.lock().unwrap().insert(key, Arc::clone(&t));
                        s.trace.stamp("read", Some(key), timing(false));
                        Some(t)
                    }
                    Err(e) => {
                        s.trace.stamp("read", Some(key), timing(true));
                        s.errors.fetch_add(1, Ordering::Relaxed);
                        *s.last_error.lock().unwrap() = format!("{key:?}: {e}");
                        None
                    }
                }
            }
        };
        if let Some(t) = tile {
            let mut q = s.queue.lock().unwrap();
            while q.results.len() >= RESULT_CAPACITY && !q.closed {
                q = s.room.wait(q).unwrap();
            }
            if !q.closed {
                q.results.push_back((key, t));
            }
            q.reading.remove(&key);
            drop(q);
            s.trace.stamp("done", Some(key), vec![]);
            if let Some(n) = &s.notifier {
                n.notify();
            }
        }
        s.queue.lock().unwrap().reading.remove(&key);
        s.busy.fetch_sub(1, Ordering::SeqCst);
    }
}

/// `Step1TileProvider.read_tile_native` for a raw uint8 channel: the tile
/// cut at the level's edge, clipped to the region in that level's pixels
/// (`Step1SourceTable.clip_to_roi`), zero outside the clip.
pub fn read_tile(s: &Shared, (c, t): Key) -> crate::error::Result<Tile> {
    let (rows, cols) = s.pyr.tile_shape(t);
    let (y0, x0) = (t.ty as i64 * TILE as i64, t.tx as i64 * TILE as i64);
    let rect = [y0, y0 + rows as i64, x0, x0 + cols as i64];
    let mut data = vec![0u8; rows * cols];
    let Some([cy0, cy1, cx0, cx1]) = clip_to_roi(rect, s.roi, s.pyr.ds_planning(t.level) as i64)
    else {
        return Ok(Tile { rows, cols, data, valid: None });
    };
    let px = s.source.read_region(&Region { channel: c as usize, level: t.level as usize,
        y0: cy0 as usize, y1: cy1 as usize, x0: cx0 as usize, x1: cx1 as usize })?;
    let w = (cx1 - cx0) as usize;
    for (i, row) in px.chunks_exact(w.max(1)).enumerate() {
        let at = ((cy0 - y0) as usize + i) * cols + (cx0 - x0) as usize;
        data[at..at + w].copy_from_slice(row);
    }
    let valid = [(cy0 - y0) as u32, (cy1 - y0) as u32, (cx0 - x0) as u32, (cx1 - x0) as u32];
    Ok(Tile { rows, cols, data, valid: Some(valid) })
}

/// `rect` = `(y0, y1, x0, x1)` in level pixels cut to the region, taken
/// outward into level pixels with `stride` (floor start, ceil end).
pub fn clip_to_roi(rect: [i64; 4], roi: Option<[i64; 4]>, stride: i64) -> Option<[i64; 4]> {
    let [y0, y1, x0, x1] = rect;
    let Some([mut by0, mut by1, mut bx0, mut bx1]) = roi else {
        return if y1 > y0 && x1 > x0 { Some(rect) } else { None };
    };
    let s = stride.max(1);
    if s > 1 {
        by0 = by0.div_euclid(s);
        bx0 = bx0.div_euclid(s);
        by1 = -((-by1).div_euclid(s));
        bx1 = -((-bx1).div_euclid(s));
    }
    let (cy0, cy1, cx0, cx1) = (y0.max(by0), y1.min(by1), x0.max(bx0), x1.min(bx1));
    if cy1 <= cy0 || cx1 <= cx0 { None } else { Some([cy0, cy1, cx0, cx1]) }
}

impl Shared {
    pub fn pyr(&self) -> &Pyramid {
        &self.pyr
    }
}

