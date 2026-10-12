//! Where a new tile's layer comes from (`_TileArrayStore._free_slot`,
//! `_new_block`): a free layer of its format, a new block (target, budget,
//! a free family index), or the least recently used tile of its format;
//! spare blocks of other formats give their bytes or sampler back when
//! needed (`reclaim`).

use super::{Growth, Slot, Store};
use crate::cache::Key;
use crate::family::Format;
use crate::gpu_block::{self, Block};
use std::collections::{HashMap, HashSet};
use std::time::Instant;

impl Store {
    /// May a block of `format` be added (budget, a free family index)?
    pub(super) fn may_add(&self, format: Format) -> bool {
        self.allocated_bytes() + self.block_bytes(format) <= self.max_bytes && self.free_index(format).is_some()
    }

    /// The lowest free index of `format`'s family under its cap.
    pub(super) fn free_index(&self, format: Format) -> Option<usize> {
        let family = format.family();
        let taken: HashSet<usize> = self.live().filter(|b| b.format.family() == family).map(|b| b.vt_index).collect();
        (0..self.caps[family]).find(|i| !taken.contains(i))
    }

    /// A free layer for a tile of `format`: an existing block's of that
    /// format (fullest first); a new block while fewer than its target
    /// exist (`grow`); the least recently used tile of that format not
    /// `required`; else a new block within the budget and the family's
    /// samplers, spare blocks of other formats given back first (`grow`).
    /// `victims`: per format, this preparation's eviction order, built the
    /// first time one is needed (least recently used last), so a frame
    /// sorts the residents once instead of scanning them per tile.
    pub(super) fn free_slot(&mut self, gl: &glow::Context, format: Format, grow: bool, required: &HashSet<Key>,
                 victims: &mut HashMap<Format, Vec<Key>>) -> Result<Slot, String> {
        let fullest = (0..self.blocks.len())
            .filter(|&i| self.blocks[i].as_ref().is_some_and(|b| b.format == format && !b.free.is_empty()))
            .min_by_key(|&i| self.blocks[i].as_ref().map_or(0, |b| b.free.len()));
        if let Some(i) = fullest {
            return Ok(Slot::Got((i, self.block(i).free.pop().unwrap())));
        }
        let target = self.targets.get(&format).copied().unwrap_or(0);
        if grow && self.live_blocks(format) < target && self.may_add(format) {
            return self.grow_slot(gl, format);
        }
        if !victims.contains_key(&format) {
            let mut order: Vec<(u64, Key)> = self.used.iter()
                .filter(|(k, _)| !required.contains(k) && self.slots.get(k)
                    .and_then(|(i, _)| self.blocks[*i].as_ref()).is_some_and(|b| b.format == format))
                .map(|(k, t)| (*t, *k)).collect();
            order.sort_unstable_by(|a, b| b.0.cmp(&a.0).then(b.1.cmp(&a.1)));
            victims.insert(format, order.into_iter().map(|(_, k)| k).collect());
        }
        // (a key gone since the order was built -- a spare block given back -- is skipped)
        while let Some(k) = victims.get_mut(&format).and_then(Vec::pop) {
            if !self.slots.contains_key(&k) {
                continue;
            }
            let slot = self.evict(&k);
            self.block(slot.0).free.retain(|&l| l != slot.1);
            return Ok(Slot::Got(slot));
        }
        if !self.could_make_room(format, required) {
            return self.no_room(format);
        }
        if !grow {
            return Ok(Slot::Held);
        }
        if !self.make_room(gl, format, required) {
            return self.no_room(format);
        }
        self.grow_slot(gl, format)
    }

    /// Nothing to evict and no block to add: the family's samplers are
    /// all taken (the capacity fallback) or the budget is.
    fn no_room(&self, format: Format) -> Result<Slot, String> {
        if self.free_index(format).is_none() {
            return Ok(Slot::Full);
        }
        Err("the GPU store cannot fit the active tiles without evicting one in use".into())
    }

    pub(super) fn grow_slot(&mut self, gl: &glow::Context, format: Format) -> Result<Slot, String> {
        let i = self.new_block(gl, format)?;
        Ok(Slot::Got((i, self.block(i).free.pop().unwrap())))
    }

    pub(super) fn evict(&mut self, key: &Key) -> (usize, u32) {
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

    pub(super) fn new_block(&mut self, gl: &glow::Context, format: Format) -> Result<usize, String> {
        let layers = self.layers(format);
        let vt_index = self.free_index(format).ok_or("no sampler left for another block")?;
        let slot = self.blocks.iter().position(|b| b.is_none()).unwrap_or(self.blocks.len());
        unsafe {
            let t = Instant::now();
            let tex = gpu_block::create_texture(gl, format, layers, false)?;
            let alloc_ms = t.elapsed().as_secs_f64() * 1000.0;
            let t = Instant::now();
            let (touch_status, gl_error) = self.pay_first_use(gl, tex, format, layers);
            let warm_ms = t.elapsed().as_secs_f64() * 1000.0;
            self.growths.push(Growth { at: crate::clock::monotonic(), format, layers, alloc_ms, warm_ms,
                                       touch_status, gl_error });
            let block = Block { texture: tex, format, layers, vt_index, free: (0..layers).rev().collect(),
                                owners: HashMap::new() };
            if slot == self.blocks.len() { self.blocks.push(Some(block)); } else { self.blocks[slot] = Some(block); }
            Ok(slot)
        }
    }

    /// A new block's first use, paid now (`gpu_touch`: attach, one sampled
    /// texel), or the old warm when switched back or the draw cannot be built.
    pub(super) unsafe fn pay_first_use(&mut self, gl: &glow::Context, tex: glow::Texture, format: Format, layers: u32)
                            -> (u32, u32) {
        unsafe {
            if !self.old_warm && self.touch.is_none() {
                match crate::gpu_touch::TouchKit::new(gl) {
                    Ok(kit) => self.touch = Some(kit),
                    Err(_) => self.old_warm = true,
                }
            }
            match &self.touch {
                Some(kit) if !self.old_warm => kit.touch(gl, tex, layers, format),
                _ => { crate::gpu_warm::warm(gl, tex, layers, format); (0, 0) }
            }
        }
    }
}
