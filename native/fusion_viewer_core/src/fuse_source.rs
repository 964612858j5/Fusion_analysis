//! One channel's samples over one unit, as `FullFusionWorker._read_one_source`
//! gets them (plan `docs/f5_rust_fusion_plan.md` §4): from the raw slide
//! (level 0, the file's own sample type) or from Step0's corrected product
//! (float32 zarr, the region's own group). A source that does not cover
//! the whole unit refuses it -- never partly filled from elsewhere.
//!
//! Integer samples stay integers (the kernel maps them through a table);
//! every other type becomes f32 exactly as NumPy's `astype(float32)` does
//! (`as f32`: round to nearest even).

use crate::error::{CoreError, Result};
use crate::fuse_planes::Plane;
use crate::reader::{RawSource, Region};
use crate::reduce::{CorrectedRegion, Read};
use crate::step1_tile::RawKind;
use std::sync::Arc;

pub enum Source {
    /// a channel of the raw pyramid, read at level 0
    Raw { src: Arc<RawSource>, kind: RawKind, index: usize },
    /// a corrected channel: its float32 array and the region's bbox
    Corrected { region: CorrectedRegion },
}

fn widen(bytes: &[u8], kind: RawKind) -> Plane {
    match kind {
        RawKind::U8 => Plane::U8(bytes.to_vec()),
        RawKind::U16 => Plane::U16(bytes.chunks_exact(2).map(|b| u16::from_ne_bytes([b[0], b[1]])).collect()),
        RawKind::I16 => Plane::F32(bytes.chunks_exact(2).map(|b| i16::from_ne_bytes([b[0], b[1]]) as f32).collect()),
        RawKind::U32 => Plane::F32(bytes.chunks_exact(4)
            .map(|b| u32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as f32).collect()),
        RawKind::I32 => Plane::F32(bytes.chunks_exact(4)
            .map(|b| i32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as f32).collect()),
        RawKind::F32 => Plane::F32(bytes.chunks_exact(4)
            .map(|b| f32::from_ne_bytes([b[0], b[1], b[2], b[3]])).collect()),
    }
}

impl Source {
    /// The unit `y0..y1, x0..x1` (level 0), or None when `wanted()` turned
    /// false while reading (a corrected read checks between chunks).
    pub fn read(&self, y0: usize, y1: usize, x0: usize, x1: usize, wanted: &dyn Fn() -> bool)
                -> Result<Option<Plane>> {
        let refuse = |covers: String| CoreError::Bounds(format!(
            "the source covers {covers} of the unit [{y0}, {y1}, {x0}, {x1}] -- no fallback to other pixels"));
        match self {
            Source::Raw { src, kind, index } => {
                let level = src.manifest().levels.first()
                    .ok_or_else(|| CoreError::Manifest("no level 0".into()))?;
                if y1 > level.height || x1 > level.width || y0 > y1 || x0 > x1 {
                    return Err(refuse(format!("{}x{}", level.height, level.width)));
                }
                let bytes = src.read_region(&Region { channel: *index, level: 0, y0, y1, x0, x1 })?;
                Ok(Some(widen(&bytes, *kind)))
            }
            Source::Corrected { region } => {
                let (h, w) = (y1 - y0, x1 - x0);
                match region.read(y0 as i64, y1 as i64, x0 as i64, x1 as i64, wanted)? {
                    Read::Cancelled => Ok(None),
                    Read::Miss => Err(refuse("nothing".into())),
                    Read::Got(p) if p.y0 == y0 as i64 && p.x0 == x0 as i64 && p.rows == h && p.cols == w => {
                        Ok(Some(Plane::F32(p.data)))
                    }
                    Read::Got(p) => Err(refuse(format!("({}, {}) at ({}, {})", p.rows, p.cols, p.y0, p.x0))),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn other_types_widen_as_numpy_astype() {
        let v: Vec<u8> = [16_777_217i32, -3, 7].iter().flat_map(|x| x.to_ne_bytes()).collect();
        match widen(&v, RawKind::I32) {
            Plane::F32(f) => assert_eq!(f, vec![16_777_216.0, -3.0, 7.0]), // nearest even
            _ => panic!(),
        }
        assert!(matches!(widen(&[1, 2], RawKind::U8), Plane::U8(_)));
        assert!(matches!(widen(&[1, 2], RawKind::U16), Plane::U16(_)));
    }
}
