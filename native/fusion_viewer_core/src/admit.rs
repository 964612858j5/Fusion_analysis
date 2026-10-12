//! Admission: which level's fine tiles fit, for every active channel at
//! once (`Step1GpuBinding._admit_new`), each channel at its own bytes per
//! texel (raw: its stored integer size; everything else 4, `_pixel_bytes`).
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
    /// A channel whose complete coarse is more bytes than this is not
    /// drawn (`max_coarse_plane_bytes_per_channel`; None: no limit).
    pub max_coarse_bytes: Option<u64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Admission {
    pub ideal: u8,
    pub level: u8,
    /// The admitted level's visible tiles, sorted by (tx, ty); empty when
    /// only the coarse is drawn.
    pub tiles: Vec<(u32, u32)>,
    pub coarse_only: bool,
    /// The admitted tiles' texels (whole slots): a channel's fine bytes
    /// are this times its bytes per texel.
    pub fine_pixels: u64,
    /// Every channel's equal share of what the total leaves (None: no
    /// total); a channel's stand-in allowance is this capped by its own
    /// room (`carry`).
    pub share: Option<u64>,
    pub fine_budget: u64,
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

    pub fn fine_bytes(&self, texel: u64) -> u64 {
        self.fine_pixels * texel
    }

    /// The stand-in allowance of a channel of `texel` bytes per texel.
    pub fn carry(&self, texel: u64) -> u64 {
        let room = self.fine_budget.saturating_sub(self.fine_bytes(texel));
        self.share.map_or(room, |s| s.min(room))
    }
}

/// Texels of `tiles` at `level`, a whole slot per tile that exists
/// (`_tiles_pixels` in array mode).
pub fn slot_pixels(p: &Pyramid, level: u8, tiles: &[(u32, u32)]) -> u64 {
    tiles.iter()
        .filter(|&&(tx, ty)| p.tile_shape(crate::geom::TileAt { level, tx, ty }).0 > 0)
        .count() as u64 * (TILE * TILE) as u64
}

/// `texels`: bytes per texel of each active channel; `bbox_l0` the
/// controller's clamped view box; `ideal` its level.
pub fn admit(p: &Pyramid, ideal: u8, bbox_l0: [i64; 4], texels: &[u64], b: &Budgets) -> Admission {
    let coarsest = p.coarsest();
    let n = texels.len().max(1) as u64;
    let (sum_texel, max_texel) = (texels.iter().sum::<u64>(), texels.iter().copied().max().unwrap_or(1));
    // the complete coarse: every tile of the coarsest level (`_full_level_keys`)
    let (across, down) = p.grid(coarsest);
    let coarse_bytes = (across * down) as u64 * (TILE * TILE) as u64 * sum_texel;
    let mut chosen: Option<(u8, Vec<(u32, u32)>, u64, u64)> = None;
    for level in ideal..=coarsest {
        let mut tiles = p.cover(bbox_l0, level);
        tiles.sort();
        let pixels = slot_pixels(p, level, &tiles);
        if tiles.len() > b.max_fine_tiles || pixels * max_texel > b.max_fine_bytes_per_channel {
            continue;
        }
        // at the coarsest level the coarse already holds every tile there
        let used = if level == coarsest { coarse_bytes } else { coarse_bytes + pixels * sum_texel };
        if b.total.is_none_or(|t| used <= t) {
            chosen = Some((level, tiles, pixels, used));
            break;
        }
    }
    let coarse_only = chosen.is_none();
    let (level, tiles, pixels, used) = chosen.unwrap_or((coarsest, Vec::new(), 0, coarse_bytes));
    let share = b.total.map(|t| t.saturating_sub(used) / n);
    let rect = if coarse_only { None } else { p.tiles_world_rect(level, &tiles) };
    let base_overflow = b.total.is_some_and(|t| coarse_bytes > t);
    Admission { ideal, level, tiles, coarse_only, fine_pixels: pixels, share,
                fine_budget: b.max_fine_bytes_per_channel, rect, base_overflow }
}
