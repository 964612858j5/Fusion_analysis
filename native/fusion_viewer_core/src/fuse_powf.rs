//! A platform-independent single-precision `powf`, bit for bit the glibc
//! (>= 2.28, x86_64, FMA build) `powf` that NumPy 1.26 calls on the
//! reference machine (plan `docs/f5_rust_fusion_plan.md` §3 step 5).
//!
//! Third-party code: translated from ARM optimized-routines, release v23.01,
//! commit 56e3bf05c19c4e28e1f5edd9093c712f16c5c32a -- `math/powf.c`,
//! `math/powf_log2_data.c`, `math/exp2f_data.c` (configuration from
//! `math/math_config.h`). Not taken from glibc's (LGPL) copy.
//!
//!   Copyright (c) 2017-2019, Arm Limited. (exp2f_data.c: 2017-2018)
//!   SPDX-License-Identifier: MIT OR Apache-2.0 WITH LLVM-exception
//!   Used under the MIT option (also in `THIRD_PARTY_NOTICES.md`, shipped
//!   in the wheel's `.dist-info/licenses/`):
//!
//!   MIT License
//!
//!   Copyright (c) 1999-2022, Arm Limited.
//!
//!   Permission is hereby granted, free of charge, to any person obtaining a copy
//!   of this software and associated documentation files (the "Software"), to deal
//!   in the Software without restriction, including without limitation the rights
//!   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//!   copies of the Software, and to permit persons to whom the Software is
//!   furnished to do so, subject to the following conditions:
//!
//!   The above copyright notice and this permission notice shall be included in all
//!   copies or substantial portions of the Software.
//!
//!   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//!   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//!   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//!   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//!   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//!   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//!   SOFTWARE.
//!
//! Configuration (glibc on x86_64): TOINT_INTRINSICS = 0 (POWF_SCALE = 1,
//! shift-based range reduction), POWF_LOG2_TABLE_BITS = 4,
//! EXP2F_TABLE_BITS = 5, WANT_ROUNDING = WANT_ERRNO = WANT_ERRNO_UFLOW = 1
//! (the "may underflow" result between 2^-150 and 2^-149). errno is not set.
//!
//! Evaluation order: glibc's x86_64 build selects `__powf_fma` (by ifunc,
//! on CPUs with FMA and AVX2), where the compiler fused exactly nine
//! `a*b + c` into one-rounding FMAs (glibc 2.39 `e_powf.c` lines 60, 65,
//! 66, 68, 69, 70, 106, 108, 109; nine vfmadd in the installed binary).
//! Those nine are `f64::mul_add` here -- fused on every machine, so the
//! result does not depend on the CPU -- and every other operation is
//! rounded separately (`r*r`, `r2*r2`, `logc + k`, `y * logx`, the SHIFT
//! steps, `y * s`). Measured: without the fused sites 116 of 3.2e11
//! (x, gamma) pairs differ from NumPy, with them none.
//!
//! On x86_64 CPUs with FMA the hardware instruction is used (runtime
//! detection); elsewhere `mul_add` is computed in software (same bits,
//! slower).

/// (invc, logc) per log2 subinterval (`__powf_log2_data.tab`).
const LOG2_TAB: [(u64, u64); 16] = [
    (0x3ff661ec79f8f3be, 0xbfdefec65b963019),
    (0x3ff571ed4aaf883d, 0xbfdb0b6832d4fca4),
    (0x3ff49539f0f010b0, 0xbfd7418b0a1fb77b),
    (0x3ff3c995b0b80385, 0xbfd39de91a6dcf7b),
    (0x3ff30d190c8864a5, 0xbfd01d9bf3f2b631),
    (0x3ff25e227b0b8ea0, 0xbfc97c1d1b3b7af0),
    (0x3ff1bb4a4a1a343f, 0xbfc2f9e393af3c9f),
    (0x3ff12358f08ae5ba, 0xbfb960cbbf788d5c),
    (0x3ff0953f419900a7, 0xbfaa6f9db6475fce),
    (0x3ff0000000000000, 0x0000000000000000),
    (0x3fee608cfd9a47ac, 0x3fb338ca9f24f53d),
    (0x3feca4b31f026aa0, 0x3fc476a9543891ba),
    (0x3feb2036576afce6, 0x3fce840b4ac4e4d2),
    (0x3fe9c2d163a1aa2d, 0x3fd40645f0c6651c),
    (0x3fe886e6037841ed, 0x3fd88e9c2c1b9ff8),
    (0x3fe767dcf5534862, 0x3fdce0a44eb17bcc),
];

/// `__powf_log2_data.poly` (POWF_SCALE = 1).
const LOG2_POLY: [u64; 5] = [
    0x3fd27616c9496e0b, 0xbfd71969a075c67a, 0x3fdec70a6ca7badd, 0xbfe7154748bef6c8,
    0x3ff71547652ab82b,
];

/// `__exp2f_data.tab` for N = 32: bits of 2^(i/N) minus (i << 47).
const EXP2_TAB: [u64; 32] = [
    0x3ff0000000000000, 0x3fefd9b0d3158574, 0x3fefb5586cf9890f, 0x3fef9301d0125b51,
    0x3fef72b83c7d517b, 0x3fef54873168b9aa, 0x3fef387a6e756238, 0x3fef1e9df51fdee1,
    0x3fef06fe0a31b715, 0x3feef1a7373aa9cb, 0x3feedea64c123422, 0x3feece086061892d,
    0x3feebfdad5362a27, 0x3feeb42b569d4f82, 0x3feeab07dd485429, 0x3feea47eb03a5585,
    0x3feea09e667f3bcd, 0x3fee9f75e8ec5f74, 0x3feea11473eb0187, 0x3feea589994cce13,
    0x3feeace5422aa0db, 0x3feeb737b0cdc5e5, 0x3feec49182a3f090, 0x3feed503b23e255d,
    0x3feee89f995ad3ad, 0x3feeff76f2fb5e47, 0x3fef199bdd85529c, 0x3fef3720dcef9069,
    0x3fef5818dcfba487, 0x3fef7c97337b9b5f, 0x3fefa4afa2a490da, 0x3fefd0765b6e4540,
];

/// `__exp2f_data.poly` for N = 32.
const EXP2_POLY: [u64; 3] = [0x3fac6af84b912394, 0x3fcebfce50fac4f3, 0x3fe62e42ff0c52d6];

/// `__exp2f_data.shift_scaled` = 0x1.8p+52 / 32.
const SHIFT: u64 = 0x42e8000000000000;
const OFF: u32 = 0x3f330000;
const SIGN_BIAS: u64 = 1 << (5 + 11);

#[inline(always)]
fn d(bits: u64) -> f64 {
    f64::from_bits(bits)
}

#[inline(always)]
fn log2_inline(ix: u32) -> f64 {
    // x = 2^k z; z in [OFF, 2*OFF], exact; 16 subintervals
    let tmp = ix.wrapping_sub(OFF);
    let i = ((tmp >> (23 - 4)) % 16) as usize;
    let top = tmp & 0xff80_0000;
    let iz = ix.wrapping_sub(top);
    let k = (top as i32) >> 23; // arithmetic shift (POWF_SCALE_BITS = 0)
    let (invc, logc) = (d(LOG2_TAB[i].0), d(LOG2_TAB[i].1));
    let z = f32::from_bits(iz) as f64;
    let a = |j: usize| d(LOG2_POLY[j]);
    // log2(x) = log1p(z/c - 1)/ln2 + log2(c) + k
    let r = z.mul_add(invc, -1.0); // e_powf.c:60
    let y0 = logc + k as f64;
    let r2 = r * r;
    let y = a(0).mul_add(r, a(1)); // :65
    let p = a(2).mul_add(r, a(3)); // :66
    let r4 = r2 * r2;
    let q = a(4).mul_add(r, y0); // :68
    let q = p.mul_add(r2, q); // :69
    y.mul_add(r4, q) // :70
}

#[inline(always)]
fn exp2_inline(xd: f64, sign_bias: u64) -> f64 {
    // x = k/N + r with r in [-1/(2N), 1/(2N)]
    let kd = xd + d(SHIFT);
    let ki = kd.to_bits();
    let kd = kd - d(SHIFT);
    let r = xd - kd;
    // exp2(x) = 2^(k/N) * 2^r ~= s * (C0*r^3 + C1*r^2 + C2*r + 1)
    let mut t = EXP2_TAB[(ki % 32) as usize];
    let ski = ki.wrapping_add(sign_bias);
    t = t.wrapping_add(ski << (52 - 5));
    let s = f64::from_bits(t);
    let c = |j: usize| d(EXP2_POLY[j]);
    let z = c(0).mul_add(r, c(1)); // :106
    let r2 = r * r;
    let y = c(2).mul_add(r, 1.0); // :108
    let y = z.mul_add(r2, y); // :109
    y * s
}

/// 0 if not an integer, 1 if odd, 2 if even (`iy` non-zero finite).
fn checkint(iy: u32) -> u32 {
    let e = iy >> 23 & 0xff;
    if e < 0x7f {
        return 0;
    }
    if e > 0x7f + 23 {
        return 2;
    }
    if iy & ((1u32 << (0x7f + 23 - e)) - 1) != 0 {
        return 0;
    }
    if iy & (1u32 << (0x7f + 23 - e)) != 0 {
        return 1;
    }
    2
}

fn zeroinfnan(ix: u32) -> bool {
    ix.wrapping_mul(2).wrapping_sub(1) >= 0x7f80_0000u32.wrapping_mul(2) - 1
}

fn issignaling(x: f32) -> bool {
    (x.to_bits() ^ 0x0040_0000).wrapping_mul(2) > 0x7fc0_0000u32.wrapping_mul(2)
}

/// glibc `math_err.c` `xflowf`: `opt_barrier(sign ? -y : y) * y`.
#[inline(never)]
fn xflowf(sign: bool, y: f32) -> f32 {
    std::hint::black_box(if sign { -y } else { y }) * y
}

/// `__math_invalidf`: `(x - x) / (x - x)`.
#[inline(never)]
fn invalidf(x: f32) -> f32 {
    let z = std::hint::black_box(x - x);
    z / z
}

#[inline(always)]
fn powf_impl(x: f32, y: f32) -> f32 {
    let mut sign_bias: u64 = 0;
    let mut ix = x.to_bits();
    let iy = y.to_bits();
    if ix.wrapping_sub(0x0080_0000) >= 0x7f80_0000 - 0x0080_0000 || zeroinfnan(iy) {
        // x < 0x1p-126, inf or nan; or y is 0, inf or nan
        if zeroinfnan(iy) {
            if iy.wrapping_mul(2) == 0 {
                return if issignaling(x) { x + y } else { 1.0 };
            }
            if ix == 0x3f80_0000 {
                return if issignaling(y) { x + y } else { 1.0 };
            }
            if ix.wrapping_mul(2) > 0x7f80_0000u32.wrapping_mul(2)
                || iy.wrapping_mul(2) > 0x7f80_0000u32.wrapping_mul(2)
            {
                return x + y;
            }
            if ix.wrapping_mul(2) == 0x3f80_0000u32.wrapping_mul(2) {
                return 1.0;
            }
            if (ix.wrapping_mul(2) < 0x3f80_0000u32.wrapping_mul(2)) == (iy & 0x8000_0000 == 0) {
                return 0.0; // |x| < 1 && y == inf, or |x| > 1 && y == -inf
            }
            return y * y;
        }
        if zeroinfnan(ix) {
            let mut x2 = x * x;
            if ix & 0x8000_0000 != 0 && checkint(iy) == 1 {
                x2 = -x2;
            }
            return if iy & 0x8000_0000 != 0 { std::hint::black_box(1.0f32) / x2 } else { x2 };
        }
        // x and y are non-zero finite
        if ix & 0x8000_0000 != 0 {
            let yint = checkint(iy);
            if yint == 0 {
                return invalidf(x);
            }
            if yint == 1 {
                sign_bias = SIGN_BIAS;
            }
            ix &= 0x7fff_ffff;
        }
        if ix < 0x0080_0000 {
            // normalize a subnormal x so its exponent becomes negative
            ix = (x * f32::from_bits(0x4b00_0000)).to_bits(); // x * 0x1p23f
            ix &= 0x7fff_ffff;
            ix = ix.wrapping_sub(23 << 23);
        }
    }
    let logx = log2_inline(ix);
    let ylogx = y as f64 * logx; // cannot overflow, y is single precision
    if (ylogx.to_bits() >> 47 & 0xffff) >= (126.0f64).to_bits() >> 47 {
        // |y * log(x)| >= 126
        let sign = sign_bias != 0;
        if ylogx > d(0x405fffffffd1d571) {
            return xflowf(sign, f32::from_bits(0x7000_0000)); // __math_oflowf: 0x1p97f
        }
        if ylogx > d(0x405fffffffa3aae2) {
            // |x^y| > 0x1.fffffep127: check whether rounding goes away from 0
            let tiny = std::hint::black_box(f32::from_bits(0x3300_0000)); // 0x1p-25f
            if (!sign && 1.0f32 + tiny != 1.0) || (sign && -1.0f32 - tiny != -1.0) {
                return xflowf(sign, f32::from_bits(0x7000_0000));
            }
        }
        if ylogx <= -150.0 {
            return xflowf(sign, f32::from_bits(0x1000_0000)); // __math_uflowf: 0x1p-95f
        }
        if ylogx < -149.0 {
            return xflowf(sign, f32::from_bits(0x1a20_0000)); // __math_may_uflowf: 0x1.4p-75f
        }
    }
    exp2_inline(ylogx, sign_bias) as f32
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "fma")]
fn powf_fma_hw(x: f32, y: f32) -> f32 {
    powf_impl(x, y)
}

/// `x ** y` as glibc's `powf` computes it (see the module header).
pub fn powf(x: f32, y: f32) -> f32 {
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("fma") {
            // SAFETY: the CPU supports FMA (checked just above)
            return unsafe { powf_fma_hw(x, y) };
        }
    }
    powf_impl(x, y)
}

/// The software path on every machine (tests compare it with `powf`).
pub fn powf_portable(x: f32, y: f32) -> f32 {
    powf_impl(x, y)
}

#[cfg(test)]
#[path = "fuse_powf_tests.rs"]
mod tests;
