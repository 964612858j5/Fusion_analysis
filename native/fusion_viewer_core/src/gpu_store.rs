//! The GPU tile store: uint8 tiles in the layers of R8UI 512x512 array
//! textures (`step1_gpu_layer._TileArrayStore`, the u8 family).
//!
//! Blocks are 64 MiB (256 layers), allocated whole and counted whole
//! against the budget, and "warmed" when created (their first use costs
//! the driver tens of ms). A tile needed by the frame being prepared is
//! never evicted; otherwise the least recently used tile gives its layer.
//! Design reference (no code copied): Odon draws tiles from GPU-resident
//! pages, one draw for many tiles.

use crate::cache::{Key, Tile};
use crate::geom::TILE;
use glow::HasContext;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

pub const BLOCK_BYTES: usize = 64 * 1024 * 1024;
pub const SLOT_BYTES: usize = TILE * TILE;

/// Per resident tile: world rect, valid world rect (None: no valid pixel)
/// and texel size `(cols, rows)` -- the numbers the shader's meta takes.
#[derive(Clone, Copy, Debug)]
pub struct Meta {
    pub rect: [f64; 4],
    pub valid: Option<[f64; 4]>,
    pub size: (usize, usize),
}

pub struct Block {
    pub texture: glow::Texture,
    pub layers: u32,
    free: Vec<u32>,
    pub owners: HashMap<u32, Key>,
}

pub struct Store {
    max_bytes: usize,
    pub layers_per_block: u32,
    pub max_blocks: usize,
    /// block slot -> block; a slot keeps its number for life (page codes
    /// name it), None once its block was deleted
    pub blocks: Vec<Option<Block>>,
    pub slots: HashMap<Key, (usize, u32)>,
    pub meta: HashMap<Key, Meta>,
    used: HashMap<Key, u64>,
    tick: u64,
    /// (block, layer) written or emptied since the meta texture last synced
    pub dirty: Vec<(usize, u32)>,
    pub uploads: u64,
    pub evictions: u64,
    pub generation: u64,
}

/// `_block_layers` for uint8: a 64 MiB block, the GL limit, and at most a
/// quarter of the budget per block.
pub fn block_layers(max_bytes: usize, max_gl_layers: usize) -> u32 {
    (BLOCK_BYTES / SLOT_BYTES).min(max_gl_layers).min(max_bytes / (4 * SLOT_BYTES)).max(1) as u32
}

pub struct Prepared {
    pub deferred: usize,
    pub deferred_growth: usize,
    pub uploaded: Vec<Key>,
    pub error: Option<String>,
}

impl Store {
    pub fn new(max_bytes: usize, max_gl_layers: usize, max_blocks: usize) -> Self {
        Store { max_bytes, layers_per_block: block_layers(max_bytes, max_gl_layers),
                max_blocks, blocks: Vec::new(), slots: HashMap::new(), meta: HashMap::new(),
                used: HashMap::new(), tick: 0, dirty: Vec::new(), uploads: 0, evictions: 0,
                generation: 0 }
    }

    /// What admission keeps free: one partly filled block (`array_slack_bytes`).
    pub fn slack_bytes(&self) -> usize {
        self.layers_per_block as usize * SLOT_BYTES
    }

    pub fn allocated_bytes(&self) -> usize {
        self.live().map(|b| b.layers as usize * SLOT_BYTES).sum()
    }

    fn live(&self) -> impl Iterator<Item = &Block> {
        self.blocks.iter().flatten()
    }

    fn block(&mut self, i: usize) -> &mut Block {
        self.blocks[i].as_mut().expect("live block")
    }

    pub fn texture(&self, i: usize) -> Option<glow::Texture> {
        self.blocks.get(i).and_then(|b| b.as_ref()).map(|b| b.texture)
    }

    pub fn owner(&self, block: usize, layer: u32) -> Option<Key> {
        self.blocks.get(block)?.as_ref()?.owners.get(&layer).copied()
    }

    pub fn is_resident(&self, key: &Key) -> bool {
        self.slots.contains_key(key)
    }

    /// Upload what `items` lacks (draw order), mandatory first; the rest
    /// only until `deadline`. `may_grow` false: no new block (except for a
    /// mandatory tile). Tiles in `required` are never evicted.
    pub fn prepare(&mut self, gl: &glow::Context, items: &[(Key, Arc<Tile>, [f64; 4])],
                   mandatory: &HashSet<Key>, required: &HashSet<Key>,
                   deadline: Option<Instant>, may_grow: bool) -> Prepared {
        self.tick += 1;
        let mut out = Prepared { deferred: 0, deferred_growth: 0, uploaded: Vec::new(), error: None };
        let mut missing: Vec<&(Key, Arc<Tile>, [f64; 4])> = Vec::new();
        let mut seen = HashSet::new();
        for item in items {
            if self.slots.contains_key(&item.0) {
                self.used.insert(item.0, self.tick);
            } else if seen.insert(item.0) {
                missing.push(item);
            }
        }
        missing.sort_by_key(|it| !mandatory.contains(&it.0));
        unsafe { gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1) };
        for (key, tile, rect) in missing {
            let must = mandatory.contains(key);
            if !must && deadline.is_some_and(|d| Instant::now() >= d) {
                out.deferred += 1;
                continue;
            }
            let slot = match self.free_slot(gl, must || may_grow, required) {
                Ok(Some(s)) => s,
                Ok(None) => {
                    out.deferred += 1;
                    out.deferred_growth += 1;
                    continue;
                }
                Err(e) => {
                    out.error = Some(e);
                    out.deferred += 1;
                    continue;
                }
            };
            self.upload(gl, slot, tile);
            self.block(slot.0).owners.insert(slot.1, *key);
            self.slots.insert(*key, slot);
            self.used.insert(*key, self.tick);
            self.meta.insert(*key, Meta { rect: *rect, valid: crate::geom::valid_world_rect(rect, tile.valid, tile.cols, tile.rows),
                                          size: (tile.cols, tile.rows) });
            self.dirty.push(slot);
            self.uploads += 1;
            self.generation += 1;
            out.uploaded.push(*key);
        }
        unsafe { gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4) };
        out
    }

    fn upload(&self, gl: &glow::Context, (block, layer): (usize, u32), tile: &Tile) {
        unsafe {
            // set every time: creating a block (`warm`) resets it to 4, and
            // an edge tile's rows are not 4-byte multiples
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, self.texture(block));
            gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, layer as i32, tile.cols as i32,
                                tile.rows as i32, 1, glow::RED_INTEGER, glow::UNSIGNED_BYTE,
                                glow::PixelUnpackData::Slice(Some(&tile.data)));
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
        }
    }

    /// A free layer: an existing block's (fullest first), a new block
    /// (`grow`), or the least recently used tile not `required`.
    /// Ok(None): a new block was needed but growth is held back.
    fn free_slot(&mut self, gl: &glow::Context, grow: bool, required: &HashSet<Key>)
                 -> Result<Option<(usize, u32)>, String> {
        let fullest = (0..self.blocks.len())
            .filter(|&i| self.blocks[i].as_ref().is_some_and(|b| !b.free.is_empty()))
            .min_by_key(|&i| self.blocks[i].as_ref().map_or(0, |b| b.free.len()));
        if let Some(i) = fullest {
            return Ok(Some((i, self.block(i).free.pop().unwrap())));
        }
        let block_bytes = self.layers_per_block as usize * SLOT_BYTES;
        if self.allocated_bytes() + block_bytes <= self.max_bytes && self.free_block_slot().is_some() {
            if !grow {
                return Ok(None);
            }
            let i = self.new_block(gl)?;
            return Ok(Some((i, self.block(i).free.pop().unwrap())));
        }
        let victim = self.used.iter().filter(|(k, _)| !required.contains(k))
            .min_by_key(|(_, t)| **t).map(|(k, _)| *k);
        match victim {
            Some(k) => {
                let slot = self.evict(&k);
                self.block(slot.0).free.retain(|&l| l != slot.1);
                Ok(Some(slot))
            }
            None => Err("the GPU store cannot fit the active tiles without evicting one in use".into()),
        }
    }

    fn evict(&mut self, key: &Key) -> (usize, u32) {
        let slot = self.slots.remove(key).expect("resident");
        self.used.remove(key);
        self.meta.remove(key);
        let b = self.block(slot.0);
        b.owners.remove(&slot.1);
        b.free.push(slot.1);
        self.dirty.push(slot);
        self.evictions += 1;
        self.generation += 1;
        slot
    }

    /// The first block number free to use (a deleted one first), while the
    /// shader's samplers allow another.
    fn free_block_slot(&self) -> Option<usize> {
        match self.blocks.iter().position(|b| b.is_none()) {
            Some(i) => Some(i),
            None if self.blocks.len() < self.max_blocks => Some(self.blocks.len()),
            None => None,
        }
    }

    fn new_block(&mut self, gl: &glow::Context) -> Result<usize, String> {
        let layers = self.layers_per_block;
        let slot = self.free_block_slot().ok_or("no sampler left for another block")?;
        unsafe {
            let tex = gl.create_texture()?;
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
            for (p, v) in [(glow::TEXTURE_MIN_FILTER, glow::NEAREST), (glow::TEXTURE_MAG_FILTER, glow::NEAREST),
                           (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE)] {
                gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, p, v as i32);
            }
            gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MAX_LEVEL, 0);
            gl.tex_image_3d(glow::TEXTURE_2D_ARRAY, 0, glow::R8UI as i32, TILE as i32, TILE as i32,
                            layers as i32, 0, glow::RED_INTEGER, glow::UNSIGNED_BYTE,
                            glow::PixelUnpackData::Slice(None));
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
            crate::gpu_warm::warm(gl, tex, layers);
            let block = Block { texture: tex, layers, free: (0..layers).rev().collect(),
                                owners: HashMap::new() };
            if slot == self.blocks.len() { self.blocks.push(Some(block)); } else { self.blocks[slot] = Some(block); }
            Ok(slot)
        }
    }

    /// Allocate blocks ahead (initialisation / a quiet moment) while the
    /// budget and the shader's samplers allow `count` more.
    pub fn reserve(&mut self, gl: &glow::Context, count: usize) -> usize {
        let block_bytes = self.layers_per_block as usize * SLOT_BYTES;
        let mut made = 0;
        while made < count && self.allocated_bytes() + block_bytes <= self.max_bytes
            && self.free_block_slot().is_some() {
            if self.new_block(gl).is_err() {
                break;
            }
            made += 1;
        }
        made
    }

    pub fn free_layers(&self) -> usize {
        self.live().map(|b| b.free.len()).sum()
    }

    /// Delete every texture (context current).
    pub fn clear(&mut self, gl: &glow::Context) {
        for b in self.blocks.drain(..).flatten() {
            unsafe { gl.delete_texture(b.texture) };
        }
        self.slots.clear();
        self.meta.clear();
        self.used.clear();
        self.dirty.clear();
        self.generation += 1;
    }

    /// Drop every tile not in `keep`, then every block left empty
    /// (`release_unused`); context current.
    pub fn release_except(&mut self, gl: &glow::Context, keep: &HashSet<Key>) {
        let gone: Vec<Key> = self.slots.keys().filter(|k| !keep.contains(k)).copied().collect();
        for k in gone {
            self.evict(&k);
        }
        for b in self.blocks.iter_mut() {
            if b.as_ref().is_some_and(|b| b.owners.is_empty()) {
                unsafe { gl.delete_texture(b.take().unwrap().texture) };
            }
        }
        self.dirty.retain(|(blk, _)| self.blocks.get(*blk).is_some_and(|b| b.is_some()));
    }
}

