//! What the core holds in CPU memory, and its bound (R3 plan §2.3). A tile
//! is one `Arc` wherever it sits -- cache, result queue, planner -- and is
//! counted once.

use super::ViewerCore;
use crate::cache::{CORRECTED, RAW};
use crate::pool::WORKER_MEMORY_BYTES;
use std::collections::HashSet;
use std::sync::Arc;

pub struct Memory {
    pub cache_raw: usize,
    pub cache_corrected: usize,
    pub cached_tiles: usize,
    /// queued tiles no longer in the cache
    pub queue_extra: usize,
    /// tiles the planner holds that are neither cached nor queued
    pub held_extra: usize,
    /// Step3's mask copies (`label_store`)
    pub label_copies: usize,
    /// R7b B1.1: decoded chunks held / being decoded (inside the corrected
    /// cache allowance, so `bound` does not change)
    pub chunk_held: usize,
    pub chunk_decoding: usize,
    /// what the readers may hold while they work (counted in `bound` only)
    pub worker_allowance: usize,
    /// cache + queue_extra + held_extra + label_copies: everything once
    pub total: usize,
    /// the most the parts above (and the readers' work) may reach
    pub bound: usize,
}

impl ViewerCore {
    pub fn memory(&self) -> Memory {
        let queued = self.pool.queued();               // the queue's lock first, alone
        let cache = self.pool.shared().cache.lock().unwrap();
        let (cache_raw, cache_corrected, cached_tiles) = (cache.bytes_of(RAW), cache.bytes_of(CORRECTED), cache.len());
        let mut seen: HashSet<*const crate::cache::Tile> = HashSet::new();
        let mut queue_extra = 0;
        for (key, tile) in queued {
            if !cache.contains(&key) && seen.insert(Arc::as_ptr(&tile)) {
                queue_extra += tile.data.len();
            }
        }
        let mut held_extra = 0;
        for (key, tile) in &self.held {
            if !cache.contains(key) && seen.insert(Arc::as_ptr(tile)) {
                held_extra += tile.data.len();
            }
        }
        drop(cache);
        let one_tile = crate::geom::TILE * crate::geom::TILE * 4;
        let worker_allowance = self.cfg.threads.clamp(1, crate::pool::MAX_READERS) * WORKER_MEMORY_BYTES + self.label_read_bound();
        let label_copies = self.labels.copy_bytes();
        let bound = self.cfg.cpu_cache_bytes + self.cfg.corrected_cache_bytes
            + self.pool.result_allowance() + one_tile + worker_allowance
            // the planner keeps at most what admission lets onto the GPU
            + self.cfg.max_raw_texture_bytes
            + crate::label_store::LABEL_BUDGET;
        let cs = self.chunk_cache.as_ref().map(|c| c.stats()).unwrap_or_default();
        let (chunk_held, chunk_decoding) = (cs.held_bytes, cs.decoding_bytes);
        Memory { cache_raw, cache_corrected, cached_tiles, queue_extra, held_extra, label_copies,
                 chunk_held, chunk_decoding,
                 worker_allowance, total: cache_raw + cache_corrected + queue_extra + held_extra + label_copies
                     + chunk_held + chunk_decoding,
                 bound }
    }
}
