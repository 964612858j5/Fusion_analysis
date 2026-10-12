//! The reader threads: read and decode the tiles the planner wants, put
//! them in the CPU cache and in a bounded result queue, and ask the GUI
//! for a redraw.
//!
//! The job list is replaced whole whenever the plan changes. A job taken
//! before that still finishes reading, but a tile the plan no longer wants
//! is then dropped -- neither cached nor handed over (plan R1 §2.6; not an
//! error, counted in `stale`). Workers hold no lock while reading or
//! decoding. When the result queue is full -- `RESULT_CAPACITY` tiles or
//! `result_bytes` bytes, which the CPU allowance reserves out of the
//! cache's share -- a worker waits (a wanted tile is never dropped);
//! `close()` wakes everyone, a waiting worker exits at once, and nobody
//! waits for a read under way. At most min(6, cores) readers.

use crate::cache::{Cache, Key, Tile};
use crate::geom::{Pyramid, TILE};
use crate::notify::Notifier;
use crate::reader::RawSource;
use crate::trace::Trace;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};

pub const RESULT_CAPACITY: usize = 1024;
/// default byte bound of the result queue (tiles waiting for the GUI)
pub const RESULT_BYTES: usize = 128 * 1024 * 1024;

struct Queue {
    jobs: VecDeque<Key>,
    /// every key of the latest plan, including those being read
    wanted: std::collections::HashSet<Key>,
    result_bytes: usize,
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
    /// tiles read, then dropped because the plan had moved on
    pub stale: AtomicUsize,
    pub errors: AtomicUsize,
    max_result_bytes: usize,
    /// test only: each read starts this much later (a slow disk), so a
    /// test can move the plan while reads are under way
    read_delay: std::time::Duration,
    pub last_error: Mutex<String>,
    source: Arc<RawSource>,
    pyr: Pyramid,
    /// per channel: raw / corrected / missing, and the analysis region
    sources: Arc<crate::corrected::Step1Sources>,
    raw_kind: crate::step1_tile::RawKind,
    /// the corrected reduction's slab budget (level-0 pixels)
    budget: usize,
    pub cache: Mutex<Cache>,
    notifier: Option<Arc<Notifier>>,
    trace: Arc<Trace>,
}

pub struct Pool {
    shared: Arc<Shared>,
}

impl Pool {
    #[allow(clippy::too_many_arguments)]
    pub fn start(source: Arc<RawSource>, pyr: Pyramid, sources: Arc<crate::corrected::Step1Sources>,
                 raw_kind: crate::step1_tile::RawKind, budget: usize, cache_bytes: usize,
                 threads: usize, result_bytes: usize, read_delay_ms: f64,
                 notifier: Option<Arc<Notifier>>, trace: Arc<Trace>) -> Pool {
        // the CPU allowance covers the cache AND the tiles waiting in the
        // result queue (they may outlive their cache entry); the queue may
        // take at most half of it. An empty queue still takes one tile
        // larger than its bound (a wanted tile is never refused), so the
        // queue holds at most `result_bytes` plus one tile.
        let result_bytes = result_bytes.min(cache_bytes / 2).max(1);
        let cache_bytes = cache_bytes.saturating_sub(result_bytes);
        let shared = Arc::new(Shared {
            queue: Mutex::new(Queue { jobs: VecDeque::new(), wanted: Default::default(), result_bytes: 0,
                                     reading: Default::default(), results: VecDeque::new(), closed: false }),
            work: Condvar::new(), room: Condvar::new(), busy: AtomicUsize::new(0),
            reads: AtomicUsize::new(0), stale: AtomicUsize::new(0), errors: AtomicUsize::new(0),
            max_result_bytes: result_bytes,
            read_delay: std::time::Duration::from_secs_f64(read_delay_ms.max(0.0) / 1000.0),
            last_error: Mutex::new(String::new()), source, pyr, sources, raw_kind, budget,
            cache: Mutex::new(Cache::new(cache_bytes)), notifier, trace,
        });
        let cores = std::thread::available_parallelism().map_or(1, |n| n.get());
        for i in 0..threads.clamp(1, 6).min(cores) {
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
        q.wanted = jobs.iter().copied().collect();
        q.jobs = jobs.into_iter().filter(|k| !q.reading.contains(k)).collect();
        drop(q);
        self.shared.work.notify_all();
        self.shared.room.notify_all();          // a worker waiting with a now-stale tile
    }

    /// Every result in hand now, without waiting.
    pub fn take_results(&self, max: usize) -> Vec<(Key, Arc<Tile>)> {
        let mut q = self.shared.queue.lock().unwrap();
        let n = q.results.len().min(max);
        let out: Vec<_> = q.results.drain(..n).collect();
        q.result_bytes -= out.iter().map(|(_, t)| t.data.len()).sum::<usize>();
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
        q.wanted.clear();
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
                if !s.read_delay.is_zero() {
                    std::thread::sleep(s.read_delay);
                }
                let (w0, c0) = (crate::clock::monotonic(), crate::clock::thread_cpu());
                let still = || s.queue.lock().unwrap().wanted.contains(&key);
                let got = read_tile(&s, key, &still);
                let timing = |err: bool, stale: bool| vec![
                    ("wall_ms", (crate::clock::monotonic() - w0) * 1000.0),
                    ("cpu_ms", (crate::clock::thread_cpu() - c0) * 1000.0),
                    ("err", err as i32 as f64), ("stale", stale as i32 as f64)];
                match got {
                    Ok(None) => {                    // cancelled: the plan left it
                        s.queue.lock().unwrap().reading.remove(&key);
                        s.trace.stamp("read", Some(key), timing(false, true));
                        s.stale.fetch_add(1, Ordering::Relaxed);
                        None
                    }
                    Ok(Some(t)) => {
                        let t = Arc::new(t);
                        s.reads.fetch_add(1, Ordering::Relaxed);
                        // still wanted? Decided, and the tile cached or the
                        // key released, under ONE hold of the queue lock: a
                        // plan that wants the key again after this sees it
                        // no longer in flight and queues it afresh
                        let mut q = s.queue.lock().unwrap();
                        let stale = !q.wanted.contains(&key);
                        if stale {
                            q.reading.remove(&key);
                        } else {
                            s.cache.lock().unwrap().insert(key, Arc::clone(&t));
                        }
                        drop(q);
                        s.trace.stamp("read", Some(key), timing(false, stale));
                        if stale {
                            s.stale.fetch_add(1, Ordering::Relaxed);
                            None
                        } else {
                            Some(t)
                        }
                    }
                    Err(e) => {
                        s.queue.lock().unwrap().reading.remove(&key);
                        s.trace.stamp("read", Some(key), timing(true, false));
                        s.errors.fetch_add(1, Ordering::Relaxed);
                        *s.last_error.lock().unwrap() = format!("{key:?}: {e}");
                        None
                    }
                }
            }
        };
        if let Some(t) = tile {
            let bytes = t.data.len();
            let mut q = s.queue.lock().unwrap();
            while !q.closed && q.wanted.contains(&key) && !q.results.is_empty()
                && (q.results.len() >= RESULT_CAPACITY || q.result_bytes + bytes > s.max_result_bytes) {
                q = s.room.wait(q).unwrap();
            }
            let deliver = !q.closed && q.wanted.contains(&key);
            if deliver {
                q.results.push_back((key, t));
                q.result_bytes += bytes;
            } else if !q.closed {
                s.stale.fetch_add(1, Ordering::Relaxed);
            }
            q.reading.remove(&key);
            drop(q);
            if deliver {
                s.trace.stamp("done", Some(key), vec![]);
                if let Some(n) = &s.notifier {
                    n.notify();
                }
            }
        }
        // every path above released the key from `reading` exactly once
        // (stale, error, or after the hand-over decision): a second
        // release here could erase the mark of a worker that has started
        // the same key again meanwhile
        s.busy.fetch_sub(1, Ordering::SeqCst);
    }
}

/// `Step1TileProvider.read_tile_native` for one key (`step1_tile::read`):
/// the tile cut at the level's edge, in its own format with its validity.
/// None when `wanted` turned false during a long (corrected) read.
pub fn read_tile(s: &Shared, (c, t): Key, wanted: &dyn Fn() -> bool)
                 -> crate::error::Result<Option<Tile>> {
    use crate::error::CoreError;
    use crate::step1_tile::{Valid, Values};
    let (rows, cols) = s.pyr.tile_shape(t);
    let (y0, x0) = (t.ty as i64 * TILE as i64, t.tx as i64 * TILE as i64);
    let rect = [y0, y0 + rows as i64, x0, x0 + cols as i64];
    let source = s.sources.of(c).ok_or_else(|| CoreError::Bounds(format!("channel {c} has no source")))?;
    if let crate::corrected::Source::Missing { reason } = source {
        return Err(CoreError::Bounds(format!("channel {c} is missing: {reason}")));
    }
    let Some(read) = crate::step1_tile::read(source, &s.source, s.raw_kind, s.sources.roi, t.level as usize, rect,
                                             s.pyr.ds_planning(t.level) as usize, TILE, s.budget, wanted)?
    else { return Ok(None) };
    let (format, data) = match read.values {
        Values::U8(v) => (crate::family::Format::U8, v),
        Values::U16(v) => (crate::family::Format::U16, v.iter().flat_map(|x| x.to_ne_bytes()).collect()),
        Values::F32(v) => (crate::family::Format::F32, v.iter().flat_map(|x| x.to_ne_bytes()).collect()),
    };
    let valid = match read.valid {
        Valid::Nothing => None,
        Valid::Rect(r) => Some([r[0] as u32, r[1] as u32, r[2] as u32, r[3] as u32]),
        Valid::Finite => Some([0, read.rows as u32, 0, read.cols as u32]),
    };
    Ok(Some(Tile { rows: read.rows, cols: read.cols, format, data, valid }))
}

impl Shared {
    pub fn pyr(&self) -> &Pyramid {
        &self.pyr
    }
}

