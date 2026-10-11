//! The CPU tile cache: decoded tiles by (channel, tile), bounded in bytes,
//! least recently used out first. Shared by the reader threads (insert)
//! and the GUI thread (get); one short lock, never held while decoding.

use crate::geom::TileAt;
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

/// One decoded tile: native uint8 samples of its real (edge-cut) shape and
/// its valid rectangle `(y0, y1, x0, x1)`, tile-local; zero elsewhere.
#[derive(Debug)]
pub struct Tile {
    pub rows: usize,
    pub cols: usize,
    pub data: Vec<u8>,
    /// None: no valid pixel (wholly outside the region)
    pub valid: Option<[u32; 4]>,
}

pub type Key = (u16, TileAt);

pub struct Cache {
    max_bytes: usize,
    bytes: usize,
    tick: u64,
    map: HashMap<Key, (Arc<Tile>, u64)>,
    order: BTreeMap<u64, Key>,
    pub hits: u64,
    pub inserts: u64,
}

impl Cache {
    pub fn new(max_bytes: usize) -> Self {
        Cache { max_bytes, bytes: 0, tick: 0, map: HashMap::new(), order: BTreeMap::new(),
                hits: 0, inserts: 0 }
    }

    pub fn bytes(&self) -> usize {
        self.bytes
    }

    pub fn get(&mut self, key: &Key) -> Option<Arc<Tile>> {
        self.tick += 1;
        let tick = self.tick;
        let entry = self.map.get_mut(key)?;
        self.order.remove(&entry.1);
        entry.1 = tick;
        self.order.insert(tick, *key);
        self.hits += 1;
        Some(Arc::clone(&entry.0))
    }

    pub fn contains(&self, key: &Key) -> bool {
        self.map.contains_key(key)
    }

    pub fn insert(&mut self, key: Key, tile: Arc<Tile>) {
        if let Some((old, t)) = self.map.remove(&key) {
            self.order.remove(&t);
            self.bytes -= old.data.len();
        }
        self.tick += 1;
        self.bytes += tile.data.len();
        self.map.insert(key, (tile, self.tick));
        self.order.insert(self.tick, key);
        self.inserts += 1;
        while self.bytes > self.max_bytes {
            let Some((&t, &k)) = self.order.iter().next() else { break };
            self.order.remove(&t);
            if let Some((old, _)) = self.map.remove(&k) {
                self.bytes -= old.data.len();
            }
        }
    }
}
