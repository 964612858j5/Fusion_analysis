//! One viewer tile of a mask (`core.step3_masks.read_label_tile`, R3b plan
//! §2.1): the tile's ids, uint32, rows x columns on its level, 0 where the
//! mask does not reach, with its world rect (x0, x1, y0, y1) -- x0 = tx * T
//! * ds_x, width = columns * ds_x, per-axis unrounded ratios, as the viewer
//! places its own tiles. Level 0 reads the mask (slide pixel minus the box
//! origin); level L the pyramid's level L (minus its origin); a level the
//! pyramid lacks is all zeros. Unavailable: a coarse level without a
//! pyramid, or a tile outside its level.

use crate::error::Result;
use crate::geom::{Pyramid, TILE};
use crate::label_source::KindSource;
use crate::zarr_v2::U32Array;

#[derive(Debug, PartialEq)]
pub struct LabelTile {
    pub rows: usize,
    pub cols: usize,
    pub ids: Vec<u32>,
    pub rect: [f64; 4],
}

#[derive(Debug, PartialEq)]
pub enum Read {
    Tile(LabelTile),
    Unavailable(String),
}

/// `read_label_tile(source, level, tx, ty, TILE, pyr.shapes)`.
pub fn read_label_tile(source: &KindSource, pyr: &Pyramid, level: u8, tx: u32, ty: u32) -> Result<Read> {
    let shapes = &pyr.shapes;
    if level as usize >= shapes.len() {
        return Ok(Read::Unavailable(format!("the viewer has no level {level}")));
    }
    let (h, w) = shapes[level as usize];
    let t = TILE;
    let (r0, c0) = (ty as usize * t, tx as usize * t);
    let (r1, c1) = ((r0 + t).min(h), (c0 + t).min(w));
    if r1 <= r0 || c1 <= c0 {
        return Ok(Read::Unavailable(format!("tile ({tx}, {ty}) is outside level {level}")));
    }
    // the same float operations, in the same order, as Python
    let ds_y = shapes[0].0 as f64 / h as f64;
    let ds_x = shapes[0].1 as f64 / w as f64;
    let rect = [c0 as f64 * ds_x, c0 as f64 * ds_x + (c1 - c0) as f64 * ds_x,
                r0 as f64 * ds_y, r0 as f64 * ds_y + (r1 - r0) as f64 * ds_y];
    let (rows, cols) = (r1 - r0, c1 - c0);
    let (array, origin): (&U32Array, [i64; 2]) = if level == 0 {
        (&source.level0, source.origin0)
    } else {
        let Some(levels) = &source.pyramid else {
            return Ok(Read::Unavailable("no label pyramid".into()));
        };
        match levels.iter().find(|lv| lv.level == level) {
            Some(lv) => (&lv.array, lv.origin),
            None => return Ok(Read::Tile(LabelTile { rows, cols, ids: vec![0; rows * cols], rect })),
        }
    };
    let mut ids = vec![0u32; rows * cols];
    let (oy, ox) = (origin[0], origin[1]);
    let (ah, aw) = (array.shape[0] as i64, array.shape[1] as i64);
    let (a0, a1) = ((r0 as i64).max(oy), (r1 as i64).min(oy + ah));
    let (b0, b1) = ((c0 as i64).max(ox), (c1 as i64).min(ox + aw));
    if a1 > a0 && b1 > b0 {
        let got = array.read_rect((a0 - oy) as usize, (a1 - oy) as usize, (b0 - ox) as usize, (b1 - ox) as usize)?;
        let gw = (b1 - b0) as usize;
        for (i, y) in (a0..a1).enumerate() {
            let dst = (y as usize - r0) * cols + (b0 as usize - c0);
            ids[dst..dst + gw].copy_from_slice(&got[i * gw..(i + 1) * gw]);
        }
    }
    Ok(Read::Tile(LabelTile { rows, cols, ids, rect }))
}
