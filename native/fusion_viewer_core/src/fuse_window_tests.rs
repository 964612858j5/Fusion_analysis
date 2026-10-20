//! The window's rules, one test each (plan §7.1). The NumPy comparison
//! itself is `tests/test_f5_rust_fusion.py`.
use super::*;

fn p(min: Option<f64>, max: Option<f64>, b: f64, c: f64, g: f64) -> Params {
    Params { min, max, brightness: b, contrast: c, gamma: g }
}

#[test]
fn clip_is_numpys() {
    assert_eq!(clip01(-0.0).to_bits(), 0);
    assert_eq!(clip01(-1.0), 0.0);
    assert_eq!(clip01(2.0), 1.0);
    assert_eq!(clip01(f32::INFINITY), 1.0);
    let nan = f32::from_bits(0x7fc0_0001);
    assert_eq!(clip01(nan).to_bits(), 0x7fc0_0001);
    assert!(clip01_f64(f64::NAN).is_nan());
}

#[test]
fn the_gamma_fast_paths_are_numpys() {
    assert_eq!(Power::of(1.0), Power::Identity);
    assert_eq!(Power::of(0.5), Power::Sqrt);
    assert_eq!(Power::of(2.0), Power::Square);
    assert_eq!(Power::of(1e-50), Power::One); // f32 underflow
    assert_eq!(Power::of(0.0), Power::Pow(1e-6));
    assert_eq!(Power::of(-3.0), Power::Pow(1e-6));
    assert_eq!(Power::of(0.7), Power::Pow(0.7));
    assert_eq!(Power::One.apply(f32::NAN), 1.0);
}

#[test]
fn non_finite_inputs_and_a_degenerate_window_are_zero() {
    let w = p(Some(0.0), Some(10.0), 0.0, 1.0, 1.0).resolve(None);
    for v in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
        assert_eq!(w.map(v).to_bits(), 0);
    }
    let d = p(Some(5.0), Some(5.0), 0.3, 1.0, 1.0).resolve(None);
    assert_eq!(d.map(7.0).to_bits(), 0);
    let neg = p(Some(5.0), Some(1.0), 0.3, 1.0, 1.0).resolve(None);
    assert_eq!(neg.map(3.0).to_bits(), 0);
}

#[test]
fn identity_contrast_is_still_computed() {
    // (x - 0.5) * 1 + 0.5 is not x for tiny x
    let w = p(Some(0.0), Some(1e9), 0.0, 1.0, 1.0).resolve(None);
    assert_eq!(w.map(1.0), 0.0);
}

#[test]
fn arithmetic_nans_are_carried() {
    // max - min > 0 in f64 but 0 in f32: 0/0 (plan §3 step 6)
    let u = p(Some(0.0), Some(1e-50), 0.1, 1.0, 1.0).resolve(None);
    assert!(u.map(0.0).is_nan());
    assert_eq!(p(Some(0.0), Some(1e-50), 0.1, 1.0, 1e-50).resolve(None).map(0.0), 1.0); // NaN ** 0
    assert!(p(Some(-1e300), Some(0.0), 0.0, 1.0, 1.0).resolve(None).map(0.0).is_nan()); // inf/inf
}

#[test]
fn an_open_window_uses_the_pixels_in_hand() {
    let r = finite_range([3.0f32, f32::NAN, -2.0, f32::INFINITY, 7.0].into_iter());
    assert_eq!(r, Some((-2.0, 7.0)));
    assert_eq!(finite_range([f32::NAN].into_iter()), None);
    let w = p(None, Some(10.0), 0.0, 1.0, 1.0).resolve(r);
    let x = clip01((5.0f32 - (-2.0f32)) / 12.0f32);
    assert_eq!(w.map(5.0), clip01(((x - 0.5) * 1.0 + 0.5) + 0.0));
    // nothing finite: 0 / 1
    let e = p(None, None, 0.0, 1.0, 1.0).resolve(None);
    assert_eq!(e.map(0.5), clip01(((0.5f32 - 0.5) * 1.0 + 0.5) + 0.0));
}
