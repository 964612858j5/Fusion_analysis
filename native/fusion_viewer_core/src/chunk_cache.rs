//! R7b B1.1 (docs/v16_5_r7b_corrected_read_plan.md): decoded corrected
//! chunks kept for reuse, one cache per viewer (its reader pool), shared by
//! the reader threads, dropped with the viewer's sources.
//!
//! A corrected chunk (1024 x 1024 float32) covers four 512 display tiles;
//! without this each of them reads and decodes the whole chunk again.
//!
//! - Key: the array's directory and the chunk's coordinates (an array's
//!   full resolution, old coarse plane and display strides are different
//!   directories).
//! - Budget: carved out of the corrected tile cache's allowance (the
//!   viewer's memory bound does not grow). A chunk being decoded is
//!   reserved first, then held; a held chunk still used by a reader is
//!   never evicted, so held + decoding never exceed the budget. When
//!   nothing can be evicted the chunk is decoded for that reader alone and
//!   not kept (as before this cache), and counted as a bypass.
//! - One decode per chunk: a reader that finds the chunk being decoded by
//!   another waits for it; the wait ends when the decode succeeds (the
//!   result is shared), fails (the waiter reads it itself), the waiter's
//!   tile is no longer wanted, or the viewer closes (`close`).

use crate::error::Result;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

type Key = (PathBuf, usize, usize);

enum Entry {
    Decoding,
    Ready { data: Arc<Vec<f32>>, used: u64 },
}

#[derive(Default, Clone, Copy, Debug)]
pub struct Stats {
    pub held_bytes: usize,
    pub decoding_bytes: usize,
    /// OF `held_bytes`: held chunks a reader is still using (not evictable
    /// now) -- a part of held, never to be added to it
    pub referenced_bytes: usize,
    pub hits: u64,
    pub misses: u64,
    pub bypass: u64,
    /// wait rounds (one per 20 ms a reader waited), not distinct waiters
    pub waits: u64,
}

struct Inner {
    map: HashMap<Key, Entry>,
    clock: u64,
    held: usize,
    decoding: usize,
    closed: bool,
    stats: Stats,
}

pub struct ChunkCache {
    budget: usize,
    inner: Mutex<Inner>,
    ready: Condvar,
}

impl std::fmt::Debug for ChunkCache {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "ChunkCache(budget {})", self.budget)
    }
}

impl ChunkCache {
    pub fn new(budget: usize) -> ChunkCache {
        ChunkCache { budget, ready: Condvar::new(),
                     inner: Mutex::new(Inner { map: HashMap::new(), clock: 0, held: 0, decoding: 0,
                                               closed: false, stats: Stats::default() }) }
    }

    /// The decoded chunk `key` (`bytes` once decoded): from the cache, from
    /// another reader's decode, or `load`ed here. None when `wanted` turned
    /// false while waiting (the tile was cancelled).
    pub fn get(&self, key: Key, bytes: usize, wanted: &dyn Fn() -> bool,
               load: &dyn Fn() -> Result<Vec<f32>>) -> Result<Option<Arc<Vec<f32>>>> {
        let mut g = self.inner.lock().unwrap();
        loop {
            if g.closed {
                drop(g);
                return load().map(|v| Some(Arc::new(v)));
            }
            g.clock += 1;
            let now = g.clock;
            match g.map.get_mut(&key) {
                Some(Entry::Ready { data, used }) => {
                    *used = now;
                    let data = Arc::clone(data);
                    g.stats.hits += 1;
                    return Ok(Some(data));
                }
                Some(Entry::Decoding) => {
                    g.stats.waits += 1;
                    let (ng, _) = self.ready.wait_timeout(g, Duration::from_millis(20)).unwrap();
                    g = ng;
                    if !wanted() {
                        return Ok(None);
                    }
                    continue;                  // decoded, failed (gone) or still under way
                }
                None => {}
            }
            g.stats.misses += 1;
            if !self.make_room(&mut g, bytes) {
                g.stats.bypass += 1;
                drop(g);
                return load().map(|v| Some(Arc::new(v)));
            }
            g.decoding += bytes;
            g.map.insert(key.clone(), Entry::Decoding);
            drop(g);
            let got = load();
            let mut g = self.inner.lock().unwrap();
            g.decoding -= bytes;
            let out = match got {
                Ok(v) => {
                    let data = Arc::new(v);
                    if g.closed {
                        g.map.remove(&key);
                    } else {
                        g.held += bytes;
                        let used = g.clock;
                        g.map.insert(key, Entry::Ready { data: Arc::clone(&data), used });
                    }
                    Ok(Some(data))
                }
                Err(e) => {
                    g.map.remove(&key);        // a waiter reads it itself
                    Err(e)
                }
            };
            drop(g);
            self.ready.notify_all();
            return out;
        }
    }

    /// Evict least-recently-used chunks no reader holds until `bytes` fit;
    /// false when they cannot.
    fn make_room(&self, g: &mut Inner, bytes: usize) -> bool {
        if bytes > self.budget {
            return false;
        }
        while g.held + g.decoding + bytes > self.budget {
            let victim = g.map.iter().filter_map(|(k, e)| match e {
                Entry::Ready { data, used } if Arc::strong_count(data) == 1 => Some((*used, k.clone())),
                _ => None,
            }).min_by_key(|(u, _)| *u);
            let Some((_, k)) = victim else { return false };
            if let Some(Entry::Ready { data, .. }) = g.map.remove(&k) {
                g.held -= data.len() * 4;
            }
        }
        true
    }

    /// The viewer is closing: wake every waiter; nothing is kept from now on.
    pub fn close(&self) {
        let mut g = self.inner.lock().unwrap();
        g.closed = true;
        g.map.retain(|_, e| matches!(e, Entry::Decoding));
        g.held = 0;
        drop(g);
        self.ready.notify_all();
    }

    pub fn stats(&self) -> Stats {
        let g = self.inner.lock().unwrap();
        let referenced = g.map.values().map(|e| match e {
            Entry::Ready { data, .. } if Arc::strong_count(data) > 1 => data.len() * 4,
            _ => 0,
        }).sum();
        Stats { held_bytes: g.held, decoding_bytes: g.decoding, referenced_bytes: referenced, ..g.stats }
    }

    pub fn budget(&self) -> usize {
        self.budget
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn key(i: usize) -> Key {
        (PathBuf::from("/a"), i, 0)
    }

    #[test]
    fn hit_after_one_decode() {
        let c = ChunkCache::new(1000);
        let n = AtomicUsize::new(0);
        let load = || { n.fetch_add(1, Ordering::SeqCst); Ok(vec![1.0f32; 10]) };
        for _ in 0..4 {
            assert_eq!(c.get(key(0), 40, &|| true, &load).unwrap().unwrap()[0], 1.0);
        }
        assert_eq!(n.load(Ordering::SeqCst), 1);
        let s = c.stats();
        assert_eq!((s.hits, s.misses, s.held_bytes), (3, 1, 40));
    }

    #[test]
    fn evicts_lru_and_bypasses_when_all_referenced() {
        let c = ChunkCache::new(80);
        let load = || Ok(vec![0.0f32; 10]);
        let a = c.get(key(0), 40, &|| true, &load).unwrap().unwrap();
        let _b = c.get(key(1), 40, &|| true, &load).unwrap().unwrap();
        // both held and referenced: a third does not fit -> bypass, budget kept
        let _c3 = c.get(key(2), 40, &|| true, &load).unwrap().unwrap();
        assert_eq!(c.stats().bypass, 1);
        assert!(c.stats().held_bytes <= 80);
        drop(a);                                         // key 0 now evictable
        let _d = c.get(key(3), 40, &|| true, &load).unwrap().unwrap();
        assert_eq!(c.stats().held_bytes, 80);
        assert!(c.stats().held_bytes + c.stats().decoding_bytes <= 80);
    }

    #[test]
    fn failure_is_not_kept() {
        let c = ChunkCache::new(1000);
        let bad = || Err(crate::error::CoreError::Io("x".into()));
        assert!(c.get(key(0), 40, &|| true, &bad).is_err());
        assert_eq!(c.stats().held_bytes, 0);
        assert_eq!(c.get(key(0), 40, &|| true, &|| Ok(vec![3.0; 10])).unwrap().unwrap()[0], 3.0);
    }

    /// Real threads: eight readers ask for the same chunk at once; it is
    /// decoded once and all eight get it.
    #[test]
    fn concurrent_same_chunk_decoded_once() {
        let c = Arc::new(ChunkCache::new(1 << 20));
        let n = Arc::new(AtomicUsize::new(0));
        let go = Arc::new(std::sync::Barrier::new(8));
        let hs: Vec<_> = (0..8).map(|_| {
            let (c, n, go) = (Arc::clone(&c), Arc::clone(&n), Arc::clone(&go));
            std::thread::spawn(move || {
                go.wait();
                let load = || {
                    n.fetch_add(1, Ordering::SeqCst);
                    std::thread::sleep(Duration::from_millis(50));
                    Ok(vec![7.0f32; 10])
                };
                c.get(key(1), 40, &|| true, &load).unwrap().unwrap()[0]
            })
        }).collect();
        for h in hs {
            assert_eq!(h.join().unwrap(), 7.0);
        }
        assert_eq!(n.load(Ordering::SeqCst), 1);
        assert_eq!(c.stats().held_bytes, 40);
    }

    /// A waiter whose tile is cancelled returns None; a waiter whose
    /// decoder fails reads the chunk itself.
    #[test]
    fn waiter_cancel_and_reread_after_failure() {
        let c = Arc::new(ChunkCache::new(1 << 20));
        let started = Arc::new(std::sync::Barrier::new(2));
        let (c1, s1) = (Arc::clone(&c), Arc::clone(&started));
        let decoder = std::thread::spawn(move || {
            let load = || {
                s1.wait();
                std::thread::sleep(Duration::from_millis(80));
                Err(crate::error::CoreError::Io("disk".into()))
            };
            c1.get(key(2), 40, &|| true, &load).is_err()
        });
        started.wait();                                  // the decode is under way
        let wanted = std::sync::atomic::AtomicBool::new(true);
        let c2 = Arc::clone(&c);
        let canceller = std::thread::spawn(move || {
            let w = std::sync::atomic::AtomicBool::new(true);
            let t0 = std::time::Instant::now();
            c2.get(key(2), 40, &|| { if t0.elapsed() > Duration::from_millis(30) { w.store(false, Ordering::SeqCst); } w.load(Ordering::SeqCst) },
                   &|| Ok(vec![1.0; 10])).unwrap()
        });
        let rereads = AtomicUsize::new(0);
        let got = c.get(key(2), 40, &|| wanted.load(Ordering::SeqCst),
                        &|| { rereads.fetch_add(1, Ordering::SeqCst); Ok(vec![5.0; 10]) }).unwrap().unwrap();
        assert!(decoder.join().unwrap(), "the first decoder failed");
        assert!(canceller.join().unwrap().is_none(), "the cancelled waiter returned None");
        assert_eq!(got[0], 5.0);
        assert_eq!(rereads.load(Ordering::SeqCst), 1, "the waiter read it itself after the failure");
    }

    /// A reader waiting on a decode that never ends returns once the
    /// viewer closes.
    #[test]
    fn close_wakes_a_waiting_reader() {
        let c = Arc::new(ChunkCache::new(1 << 20));
        {
            let mut g = c.inner.lock().unwrap();
            g.map.insert(key(5), Entry::Decoding);       // someone else's decode, stuck
        }
        let c2 = Arc::clone(&c);
        let waiter = std::thread::spawn(move || {
            c2.get(key(5), 40, &|| true, &|| Ok(vec![2.0; 10])).unwrap().unwrap()[0]
        });
        std::thread::sleep(Duration::from_millis(60));
        assert!(!waiter.is_finished(), "still waiting before close");
        c.close();
        assert_eq!(waiter.join().unwrap(), 2.0);         // after close it reads for itself
        assert_eq!(c.stats().held_bytes, 0);
    }
}
