//! Blosc1 frames compressed with LZ4 -> their bytes (plan R1 §3): what
//! numcodecs' `Blosc(cname="lz4")` writes into the corrected products.
//!
//! A frame is a 16-byte header (version, LZ4 format version, flags, type
//! size, uncompressed size, block size, frame size) followed either by the
//! bytes as they are (flag 0x02, "memcpyed") or by a table of block starts
//! and the blocks. A block is one stream, or -- unless flag 0x10 says
//! "don't split" -- one stream per byte of the type (when the type is at
//! most 16 bytes wide, the block holds at least 128 elements' bytes and is
//! not the short last block). A stream whose stored size equals its
//! decoded size is stored raw. Byte shuffle (flag 0x01) is undone per
//! block, the bytes past the last whole element left as they are. Bit
//! shuffle (0x04) and any codec but LZ4 are refused, as are frames that do
//! not add up. Pure Rust (LZ4 in `lz4.rs`): no C toolchain on Windows.
//! Design reference: the c-blosc 1.x frame layout (no code copied).

use crate::error::{CoreError, Result};

const HEADER: usize = 16;
const DOSHUFFLE: u8 = 0x01;
const MEMCPYED: u8 = 0x02;
const DOBITSHUFFLE: u8 = 0x04;
const DONT_SPLIT: u8 = 0x10;
const LZ4_CODE: u8 = 1;
const MAX_SPLITS: usize = 16;
const MIN_BUFFERSIZE: usize = 128;

fn bad(why: impl Into<String>) -> CoreError {
    CoreError::Decode(format!("blosc: {}", why.into()))
}

fn u32_at(buf: &[u8], at: usize) -> Result<usize> {
    let b = buf.get(at..at + 4).ok_or_else(|| bad("frame shorter than its header says"))?;
    Ok(u32::from_le_bytes(b.try_into().unwrap()) as usize)
}

/// The frame's uncompressed size, from its header.
pub fn decoded_len(frame: &[u8]) -> Result<usize> {
    if frame.len() < HEADER {
        return Err(bad("frame shorter than a header"));
    }
    u32_at(frame, 4)
}

/// Decode a whole frame.
pub fn decode(frame: &[u8]) -> Result<Vec<u8>> {
    let mut out = vec![0u8; decoded_len(frame)?];
    decode_into(frame, &mut out)?;
    Ok(out)
}

thread_local! {
    /// one block of shuffled bytes per thread, reused frame after frame
    static TMP: std::cell::RefCell<Vec<u8>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Decode a whole frame into `out`, which must be exactly its size.
pub fn decode_into(frame: &[u8], out: &mut [u8]) -> Result<()> {
    if frame.len() < HEADER {
        return Err(bad("frame shorter than a header"));
    }
    let flags = frame[2];
    let typesize = frame[3].max(1) as usize;
    let nbytes = u32_at(frame, 4)?;
    let blocksize = u32_at(frame, 8)?;
    let cbytes = u32_at(frame, 12)?;
    if cbytes > frame.len() {
        return Err(bad(format!("frame says {cbytes} bytes, {} given", frame.len())));
    }
    if out.len() != nbytes {
        return Err(bad(format!("frame holds {nbytes} bytes, {} expected", out.len())));
    }
    if flags & MEMCPYED != 0 {
        if cbytes != HEADER + nbytes {
            return Err(bad(format!("memcpyed frame of {cbytes} bytes holds {nbytes}")));
        }
        let body = frame.get(HEADER..HEADER + nbytes).ok_or_else(|| bad("memcpyed frame too short"))?;
        out.copy_from_slice(body);
        return Ok(());
    }
    if flags & DOBITSHUFFLE != 0 {
        return Err(bad("bit shuffle not supported"));
    }
    if flags >> 5 != LZ4_CODE {
        return Err(bad(format!("codec {} not supported (lz4 only)", flags >> 5)));
    }
    if nbytes == 0 {
        return Ok(());
    }
    if blocksize == 0 {
        return Err(bad("block size 0"));
    }
    let nblocks = nbytes.div_ceil(blocksize);
    let leftover = nbytes % blocksize;
    TMP.with(|tmp| {
        let mut tmp = tmp.borrow_mut();
        // never more than the frame's own size (a bad header must not
        // make this allocate gigabytes)
        let need = blocksize.min(nbytes);
        if tmp.len() < need {
            tmp.resize(need, 0);
        }
        for b in 0..nblocks {
            let start = u32_at(frame, HEADER + 4 * b)?;
            let last_short = leftover != 0 && b == nblocks - 1;
            let bsize = if last_short { leftover } else { blocksize };
            let dest = &mut out[b * blocksize..b * blocksize + bsize];
            if flags & DOSHUFFLE != 0 && typesize > 1 {
                decode_block(&frame[..cbytes], start, flags, typesize, bsize, last_short, &mut tmp[..bsize])?;
                unshuffle(&tmp[..bsize], typesize, dest);
            } else {
                decode_block(&frame[..cbytes], start, flags, typesize, bsize, last_short, dest)?;
            }
        }
        Ok(())
    })
}

/// One block's streams, from `start`, into `dest` (exactly `bsize` bytes).
fn decode_block(frame: &[u8], start: usize, flags: u8, typesize: usize, bsize: usize,
                last_short: bool, dest: &mut [u8]) -> Result<()> {
    let split = flags & DONT_SPLIT == 0 && typesize <= MAX_SPLITS
        && bsize / typesize >= MIN_BUFFERSIZE && !last_short;
    let nsplits = if split { typesize } else { 1 };
    let neblock = bsize / nsplits;
    if neblock * nsplits != bsize {
        return Err(bad("block does not split evenly"));
    }
    let mut at = start;
    for j in 0..nsplits {
        let stored = u32_at(frame, at)?;
        at += 4;
        let src = frame.get(at..at + stored).ok_or_else(|| bad("stream past the frame"))?;
        let part = &mut dest[j * neblock..(j + 1) * neblock];
        if stored == neblock {
            part.copy_from_slice(src);                       // stored raw
        } else {
            let n = crate::lz4::decompress_into(src, part)?;
            if n != neblock {
                return Err(bad(format!("lz4 stream gave {n} of {neblock} bytes")));
            }
        }
        at += stored;
    }
    Ok(())
}

/// Undo byte shuffle: `src` holds byte 0 of every element, then byte 1...;
/// the bytes past the last whole element are copied as they are. Written
/// element by element (sequential stores), with the common widths unrolled.
fn unshuffle(src: &[u8], typesize: usize, dest: &mut [u8]) {
    let n = src.len() / typesize;
    match typesize {
        #[cfg(target_arch = "x86_64")]
        4 => unshuffle4_sse2(src, n, dest),
        #[cfg(not(target_arch = "x86_64"))]
        4 => {
            let (p0, rest) = src.split_at(n);
            let (p1, rest) = rest.split_at(n);
            let (p2, rest) = rest.split_at(n);
            let p3 = &rest[..n];
            for (i, out) in dest[..4 * n].chunks_exact_mut(4).enumerate() {
                out.copy_from_slice(&[p0[i], p1[i], p2[i], p3[i]]);
            }
        }
        2 => {
            let (p0, rest) = src.split_at(n);
            let p1 = &rest[..n];
            for (i, out) in dest[..2 * n].chunks_exact_mut(2).enumerate() {
                out.copy_from_slice(&[p0[i], p1[i]]);
            }
        }
        _ => {
            for (i, out) in dest[..typesize * n].chunks_exact_mut(typesize).enumerate() {
                for (j, b) in out.iter_mut().enumerate() {
                    *b = src[j * n + i];
                }
            }
        }
    }
    let whole = n * typesize;
    dest[whole..].copy_from_slice(&src[whole..]);
}

/// Byte-unshuffle of 4-byte elements with SSE2 (every x86-64 has it):
/// 16 elements per step by interleaving the four byte planes; the rest
/// element by element.
#[cfg(target_arch = "x86_64")]
fn unshuffle4_sse2(src: &[u8], n: usize, dest: &mut [u8]) {
    use std::arch::x86_64::*;
    let (p0, p1, p2, p3) = (&src[..n], &src[n..2 * n], &src[2 * n..3 * n], &src[3 * n..4 * n]);
    let vec_n = n / 16 * 16;
    let out = &mut dest[..4 * n];
    let mut i = 0;
    while i < vec_n {
        // SAFETY: i + 16 <= n for every plane and 4 * (i + 16) <= out.len();
        // unaligned loads/stores; SSE2 is part of x86-64.
        unsafe {
            let a = _mm_loadu_si128(p0.as_ptr().add(i) as *const __m128i);
            let b = _mm_loadu_si128(p1.as_ptr().add(i) as *const __m128i);
            let c = _mm_loadu_si128(p2.as_ptr().add(i) as *const __m128i);
            let d = _mm_loadu_si128(p3.as_ptr().add(i) as *const __m128i);
            let ab_lo = _mm_unpacklo_epi8(a, b);
            let ab_hi = _mm_unpackhi_epi8(a, b);
            let cd_lo = _mm_unpacklo_epi8(c, d);
            let cd_hi = _mm_unpackhi_epi8(c, d);
            let o = out.as_mut_ptr().add(4 * i) as *mut __m128i;
            _mm_storeu_si128(o, _mm_unpacklo_epi16(ab_lo, cd_lo));
            _mm_storeu_si128(o.add(1), _mm_unpackhi_epi16(ab_lo, cd_lo));
            _mm_storeu_si128(o.add(2), _mm_unpacklo_epi16(ab_hi, cd_hi));
            _mm_storeu_si128(o.add(3), _mm_unpackhi_epi16(ab_hi, cd_hi));
        }
        i += 16;
    }
    for j in vec_n..n {
        out[4 * j..4 * j + 4].copy_from_slice(&[p0[j], p1[j], p2[j], p3[j]]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shuffle_roundtrip_with_a_tail() {
        let data: Vec<u8> = (0..23u8).collect();         // 5 elements of 4 + 3 bytes
        let n = 5;
        let mut shuffled = vec![0u8; 23];
        for i in 0..n {
            for j in 0..4 {
                shuffled[j * n + i] = data[i * 4 + j];
            }
        }
        shuffled[20..].copy_from_slice(&data[20..]);
        let mut back = vec![0u8; 23];
        unshuffle(&shuffled, 4, &mut back);
        assert_eq!(back, data);
        // the vector path: 37 elements of 4 (two steps of 16 + 5) + 2 bytes
        let data: Vec<u8> = (0..150u32).map(|v| (v * 7 % 251) as u8).collect();
        let n = 37;
        let mut shuffled = vec![0u8; 150];
        for i in 0..n {
            for j in 0..4 {
                shuffled[j * n + i] = data[i * 4 + j];
            }
        }
        shuffled[148..].copy_from_slice(&data[148..]);
        let mut back = vec![0u8; 150];
        unshuffle(&shuffled, 4, &mut back);
        assert_eq!(back, data);
    }

    #[test]
    fn refuses_what_it_does_not_decode() {
        let mut frame = vec![2u8, 1, (LZ4_CODE << 5) | DOBITSHUFFLE, 4];
        frame.extend_from_slice(&16u32.to_le_bytes());
        frame.extend_from_slice(&16u32.to_le_bytes());
        frame.extend_from_slice(&32u32.to_le_bytes());
        frame.resize(32, 0);
        assert!(decode(&frame).is_err());
        frame[2] = 3 << 5;                                // zlib codec
        assert!(decode(&frame).is_err());
        assert!(decode(&frame[..10]).is_err());
    }
}
