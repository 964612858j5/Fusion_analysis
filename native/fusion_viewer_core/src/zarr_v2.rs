//! One zarr v2 array as the corrected products store it (plan R1 §3):
//! 2-D, C order, no filters, little-endian float32, Blosc-LZ4 chunks in
//! files named "<row>.<col>" (the '.' separator only, plan R1 §2.2). A chunk file that does not
//! exist reads as `fill_value`; an edge chunk is stored at the full chunk
//! shape and cut. Anything else is refused when the array is described
//! (the caller then stays with Python). The metadata comes parsed from
//! Python (`step1_core_adapter.build_corrected_manifest`); this module
//! reads chunk files by path, as zarr does.

use crate::error::{CoreError, Result};
use std::io::ErrorKind;
use std::sync::atomic::{AtomicU64, Ordering};

/// Chunk files decoded since the process started (tests check that a
/// reduction decodes each chunk once).
pub static CHUNKS_DECODED: AtomicU64 = AtomicU64::new(0);
use std::path::PathBuf;

/// What a `.zarray` says, already checked to be the one layout taken.
#[derive(Clone, Debug)]
pub struct ArrayMeta {
    pub dir: PathBuf,
    pub shape: [usize; 2],
    pub chunks: [usize; 2],
    pub fill: f32,
    pub separator: char,
}

impl ArrayMeta {
    /// Check the fields Python parsed from `.zarray`.
    pub fn new(dir: PathBuf, shape: [usize; 2], chunks: [usize; 2], dtype: &str, order: &str,
               compressor: &str, cname: &str, filters_none: bool, fill: f32, separator: char)
               -> Result<Self> {
        let refuse = |why: String| Err(CoreError::Manifest(format!("zarr array {}: {why}", dir.display())));
        if dtype != "<f4" {
            return refuse(format!("dtype {dtype} not taken (<f4 only)"));
        }
        if order != "C" {
            return refuse(format!("order {order} not taken"));
        }
        if !filters_none {
            return refuse("filters not taken".into());
        }
        if compressor != "blosc" || cname != "lz4" {
            return refuse(format!("compressor {compressor}/{cname} not taken (blosc lz4 only)"));
        }
        if separator != '.' {
            return refuse(format!("dimension separator {separator:?} not taken"));
        }
        if chunks[0] == 0 || chunks[1] == 0 {
            return refuse("empty chunk shape".into());
        }
        Ok(ArrayMeta { dir, shape, chunks, fill, separator })
    }

    fn chunk_path(&self, cy: usize, cx: usize) -> PathBuf {
        self.dir.join(format!("{cy}{}{cx}", self.separator))
    }

    /// One chunk at its full stored shape, row-major f32 (`fill` if absent).
    pub fn read_chunk(&self, cy: usize, cx: usize) -> Result<Vec<f32>> {
        let n = self.chunks[0] * self.chunks[1];
        let frame = match std::fs::read(self.chunk_path(cy, cx)) {
            Ok(b) => b,
            Err(e) if e.kind() == ErrorKind::NotFound => return Ok(vec![self.fill; n]),
            Err(e) => return Err(CoreError::Io(format!("{}: {e}", self.chunk_path(cy, cx).display()))),
        };
        CHUNKS_DECODED.fetch_add(1, Ordering::Relaxed);
        let mut values = vec![0f32; n];
        // the chunk's bytes go straight into the f32 buffer (little-endian
        // f32 is the host's own layout on the platforms built for)
        #[cfg(target_endian = "little")]
        {
            // SAFETY: an f32 slice viewed as its bytes; any byte pattern is
            // a valid f32, length and alignment come from the f32 slice
            let bytes = unsafe { std::slice::from_raw_parts_mut(values.as_mut_ptr() as *mut u8, n * 4) };
            crate::blosc::decode_into(&frame, bytes).map_err(|e| CoreError::Decode(format!(
                "chunk {cy}.{cx} of {}: {e}", self.dir.display())))?;
        }
        #[cfg(target_endian = "big")]
        {
            let mut bytes = vec![0u8; n * 4];
            crate::blosc::decode_into(&frame, &mut bytes)?;
            for (v, b) in values.iter_mut().zip(bytes.chunks_exact(4)) {
                *v = f32::from_le_bytes(b.try_into().unwrap());
            }
        }
        Ok(values)
    }

    /// Rows `y0..y1`, columns `x0..x1` (inside the shape), row-major.
    /// `still_wanted` is asked between chunks; false stops with None.
    pub fn read_rect(&self, y0: usize, y1: usize, x0: usize, x1: usize,
                     still_wanted: &dyn Fn() -> bool) -> Result<Option<Vec<f32>>> {
        if y1 > self.shape[0] || x1 > self.shape[1] || y0 > y1 || x0 > x1 {
            return Err(CoreError::Bounds(format!(
                "rect {y0}..{y1} x {x0}..{x1} outside {:?}", self.shape)));
        }
        let (h, w) = (y1 - y0, x1 - x0);
        let mut out = vec![0f32; h * w];
        if h == 0 || w == 0 {
            return Ok(Some(out));
        }
        let [ch, cw] = self.chunks;
        for cy in y0 / ch..y1.div_ceil(ch) {
            for cx in x0 / cw..x1.div_ceil(cw) {
                if !still_wanted() {
                    return Ok(None);
                }
                let chunk = self.read_chunk(cy, cx)?;
                let (ry0, ry1) = (y0.max(cy * ch), y1.min((cy + 1) * ch));
                let (rx0, rx1) = (x0.max(cx * cw), x1.min((cx + 1) * cw));
                for y in ry0..ry1 {
                    let src = &chunk[(y - cy * ch) * cw + (rx0 - cx * cw)..(y - cy * ch) * cw + (rx1 - cx * cw)];
                    out[(y - y0) * w + (rx0 - x0)..(y - y0) * w + (rx1 - x0)].copy_from_slice(src);
                }
            }
        }
        Ok(Some(out))
    }
}
