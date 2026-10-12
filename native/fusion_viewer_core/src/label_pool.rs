//! Step3's label reader (R3b plan §2.3): ONE thread of the core's own (the
//! Python binding had one too), never sharing the image readers' pool.
//! The planner replaces the job list; the thread takes the next job of the
//! current generation while held + being read + delivered-not-taken stays
//! within the budget (`Step3LabelBinding._next_job_locked`), reads it
//! (`label_read`) without any lock and queues the result; the paint takes
//! results without waiting (`take`). One lock and one condition variable
//! of its own, never nested with the image pool's. Paused, retargeted or
//! closed, a thread waiting for room is woken; closing hands the thread to
//! the shared closing list (`pool::register_closing`).

use crate::geom::Pyramid;
use crate::label_plan::LKey;
use crate::label_read::{read_label_tile, Read};
use crate::label_source::KindSource;
use crate::notify::Notifier;
use std::collections::{HashSet, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};

pub type Sources = Arc<[Option<KindSource>; 2]>;

#[derive(Default)]
struct Q {
    jobs: VecDeque<(LKey, usize)>,
    generation: u64,
    sources: Option<Sources>,
    reading: HashSet<LKey>,
    /// bytes of jobs being read or delivered and not yet taken
    pending: usize,
    /// bytes the planner holds (set by the GUI after each change)
    held: usize,
    results: VecDeque<(LKey, usize, Result<Read, String>)>,
    closed: bool,
}

struct Shared {
    q: Mutex<Q>,
    cv: Condvar,
    budget: usize,
    pyr: Pyramid,
    notifier: Option<Arc<Notifier>>,
    faults: Arc<crate::fault::Faults>,
    stale: AtomicUsize,
    /// test only (`BLOCK01_RUST_TEST_LABEL_DELAY_MS`): each read starts this
    /// much later, so a test can move the camera or close while reads wait
    delay: std::time::Duration,
}

pub struct LabelPool {
    s: Arc<Shared>,
    handle: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl LabelPool {
    pub fn start(pyr: Pyramid, budget: usize, notifier: Option<Arc<Notifier>>,
                 faults: Arc<crate::fault::Faults>) -> LabelPool {
        let delay = std::env::var("BLOCK01_RUST_TEST_LABEL_DELAY_MS").ok().and_then(|v| v.parse::<f64>().ok())
            .map_or(std::time::Duration::ZERO, |ms| std::time::Duration::from_secs_f64(ms.max(0.0) / 1000.0));
        let s = Arc::new(Shared { q: Mutex::new(Q::default()), cv: Condvar::new(), budget, pyr, notifier, faults,
                                  stale: AtomicUsize::new(0), delay });
        let w = Arc::clone(&s);
        let handle = std::thread::Builder::new().name("fvc-labels".into()).spawn(move || worker(w))
            .expect("spawn label reader");
        LabelPool { s, handle: Mutex::new(Some(handle)) }
    }

    /// New sources: a new generation; every job of the old one goes.
    pub fn set_sources(&self, generation: u64, sources: Option<Sources>) {
        let mut q = self.s.q.lock().unwrap();
        self.s.stale.fetch_add(q.jobs.len(), Ordering::Relaxed);
        q.jobs.clear();
        q.generation = generation;
        q.sources = sources;
        q.held = 0;
        drop(q);
        self.s.cv.notify_all();
    }

    /// The planner's jobs, in order (already without the held and the
    /// tiles being read). Returns (newly requested, dropped as no longer
    /// wanted) -- `requested` / `stale_dropped`.
    pub fn set_jobs(&self, jobs: Vec<(LKey, usize)>, wanted: &HashSet<LKey>, held: usize) -> (usize, usize) {
        let mut q = self.s.q.lock().unwrap();
        let queued: HashSet<LKey> = q.jobs.iter().map(|j| j.0).collect();
        let dropped = queued.iter().filter(|k| !wanted.contains(k)).count();
        let new = jobs.iter().filter(|j| !queued.contains(&j.0)).count();
        self.s.stale.fetch_add(dropped, Ordering::Relaxed);
        // a tile the thread took since the planner looked is not queued again
        q.jobs = jobs.into_iter().filter(|j| !q.reading.contains(&j.0)).collect();
        q.held = held;
        drop(q);
        self.s.cv.notify_all();
        (new, dropped)
    }

    pub fn set_held(&self, held: usize) {
        self.s.q.lock().unwrap().held = held;
        self.s.cv.notify_all();
    }

    /// Paused: nothing is asked for.
    pub fn clear_jobs(&self) {
        let mut q = self.s.q.lock().unwrap();
        self.s.stale.fetch_add(q.jobs.len(), Ordering::Relaxed);
        q.jobs.clear();
        drop(q);
        self.s.cv.notify_all();
    }

    /// The tiles being read now (planning leaves them out of the jobs).
    pub fn reading(&self) -> HashSet<LKey> {
        self.s.q.lock().unwrap().reading.clone()
    }

    /// Every result in hand, without waiting; their bytes stop counting
    /// as pending and the keys as being read (`_accept`'s first step).
    pub fn take(&self) -> Vec<(LKey, Result<Read, String>)> {
        let mut q = self.s.q.lock().unwrap();
        let out: Vec<_> = q.results.drain(..).collect();
        for (key, bytes, _) in &out {
            q.pending -= bytes;
            q.reading.remove(key);
        }
        drop(q);
        if !out.is_empty() {
            self.s.cv.notify_all();
        }
        out.into_iter().map(|(k, _, r)| (k, r)).collect()
    }

    /// (queued, being read, pending bytes, dropped as stale so far)
    pub fn counts(&self) -> (usize, usize, usize, usize) {
        let q = self.s.q.lock().unwrap();
        (q.jobs.len(), q.reading.len(), q.pending, self.s.stale.load(Ordering::Relaxed))
    }

    pub fn alive(&self) -> bool {
        self.handle.lock().unwrap().as_ref().is_some_and(|h| !h.is_finished())
    }

    /// Stop: the thread ends after the tile in hand; nobody waits here.
    pub fn close(&self) {
        let mut q = self.s.q.lock().unwrap();
        q.closed = true;
        q.jobs.clear();
        drop(q);
        self.s.cv.notify_all();
        if let Some(h) = self.handle.lock().unwrap().take() {
            crate::pool::register_closing(h);
        }
    }
}

impl Drop for LabelPool {
    fn drop(&mut self) {
        self.close();
    }
}

fn worker(s: Arc<Shared>) {
    loop {
        let (key, bytes, sources) = {
            let mut q = s.q.lock().unwrap();
            let job = loop {
                if q.closed {
                    return;
                }
                // an older generation's job is dropped; a job over the
                // budget waits until the GUI takes results or plans again
                while q.jobs.front().is_some_and(|j| j.0 .0 != q.generation) {
                    q.jobs.pop_front();
                    s.stale.fetch_add(1, Ordering::Relaxed);
                }
                match q.jobs.front() {
                    Some(&(key, bytes)) if q.held + q.pending + bytes <= s.budget => {
                        q.jobs.pop_front();
                        break (key, bytes);
                    }
                    _ => q = s.cv.wait(q).unwrap(),
                }
            };
            q.pending += job.1;
            q.reading.insert(job.0);
            (job.0, job.1, q.sources.clone())
        };
        if !s.delay.is_zero() {
            std::thread::sleep(s.delay);
        }
        let result = match sources.as_ref().and_then(|s| s[key.1].as_ref()) {
            _ if s.faults.label_fails() => Err("injected label read failure (test)".to_string()),
            Some(src) => read_label_tile(src, &s.pyr, key.2, key.3, key.4).map_err(|e| e.to_string()),
            None => Ok(Read::Unavailable("no source".into())),
        };
        let mut q = s.q.lock().unwrap();
        if q.closed {
            return;
        }
        q.results.push_back((key, bytes, result));
        drop(q);
        if let Some(n) = &s.notifier {
            n.notify();
        }
    }
}
