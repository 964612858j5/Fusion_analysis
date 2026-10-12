//! The reader threads: read and decode the tiles the planner wants, put
//! them in the CPU cache and in a bounded result queue, and ask the GUI
//! for a redraw.
//!
//! The job list is replaced whole whenever the plan changes. A job taken
//! before that still finishes reading, but a tile the plan no longer wants
//! is then dropped -- neither cached nor handed over (plan R1 §2.6; not an
//! error, counted in `stale`). Workers hold no lock while reading or
//! decoding. When the result queue is full -- `RESULT_CAPACITY` tiles or
//! `result_bytes` bytes, an allowance of its own beside the cache's two
//! (R3 plan §2.3) -- a worker waits (a wanted tile is never dropped);
//! `close()` wakes everyone, a waiting worker exits at once, and nobody
//! waits for a read under way. At most min(6, cores) readers.

use crate::cache::{Cache, Key, Tile};
use crate::geom::Pyramid;
use crate::notify::Notifier;
use crate::reader::RawSource;
use crate::trace::Trace;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};

mod worker;
pub use worker::read_tile;

pub const RESULT_CAPACITY: usize = 1024;
/// default byte bound of the result queue (tiles waiting for the GUI)
pub const RESULT_BYTES: usize = 256 * 1024 * 1024;
/// what one reader may hold while it works (R3 plan §2.3): a corrected
/// reduction's slab (`MAX_REDUCTION_ELEMENTS` x 4 B = 16 MiB), one decoded
/// chunk (4 MiB), the output block's sums and counts (~4.2 MiB) and the
/// result tile (1 MiB), rounded up. Change it with the slab size.
pub const WORKER_MEMORY_BYTES: usize = 32 * 1024 * 1024;

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
    /// read failures, for the runtime fallback (`viewer::failures`)
    pub failures: Mutex<Failures>,
    faults: Arc<crate::fault::Faults>,
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

/// A failed tile is not queued again before this long after its failure,
/// nor at all after `KEY_LIMIT` failures (R3 plan §2.5; `viewer::failures`).
pub const RETRY_S: f64 = 0.2;
pub const KEY_LIMIT: u32 = 3;

/// Read failures since the last success (R3 plan §2.5): per tile, and in a
/// row over all tiles. Lock order: `queue` before `failures`, never the
/// other way.
#[derive(Default)]
pub struct Failures {
    /// per tile: failures since its last success, and when the latest was
    pub per_key: std::collections::HashMap<Key, (u32, f64)>,
    pub consecutive: u32,
    /// the longest run of failures seen: a run that reached the limit is
    /// not undone by a success that follows before the next paint checks
    pub longest_run: u32,
}

pub struct Pool {
    shared: Arc<Shared>,
    /// the readers, handed to `CLOSING` by `close()`
    handles: Mutex<Vec<std::thread::JoinHandle<()>>>,
}

/// Readers of closed pools still finishing their tile (R3 plan §2.5): the
/// program's exit waits for them, within one shared deadline
/// (`wait_closing`).
static CLOSING: Mutex<Vec<std::thread::JoinHandle<()>>> = Mutex::new(Vec::new());

/// Another reader of the core told to stop (Step3's label reader, R3b):
/// the program's exit waits for it with the rest.
pub fn register_closing(handle: std::thread::JoinHandle<()>) {
    CLOSING.lock().unwrap().push(handle);
}

/// Wait at most `timeout_s` for the readers of every closed pool; returns
/// how many are still running then (left to the OS).
pub fn wait_closing(timeout_s: f64) -> usize {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(timeout_s.max(0.0));
    loop {
        let mut list = CLOSING.lock().unwrap();
        let (done, running): (Vec<_>, Vec<_>) = list.drain(..).partition(|h| h.is_finished());
        *list = running;
        let left = list.len();
        drop(list);
        for h in done {
            let _ = h.join();
        }
        if left == 0 || std::time::Instant::now() >= deadline {
            return left;
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}

impl Pool {
    #[allow(clippy::too_many_arguments)]
    pub fn start(source: Arc<RawSource>, pyr: Pyramid, sources: Arc<crate::corrected::Step1Sources>,
                 raw_kind: crate::step1_tile::RawKind, budget: usize, cache_bytes: [usize; 2],
                 threads: usize, result_bytes: usize, read_delay_ms: f64,
                 notifier: Option<Arc<Notifier>>, trace: Arc<Trace>, faults: Arc<crate::fault::Faults>) -> Pool {
        // the result queue has its own allowance (its tiles may outlive
        // their cache entry). An empty queue still takes one tile larger
        // than its bound (a wanted tile is never refused), so the queue
        // holds at most `result_bytes` plus one tile.
        let result_bytes = result_bytes.max(1);
        let corrected = sources.names.keys().copied()
            .filter(|c| matches!(sources.of(*c), Some(crate::corrected::Source::Corrected { .. }))).collect();
        let shared = Arc::new(Shared {
            queue: Mutex::new(Queue { jobs: VecDeque::new(), wanted: Default::default(), result_bytes: 0,
                                     reading: Default::default(), results: VecDeque::new(), closed: false }),
            work: Condvar::new(), room: Condvar::new(), busy: AtomicUsize::new(0),
            reads: AtomicUsize::new(0), stale: AtomicUsize::new(0), errors: AtomicUsize::new(0),
            max_result_bytes: result_bytes,
            read_delay: std::time::Duration::from_secs_f64(read_delay_ms.max(0.0) / 1000.0),
            last_error: Mutex::new(String::new()), failures: Mutex::new(Failures::default()), faults,
            source, pyr, sources, raw_kind, budget,
            cache: Mutex::new(Cache::new(cache_bytes, corrected)), notifier, trace,
        });
        let cores = std::thread::available_parallelism().map_or(1, |n| n.get());
        let mut handles = Vec::new();
        for i in 0..threads.clamp(1, 6).min(cores) {
            let s = Arc::clone(&shared);
            handles.push(std::thread::Builder::new().name(format!("fvc-read-{i}"))
                .spawn(move || worker::worker(s)).expect("spawn reader"));
        }
        Pool { shared, handles: Mutex::new(handles) }
    }

    pub fn shared(&self) -> &Arc<Shared> {
        &self.shared
    }

    /// Replace the jobs (the planner's current wants, in order). A failed
    /// tile too soon after its failure, or out of retries, stays wanted but
    /// is not queued -- decided under the queue lock, with the submission.
    pub fn set_jobs(&self, jobs: Vec<Key>) {
        let mut q = self.shared.queue.lock().unwrap();
        q.wanted = jobs.iter().copied().collect();
        let now = crate::clock::monotonic();
        let f = self.shared.failures.lock().unwrap();
        let held_back = |k: &Key| f.per_key.get(k).is_some_and(|(n, at)| *n >= KEY_LIMIT || now - at < RETRY_S);
        q.jobs = jobs.into_iter().filter(|k| !q.reading.contains(k) && !held_back(k)).collect();
        drop(f);
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

    /// The tiles waiting in the result queue (memory accounting).
    pub fn queued(&self) -> Vec<(Key, Arc<Tile>)> {
        self.shared.queue.lock().unwrap().results.iter().cloned().collect()
    }

    /// The queue's byte allowance (one tile over it at most).
    pub fn result_allowance(&self) -> usize {
        self.shared.max_result_bytes
    }

    /// Is this tile queued or being read now?
    pub fn queued_or_reading(&self, key: &Key) -> bool {
        let q = self.shared.queue.lock().unwrap();
        q.reading.contains(key) || q.jobs.contains(key)
    }

    pub fn pending_results(&self) -> usize {
        self.shared.queue.lock().unwrap().results.len()
    }

    /// No job queued and no read under way.
    pub fn idle(&self) -> bool {
        self.shared.queue.lock().unwrap().jobs.is_empty() && self.shared.busy.load(Ordering::SeqCst) == 0
    }

    /// Stop: workers exit after the tile in hand; nobody waits for them
    /// here -- they join `CLOSING`, which the program's exit waits for.
    pub fn close(&self) {
        let mut q = self.shared.queue.lock().unwrap();
        q.closed = true;
        q.jobs.clear();
        q.wanted.clear();
        drop(q);
        self.shared.work.notify_all();
        self.shared.room.notify_all();
        let handles: Vec<_> = self.handles.lock().unwrap().drain(..).collect();
        CLOSING.lock().unwrap().extend(handles);
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        self.close();
    }
}

impl Shared {
    pub fn pyr(&self) -> &Pyramid {
        &self.pyr
    }
}

