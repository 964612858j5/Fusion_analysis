//! Admission: which level's fine tiles fit, for every active channel at
//! once (`Step1GpuBinding._admit_new`, raw uint8 channels).
//!
//! From the level the controller asked for towards coarse, the first level
//! whose visible tiles fit the per-channel tile and byte limits AND, with
//! the complete coarse of every active channel, the total budget. None
//! fits: the coarse alone is drawn ("resolution limited"). Bytes count
//! whole 512x512 array slots, as the layer allocates them.

use crate::geom::{Pyramid, TILE};

#[derive(Clone, Debug)]
pub struct Budgets {
    pub max_fine_tiles: usize,
    pub max_fine_bytes_per_channel: u64,
    /// The total raw-texture budget LESS the array block slack, or None.
    pub total: Option<u64>,
    /// Bytes per texel of every channel (1: uint8).
    pub texel_bytes: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Admission {
    pub ideal: u8,
    pub level: u8,
    /// The admitted level's visible tiles, sorted by (tx, ty); empty when
    /// only the coarse is drawn.
    pub tiles: Vec<(u32, u32)>,
    pub coarse_only: bool,
    /// Per channel: this level's fine bytes, and the stand-in allowance.
    pub fine_bytes: u64,
    pub carry: u64,
    /// The world rect the admitted tiles cover (None: coarse only).
    pub rect: Option<[f64; 4]>,
    /// The complete coarse of the active channels alone exceeds the total
    /// budget: nothing new is drawn, the last frame stays (`base_overflow`).
    pub base_overflow: bool,
}

impl Admission {
    pub fn limited(&self) -> bool {
        self.level != self.ideal || self.coarse_only
    }
}

/// Texels of `tiles` at `level`, a whole slot per tile that exists
/// (`_tiles_pixels` in array mode).
pub fn slot_pixels(p: &Pyramid, level: u8, tiles: &[(u32, u32)]) -> u64 {
    tiles.iter()
        .filter(|&&(tx, ty)| p.tile_shape(crate::geom::TileAt { level, tx, ty }).0 > 0)
        .count() as u64 * (TILE * TILE) as u64
}

/// `channels`: how many channels are active; `bbox_l0` the controller's
/// clamped view box; `ideal` its level.
pub fn admit(p: &Pyramid, ideal: u8, bbox_l0: [i64; 4], channels: usize, b: &Budgets) -> Admission {
    let coarsest = p.coarsest();
    let n = channels.max(1) as u64;
    // the complete coarse: every tile of the coarsest level (`_full_level_keys`)
    let (across, down) = p.grid(coarsest);
    let coarse_bytes = (across * down) as u64 * (TILE * TILE) as u64 * b.texel_bytes * channels as u64;
    let mut chosen: Option<(u8, Vec<(u32, u32)>, u64, u64)> = None;
    for level in ideal..=coarsest {
        let mut tiles = p.cover(bbox_l0, level);
        tiles.sort();
        let fine = slot_pixels(p, level, &tiles) * b.texel_bytes;
        if tiles.len() > b.max_fine_tiles || fine > b.max_fine_bytes_per_channel {
            continue;
        }
        // at the coarsest level the coarse already holds every tile there
        let used = if level == coarsest { coarse_bytes } else { coarse_bytes + fine * channels as u64 };
        if b.total.is_none_or(|t| used <= t) {
            chosen = Some((level, tiles, fine, used));
            break;
        }
    }
    let coarse_only = chosen.is_none();
    let (level, tiles, fine, used) = chosen.unwrap_or((coarsest, Vec::new(), 0, coarse_bytes));
    let room = b.max_fine_bytes_per_channel.saturating_sub(fine);
    let carry = match b.total {
        None => room,
        Some(t) => (t.saturating_sub(used) / n).min(room),
    };
    let rect = if coarse_only { None } else { p.tiles_world_rect(level, &tiles) };
    let base_overflow = b.total.is_some_and(|t| coarse_bytes > t);
    Admission { ideal, level, tiles, coarse_only, fine_bytes: fine, carry, rect, base_overflow }
}
