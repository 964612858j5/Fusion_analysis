//! Test-only fault injection (`BLOCK01_RUST_TEST_FAULT`, R3 plan §2.5): a
//! read, a paint or a GL re-initialisation that fails, so the tests drive
//! the REAL counting and fallback paths. Unset (the product), nothing ever
//! fails here.
//!
//!   tile:N   the first tile asked for fails its first N reads
//!   all:N    the first N reads fail, whatever the tile
//!   paint:N  frame N reports an error
//!   reinit   every GL initialisation after the first fails
//!   init     the first GL initialisation fails (the mount falls back at start)

use crate::cache::Key;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

#[derive(Debug, Clone, Copy, PartialEq)]
enum Kind {
    None,
    Tile(u32),
    All(u32),
    Paint(u64),
    Reinit,
    Init,
}

#[derive(Debug)]
pub struct Faults {
    kind: Kind,
    failed: AtomicU32,
    victim: Mutex<Option<Key>>,
}

impl Faults {
    pub fn parse(spec: &str) -> Faults {
        let (name, n) = spec.split_once(':').unwrap_or((spec, "0"));
        let n: u64 = n.trim().parse().unwrap_or(0);
        let kind = match name.trim() {
            "tile" => Kind::Tile(n as u32),
            "all" => Kind::All(n as u32),
            "paint" => Kind::Paint(n),
            "reinit" => Kind::Reinit,
            "init" => Kind::Init,
            _ => Kind::None,
        };
        Faults { kind, failed: AtomicU32::new(0), victim: Mutex::new(None) }
    }

    pub fn from_env() -> Faults {
        Faults::parse(&std::env::var("BLOCK01_RUST_TEST_FAULT").unwrap_or_default())
    }

    /// Does this read fail? (Called once per read attempt.)
    pub fn read_fails(&self, key: Key) -> bool {
        match self.kind {
            Kind::Tile(n) => {
                let mut victim = self.victim.lock().unwrap();
                if *victim.get_or_insert(key) != key {
                    return false;
                }
                drop(victim);
                self.take_one(n)
            }
            Kind::All(n) => self.take_one(n),
            _ => false,
        }
    }

    /// One more failure while fewer than `n` have happened.
    fn take_one(&self, n: u32) -> bool {
        let mut seen = self.failed.load(Ordering::SeqCst);
        while seen < n {
            match self.failed.compare_exchange(seen, seen + 1, Ordering::SeqCst, Ordering::SeqCst) {
                Ok(_) => return true,
                Err(now) => seen = now,
            }
        }
        false
    }

    pub fn paint_fails(&self, frame: u64) -> bool {
        self.kind == Kind::Paint(frame)
    }

    /// `attach`: the how-many-th GL initialisation this is (1 = the first).
    pub fn reinit_fails(&self, attach: u32) -> bool {
        (self.kind == Kind::Reinit && attach >= 2) || (self.kind == Kind::Init && attach == 1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geom::TileAt;

    fn key(tx: u32) -> Key {
        (0, TileAt { level: 0, tx, ty: 0 })
    }

    #[test]
    fn tile_fails_only_its_first_tile_n_times() {
        let f = Faults::parse("tile:2");
        assert!(f.read_fails(key(1)));
        assert!(!f.read_fails(key(2)));
        assert!(f.read_fails(key(1)));
        assert!(!f.read_fails(key(1)));
    }

    #[test]
    fn all_fails_the_first_n_reads_and_none_is_inert() {
        let f = Faults::parse("all:2");
        assert!(f.read_fails(key(1)) && f.read_fails(key(2)) && !f.read_fails(key(3)));
        let none = Faults::parse("");
        assert!(!none.read_fails(key(1)) && !none.paint_fails(1) && !none.reinit_fails(2));
        assert!(Faults::parse("paint:3").paint_fails(3) && Faults::parse("reinit").reinit_fails(2));
        assert!(Faults::parse("init").reinit_fails(1) && !Faults::parse("init").reinit_fails(2));
    }
}
