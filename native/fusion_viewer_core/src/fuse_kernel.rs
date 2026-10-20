//! One strip of pixels fused and quantised: `fuse_channels` followed by
//! `_fuse_tile`'s x65535 uint16 cast (plan §3), under NumPy 1.26:
//!
//!   acc_g   = 0;  acc_g += f32(signal * w)        members in order
//!   acc_g   = clip(acc_g * gw);  cyto = maximum(cyto, acc_g)   (cyto from 0)
//!   cyto    = clip(cyto)
//!   nucleus = clip(signal_n * w_n)                (0 without a nucleus)
//!   out     = [u16(cyto * 65535), u16(nucleus * 65535)]   truncating; NaN -> 0
//!
//! `maximum` is NumPy's: a NaN first argument wins, then a NaN second one,
//! otherwise `a > b ? a : b`. A term yields `f32(signal * w)` per pixel.

use crate::fuse_window::{Window, clip01};

/// One weighted channel over the unit's pixels.
pub enum Term<'a> {
    /// integer samples through a weighted table (`fuse_lut::build`)
    U8 { raw: &'a [u8], table: &'a [f32] },
    U16 { raw: &'a [u16], table: &'a [f32] },
    /// float samples mapped per pixel, then weighted
    F32 { raw: &'a [f32], window: Window, weight: f32 },
}

impl Term<'_> {
    /// `out[i] = f(at + i)` when `add` is false, `out[i] += f(at + i)` otherwise.
    #[inline]
    fn apply(&self, out: &mut [f32], at: usize, add: bool) {
        let n = out.len();
        macro_rules! each {
            ($raw:expr, $f:expr) => {
                if add {
                    for (o, &v) in out.iter_mut().zip(&$raw[at..at + n]) {
                        *o += $f(v);
                    }
                } else {
                    for (o, &v) in out.iter_mut().zip(&$raw[at..at + n]) {
                        *o = $f(v);
                    }
                }
            };
        }
        match self {
            Term::U8 { raw, table } => each!(raw, |v: u8| table[v as usize]),
            Term::U16 { raw, table } => each!(raw, |v: u16| table[v as usize]),
            Term::F32 { raw, window, weight } => each!(raw, |v: f32| window.map(v) * *weight),
        }
    }
}

pub struct GroupTerms<'a> {
    pub weight: f32,
    pub terms: Vec<Term<'a>>,
}

/// NumPy's `maximum` for floats.
#[inline(always)]
pub fn np_maximum(a: f32, b: f32) -> f32 {
    if a.is_nan() {
        a
    } else if a > b {
        a
    } else {
        b
    }
}

/// `astype(uint16)` of `x * 65535` for x in [0, 1] or NaN (NaN -> 0).
#[inline(always)]
pub fn quantise(x: f32) -> u16 {
    (x * 65535.0) as u16
}

/// Scratch strips of one length, reused across strips.
pub struct Scratch {
    acc: Vec<f32>,
    cyto: Vec<f32>,
    nuc: Vec<f32>,
}

impl Scratch {
    pub fn new(len: usize) -> Self {
        Scratch { acc: vec![0.0; len], cyto: vec![0.0; len], nuc: vec![0.0; len] }
    }
}

/// Fuse pixels `at .. at + n` into `out` (`2 * n` u16, interleaved cyto,
/// nucleus), `n = out.len() / 2 <= scratch length`.
pub fn fuse_strip(groups: &[GroupTerms], nucleus: Option<&Term>, at: usize,
                  scratch: &mut Scratch, out: &mut [u16]) {
    let n = out.len() / 2;
    let (acc, cyto, nuc) = (&mut scratch.acc[..n], &mut scratch.cyto[..n], &mut scratch.nuc[..n]);
    cyto.fill(0.0);
    for g in groups {
        acc.fill(0.0);
        for t in &g.terms {
            t.apply(acc, at, true);
        }
        for (c, &a) in cyto.iter_mut().zip(acc.iter()) {
            *c = np_maximum(*c, clip01(a * g.weight));
        }
    }
    match nucleus {
        Some(t) => t.apply(nuc, at, false),
        None => nuc.fill(0.0),
    }
    for (i, px) in out.chunks_exact_mut(2).enumerate() {
        px[0] = quantise(clip01(cyto[i]));
        px[1] = quantise(clip01(nuc[i]));
    }
}

/// Fuse `n` pixels in strips of `strip` into `out` (`2 * n` u16).
pub fn fuse_all(groups: &[GroupTerms], nucleus: Option<&Term>, n: usize, strip: usize, out: &mut [u16]) {
    let strip = strip.max(1);
    let mut scratch = Scratch::new(strip.min(n.max(1)));
    let mut at = 0;
    while at < n {
        let k = strip.min(n - at);
        fuse_strip(groups, nucleus, at, &mut scratch, &mut out[at * 2..(at + k) * 2]);
        at += k;
    }
}

#[cfg(test)]
#[path = "fuse_kernel_tests.rs"]
mod tests;
