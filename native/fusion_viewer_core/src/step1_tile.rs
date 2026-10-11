//! One Step1 tile of one channel, as `Step1TileProvider.read_tile_native`
//! makes it (plan R1 §2.5): values in their own format plus where they are
//! valid. `rect` is (y0, y1, x0, x1) in the tile's level, `stride` that
//! level's `round(h0/hk)` (`_level_rect`).
//!
//! * RAW: the ROI taken to the level outward (`clip_to_roi`), read from the
//!   pyramid's own level. u8/u16 stay integers, zero outside, valid on the
//!   clipped rectangle; other types become f32, NaN outside, valid where
//!   finite. A tile outside the ROI is zeros (u8), nothing valid.
//! * CORRECTED: the accepted coarse plane's slice at this stride, or the
//!   reduction of the level-0 rectangle (`reduce`); f32, NaN where not
//!   valid, valid where finite.
//! * MISSING: nothing valid; Python's tile is `tile_size` square of NaN.

use crate::corrected::{Plane, Source};
use crate::error::Result;
use crate::reader::{RawSource, Region};
use crate::reduce::{CorrectedRegion, Reduced, reduce};

#[derive(Debug)]
pub enum Values {
    U8(Vec<u8>),
    U16(Vec<u16>),
    F32(Vec<f32>),
}

#[derive(Debug, PartialEq)]
pub enum Valid {
    Nothing,
    /// tile-local (y0, y1, x0, x1)
    Rect([usize; 4]),
    /// where the f32 value is finite
    Finite,
}

#[derive(Debug)]
pub struct Tile {
    pub rows: usize,
    pub cols: usize,
    pub values: Values,
    pub valid: Valid,
}

/// What a raw channel's samples are (the manifest's dtype).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RawKind {
    U8,
    U16,
    I16,
    U32,
    I32,
    F32,
}

impl RawKind {
    pub fn parse(dtype: &str) -> Option<Self> {
        Some(match dtype.trim_start_matches(['<', '>', '=', '|']) {
            "uint8" | "u1" => RawKind::U8,
            "uint16" | "u2" => RawKind::U16,
            "int16" | "i2" => RawKind::I16,
            "uint32" | "u4" => RawKind::U32,
            "int32" | "i4" => RawKind::I32,
            "float32" | "f4" => RawKind::F32,
            _ => return None,
        })
    }
}

/// `clip_to_roi(rect, stride)`: the ROI taken outward to the level.
pub fn clip_to_roi(rect: [i64; 4], stride: usize, roi: Option<[i64; 4]>) -> Option<[i64; 4]> {
    let [y0, y1, x0, x1] = rect;
    let Some([mut by0, mut by1, mut bx0, mut bx1]) = roi else { return Some(rect) };
    let s = stride.max(1) as i64;
    if s > 1 {
        by0 = by0.div_euclid(s);
        bx0 = bx0.div_euclid(s);
        by1 = -((-by1).div_euclid(s));
        bx1 = -((-bx1).div_euclid(s));
    }
    let (cy0, cy1, cx0, cx1) = (y0.max(by0), y1.min(by1), x0.max(bx0), x1.min(bx1));
    (cy1 > cy0 && cx1 > cx0).then_some([cy0, cy1, cx0, cx1])
}

fn sample(kind: RawKind, b: &[u8]) -> f32 {
    match kind {
        RawKind::I16 => i16::from_ne_bytes([b[0], b[1]]) as f32,
        RawKind::U32 => u32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as f32,
        RawKind::I32 => i32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as f32,
        RawKind::F32 => f32::from_ne_bytes([b[0], b[1], b[2], b[3]]),
        RawKind::U8 => b[0] as f32,
        RawKind::U16 => u16::from_ne_bytes([b[0], b[1]]) as f32,
    }
}

fn raw_tile(raw: &RawSource, kind: RawKind, index: usize, level: usize, rect: [i64; 4],
            stride: usize, roi: Option<[i64; 4]>) -> Result<Tile> {
    let [y0, y1, x0, x1] = rect;
    let (rows, cols) = ((y1 - y0) as usize, (x1 - x0) as usize);
    let Some([cy0, cy1, cx0, cx1]) = clip_to_roi(rect, stride, roi) else {
        return Ok(Tile { rows, cols, values: Values::U8(vec![0; rows * cols]), valid: Valid::Nothing });
    };
    let bytes = raw.read_region(&Region { channel: index, level, y0: cy0 as usize, y1: cy1 as usize,
                                           x0: cx0 as usize, x1: cx1 as usize })?;
    let (ly0, lx0, h, w) = ((cy0 - y0) as usize, (cx0 - x0) as usize, (cy1 - cy0) as usize, (cx1 - cx0) as usize);
    let local = [ly0, ly0 + h, lx0, lx0 + w];
    Ok(match kind {
        RawKind::U8 => {
            let mut v = vec![0u8; rows * cols];
            for r in 0..h {
                v[(ly0 + r) * cols + lx0..(ly0 + r) * cols + lx0 + w].copy_from_slice(&bytes[r * w..(r + 1) * w]);
            }
            Tile { rows, cols, values: Values::U8(v), valid: Valid::Rect(local) }
        }
        RawKind::U16 => {
            let mut v = vec![0u16; rows * cols];
            for r in 0..h {
                for c in 0..w {
                    let i = (r * w + c) * 2;
                    v[(ly0 + r) * cols + lx0 + c] = u16::from_ne_bytes([bytes[i], bytes[i + 1]]);
                }
            }
            Tile { rows, cols, values: Values::U16(v), valid: Valid::Rect(local) }
        }
        other => {
            let width = if matches!(other, RawKind::I16) { 2 } else { 4 };
            let mut v = vec![f32::NAN; rows * cols];
            for r in 0..h {
                for c in 0..w {
                    let i = (r * w + c) * width;
                    v[(ly0 + r) * cols + lx0 + c] = sample(other, &bytes[i..i + width]);
                }
            }
            Tile { rows, cols, values: Values::F32(v), valid: Valid::Finite }
        }
    })
}

/// `CoarsePlane.tile(rect)`: (values, valid) from the plane, rect level-k.
fn plane_tile(plane: &Plane, rect: [i64; 4], wanted: &dyn Fn() -> bool) -> Result<Option<Reduced>> {
    let [y0, y1, x0, x1] = rect;
    let (rows, cols) = ((y1 - y0).max(0) as usize, (x1 - x0).max(0) as usize);
    let mut values = vec![0f32; rows * cols];
    let mut valid = vec![false; rows * cols];
    let [by0, bx0] = plane.origin;
    let (ph, pw) = (plane.array.shape[0] as i64, plane.array.shape[1] as i64);
    let (sy0, sy1) = ((y0 - by0).max(0), (y1 - by0).min(ph));
    let (sx0, sx1) = ((x0 - bx0).max(0), (x1 - bx0).min(pw));
    if rows > 0 && cols > 0 && sy1 > sy0 && sx1 > sx0 {
        let Some(block) = plane.array.read_rect(sy0 as usize, sy1 as usize, sx0 as usize, sx1 as usize, wanted)?
        else { return Ok(None) };
        let (oy, ox) = ((sy0 + by0 - y0) as usize, (sx0 + bx0 - x0) as usize);
        let (h, w) = ((sy1 - sy0) as usize, (sx1 - sx0) as usize);
        for r in 0..h {
            let at = (oy + r) * cols + ox;
            values[at..at + w].copy_from_slice(&block[r * w..(r + 1) * w]);
            valid[at..at + w].iter_mut().for_each(|v| *v = true);
        }
    }
    if !wanted() {
        return Ok(None);
    }
    Ok(Some(Reduced { values, valid, rows, cols }))
}

fn corrected_tile(region: &CorrectedRegion, planes: &std::collections::HashMap<usize, Plane>,
                  rect: [i64; 4], stride: usize, budget: usize, wanted: &dyn Fn() -> bool)
                  -> Result<Option<Tile>> {
    let mut got = None;
    if stride > 1 {
        if let Some(plane) = planes.get(&stride) {
            match plane_tile(plane, rect, wanted) {
                Ok(None) => return Ok(None),
                Ok(Some(t)) => got = Some(t),
                Err(_) => {}            // a plane read that failed: reduce instead
            }
        }
    }
    let red = match got {
        Some(t) => t,
        None => {
            let s = stride as i64;
            let level0 = [rect[0] * s, rect[1] * s, rect[2] * s, rect[3] * s];
            let Some(r) = reduce(Some(region), level0, stride, budget, wanted)? else { return Ok(None) };
            r
        }
    };
    let values = red.values.iter().zip(&red.valid).map(|(&v, &ok)| if ok { v } else { f32::NAN }).collect();
    Ok(Some(Tile { rows: red.rows, cols: red.cols, values: Values::F32(values), valid: Valid::Finite }))
}

/// One tile; None when `wanted` turned false on the way (cancelled).
#[allow(clippy::too_many_arguments)]
pub fn read(source: &Source, raw: &RawSource, raw_kind: RawKind, roi: Option<[i64; 4]>, level: usize,
            rect: [i64; 4], stride: usize, tile_size: usize, budget: usize,
            wanted: &dyn Fn() -> bool) -> Result<Option<Tile>> {
    match source {
        Source::Raw { index } => raw_tile(raw, raw_kind, *index, level, rect, stride, roi).map(Some),
        Source::Corrected { region, planes } => corrected_tile(region, planes, rect, stride, budget, wanted),
        Source::Missing { .. } => Ok(Some(Tile { rows: tile_size, cols: tile_size,
                                                  values: Values::F32(vec![f32::NAN; tile_size * tile_size]),
                                                  valid: Valid::Nothing })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roi_goes_outward() {
        assert_eq!(clip_to_roi([0, 10, 0, 10], 4, Some([5, 9, 3, 37])), Some([1, 3, 0, 10]));
        assert_eq!(clip_to_roi([0, 10, 0, 10], 1, None), Some([0, 10, 0, 10]));
        assert_eq!(clip_to_roi([0, 10, 0, 10], 4, Some([80, 90, 0, 4])), None);
    }
}
