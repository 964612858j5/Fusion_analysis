//! Giving GPU blocks back (`_TileArrayStore._free_slot`'s "room for a new
//! block"): when a block of one format does not fit the budget or its
//! sampler family, empty blocks of other formats go first, then blocks of
//! other formats none of whose tiles the frame needs (their tiles are
//! evicted). And when the sampler split changes (`frame::top_up`), blocks
//! of channels no longer drawn leave a shrinking family and the rest are
//! renumbered into it.

use super::Store;
use crate::cache::Key;
use crate::family::Format;
use std::collections::HashSet;

impl Store {
    /// Evict every tile of block `i` and delete its texture.
    fn drop_block(&mut self, gl: &glow::Context, i: usize) {
        use glow::HasContext;
        let owners: Vec<Key> = self.blocks[i].as_ref().map_or(Vec::new(), |b| b.owners.values().copied().collect());
        for k in owners {
            self.evict(&k);
        }
        if let Some(b) = self.blocks[i].take() {
            unsafe { gl.delete_texture(b.texture) };
        }
        self.dirty.retain(|(blk, _)| *blk != i);
    }

    /// A block of another format than `format` to give back, in
    /// `family` (any family when None): an empty one first, then one none
    /// of whose tiles is `required`.
    fn spare_block(&self, format: Format, family: Option<usize>, required: &HashSet<Key>) -> Option<usize> {
        let fits = |i: usize| self.blocks[i].as_ref()
            .is_some_and(|b| b.format != format && family.is_none_or(|f| b.format.family() == f));
        let all = 0..self.blocks.len();
        all.clone().find(|&i| fits(i) && self.blocks[i].as_ref().is_some_and(|b| b.owners.is_empty()))
            .or_else(|| all.into_iter().find(|&i| fits(i) && self.blocks[i].as_ref()
                .is_some_and(|b| !b.owners.values().any(|k| required.contains(k)))))
    }

    /// Could one block of `format` be added after giving back spare blocks?
    pub(super) fn could_make_room(&self, format: Format, required: &HashSet<Key>) -> bool {
        let family = self.free_index(format).is_none().then(|| format.family());
        self.may_add(format) || self.spare_block(format, family, required).is_some()
    }

    /// Give back spare blocks of other formats until one block of
    /// `format` fits the budget and its family has a free sampler. True
    /// when it fits now.
    pub(super) fn make_room(&mut self, gl: &glow::Context, format: Format, required: &HashSet<Key>) -> bool {
        while !self.may_add(format) {
            let family = self.free_index(format).is_none().then(|| format.family());
            let Some(i) = self.spare_block(format, family, required) else { return false };
            self.drop_block(gl, i);
        }
        true
    }

    /// Reserving ahead (`frame::top_up`): give back blocks of other
    /// formats holding no tile of a channel in `drawn`, until a block of
    /// `format` fits. True when it fits now.
    pub fn reclaim_idle(&mut self, gl: &glow::Context, format: Format, drawn: &HashSet<u16>) -> bool {
        let busy: HashSet<Key> = self.slots.keys().filter(|k| drawn.contains(&k.0)).copied().collect();
        self.make_room(gl, format, &busy)
    }

    /// Take the sampler split `caps`: while a family has more blocks than
    /// its new share, blocks holding no tile of a channel in `drawn` are
    /// given back (empty ones first); the blocks left above the share are
    /// renumbered into free indices below it. False (nothing changed but
    /// the blocks given back) when a family still holds too many.
    pub fn fit_caps(&mut self, gl: &glow::Context, caps: [usize; 2], drawn: &HashSet<u16>) -> bool {
        for family in 0..2 {
            loop {
                let mine: Vec<usize> = (0..self.blocks.len())
                    .filter(|&i| self.blocks[i].as_ref().is_some_and(|b| b.format.family() == family)).collect();
                if mine.len() <= caps[family] {
                    break;
                }
                let idle = |i: &usize| self.blocks[*i].as_ref()
                    .is_some_and(|b| !b.owners.values().any(|k| drawn.contains(&k.0)));
                let empty = mine.iter().copied().filter(idle)
                    .min_by_key(|&i| self.blocks[i].as_ref().map_or(0, |b| b.owners.len()));
                let Some(i) = empty else { return false };
                self.drop_block(gl, i);
            }
        }
        for family in 0..2 {
            let above: Vec<usize> = (0..self.blocks.len()).filter(|&i| self.blocks[i].as_ref()
                .is_some_and(|b| b.format.family() == family && b.vt_index >= caps[family])).collect();
            for i in above {
                let taken: HashSet<usize> = self.blocks.iter().flatten()
                    .filter(|b| b.format.family() == family).map(|b| b.vt_index).collect();
                let free = (0..caps[family]).find(|v| !taken.contains(v)).expect("counted above");
                let b = self.blocks[i].as_mut().expect("live block");
                b.vt_index = free;
                let layers: Vec<u32> = b.owners.keys().copied().collect();
                self.dirty.extend(layers.into_iter().map(|l| (i, l)));
                self.generation += 1;                    // a composition must see the new numbers
            }
        }
        self.caps = caps;
        true
    }
}
