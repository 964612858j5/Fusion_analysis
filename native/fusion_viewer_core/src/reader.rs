//! Reading regions of a raw pyramid: locate blocks, read, decode, crop.
//!
//! Semantics follow `RawTileProvider.read_region` with the direct path:
//! the region is clamped by the caller, an inverted or empty region is
//! empty, a block with byte count 0 is zeros.

use crate::decode::decode_block;
use crate::error::{CoreError, Result};
use crate::fileid;
use crate::manifest::{LevelTable, RawManifest};
use crate::posread::read_exact_at;
use std::fs::File;

/// An open raw pyramid. Shared by reader threads (`&self` reads only).
pub struct RawSource {
    file: File,
    manifest: RawManifest,
}

/// One region request: channel, level and a half-open pixel box.
#[derive(Clone, Copy, Debug)]
pub struct Region {
    pub channel: usize,
    pub level: usize,
    pub y0: usize,
    pub y1: usize,
    pub x0: usize,
    pub x1: usize,
}

impl RawSource {
    /// Open the file and refuse it unless it is the one the manifest saw.
    pub fn open(manifest: RawManifest) -> Result<Self> {
        for level in &manifest.levels {
            level.validate()?;
        }
        let file = File::open(&manifest.path)
            .map_err(|e| CoreError::Io(format!("open {}: {e}", manifest.path.display())))?;
        let actual = fileid::of(&file).map_err(|e| CoreError::Io(format!("stat: {e}")))?;
        if !actual.matches(&manifest.expected) {
            return Err(CoreError::Identity(format!(
                "{} changed since it was parsed: expected {:?}, found {:?}",
                manifest.path.display(), manifest.expected, actual)));
        }
        Ok(RawSource { file, manifest })
    }

    pub fn manifest(&self) -> &RawManifest {
        &self.manifest
    }

    fn level(&self, level: usize) -> Result<&LevelTable> {
        self.manifest.levels.get(level)
            .ok_or_else(|| CoreError::Bounds(format!("level {level}")))
    }

    /// One decoded block, `stored_rows(by) x block_cols` samples.
    pub fn read_block(&self, level: usize, c: usize, by: usize, bx: usize) -> Result<Vec<u8>> {
        let t = self.level(level)?;
        let (offset, count) = t.locate(c, by, bx)?;
        let rows = t.stored_rows(by);
        if count == 0 {
            return Ok(vec![0u8; rows * t.block_cols * t.format.sample_bytes]);
        }
        let mut raw = vec![0u8; count as usize];
        read_exact_at(&self.file, &mut raw, offset)
            .map_err(|e| CoreError::Io(format!("read {count} B at {offset}: {e}")))?;
        decode_block(&raw, &t.format, rows, t.block_cols)
    }

    /// The region's samples, row-major, native-endian.
    pub fn read_region(&self, r: &Region) -> Result<Vec<u8>> {
        let t = self.level(r.level)?;
        if r.y1 > t.height || r.x1 > t.width {
            return Err(CoreError::Bounds(format!("region {r:?} past {}x{}", t.height, t.width)));
        }
        let (h, w) = (r.y1.saturating_sub(r.y0), r.x1.saturating_sub(r.x0));
        let sb = t.format.sample_bytes;
        let mut out = vec![0u8; h * w * sb];
        if h == 0 || w == 0 {
            return Ok(out);
        }
        let (br, bc) = (t.block_rows, t.block_cols);
        for by in r.y0 / br..r.y1.div_ceil(br) {
            for bx in r.x0 / bc..r.x1.div_ceil(bc) {
                let block = self.read_block(r.level, r.channel, by, bx)?;
                copy_overlap(&block, t, by, bx, r, &mut out, w);
            }
        }
        Ok(out)
    }
}

/// Copy the part of block (`by`, `bx`) inside region `r` into `out`.
fn copy_overlap(block: &[u8], t: &LevelTable, by: usize, bx: usize, r: &Region,
                out: &mut [u8], out_w: usize) {
    let sb = t.format.sample_bytes;
    let (oy, ox) = (by * t.block_rows, bx * t.block_cols);
    let (y0, y1) = (r.y0.max(oy), r.y1.min(oy + t.block_rows));
    let (x0, x1) = (r.x0.max(ox), r.x1.min(ox + t.block_cols));
    let n = (x1 - x0) * sb;
    for y in y0..y1 {
        let src = ((y - oy) * t.block_cols + (x0 - ox)) * sb;
        let dst = ((y - r.y0) * out_w + (x0 - r.x0)) * sb;
        out[dst..dst + n].copy_from_slice(&block[src..src + n]);
    }
}
