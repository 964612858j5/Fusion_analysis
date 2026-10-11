//! Stage records with the names and fields of `utils/tile_trace.py`
//! (req / start / read / done / recv / gpu, and cov / present / paint),
//! stamped on the `time.monotonic()` base when they happen and handed to
//! Python in one batch (`drain`) to be merged with their own timestamps.

use crate::clock::monotonic;
use crate::geom::TileAt;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

const LIMIT: usize = 4_000_000;

#[derive(Clone, Debug)]
pub struct Record {
    pub t: f64,
    pub stage: &'static str,
    /// (channel, level, tx, ty), when the record is about a tile
    pub key: Option<(u16, u8, u32, u32)>,
    /// extra numeric fields, by name
    pub extra: Vec<(&'static str, f64)>,
}

#[derive(Default)]
pub struct Trace {
    on: AtomicBool,
    records: Mutex<Vec<Record>>,
}

impl Trace {
    pub fn set_on(&self, on: bool) {
        self.on.store(on, Ordering::Relaxed);
    }

    pub fn on(&self) -> bool {
        self.on.load(Ordering::Relaxed)
    }

    pub fn stamp(&self, stage: &'static str, key: Option<(u16, TileAt)>, extra: Vec<(&'static str, f64)>) {
        if !self.on() {
            return;
        }
        let rec = Record { t: monotonic(), stage,
                           key: key.map(|(c, t)| (c, t.level, t.tx, t.ty)), extra };
        let mut r = self.records.lock().unwrap();
        if r.len() < LIMIT {
            r.push(rec);
        }
    }

    pub fn drain(&self) -> Vec<Record> {
        std::mem::take(&mut *self.records.lock().unwrap())
    }
}
