//! One reader thread: take a job, read and decode it without any lock,
//! hand it over (cache + result queue) or count its failure (R3 plan §2.5:
//! a failure notifies the GUI, which retries or falls back).

use super::{Shared, RESULT_CAPACITY};
use crate::cache::{Key, Tile};
use crate::geom::TILE;
use std::sync::atomic::Ordering;
use std::sync::Arc;

pub(super) fn worker(s: Arc<Shared>) {
    loop {
        let (key, started_ahead) = {
            let mut q = s.queue.lock().unwrap();
            loop {
                if q.closed {
                    return;
                }
                // the first job that may start: a read-ahead one only while
                // fewer than its limit are being read (R7c step 2)
                let ahead_busy = q.ahead_reading.len();
                let open = |k: &Key| !q.prefetch.contains(k) || q.prefetch_limit.is_none_or(|n| ahead_busy < n);
                if let Some(i) = q.jobs.iter().position(open) {
                    let k = q.jobs.remove(i).unwrap();
                    s.busy.fetch_add(1, Ordering::SeqCst);
                    q.reading.insert(k);
                    let ahead = q.prefetch.contains(&k);
                    if ahead {
                        q.ahead_reading.insert(k);
                    }
                    break (k, ahead);
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
                crate::read_split::reset();
                let (w0, c0) = (crate::clock::monotonic(), crate::clock::thread_cpu());
                let still = || s.queue.lock().unwrap().wanted.contains(&key);
                let fails = if started_ahead { s.faults.ahead_read_fails(key) } else { s.faults.read_fails(key) };
                let got = if fails {
                    Err(crate::error::CoreError::Io("injected read failure (test)".into()))
                } else {
                    read_tile(&s, key, &still)
                };
                // R7a: the read's own stages, taken as the read returns
                // (`read_split`; empty when off); `rest_ms` is against the
                // read alone, the record's `wall_ms` stays as it was
                let split = crate::read_split::take((crate::clock::monotonic() - w0) * 1000.0);
                let timing = |err: bool, stale: bool| {
                    let mut v = vec![
                        ("wall_ms", (crate::clock::monotonic() - w0) * 1000.0),
                        ("cpu_ms", (crate::clock::thread_cpu() - c0) * 1000.0),
                        ("err", err as i32 as f64), ("stale", stale as i32 as f64)];
                    v.extend(split.iter().copied());
                    v
                };
                match got {
                    Ok(None) => {                    // cancelled: the plan left it
                        s.queue.lock().unwrap().release(&key);
                        s.trace.stamp("read", Some(key), timing(false, true));
                        s.stale.fetch_add(1, Ordering::Relaxed);
                        None
                    }
                    Ok(Some(t)) => {
                        let t = Arc::new(t);
                        s.reads.fetch_add(1, Ordering::Relaxed);
                        {
                            let mut f = s.failures.lock().unwrap();
                            f.per_key.remove(&key);
                            f.consecutive = 0;
                        }
                        // still wanted? Decided, and the tile cached or the
                        // key released, under ONE hold of the queue lock: a
                        // plan that wants the key again after this sees it
                        // no longer in flight and queues it afresh
                        let mut q = s.queue.lock().unwrap();
                        let stale = !q.wanted.contains(&key);
                        let mut cached_bytes = 0;
                        if stale {
                            q.release(&key);
                        } else {
                            let mut c = s.cache.lock().unwrap();
                            c.insert(key, Arc::clone(&t));
                            cached_bytes = c.bytes();
                        }
                        drop(q);
                        let mut rec = timing(false, stale);
                        if s.trace.on() {
                            // measurement: the CPU cache's bytes after this read (R7c step 2)
                            rec.push(("cache_mb", cached_bytes as f64 / 1048576.0));
                        }
                        s.trace.stamp("read", Some(key), rec);
                        if stale {
                            s.stale.fetch_add(1, Ordering::Relaxed);
                            None
                        } else {
                            Some(t)
                        }
                    }
                    Err(e) => {
                        // the failure is on record BEFORE the key leaves
                        // `reading`: whoever queues it next sees it (lock
                        // order: never failures inside queue here)
                        *s.last_error.lock().unwrap() = format!("{key:?}: {e}");
                        // quiet only while it is still read ahead (or nobody's):
                        // one that came into view meanwhile is the picture's
                        // failure -- counted, and the GUI told (codex review)
                        let quiet = started_ahead && {
                            let q = s.queue.lock().unwrap();
                            q.prefetch.contains(&key) || !q.wanted.contains(&key)
                        };
                        {
                            let mut f = s.failures.lock().unwrap();
                            let entry = f.per_key.entry(key).or_insert((0, 0.0));
                            *entry = (entry.0 + 1, crate::clock::monotonic());
                            // a read-ahead failure (R7c step 2) is held back
                            // like any other, but is not the picture's: no
                            // run, no notice -- it cannot make the GUI give up
                            if !quiet {
                                f.consecutive += 1;
                                f.longest_run = f.longest_run.max(f.consecutive);
                            }
                        }
                        s.queue.lock().unwrap().release(&key);
                        s.trace.stamp("read", Some(key), timing(true, false));
                        s.errors.fetch_add(1, Ordering::Relaxed);
                        if !quiet {
                            if let Some(n) = &s.notifier {
                                n.notify();          // the GUI decides: retry or fall back
                            }
                        }
                        None
                    }
                }
            }
        };
        if let Some(t) = tile {
            let bytes = t.data.len();
            let mut q = s.queue.lock().unwrap();
            // a read-ahead key is cached already and nobody waits for it --
            // decided after any wait, as the plan may change meanwhile
            while !q.prefetch.contains(&key) && !q.closed && q.wanted.contains(&key) && !q.results.is_empty()
                && (q.results.len() >= RESULT_CAPACITY || q.result_bytes + bytes > s.max_result_bytes) {
                q = s.room.wait(q).unwrap();
            }
            let ahead = q.prefetch.contains(&key);
            let deliver = !ahead && !q.closed && q.wanted.contains(&key);
            if deliver {
                q.results.push_back((key, t));
                q.result_bytes += bytes;
            } else if !ahead && !q.closed {
                s.stale.fetch_add(1, Ordering::Relaxed);
            }
            q.release(&key);
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

