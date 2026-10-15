//! The mean of a corrected product over a level-0 rectangle at a stride,
//! over its VALID samples (plan R1 §2.3): `step1_source.reduce_corrected`,
//! rule for rule.
//!
//! * Blocks are anchored at the level-0 origin; output block (i, j) of the
//!   rectangle is level-0 block (y0 // s + i, x0 // s + j).
//! * Only the rectangle's overlap with the product's region is read, in
//!   slabs on the PRODUCT's chunk grid (user ruling 2026-10-12, R2 plan
//!   §2.2): whole chunks, at most `budget` level-0 pixels, so a chunk is
//!   decoded once per request. Blocks crossing a slab edge receive from
//!   both slabs; a slab's blocks come from where it was actually read.
//!   (Python walks `_slab_geometry`'s block-stepped slabs; the sums differ
//!   only in their order.)
//! * A block's count is its number of real samples (`_span_per_block`); a
//!   NaN sample counts, so its block's mean is NaN. Sums are float64, the
//!   mean is cast to float32; a block with no sample is 0 and not valid.
//! * Stride 1 within the budget is a copy, not a reduction.
//!
//! The summation order differs from numpy's, so results agree within the
//! plan's tolerance |a-b| <= 1e-6 + 1e-6|b|, not bit for bit; counts and
//! validity are exact.
//! `wanted` is asked between slabs: false returns None (cancelled).

use crate::error::Result;
use crate::zarr_v2::ArrayMeta;

pub const MAX_REDUCTION_ELEMENTS: usize = 4 * 1024 * 1024;

/// One ROI's corrected array, placed at its region's level-0 origin.
pub struct CorrectedRegion {
    pub array: ArrayMeta,
    /// (y0, y1, x0, x1), level 0
    pub bbox: [i64; 4],
}

/// Pixels read from the region: where they start (level 0) and their size.
pub struct Patch {
    pub y0: i64,
    pub x0: i64,
    pub rows: usize,
    pub cols: usize,
    pub data: Vec<f32>,
}

pub enum Read {
    /// the rectangle misses the region
    Miss,
    Cancelled,
    Got(Patch),
}

impl CorrectedRegion {
    pub fn overlaps(&self, y0: i64, y1: i64, x0: i64, x1: i64) -> bool {
        let [by0, by1, bx0, bx1] = self.bbox;
        !(y1 <= by0 || y0 >= by1 || x1 <= bx0 || x0 >= bx1)
    }

    /// `CorrectedRegion.read`: the overlap with the region, cut to the
    /// array's own extent (`ArrayRegionSource.read_region`).
    pub fn read(&self, y0: i64, y1: i64, x0: i64, x1: i64, wanted: &dyn Fn() -> bool) -> Result<Read> {
        let [by0, by1, bx0, bx1] = self.bbox;
        let (oy0, oy1, ox0, ox1) = (y0.max(by0), y1.min(by1), x0.max(bx0), x1.min(bx1));
        if oy1 <= oy0 || ox1 <= ox0 {
            return Ok(Read::Miss);
        }
        let (ay1, ax1) = (by0 + self.array.shape[0] as i64, bx0 + self.array.shape[1] as i64);
        let (cy0, cy1, cx0, cx1) = (oy0, oy1.min(ay1), ox0, ox1.min(ax1));
        if cy1 <= cy0 || cx1 <= cx0 {
            return Ok(Read::Got(Patch { y0: cy0, x0: cx0, rows: 0, cols: 0, data: Vec::new() }));
        }
        let rect = ((cy0 - by0) as usize, (cy1 - by0) as usize, (cx0 - bx0) as usize, (cx1 - bx0) as usize);
        Ok(match self.array.read_rect(rect.0, rect.1, rect.2, rect.3, wanted)? {
            None => Read::Cancelled,
            Some(data) => Read::Got(Patch { y0: cy0, x0: cx0, rows: rect.1 - rect.0,
                                            cols: rect.3 - rect.2, data }),
        })
    }
}

/// `(values, valid, rows, cols)` at the stride, row-major.
pub struct Reduced {
    pub values: Vec<f32>,
    pub valid: Vec<bool>,
    pub rows: usize,
    pub cols: usize,
}

fn isqrt(v: usize) -> usize {
    let mut r = (v as f64).sqrt() as usize;
    while r > 0 && r * r > v {
        r -= 1;
    }
    while (r + 1) * (r + 1) <= v {
        r += 1;
    }
    r
}

/// (rows, columns) of one slab on the product's chunk grid: k x k whole
/// chunks, the largest k that keeps a slab within `budget` pixels. A chunk
/// larger than the budget (only with test budgets) is cut into bands of
/// whole rows, then re-decoded per band.
pub fn slab_on_chunks(chunks: [usize; 2], budget: usize) -> (usize, usize) {
    let [ch, cw] = chunks;
    let per = (ch * cw).max(1);
    if per <= budget {
        let k = isqrt(budget / per).max(1);
        // widen along x while it still fits (a 2x3 grid beats 2x2)
        let kx = (budget / (per * k)).max(k);
        (k * ch, kx * cw)
    } else {
        ((budget / cw.max(1)).clamp(1, ch), cw)
    }
}

/// `_span_per_block`: real samples of `length` from `front` per block.
fn span_per_block(front: usize, length: usize, stride: usize, blocks: usize) -> Vec<u64> {
    (0..blocks).map(|k| {
        let (start, end) = (k * stride, k * stride + stride);
        let (lo, hi) = (start.max(front), end.min(front + length));
        hi.saturating_sub(lo) as u64
    }).collect()
}

/// `reduce_corrected(region, rect, stride, budget)`; `rect` is level 0.
pub fn reduce(region: Option<&CorrectedRegion>, rect: [i64; 4], stride: usize, budget: usize,
              wanted: &dyn Fn() -> bool) -> Result<Option<Reduced>> {
    Ok(reduce_as(region, rect, stride, budget, wanted, &|m| m as f32, &|v| v)?
        .map(|(values, valid, rows, cols)| Reduced { values, valid, rows, cols }))
}

/// R7c 1E: the same reduction quantised the way the display layers are
/// (`display_pyramid`: the float64 mean, `floor(mean + 0.5)`, once) -- before
/// any float32 rounding could move a mean across a half. For a channel
/// whose samples are all integers in 0..65535 (the display layers' rule).
pub fn reduce_u16(region: Option<&CorrectedRegion>, rect: [i64; 4], stride: usize, budget: usize,
                  wanted: &dyn Fn() -> bool) -> Result<Option<(Vec<u16>, Vec<bool>, usize, usize)>> {
    reduce_as(region, rect, stride, budget, wanted, &|m| (m + 0.5).floor().clamp(0.0, 65535.0) as u16,
              &|v| (v as f64 + 0.5).floor().clamp(0.0, 65535.0) as u16)
}

/// The reduction, each output value made by `mean` (block means, float64)
/// or `sample` (stride 1 within the budget: the samples themselves).
#[allow(clippy::type_complexity)]
fn reduce_as<T: Copy + Default>(region: Option<&CorrectedRegion>, rect: [i64; 4], stride: usize, budget: usize,
                                wanted: &dyn Fn() -> bool, mean: &dyn Fn(f64) -> T, sample: &dyn Fn(f32) -> T)
                                -> Result<Option<(Vec<T>, Vec<bool>, usize, usize)>> {
    let [y0, y1, x0, x1] = rect;
    let s = stride.max(1);
    let (height, width) = ((y1 - y0).max(0) as usize, (x1 - x0).max(0) as usize);
    let budget = budget.max(1);
    if s == 1 && height * width <= budget {
        let mut values = vec![T::default(); height * width];
        let mut valid = vec![false; height * width];
        if let Some(r) = region.filter(|r| height > 0 && width > 0 && r.overlaps(y0, y1, x0, x1)) {
            match r.read(y0, y1, x0, x1, wanted)? {
                Read::Cancelled => return Ok(None),
                Read::Miss => {}
                Read::Got(p) => {
                    let (ry, rx) = ((p.y0 - y0) as usize, (p.x0 - x0) as usize);
                    for i in 0..p.rows {
                        let at = (ry + i) * width + rx;
                        for (o, &v) in values[at..at + p.cols].iter_mut().zip(&p.data[i * p.cols..(i + 1) * p.cols]) {
                            *o = sample(v);
                        }
                        valid[at..at + p.cols].iter_mut().for_each(|v| *v = true);
                    }
                }
            }
        }
        if !wanted() {
            return Ok(None);
        }
        return Ok(Some((values, valid, height, width)));
    }
    let si = s as i64;
    let (py, px) = (y0.rem_euclid(si) as usize, x0.rem_euclid(si) as usize);
    let (out_h, out_w) = ((py + height).div_ceil(s), (px + width).div_ceil(s));
    let mut sums = vec![0f64; out_h * out_w];
    let mut counts = vec![0u64; out_h * out_w];
    if let Some(r) = region.filter(|_| out_h > 0 && out_w > 0) {
        let [by0, by1, bx0, bx1] = r.bbox;
        let (oy0, oy1, ox0, ox1) = (y0.max(by0), y1.min(by1), x0.max(bx0), x1.min(bx1));
        if oy1 > oy0 && ox1 > ox0 {
            // slabs on the PRODUCT's chunk grid (it starts at the region's
            // level-0 origin), so a chunk is decoded once per request; the
            // level-0-anchored blocks that straddle two slabs simply receive
            // from both (`accumulate` counts real samples by placement)
            let (rows, band) = slab_on_chunks(r.array.chunks, budget);
            let (base_by, base_bx) = (y0.div_euclid(si), x0.div_euclid(si));
            let (rows, band) = (rows as i64, band as i64);
            let mut cy = oy0;
            while cy < oy1 {
                let stop_y = oy1.min(by0 + ((cy - by0).div_euclid(rows) + 1) * rows);
                let mut cx = ox0;
                while cx < ox1 {
                    let stop_x = ox1.min(bx0 + ((cx - bx0).div_euclid(band) + 1) * band);
                    if !wanted() {
                        return Ok(None);
                    }
                    match r.read(cy, stop_y, cx, stop_x, wanted)? {
                        Read::Cancelled => return Ok(None),
                        Read::Miss => {}
                        Read::Got(p) => accumulate(&mut sums, &mut counts, out_w, &p, s, base_by, base_bx),
                    }
                    cx = stop_x;
                }
                cy = stop_y;
            }
        }
    }
    if !wanted() {                        // cancelled during the last slab
        return Ok(None);
    }
    let valid: Vec<bool> = counts.iter().map(|&c| c > 0).collect();
    let values = sums.iter().zip(&counts)
        .map(|(&sum, &c)| if c > 0 { mean(sum / c as f64) } else { T::default() }).collect();
    Ok(Some((values, valid, out_h, out_w)))
}

/// `_accumulate_blocks`: one slab into the global block sums and counts.
fn accumulate(sums: &mut [f64], counts: &mut [u64], out_w: usize, p: &Patch, s: usize,
              base_by: i64, base_bx: i64) {
    if p.rows == 0 || p.cols == 0 {
        return;
    }
    let si = s as i64;
    let (front_y, front_x) = (p.y0.rem_euclid(si) as usize, p.x0.rem_euclid(si) as usize);
    let (bh, bw) = ((front_y + p.rows).div_ceil(s), (front_x + p.cols).div_ceil(s));
    let rows_in = span_per_block(front_y, p.rows, s, bh);
    let cols_in = span_per_block(front_x, p.cols, s, bw);
    let (oy, ox) = ((p.y0.div_euclid(si) - base_by) as usize, (p.x0.div_euclid(si) - base_bx) as usize);
    // the slab's columns cut at block boundaries, once
    let cuts: Vec<(usize, usize)> = (0..bw)
        .map(|j| ((j * s).max(front_x) - front_x, ((j + 1) * s).min(front_x + p.cols) - front_x))
        .collect();
    let mut block = vec![0f64; bw];
    for i in 0..bh {
        block.iter_mut().for_each(|v| *v = 0.0);
        let (r0, r1) = ((i * s).max(front_y) - front_y, ((i + 1) * s).min(front_y + p.rows) - front_y);
        for r in r0..r1 {
            let row = &p.data[r * p.cols..(r + 1) * p.cols];
            for (acc, &(c0, c1)) in block.iter_mut().zip(&cuts) {
                *acc += row[c0..c1].iter().map(|&v| v as f64).sum::<f64>();
            }
        }
        for j in 0..bw {
            let at = (oy + i) * out_w + ox + j;
            sums[at] += block[j];
            counts[at] += rows_in[i] * cols_in[j];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slabs_follow_the_chunks() {
        assert_eq!(slab_on_chunks([1024, 1024], 4 * 1024 * 1024), (2048, 2048));
        assert_eq!(slab_on_chunks([1024, 1024], 3 * 1024 * 1024), (1024, 3072));
        assert_eq!(slab_on_chunks([300, 250], 5000), (20, 250));
        assert_eq!(slab_on_chunks([300, 250], 997), (3, 250));
    }

}
