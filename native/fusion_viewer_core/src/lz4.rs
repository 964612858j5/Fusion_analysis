//! LZ4 block decoding into a buffer of known size (what Blosc stores per
//! stream). Pure, safe Rust, never panics on bad input.
//!
//! Written here because the corrected products' shuffled float bytes are
//! full of short-offset matches (runs of one exponent byte) that a
//! byte-at-a-time overlapping copy makes ~4x slower than C (measured,
//! docs §49); a match whose offset is shorter than its length is copied by
//! doubling the already-written period instead.
//! Format reference: the LZ4 block format description (no code copied).

use crate::error::{CoreError, Result};

fn bad(why: &str) -> CoreError {
    CoreError::Decode(format!("lz4: {why}"))
}

/// A length's 255-continued extension, starting at `src[*i]`.
#[inline]
fn extend(src: &[u8], i: &mut usize, mut len: usize) -> Result<usize> {
    loop {
        let b = *src.get(*i).ok_or_else(|| bad("length runs past the input"))?;
        *i += 1;
        len += b as usize;
        if b != 255 {
            return Ok(len);
        }
    }
}

/// Decode `src` into `dst`; returns the bytes written.
pub fn decompress_into(src: &[u8], dst: &mut [u8]) -> Result<usize> {
    let (mut i, mut o) = (0usize, 0usize);
    if src.is_empty() {
        return Err(bad("empty input"));
    }
    loop {
        let token = *src.get(i).ok_or_else(|| bad("missing token"))?;
        i += 1;
        let mut lit = (token >> 4) as usize;
        if lit == 15 {
            lit = extend(src, &mut i, lit)?;
        }
        let from = src.get(i..i + lit).ok_or_else(|| bad("literals past the input"))?;
        dst.get_mut(o..o + lit).ok_or_else(|| bad("literals past the output"))?.copy_from_slice(from);
        i += lit;
        o += lit;
        if i == src.len() {
            return Ok(o);                                  // the last sequence: literals only
        }
        let off = src.get(i..i + 2).ok_or_else(|| bad("missing offset"))?;
        let offset = u16::from_le_bytes([off[0], off[1]]) as usize;
        i += 2;
        if offset == 0 || offset > o {
            return Err(bad("offset outside the output"));
        }
        let mut mlen = (token & 15) as usize + 4;
        if token & 15 == 15 {
            mlen = extend(src, &mut i, mlen)?;
        }
        if o + mlen > dst.len() {
            return Err(bad("match past the output"));
        }
        let start = o - offset;
        if offset >= mlen {
            dst.copy_within(start..start + mlen, o);
        } else if offset == 1 {
            let v = dst[start];
            dst[o..o + mlen].fill(v);
        } else {
            // the period [start, o) repeats: copy what is already written,
            // doubling the source window each time
            let mut done = 0;
            while done < mlen {
                let n = (o + done - start).min(mlen - done);
                dst.copy_within(start..start + n, o + done);
                done += n;
            }
        }
        o += mlen;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals_and_overlapping_matches() {
        // "abc" literals, then a match of 9 at offset 3 -> "abcabcabcabc"
        let src = [0x35u8, b'a', b'b', b'c', 3, 0, 0x10, b'!'];
        let mut dst = [0u8; 13];
        assert_eq!(decompress_into(&src, &mut dst).unwrap(), 13);
        assert_eq!(&dst, b"abcabcabcabc!");
    }

    #[test]
    fn bad_input_is_an_error_not_a_panic() {
        let mut dst = [0u8; 8];
        assert!(decompress_into(&[], &mut dst).is_err());
        assert!(decompress_into(&[0xF0], &mut dst).is_err());
        assert!(decompress_into(&[0x10, b'a', 5, 0], &mut dst).is_err());   // offset past output
        assert!(decompress_into(&[0x80, 1, 2], &mut dst).is_err());         // literals past input
    }
}
