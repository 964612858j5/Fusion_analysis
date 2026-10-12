//! The GPU tile store (`step1_gpu_layer._TileArrayStore`): tiles in the
//! layers of array-texture blocks, one storage format per block (u8 /
//! u16 / f32, `family`), each block numbered within its sampler family.
//!
//! Every block counts whole against the budget. A tile needed by the frame
//! being prepared is never evicted; otherwise the least recently used tile
//! of the SAME format gives its layer. When the budget or the family's
//! samplers are full, blocks of other formats with no tile the frame needs
//! are given back (`reclaim`). A family has as many
//! blocks as the shader binds samplers for it (`caps`); a tile that finds
//! its family full with nothing to evict is reported (`Prepared::capacity`)
//! and the picture goes back to the Python layer (R2 plan §2.3).
//! Blocks are created ahead of need (`frame::top_up`) and paid for at
//! creation (`gpu_touch`, docs §48).
//! Design reference (no code copied): Odon draws tiles from GPU-resident
//! pages, one draw for many tiles.

use crate::cache::{Key, Tile};
use crate::family::Format;
use crate::gpu_block::{self, Block, SLOT_PIXELS};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

mod alloc;
mod reclaim;

/// Per resident tile: world rect, valid world rect (None: no valid pixel)
/// and texel size `(cols, rows)` -- the numbers the shader's meta takes.
#[derive(Clone, Copy, Debug)]
pub struct Meta {
    pub rect: [f64; 4],
    pub valid: Option<[f64; 4]>,
    pub size: (usize, usize),
}

/// One block creation: when, its format and size, its two stages (ms).
#[derive(Clone, Copy, Debug)]
pub struct Growth {
    pub at: f64,
    pub format: Format,
    pub layers: u32,
    pub alloc_ms: f64,
    pub warm_ms: f64,
    /// the touch's attachment status and first GL error (0, 0 for the old warm)
    pub touch_status: u32,
    pub gl_error: u32,
}

pub struct Prepared {
    pub deferred: usize,
    pub deferred_growth: usize,
    pub uploaded: Vec<Key>,
    pub error: Option<String>,
    /// a tile's sampler family is full and nothing could be evicted
    pub capacity: bool,
}

pub(crate) enum Slot {
    Got((usize, u32)),
    Held,
    Full,
}

pub struct Store {
    max_bytes: usize,
    max_gl_layers: usize,
    /// blocks the shader binds per family: [integer, float]
    pub caps: [usize; 2],
    /// block number -> block; a number is kept for life (meta rows and
    /// page codes name the block's family index), None once deleted
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
    /// blocks created since last drained, for the A9 record (`gpu.grow`)
    pub growths: Vec<Growth>,
    /// blocks per format the frame wants to exist ahead of need
    /// (`frame::top_up`); up to these a new block comes before an eviction
    pub targets: HashMap<Format, usize>,
    touch: Option<crate::gpu_touch::TouchKit>,
    /// rollback switch BLOCK01_RUST_OLD_WARM=1: the old FBO warm (§48)
    old_warm: bool,
}

impl Store {
    pub fn new(max_bytes: usize, max_gl_layers: usize, caps: [usize; 2]) -> Self {
        Store { max_bytes, max_gl_layers, caps, blocks: Vec::new(), slots: HashMap::new(),
                meta: HashMap::new(), used: HashMap::new(), tick: 0, dirty: Vec::new(), uploads: 0,
                evictions: 0, generation: 0, growths: Vec::new(), targets: HashMap::new(), touch: None,
                old_warm: std::env::var_os("BLOCK01_RUST_OLD_WARM").is_some_and(|v| v == "1") }
    }

    pub fn layers(&self, format: Format) -> u32 {
        gpu_block::block_layers(self.max_bytes, self.max_gl_layers, format)
    }

    pub fn block_bytes(&self, format: Format) -> usize {
        self.layers(format) as usize * SLOT_PIXELS * format.bytes()
    }

    /// What admission keeps free (`array_slack_bytes`): one block per
    /// format in use.
    pub fn slack_bytes(&self, formats: &[Format]) -> usize {
        let set: HashSet<Format> = formats.iter().copied().collect();
        set.into_iter().map(|f| self.block_bytes(f)).sum()
    }

    pub fn allocated_bytes(&self) -> usize {
        self.live().map(Block::bytes).sum()
    }

    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    fn live(&self) -> impl Iterator<Item = &Block> {
        self.blocks.iter().flatten()
    }

    fn block(&mut self, i: usize) -> &mut Block {
        self.blocks[i].as_mut().expect("live block")
    }

    pub fn live_blocks(&self, format: Format) -> usize {
        self.live().filter(|b| b.format == format).count()
    }

    /// The block bound to sampler `u_u<i>` (family 0) / `u_f<i>` (1).
    pub fn texture_of(&self, family: usize, vt_index: usize) -> Option<glow::Texture> {
        self.live().find(|b| b.format.family() == family && b.vt_index == vt_index).map(|b| b.texture)
    }

    /// (family, family index) of block `i`.
    pub fn row_of(&self, i: usize) -> Option<(usize, usize)> {
        self.blocks.get(i)?.as_ref().map(|b| (b.format.family(), b.vt_index))
    }

    /// (family, family index, layer) of a resident tile: its page code.
    pub fn code(&self, key: &Key) -> Option<(usize, usize, u32)> {
        let &(i, layer) = self.slots.get(key)?;
        self.row_of(i).map(|(f, v)| (f, v, layer))
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
        let mut out = Prepared { deferred: 0, deferred_growth: 0, uploaded: Vec::new(), error: None,
                                 capacity: false };
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
        // per format, the eviction order (built once when first needed)
        let mut victims: HashMap<Format, Vec<Key>> = HashMap::new();
        for (key, tile, rect) in missing {
            let must = mandatory.contains(key);
            if !must && deadline.is_some_and(|d| Instant::now() >= d) {
                out.deferred += 1;
                continue;
            }
            let slot = match self.free_slot(gl, tile.format, must || may_grow, required, &mut victims) {
                Ok(Slot::Got(s)) => s,
                Ok(Slot::Held) => {
                    out.deferred += 1;
                    out.deferred_growth += 1;
                    continue;
                }
                Ok(Slot::Full) => {
                    out.capacity = true;
                    out.deferred += 1;
                    continue;
                }
                Err(e) => {
                    out.error = Some(e);
                    out.deferred += 1;
                    continue;
                }
            };
            unsafe { gpu_block::upload(gl, self.block(slot.0).texture, slot.1, tile) };
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
        out
    }

    /// Allocate up to `count` blocks of `format` ahead (budget and samplers
    /// permitting).
    pub fn reserve(&mut self, gl: &glow::Context, format: Format, count: usize) -> usize {
        let mut made = 0;
        while made < count && self.may_add(format) {
            if self.new_block(gl, format).is_err() {
                break;
            }
            made += 1;
        }
        made
    }

    /// Delete every texture (context current).
    pub fn clear(&mut self, gl: &glow::Context) {
        use glow::HasContext;
        for b in self.blocks.drain(..).flatten() {
            unsafe { gl.delete_texture(b.texture) };
        }
        if let Some(kit) = self.touch.take() {
            unsafe { kit.destroy(gl) };
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
        use glow::HasContext;
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
