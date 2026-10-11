//! Block decoding: compressed TIFF block bytes -> native-endian samples.
//!
//! Pure functions, no I/O and no state. The output is byte-identical to the
//! existing Python path (`raw_tile_provider._direct_read`): imagecodecs
//! decompresses, `np.frombuffer` takes the first `rows*cols` samples in the
//! file's byte order, a horizontal predictor is undone per row, and the
//! result is stored in native byte order.
//!
//! Design reference (no code copied): Odon `xenium/tiff_pyramid.rs`
//! decodes the file's own tile instead of going through a generic array
//! layer; the same idea is behind Python's `_ChunkPlan`.

use crate::error::{CoreError, Result};
use std::io::Read;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Compression {
    None,
    Lzw,
    Deflate,
}

impl Compression {
    /// TIFF compression tag value -> supported codec (zstd comes later).
    pub fn from_tiff(code: u32) -> Option<Self> {
        match code {
            1 => Some(Compression::None),
            5 => Some(Compression::Lzw),
            8 | 32946 => Some(Compression::Deflate),
            _ => None,
        }
    }
}

/// How one block's samples are stored.
#[derive(Clone, Copy, Debug)]
pub struct BlockFormat {
    pub compression: Compression,
    /// TIFF predictor 2 (horizontal differencing) when true.
    pub horizontal_predictor: bool,
    /// Bytes per sample: 1, 2, 4 or 8.
    pub sample_bytes: usize,
    pub big_endian: bool,
}

/// Decode `raw` into exactly `rows * cols` native-endian samples.
pub fn decode_block(raw: &[u8], fmt: &BlockFormat, rows: usize, cols: usize) -> Result<Vec<u8>> {
    let need = rows * cols * fmt.sample_bytes;
    let mut out = decompress(raw, fmt.compression, need)?;
    if out.len() < need {
        return Err(CoreError::Decode(format!(
            "block decoded to {} bytes, {} expected", out.len(), need)));
    }
    out.truncate(need);
    if fmt.big_endian != cfg!(target_endian = "big") {
        swap_bytes(&mut out, fmt.sample_bytes);
    }
    if fmt.horizontal_predictor {
        undo_horizontal(&mut out, fmt.sample_bytes, cols)?;
    }
    Ok(out)
}

fn decompress(raw: &[u8], compression: Compression, need: usize) -> Result<Vec<u8>> {
    match compression {
        Compression::None => Ok(raw.to_vec()),
        Compression::Deflate => {
            let mut out = Vec::with_capacity(need);
            flate2::read::ZlibDecoder::new(raw)
                .read_to_end(&mut out)
                .map_err(|e| CoreError::Decode(format!("deflate: {e}")))?;
            Ok(out)
        }
        Compression::Lzw => lzw_exact(raw, need),
    }
}

/// LZW-decode until `need` bytes are out, then stop. tifffile decodes into
/// a buffer of the block's size the same way, so a stream with damaged
/// bytes AFTER the block's last pixel still reads (two strips of Kevin's
/// qptiff, coarsest level, are like that; imagecodecs' unsized decode
/// refuses them). A stream that ends or breaks before `need` is an error.
fn lzw_exact(raw: &[u8], need: usize) -> Result<Vec<u8>> {
    let mut out = vec![0u8; need];
    let mut dec = weezl::decode::Decoder::with_tiff_size_switch(weezl::BitOrder::Msb, 8);
    let (mut inp, mut filled) = (raw, 0usize);
    while filled < need {
        let r = dec.decode_bytes(inp, &mut out[filled..]);
        inp = &inp[r.consumed_in..];
        filled += r.consumed_out;
        match r.status {
            Ok(weezl::LzwStatus::Ok) if r.consumed_in > 0 || r.consumed_out > 0 => {}
            Ok(_) if filled >= need => {}
            Ok(status) => return Err(CoreError::Decode(format!(
                "lzw stream stopped ({status:?}) at {filled} of {need} bytes"))),
            Err(e) => return Err(CoreError::Decode(format!(
                "lzw: {e:?} at {filled} of {need} bytes"))),
        }
    }
    Ok(out)
}

fn swap_bytes(buf: &mut [u8], sample_bytes: usize) {
    if sample_bytes > 1 {
        for sample in buf.chunks_exact_mut(sample_bytes) {
            sample.reverse();
        }
    }
}

/// Undo TIFF predictor 2 on native-endian unsigned/signed integer samples
/// (wrapping adds, as numpy's integer cumsum in imagecodecs.delta_decode).
fn undo_horizontal(buf: &mut [u8], sample_bytes: usize, cols: usize) -> Result<()> {
    let row_bytes = cols * sample_bytes;
    if row_bytes == 0 {
        return Ok(());
    }
    match sample_bytes {
        1 => buf.chunks_exact_mut(row_bytes).for_each(|row| {
            for i in 1..row.len() {
                row[i] = row[i].wrapping_add(row[i - 1]);
            }
        }),
        2 => buf.chunks_exact_mut(row_bytes).for_each(|row| {
            let mut prev = 0u16;
            for s in row.chunks_exact_mut(2) {
                prev = prev.wrapping_add(u16::from_ne_bytes([s[0], s[1]]));
                s.copy_from_slice(&prev.to_ne_bytes());
            }
        }),
        4 => buf.chunks_exact_mut(row_bytes).for_each(|row| {
            let mut prev = 0u32;
            for s in row.chunks_exact_mut(4) {
                prev = prev.wrapping_add(u32::from_ne_bytes([s[0], s[1], s[2], s[3]]));
                s.copy_from_slice(&prev.to_ne_bytes());
            }
        }),
        n => return Err(CoreError::Decode(format!("predictor on {n}-byte samples"))),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn predictor_u8_rows_restart() {
        let mut b = vec![1u8, 1, 1, 5, 255, 2];
        undo_horizontal(&mut b, 1, 3).unwrap();
        assert_eq!(b, vec![1, 2, 3, 5, 4, 6]);
    }

    #[test]
    fn raw_block_too_short_is_an_error() {
        let fmt = BlockFormat { compression: Compression::None, horizontal_predictor: false,
                                sample_bytes: 1, big_endian: false };
        assert!(decode_block(&[0u8; 3], &fmt, 2, 2).is_err());
    }
}
