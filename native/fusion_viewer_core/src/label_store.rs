//! Step3's masks as the core keeps them (R3 plan §2.1, L1): the snapshot
//! the label binding hands over (`Step1GpuLayer.set_labels`), and copies of
//! the uint32 planes it named, so a later upload never depends on a Python
//! object. Copies are bounded like the label textures (`LABEL_BUDGET`,
//! least recently used out first); a plane copied before is not copied
//! again. Python planned and read the planes; the core draws them
//! (`render::labels`).

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

/// `GPU_LABEL_TEXTURE_BYTES`: the label textures' budget, and the copies'.
pub const LABEL_BUDGET: usize = 256 * 1024 * 1024;

/// One plane's ids, rows top first (`LabelPlane.ids`).
#[derive(Debug)]
pub struct Plane {
    pub rows: usize,
    pub cols: usize,
    pub ids: Vec<u32>,
}

impl Plane {
    pub fn bytes(&self) -> usize {
        self.ids.len() * 4
    }
}

/// One mask layer (`LabelLayer`): its planes in drawing order (id, level-0
/// world rect x0 x1 y0 y1), how it is drawn, and its outline radius in
/// screen pixels (`label_radius`, worked out by Python for the device
/// pixel ratio).
#[derive(Clone, Debug, PartialEq)]
pub struct Layer {
    pub planes: Vec<(u64, [f64; 4])>,
    pub color: [f32; 3],
    pub alpha: f32,
    pub radius: i32,
    pub fill: bool,
}

#[derive(Default)]
pub struct Labels {
    /// the visible layers with planes, in drawing order
    pub layers: Vec<Layer>,
    /// a new snapshot not yet drawn
    pub dirty: bool,
    copies: HashMap<u64, (Arc<Plane>, u64)>,
    order: BTreeMap<u64, u64>,
    tick: u64,
    bytes: usize,
    /// `Step1GpuLayer._label_counts`
    pub image_submissions: u64,
    pub label_compositions: u64,
    pub label_passes: u64,
    /// the last composition drew masks: `shown` is the picture
    pub shown: bool,
}

impl Labels {
    pub fn has(&self, id: u64) -> bool {
        self.copies.contains_key(&id)
    }

    pub fn copy_bytes(&self) -> usize {
        self.bytes
    }

    /// Keep a copy of plane `id`; older copies not in `keep` leave while
    /// over the budget.
    pub fn insert(&mut self, id: u64, plane: Plane, keep: &std::collections::HashSet<u64>) {
        if let Some((old, t)) = self.copies.remove(&id) {
            self.order.remove(&t);
            self.bytes -= old.bytes();
        }
        self.tick += 1;
        self.bytes += plane.bytes();
        self.copies.insert(id, (Arc::new(plane), self.tick));
        self.order.insert(self.tick, id);
        let victims: Vec<(u64, u64)> = self.order.iter().map(|(t, i)| (*t, *i)).collect();
        for (t, victim) in victims {
            if self.bytes <= LABEL_BUDGET {
                break;
            }
            if keep.contains(&victim) {
                continue;
            }
            self.order.remove(&t);
            if let Some((old, _)) = self.copies.remove(&victim) {
                self.bytes -= old.bytes();
            }
        }
    }

    pub fn plane(&mut self, id: u64) -> Option<Arc<Plane>> {
        self.tick += 1;
        let tick = self.tick;
        let entry = self.copies.get_mut(&id)?;
        self.order.remove(&entry.1);
        entry.1 = tick;
        self.order.insert(tick, id);
        Some(Arc::clone(&entry.0))
    }

    /// A new snapshot (only visible layers with planes are kept).
    pub fn set(&mut self, layers: Vec<Layer>) {
        self.layers = layers.into_iter().filter(|l| !l.planes.is_empty()).collect();
        self.dirty = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plane(n: usize) -> Plane {
        Plane { rows: 1, cols: n, ids: vec![1; n] }
    }

    #[test]
    fn copies_stay_under_the_budget_but_keep_the_needed() {
        let mut l = Labels::default();
        let quarter = LABEL_BUDGET / 4 / 4;              // a quarter of the budget, in ids
        let keep: std::collections::HashSet<u64> = [1u64].into_iter().collect();
        for id in 1..=5u64 {
            l.insert(id, plane(quarter), &keep);
        }
        assert!(l.copy_bytes() <= LABEL_BUDGET);
        assert!(l.has(1), "a plane the snapshot needs is never dropped");
        assert!(!l.has(2) && l.has(5));
    }
}
