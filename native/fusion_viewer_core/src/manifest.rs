//! The minimal source manifest for one raw TIFF pyramid.
//!
//! Python builds it once (from `raw_tile_provider._ChunkPlan` and
//! `os.stat`); Rust works from it alone and never calls back to ask where
//! a block is. The full manifest (sources per channel, corrected products,
//! ROI) is specified in docs/v16_5_r0_interfaces.md; this is the part the
//! raw reader needs.

use crate::decode::BlockFormat;
use crate::error::{CoreError, Result};
use crate::fileid::FileId;
use std::path::PathBuf;

/// One pyramid level's block table. A stripped page is a level whose
/// blocks are full-width strips (`block_cols == width`, `across == 1`);
/// its last strip may hold fewer rows.
#[derive(Clone, Debug)]
pub struct LevelTable {
    pub height: usize,
    pub width: usize,
    pub block_rows: usize,
    pub block_cols: usize,
    pub stripped: bool,
    pub format: BlockFormat,
    pub channels: usize,
    /// `offsets[c * blocks + i]`, `counts[...]` likewise; count 0 = empty.
    pub offsets: Vec<u64>,
    pub counts: Vec<u64>,
}

impl LevelTable {
    pub fn across(&self) -> usize {
        self.width.div_ceil(self.block_cols)
    }

    pub fn down(&self) -> usize {
        self.height.div_ceil(self.block_rows)
    }

    pub fn blocks(&self) -> usize {
        self.across() * self.down()
    }

    /// Rows the block at block-row `by` actually stores.
    pub fn stored_rows(&self, by: usize) -> usize {
        if self.stripped {
            self.block_rows.min(self.height - by * self.block_rows)
        } else {
            self.block_rows // TIFF tiles are always stored whole
        }
    }

    /// (offset, count) of channel `c`'s block (`by`, `bx`).
    pub fn locate(&self, c: usize, by: usize, bx: usize) -> Result<(u64, u64)> {
        if c >= self.channels || by >= self.down() || bx >= self.across() {
            return Err(CoreError::Bounds(format!("channel {c} block ({by}, {bx})")));
        }
        let i = c * self.blocks() + by * self.across() + bx;
        Ok((self.offsets[i], self.counts[i]))
    }

    pub fn validate(&self) -> Result<()> {
        let want = self.channels * self.blocks();
        if self.block_rows == 0 || self.block_cols == 0 {
            return Err(CoreError::Manifest("zero block size".into()));
        }
        if self.stripped && self.block_cols != self.width {
            return Err(CoreError::Manifest("a strip must be the full width".into()));
        }
        if self.offsets.len() != want || self.counts.len() != want {
            return Err(CoreError::Manifest(format!(
                "{} offsets / {} counts for {} channels x {} blocks",
                self.offsets.len(), self.counts.len(), self.channels, self.blocks())));
        }
        if !matches!(self.format.sample_bytes, 1 | 2 | 4 | 8) {
            return Err(CoreError::Manifest(format!("{}-byte samples", self.format.sample_bytes)));
        }
        Ok(())
    }
}

/// What Rust needs to open and read one raw pyramid file.
#[derive(Clone, Debug)]
pub struct RawManifest {
    pub path: PathBuf,
    /// What Python's `os.stat` saw when it parsed the file.
    pub expected: FileId,
    pub levels: Vec<LevelTable>,
}
