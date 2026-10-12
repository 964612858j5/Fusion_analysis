//! The virtual texture's CPU side (`step1_gpu_vt.VirtualCompositor`):
//! per drawn channel a page table from (level, tx, ty) to the array slot
//! holding that tile, a per-slot meta table (rect, valid rect, size) and
//! the per-row parameters, with the ranges that changed since the last
//! upload. No GL here; `render.rs` uploads what this marks dirty.

use crate::cache::Key;
use crate::geom::{Pyramid, TileAt, TILE};
use crate::gpu_store::Store;
use crate::plan::Ch;
use std::collections::HashMap;

pub const BLOCK_ROWS: usize = 3;
pub const PARAM_TEXELS: usize = 5;
pub const MAX_LEVELS: usize = 16;

/// One drawn row's display: mapping (lo, hi, gamma), colour, weight, and
/// for Fusion its group (-1: the nucleus) and the group's weight.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RowParams {
    pub mapping: [f32; 3],
    pub color: [f32; 3],
    pub weight: f32,
    pub group: f32,
    pub group_weight: f32,
}

pub struct Vt {
    pub grid: Vec<(u32, u32)>,
    pub offset: Vec<u32>,
    pub ds: Vec<(f64, f64)>,
    pub width: usize,
    pub height: usize,
    pub channel_slots: HashMap<Ch, usize>,
    /// layers x height x width, -1 = empty
    pub pages: Vec<i32>,
    written: HashMap<Ch, HashMap<TileAt, i32>>,
    /// (layer, first row, end row) changed since the last upload
    pub dirty_pages: Vec<(usize, usize, usize)>,
    pub pages_resized: bool,
    /// blocks per family the shader binds ([integer, float]): meta rows
    /// are `(family * caps[0] + index) * BLOCK_ROWS` (`U_BLOCKS`)
    pub caps: [usize; 2],
    pub meta_rows: usize,
    pub meta_width: usize,
    pub meta: Vec<[f32; 4]>,
    pub meta_dirty_rows: Vec<usize>,
    pub meta_resized: bool,
}

impl Vt {
    /// The page atlas laid out for the whole pyramid: every level side by
    /// side, level 0 extent; meta rows for `caps` blocks, `layers` columns.
    pub fn new(pyr: &Pyramid, caps: [usize; 2], layers: usize) -> Self {
        let blocks = caps[0] + caps[1];
        let (h0, w0) = pyr.shapes[0];
        let (mut grid, mut offset, mut ds, mut x) = (Vec::new(), Vec::new(), Vec::new(), 0u32);
        for level in 0..pyr.levels() as u8 {
            let (ds_y, ds_x) = pyr.ds_yx(level);
            let gw = (w0 as f64 / (TILE as f64 * ds_x)).ceil() as u32 + 1;
            let gh = (h0 as f64 / (TILE as f64 * ds_y)).ceil() as u32 + 1;
            grid.push((gw, gh));
            offset.push(x);
            ds.push((ds_x, ds_y));
            x += gw;
        }
        let height = grid.iter().map(|g| g.1).max().unwrap_or(1) as usize;
        Vt { grid, offset, ds, width: x as usize, height, channel_slots: HashMap::new(),
             pages: Vec::new(), written: HashMap::new(), dirty_pages: Vec::new(), pages_resized: true,
             caps, meta_rows: blocks * BLOCK_ROWS, meta_width: layers.max(1),
             meta: vec![[0.0; 4]; blocks * BLOCK_ROWS * layers.max(1)],
             meta_dirty_rows: Vec::new(), meta_resized: true }
    }

    /// A new sampler split: the meta table is laid out again (the caller
    /// marks every resident slot dirty so it is refilled).
    pub fn set_caps(&mut self, caps: [usize; 2]) {
        if caps == self.caps {
            return;
        }
        self.caps = caps;
        self.meta_rows = (caps[0] + caps[1]) * BLOCK_ROWS;
        self.meta = vec![[0.0; 4]; self.meta_rows * self.meta_width];
        self.meta_resized = true;
    }

    pub fn layers(&self) -> usize {
        self.channel_slots.len().max(1)
    }

    fn slot_of(&mut self, c: Ch) -> usize {
        let n = self.channel_slots.len();
        let slot = *self.channel_slots.entry(c).or_insert(n);
        let need = self.layers() * self.height * self.width;
        if self.pages.len() < need {
            self.pages.resize(need, -1);
            self.pages_resized = true;
        }
        slot
    }

    /// Bring the page tables to `rows` (drawn channels and their drawn
    /// tiles, coarse to fine): a tile not resident is left out, a channel
    /// no longer drawn empties.
    pub fn sync_pages(&mut self, rows: &[(Ch, Vec<TileAt>)], store: &Store) {
        let drawn: Vec<Ch> = rows.iter().map(|r| r.0).collect();
        for c in self.written.keys().copied().collect::<Vec<_>>() {
            if !drawn.contains(&c) {
                let slot = self.channel_slots[&c];
                let plane = self.height * self.width;
                self.pages[slot * plane..(slot + 1) * plane].fill(-1);
                self.dirty_pages.push((slot, 0, self.height));
                self.written.remove(&c);
            }
        }
        for (c, tiles) in rows {
            let slot = self.slot_of(*c);
            let mut cells: HashMap<TileAt, i32> = HashMap::new();
            for t in tiles {
                if let Some((family, index, layer)) = store.code(&(*c, *t)) {
                    cells.insert(*t, ((family as i32) << 24) | ((index as i32) << 16) | layer as i32);
                }
            }
            let before = self.written.remove(c).unwrap_or_default();
            if before == cells {
                self.written.insert(*c, cells);
                continue;
            }
            let (mut lo, mut hi) = (usize::MAX, 0usize);
            for t in before.keys().filter(|t| !cells.contains_key(t)) {
                self.put(slot, *t, -1);
                lo = lo.min(t.ty as usize);
                hi = hi.max(t.ty as usize + 1);
            }
            for (t, code) in &cells {
                if before.get(t) != Some(code) {
                    self.put(slot, *t, *code);
                    lo = lo.min(t.ty as usize);
                    hi = hi.max(t.ty as usize + 1);
                }
            }
            if lo < hi {
                self.dirty_pages.push((slot, lo, hi));
            }
            self.written.insert(*c, cells);
        }
    }

    fn put(&mut self, slot: usize, t: TileAt, code: i32) {
        let (gw, gh) = self.grid[t.level as usize];
        if t.tx >= gw || t.ty >= gh {
            return;
        }
        let x = self.offset[t.level as usize] as usize + t.tx as usize;
        self.pages[(slot * self.height + t.ty as usize) * self.width + x] = code;
    }

    /// Follow the store's uploads and evictions into the meta table: per
    /// block three rows (rect, valid rect, size), one column per layer.
    pub fn sync_meta(&mut self, store: &mut Store) {
        for (block, layer) in std::mem::take(&mut store.dirty) {
            let Some((family, index)) = store.row_of(block) else { continue };
            let row = (family * self.caps[0] + index) * BLOCK_ROWS;
            if row + 2 >= self.meta_rows || layer as usize >= self.meta_width {
                continue;
            }
            let owner = store.owner(block, layer);
            let values = match owner.and_then(|k: Key| store.meta.get(&k)) {
                None => [[0.0f32; 4]; 3],
                Some(m) => {
                    let f = |r: [f64; 4]| [r[0] as f32, r[1] as f32, r[2] as f32, r[3] as f32];
                    [f(m.rect), m.valid.map(f).unwrap_or([0.0; 4]),
                     [m.size.0 as f32, m.size.1 as f32, 0.0, 0.0]]
                }
            };
            for (i, v) in values.iter().enumerate() {
                self.meta[(row + i) * self.meta_width + layer as usize] = *v;
            }
            self.meta_dirty_rows.push(row);
        }
    }

    /// The parameter rows in draw order (`upload_params`): mapping and the
    /// channel's page layer; colour and weight; group, group weight and
    /// the number of levels used; the levels used, finest first.
    /// A channel may appear in several rows (Fusion groups); its pages are one.
    pub fn params(&self, rows: &[(Ch, RowParams)]) -> Vec<[f32; 4]> {
        let mut out = vec![[0.0f32; 4]; rows.len().max(1) * PARAM_TEXELS];
        for (i, (c, p)) in rows.iter().enumerate() {
            let slot = self.channel_slots.get(c).copied().unwrap_or(0) as f32;
            let mut used: Vec<u8> = self.written.get(c)
                .map(|cells| { let mut l: Vec<u8> = cells.keys().map(|t| t.level).collect(); l.sort(); l.dedup(); l })
                .unwrap_or_default();
            used.sort_by(|a, b| self.ds[*a as usize].0.partial_cmp(&self.ds[*b as usize].0).unwrap());
            let mut padded = [0.0f32; 8];
            for (k, l) in used.iter().take(8).enumerate() {
                padded[k] = *l as f32;
            }
            let r = i * PARAM_TEXELS;
            out[r] = [p.mapping[0], p.mapping[1], p.mapping[2], slot];
            out[r + 1] = [p.color[0], p.color[1], p.color[2], p.weight];
            out[r + 2] = [p.group, p.group_weight, used.len() as f32, 0.0];
            out[r + 3] = [padded[0], padded[1], padded[2], padded[3]];
            out[r + 4] = [padded[4], padded[5], padded[6], padded[7]];
        }
        out
    }

    /// `u_level_a` (tile world w, h, page x offset, 0) and `u_level_b`.
    pub fn level_uniforms(&self) -> ([f32; MAX_LEVELS * 4], [i32; MAX_LEVELS * 2]) {
        let (mut a, mut b) = ([0.0f32; MAX_LEVELS * 4], [0i32; MAX_LEVELS * 2]);
        for (i, ((ds_x, ds_y), (gw, gh))) in self.ds.iter().zip(&self.grid).enumerate().take(MAX_LEVELS) {
            a[i * 4] = (TILE as f64 * ds_x) as f32;
            a[i * 4 + 1] = (TILE as f64 * ds_y) as f32;
            a[i * 4 + 2] = self.offset[i] as f32;
            b[i * 2] = *gw as i32;
            b[i * 2 + 1] = *gh as i32;
        }
        (a, b)
    }

    /// Forget every page entry (after the store released tiles).
    pub fn forget(&mut self) {
        self.pages.fill(-1);
        self.written.clear();
        self.pages_resized = true;
    }
}
