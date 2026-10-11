//! Pyramid geometry, exactly as the Python viewer computes it.
//!
//! Two downsamples are in use and must not be mixed up:
//! * PLANNING (which tiles cover a view): ROUNDED, one number per level,
//!   `round(h0 / hL)` (`RawTileProvider.level_downsample`,
//!   `Step1GpuBinding._level_ds`);
//! * PLACEMENT (where a tile sits in the world): UNROUNDED, per axis,
//!   `(h0 / hL, w0 / wL)` (`level_downsample_yx`, `_world_rect`).
//! Edge tiles keep their real size (never padded to 512): their world rect
//! is `x0 + width * ds_x`. All arithmetic is f64 in Python's order; values
//! become f32 only where the GPU takes them.

pub const TILE: usize = 512;

/// One tile of one pyramid level (the channel is carried elsewhere).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TileAt {
    pub level: u8,
    pub tx: u32,
    pub ty: u32,
}

/// The pyramid's level shapes, `(height, width)` per level, level 0 first.
#[derive(Clone, Debug)]
pub struct Pyramid {
    pub shapes: Vec<(usize, usize)>,
}

/// Python's `round()` on a float: halves go to the even neighbour.
pub fn round_half_even(v: f64) -> f64 {
    let r = v.round();
    if (v - v.trunc()).abs() == 0.5 && r % 2.0 != 0.0 { r - v.signum() } else { r }
}

impl Pyramid {
    pub fn levels(&self) -> usize {
        self.shapes.len()
    }

    pub fn coarsest(&self) -> u8 {
        (self.shapes.len() - 1) as u8
    }

    /// Planning downsample: `round(h0 / hL)` (1.0 when hL is 0).
    pub fn ds_planning(&self, level: u8) -> f64 {
        let (h0, _) = self.shapes[0];
        let (hn, _) = self.shapes[level as usize];
        if hn == 0 { 1.0 } else { round_half_even(h0 as f64 / hn as f64) }
    }

    /// Placement downsample `(ds_y, ds_x)`, unrounded, per axis.
    pub fn ds_yx(&self, level: u8) -> (f64, f64) {
        let (h0, w0) = self.shapes[0];
        let (hn, wn) = self.shapes[level as usize];
        let ds_y = if hn > 0 { h0 as f64 / hn as f64 } else { 1.0 };
        let ds_x = if wn > 0 { w0 as f64 / wn as f64 } else { 1.0 };
        (ds_y, ds_x)
    }

    /// `(across, down)` tiles of a level.
    pub fn grid(&self, level: u8) -> (u32, u32) {
        let (h, w) = self.shapes[level as usize];
        (w.div_ceil(TILE) as u32, h.div_ceil(TILE) as u32)
    }

    /// The tile's stored `(rows, cols)`, cut at the level's edge; (0, 0)
    /// for a tile outside the level (`_level_rect` / `_tiles_pixels`).
    pub fn tile_shape(&self, t: TileAt) -> (usize, usize) {
        let (h, w) = self.shapes[t.level as usize];
        let rows = h.saturating_sub(t.ty as usize * TILE).min(TILE);
        let cols = w.saturating_sub(t.tx as usize * TILE).min(TILE);
        if rows == 0 || cols == 0 { (0, 0) } else { (rows, cols) }
    }

    /// The tile's world rect `(x0, x1, y0, y1)` (`_world_rect`), or None
    /// for a tile outside the level.
    pub fn tile_world_rect(&self, t: TileAt) -> Option<[f64; 4]> {
        let (rows, cols) = self.tile_shape(t);
        if rows == 0 {
            return None;
        }
        let (ds_y, ds_x) = self.ds_yx(t.level);
        let x0 = (t.tx as usize * TILE) as f64 * ds_x;
        let y0 = (t.ty as usize * TILE) as f64 * ds_y;
        Some([x0, x0 + cols as f64 * ds_x, y0, y0 + rows as f64 * ds_y])
    }

    /// The tiles of `level` covering a level-0 box `bbox_l0` =
    /// `(y0, x0, y1, x1)` (already clamped and truncated by the controller):
    /// each corner divided by the planning downsample and truncated, then
    /// covered with floor/ceil (`visible_tiles_for_viewport`). Sorted by
    /// (ty, tx). Like Python, the cover is not clipped to the level grid.
    pub fn cover(&self, bbox_l0: [i64; 4], level: u8) -> Vec<(u32, u32)> {
        let ds = self.ds_planning(level);
        let conv = |v: i64| (v as f64 / ds) as i64; // int() truncates toward 0
        let (y0, x0, y1, x1) = (conv(bbox_l0[0]), conv(bbox_l0[1]), conv(bbox_l0[2]), conv(bbox_l0[3]));
        if y1 <= y0 || x1 <= x0 {
            return Vec::new();
        }
        let t = TILE as i64;
        let (tx0, tx1) = (x0.div_euclid(t), (x1 - 1).div_euclid(t));
        let (ty0, ty1) = (y0.div_euclid(t), (y1 - 1).div_euclid(t));
        let mut out = Vec::new();
        for ty in ty0..=ty1 {
            for tx in tx0..=tx1 {
                if tx >= 0 && ty >= 0 {
                    out.push((tx as u32, ty as u32));
                }
            }
        }
        out
    }

    /// The world rect covered by `tiles` of `level` (`_tiles_world_rect`).
    pub fn tiles_world_rect(&self, level: u8, tiles: &[(u32, u32)]) -> Option<[f64; 4]> {
        let mut acc: Option<[f64; 4]> = None;
        for &(tx, ty) in tiles {
            if let Some(r) = self.tile_world_rect(TileAt { level, tx, ty }) {
                acc = Some(match acc {
                    None => r,
                    Some(a) => [a[0].min(r[0]), a[1].max(r[1]), a[2].min(r[2]), a[3].max(r[3])],
                });
            }
        }
        acc
    }
}

/// Does a rect `(x0, x1, y0, y1)` meet `view` (`Step1GpuBinding._overlaps`)?
pub fn overlaps(r: &[f64; 4], view: &Option<[f64; 4]>) -> bool {
    match view {
        None => false,
        Some(v) => r[0] < v[1] && r[1] > v[0] && r[2] < v[3] && r[3] > v[2],
    }
}

/// The output pixels whose centre lies inside `roi` (`roi_scissor_box`):
/// `(x, y, width, height)` in GL pixels (origin bottom-left), or None when
/// there is no region. World y grows downward on screen.
pub fn roi_scissor_box(view: [f64; 4], roi: Option<[f64; 4]>, size: (i32, i32)) -> Option<[i32; 4]> {
    let [bx0, bx1, by0, by1] = roi?;
    let [vx0, vx1, vy0, vy1] = view;
    let (w, h) = size;
    if w <= 0 || h <= 0 || vx1 <= vx0 || vy1 <= vy0 || bx1 <= bx0 || by1 <= by0 {
        return Some([0, 0, 0, 0]);
    }
    let (sx, sy) = ((vx1 - vx0) / w as f64, (vy1 - vy0) / h as f64);
    let first_col = ((bx0 - vx0) / sx - 0.5).ceil() as i64;
    let stop_col = ((bx1 - vx0) / sx - 0.5).ceil() as i64;
    let first_row = ((vy1 - by1) / sy - 0.5).floor() as i64 + 1;
    let stop_row = ((vy1 - by0) / sy - 0.5).floor() as i64 + 1;
    let c = |v: i64, hi: i32| v.clamp(0, hi as i64) as i32;
    let (x, right, y, top) = (c(first_col, w), c(stop_col, w), c(first_row, h), c(stop_row, h));
    Some([x, y, (right - x).max(0), (top - y).max(0)])
}

/// `_valid_world_rect`: a tile's valid pixels `(y0, y1, x0, x1)` (tile
/// local) as a world rect, or None when it has no valid pixel.
pub fn valid_world_rect(rect: &[f64; 4], valid: Option<[u32; 4]>, cols: usize, rows: usize) -> Option<[f64; 4]> {
    let [y0v, y1v, x0v, x1v] = valid?;
    if y1v <= y0v || x1v <= x0v {
        return None;
    }
    let [x0, x1, y0, y1] = *rect;
    let (sx, sy) = ((x1 - x0) / cols as f64, (y1 - y0) / rows as f64);
    Some([x0 + x0v as f64 * sx, x0 + x1v as f64 * sx, y0 + y0v as f64 * sy, y0 + y1v as f64 * sy])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_round_is_half_even() {
        assert_eq!(round_half_even(2.5), 2.0);
        assert_eq!(round_half_even(3.5), 4.0);
        assert_eq!(round_half_even(32.01), 32.0);
    }

    #[test]
    fn edge_tiles_are_not_padded() {
        let p = Pyramid { shapes: vec![(1000, 700), (500, 350)] };
        assert_eq!(p.tile_shape(TileAt { level: 0, tx: 1, ty: 1 }), (488, 188));
        assert_eq!(p.tile_world_rect(TileAt { level: 1, tx: 0, ty: 0 }),
                   Some([0.0, 700.0, 0.0, 1000.0]));
    }
}
