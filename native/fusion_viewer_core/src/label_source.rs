//! Step3's masks as the core reads them (R3b plan §2.1): per kind (cell,
//! nucleus) the level-0 mask with its bounding box's origin, and the label
//! pyramid's levels on disk (`core.label_pyramid`: level number, array,
//! origin) -- or none, when the pyramid is missing (then only level 0 is
//! read; Python builds the pyramid and hands it over). Python parsed the
//! `.zarray`s; `zarr_v2::U32Array` checked them.

use crate::zarr_v2::U32Array;

pub const CELL: usize = 0;
pub const NUCLEUS: usize = 1;
/// Drawn in this order: the nucleus lies on top (`step3_label_binding.KINDS`).
pub const KINDS: [usize; 2] = [CELL, NUCLEUS];

/// One pyramid level: its number in the viewer's levels, its array and
/// where the array's (0, 0) lies on that level `(row, col)`.
#[derive(Clone, Debug)]
pub struct PyramidLevel {
    pub level: u8,
    pub array: U32Array,
    pub origin: [i64; 2],
}

#[derive(Clone, Debug)]
pub struct KindSource {
    pub level0: U32Array,
    /// the mask's bounding box origin on level 0 `(row, col)`
    pub origin0: [i64; 2],
    /// None: no pyramid (coarse levels are neither read nor drawn)
    pub pyramid: Option<Vec<PyramidLevel>>,
}

impl KindSource {
    pub fn has_pyramid(&self) -> bool {
        self.pyramid.is_some()
    }

    /// Temporary bytes one tile read may hold (the memory bound).
    pub fn read_bytes_bound(&self, tile: usize) -> usize {
        let mut most = self.level0.read_bytes_bound(tile);
        for lv in self.pyramid.iter().flatten() {
            most = most.max(lv.array.read_bytes_bound(tile));
        }
        most
    }
}
