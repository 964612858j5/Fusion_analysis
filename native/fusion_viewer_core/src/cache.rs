//! The CPU tile cache: decoded tiles by (channel, tile), bounded in bytes,
//! least recently used out first. Shared by the reader threads (insert)
//! and the GUI thread (get); one short lock, never held while decoding.

use crate::geom::TileAt;
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

/// One decoded tile of its real (edge-cut) shape, its samples in `format`
/// (native-endian bytes): u8 / u16 with their valid rectangle `(y0, y1, x0,
/// x1)`, tile-local, zero elsewhere; f32 valid where finite (NaN outside),
/// its rectangle then the whole tile.
#[derive(Debug)]
pub struct Tile {
    pub rows: usize,
    pub cols: usize,
    pub format: crate::family::Format,
    pub data: Vec<u8>,
    /// None: no valid pixel (wholly outside the region)
    pub valid: Option<[u32; 4]>,
}

pub type Key = (u16, TileAt);

/// Which allowance a channel's tiles count against (`STEP1_VIEWER_RAW_CACHE_BYTES`
/// / `STEP1_VIEWER_CORRECTED_CACHE_BYTES`, the Python path's two caches).
pub const RAW: usize = 0;
pub const CORRECTED: usize = 1;

pub struct Cache {
    max_bytes: [usize; 2],
    bytes: [usize; 2],
    /// channels whose tiles are corrected (the rest count as raw)
    corrected: std::collections::HashSet<u16>,
    tick: u64,
    map: HashMap<Key, (Arc<Tile>, u64)>,
    order: [BTreeMap<u64, Key>; 2],
    pub hits: u64,
    pub inserts: u64,
}

impl Cache {
    pub fn new(max_bytes: [usize; 2], corrected: std::collections::HashSet<u16>) -> Self {
        Cache { max_bytes, bytes: [0; 2], corrected, tick: 0, map: HashMap::new(),
                order: [BTreeMap::new(), BTreeMap::new()], hits: 0, inserts: 0 }
    }

    fn class(&self, key: &Key) -> usize {
        if self.corrected.contains(&key.0) { CORRECTED } else { RAW }
    }

    pub fn bytes(&self) -> usize {
        self.bytes[RAW] + self.bytes[CORRECTED]
    }

    /// Bytes held against one allowance (`RAW` / `CORRECTED`).
    pub fn bytes_of(&self, class: usize) -> usize {
        self.bytes[class]
    }

    pub fn get(&mut self, key: &Key) -> Option<Arc<Tile>> {
        self.tick += 1;
        let (tick, class) = (self.tick, self.class(key));
        let entry = self.map.get_mut(key)?;
        self.order[class].remove(&entry.1);
        entry.1 = tick;
        self.order[class].insert(tick, *key);
        self.hits += 1;
        Some(Arc::clone(&entry.0))
    }

    /// S0-B: a channel added at run time (a Python source) counts against
    /// the corrected allowance from now on.
    pub fn add_corrected(&mut self, channel: u16) {
        self.corrected.insert(channel);
    }

    /// S0-B: every tile of `channel` leaves, and the channel leaves its
    /// class (a retired Python source; its number is never used again).
    pub fn remove_channel(&mut self, channel: u16) {
        let gone: Vec<Key> = self.map.keys().filter(|k| k.0 == channel).copied().collect();
        for key in gone {
            let class = self.class(&key);
            if let Some((old, t)) = self.map.remove(&key) {
                self.order[class].remove(&t);
                self.bytes[class] -= old.data.len();
            }
        }
        self.corrected.remove(&channel);
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    pub fn contains(&self, key: &Key) -> bool {
        self.map.contains_key(key)
    }

    /// Insert; the least recently used tiles of the SAME allowance leave
    /// while it is over.
    pub fn insert(&mut self, key: Key, tile: Arc<Tile>) {
        let class = self.class(&key);
        if let Some((old, t)) = self.map.remove(&key) {
            self.order[class].remove(&t);
            self.bytes[class] -= old.data.len();
        }
        self.tick += 1;
        self.bytes[class] += tile.data.len();
        self.map.insert(key, (tile, self.tick));
        self.order[class].insert(self.tick, key);
        self.inserts += 1;
        while self.bytes[class] > self.max_bytes[class] {
            let Some((&t, &k)) = self.order[class].iter().next() else { break };
            self.order[class].remove(&t);
            if let Some((old, _)) = self.map.remove(&k) {
                self.bytes[class] -= old.data.len();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tile(n: usize) -> Arc<Tile> {
        Arc::new(Tile { rows: 1, cols: n, format: crate::family::Format::U8, data: vec![0; n], valid: None })
    }

    fn key(c: u16, tx: u32) -> Key {
        (c, TileAt { level: 0, ty: 0, tx })
    }

    #[test]
    fn each_allowance_evicts_only_its_own_tiles() {
        let mut c = Cache::new([10, 20], [1u16].into_iter().collect());
        c.insert(key(0, 0), tile(6));
        c.insert(key(1, 0), tile(15));
        c.insert(key(0, 1), tile(6));          // raw over 10: its oldest goes
        assert!(!c.contains(&key(0, 0)) && c.contains(&key(0, 1)));
        assert!(c.contains(&key(1, 0)));        // the corrected tile is untouched
        assert_eq!((c.bytes_of(RAW), c.bytes_of(CORRECTED)), (6, 15));
        c.insert(key(1, 1), tile(10));         // corrected over 20
        assert!(!c.contains(&key(1, 0)) && c.contains(&key(0, 1)));
        assert_eq!(c.bytes(), 16);
    }

    #[test]
    fn a_run_time_channel_counts_as_corrected_and_leaves_whole() {
        let mut c = Cache::new([10, 20], std::collections::HashSet::new());
        c.add_corrected(0x8000);
        c.insert(key(0x8000, 0), tile(15));     // over the raw 10, within 20
        c.insert(key(0x8000, 1), tile(4));
        c.insert(key(0, 0), tile(6));
        assert_eq!((c.bytes_of(RAW), c.bytes_of(CORRECTED)), (6, 19));
        c.remove_channel(0x8000);
        assert!(!c.contains(&key(0x8000, 0)) && !c.contains(&key(0x8000, 1)));
        assert_eq!((c.bytes_of(RAW), c.bytes_of(CORRECTED)), (6, 0));
        assert!(c.contains(&key(0, 0)));
    }
}
