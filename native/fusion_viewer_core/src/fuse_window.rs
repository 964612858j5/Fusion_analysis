//! One channel's committed display window applied to one value, bit for
//! bit `core/channel_remap.apply_channel_remap` under NumPy 1.26 (plan
//! `docs/f5_rust_fusion_plan.md` §3 steps 1-6). Every step is rounded to
//! f32; nothing is fused:
//!
//!   v not finite                -> 0 (NumPy's final `where(finite, x, 0)`)
//!   max - min <= 0 (in f64)     -> 0 for every pixel (a NaN endpoint is not
//!                                  "<= 0": its NaN is carried)
//!   x = clip((v - f32(min)) / f32(max - min))        denominator taken in f64
//!   x = clip(((x - 0.5) * f32(contrast) + 0.5) + f32(brightness))
//!   x = clip(x ** f32(gamma))                        gamma <= 0 -> 1e-6
//!
//! `clip` is NumPy's `_NPY_CLIP` for floats: NaN stays NaN (with its bits),
//! otherwise `min(max(x, 0), 1)` with `a > b ? a : b` -- so -0.0 becomes
//! +0.0. Arithmetic NaNs (`0/0` from an underflowed denominator, `inf/inf`
//! from `min = -1e300`) are carried, not zeroed (§3 step 6).
//!
//! `x ** e` follows NumPy 1.26's `fast_scalar_power` for an f32 array and
//! an f32 scalar: e == 1 -> x, 0.5 -> sqrt, 2 -> x*x, 0 -> 1 (`ones_like`);
//! otherwise `fuse_powf::powf` (glibc's algorithm). e == -1 cannot occur
//! (gamma <= 0 is replaced first).

/// NumPy's float clip to [0, 1].
#[inline(always)]
pub fn clip01(x: f32) -> f32 {
    if x.is_nan() {
        return x;
    }
    let m = if x > 0.0 { x } else { 0.0 };
    if m < 1.0 { m } else { 1.0 }
}

/// NumPy's float clip to [0, 1] in f64 (`np.clip` of a Python weight).
#[inline(always)]
pub fn clip01_f64(x: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    let m = if x > 0.0 { x } else { 0.0 };
    if m < 1.0 { m } else { 1.0 }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Power {
    Identity,
    Sqrt,
    Square,
    One,
    Pow(f32),
}

impl Power {
    /// The exponent step for a user gamma (a Python float).
    pub fn of(gamma: f64) -> Power {
        let gamma = if gamma <= 0.0 { 1e-6 } else { gamma };
        let e = gamma as f32;
        if e == 1.0 {
            Power::Identity
        } else if e == 0.5 {
            Power::Sqrt
        } else if e == 2.0 {
            Power::Square
        } else if e == 0.0 {
            Power::One
        } else {
            Power::Pow(e)
        }
    }

    #[inline(always)]
    pub fn apply(self, x: f32) -> f32 {
        match self {
            Power::Identity => x,
            Power::Sqrt => x.sqrt(),
            Power::Square => x * x,
            Power::One => 1.0,
            Power::Pow(e) => crate::fuse_powf::powf(x, e),
        }
    }
}

/// The parameters as committed (Python floats). `min`/`max` None: derived
/// from the pixels in hand (the unit), as Python derives them per tile.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Params {
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub brightness: f64,
    pub contrast: f64,
    pub gamma: f64,
}

impl Default for Params {
    /// `channel_remap.DEFAULT_PARAMS`
    fn default() -> Self {
        Params { min: None, max: None, brightness: 0.0, contrast: 1.0, gamma: 1.0 }
    }
}

/// A window resolved to the f32 constants the arithmetic uses.
#[derive(Clone, Copy, Debug)]
pub struct Window {
    degenerate: bool,
    min: f32,
    denom: f32,
    contrast: f32,
    brightness: f32,
    power: Power,
}

impl Params {
    /// True when min or max is derived from the pixels in hand.
    pub fn open(&self) -> bool {
        self.min.is_none() || self.max.is_none()
    }

    /// The window, given the finite (min, max) of the pixels in hand
    /// (`finite_range`), which only an open window uses.
    pub fn resolve(&self, seen: Option<(f64, f64)>) -> Window {
        let (mut lo, mut hi) = (self.min, self.max);
        if self.open() {
            match seen {
                Some((a, b)) => {
                    lo = lo.or(Some(a));
                    hi = hi.or(Some(b));
                }
                None => {
                    lo = lo.or(Some(0.0));
                    hi = hi.or(Some(1.0));
                }
            }
        }
        let (lo, hi) = (lo.unwrap_or(0.0), hi.unwrap_or(1.0));
        let denom = hi - lo;
        Window {
            // `denom <= 0.0`, as Python: a NaN endpoint is not degenerate, its
            // NaN runs through the arithmetic (codex F5-1 r1 P0)
            degenerate: denom <= 0.0,
            min: lo as f32,
            denom: denom as f32,
            contrast: self.contrast as f32,
            brightness: self.brightness as f32,
            power: Power::of(self.gamma),
        }
    }
}

impl Window {
    #[inline(always)]
    pub fn map(&self, v: f32) -> f32 {
        if self.degenerate || !v.is_finite() {
            return 0.0;
        }
        let x = clip01((v - self.min) / self.denom);
        let x = clip01(((x - 0.5) * self.contrast + 0.5) + self.brightness);
        clip01(self.power.apply(x))
    }
}

/// (min, max) over the finite values, as Python floats (`float(arr.min())`).
pub fn finite_range(values: impl Iterator<Item = f32>) -> Option<(f64, f64)> {
    let mut r: Option<(f32, f32)> = None;
    for v in values.filter(|v| v.is_finite()) {
        r = Some(match r {
            None => (v, v),
            Some((a, b)) => (if v < a { v } else { a }, if v > b { v } else { b }),
        });
    }
    r.map(|(a, b)| (a as f64, b as f64))
}

/// `apply_channel_remap` over one plane: an open window takes that plane's
/// own finite range.
pub fn remap_plane(params: &Params, values: &[f32]) -> Vec<f32> {
    let w = params.resolve(if params.open() { finite_range(values.iter().copied()) } else { None });
    values.iter().map(|&v| w.map(v)).collect()
}

#[cfg(test)]
#[path = "fuse_window_tests.rs"]
mod tests;
