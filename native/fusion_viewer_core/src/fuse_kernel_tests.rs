//! The kernel's rules, one test each (plan §7.1). The NumPy comparison
//! itself is `tests/test_f5_rust_fusion.py`.
use super::*;
use crate::fuse_lut;
use crate::fuse_window::Params;

fn win(min: f64, max: f64) -> Window {
    Params { min: Some(min), max: Some(max), ..Params::default() }.resolve(None)
}

fn run(groups: &[GroupTerms], nucleus: Option<&Term>, n: usize) -> Vec<u16> {
    let mut out = vec![7u16; n * 2];
    fuse_all(groups, nucleus, n, 3, &mut out); // strips of 3: crosses strip edges
    out
}

#[test]
fn nothing_to_fuse_is_zero() {
    assert_eq!(run(&[], None, 5), vec![0; 10]);
    let empty = [GroupTerms { weight: 1.0, terms: vec![] }];
    assert_eq!(run(&empty, None, 5), vec![0; 10]);
}

#[test]
fn a_group_sums_then_weights_then_clips_and_groups_take_the_max() {
    let a = [0.0f32, 50.0, 100.0, 100.0, 25.0];
    let b = [100.0f32, 50.0, 100.0, 0.0, 25.0];
    let w = win(0.0, 100.0);
    let g1 = GroupTerms { weight: 0.5, terms: vec![Term::F32 { raw: &a, window: w, weight: 1.0 },
                                                   Term::F32 { raw: &b, window: w, weight: 1.0 }] };
    let g2 = GroupTerms { weight: 1.0, terms: vec![Term::F32 { raw: &b, window: w, weight: 0.25 }] };
    let out = run(&[g1, g2], Some(&Term::F32 { raw: &a, window: w, weight: 1.0 }), 5);
    let want_c = [0.5f32, 0.5, 1.0, 0.5, 0.25];
    for i in 0..5 {
        assert_eq!(out[2 * i], quantise(want_c[i]), "cyto {i}");
        assert_eq!(out[2 * i + 1], quantise(clip01(w.map(a[i]))), "nucleus {i}");
    }
}

#[test]
fn a_group_weight_above_one_is_the_callers_to_clip_and_the_sum_is_clipped() {
    let a = [100.0f32];
    let w = win(0.0, 100.0);
    let g = GroupTerms { weight: 1.0, terms: vec![Term::F32 { raw: &a, window: w, weight: 1.0 },
                                                  Term::F32 { raw: &a, window: w, weight: 1.0 }] };
    assert_eq!(run(&[g], None, 1), vec![65535, 0]);
    let zero = GroupTerms { weight: 0.0, terms: vec![Term::F32 { raw: &a, window: w, weight: 1.0 }] };
    assert_eq!(run(&[zero], None, 1), vec![0, 0]);
}

#[test]
fn integer_tables_equal_the_float_path() {
    let raw8: Vec<u8> = (0..=255).collect();
    let raw16: Vec<u16> = (0..=65535).collect();
    let p = Params { min: Some(1.0), max: Some(105.203125), brightness: -0.03, contrast: 0.8, gamma: 1.5 };
    let w = p.resolve(None);
    let t8 = fuse_lut::build(&w, 0.33, 256);
    let t16 = fuse_lut::build(&w, 0.33, 65536);
    let f8: Vec<f32> = raw8.iter().map(|&v| v as f32).collect();
    let f16: Vec<f32> = raw16.iter().map(|&v| v as f32).collect();
    let mut a = vec![0f32; 256];
    Term::U8 { raw: &raw8, table: &t8 }.apply(&mut a, 0, false);
    let mut b = vec![0f32; 256];
    Term::F32 { raw: &f8, window: w, weight: 0.33 }.apply(&mut b, 0, false);
    assert!(a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits()));
    let mut a = vec![0f32; 65536];
    Term::U16 { raw: &raw16, table: &t16 }.apply(&mut a, 0, false);
    let mut b = vec![0f32; 65536];
    Term::F32 { raw: &f16, window: w, weight: 0.33 }.apply(&mut b, 0, false);
    assert!(a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits()));
}

#[test]
fn nan_wins_the_maximum_and_quantises_to_zero() {
    assert!(np_maximum(f32::NAN, 0.5).is_nan());
    assert!(np_maximum(0.5, f32::NAN).is_nan());
    assert_eq!(np_maximum(0.0, -0.0).to_bits(), (-0.0f32).to_bits()); // equal: the second
    assert_eq!(quantise(f32::NAN), 0);
    // an underflowed window gives NaN (plan §3 step 6): the whole group, and
    // the maximum over groups, become NaN -> 0
    let nan_w = Params { min: Some(0.0), max: Some(1e-50), brightness: 0.1, ..Params::default() }.resolve(None);
    let ok_w = win(0.0, 100.0);
    let a = [0.0f32, 50.0];
    let same = GroupTerms { weight: 1.0, terms: vec![Term::F32 { raw: &a, window: ok_w, weight: 1.0 },
                                                     Term::F32 { raw: &a, window: nan_w, weight: 1.0 }] };
    assert_eq!(run(&[same], None, 2)[0], 0);
    let g_ok = GroupTerms { weight: 1.0, terms: vec![Term::F32 { raw: &a, window: ok_w, weight: 1.0 }] };
    let g_nan = GroupTerms { weight: 1.0, terms: vec![Term::F32 { raw: &a, window: nan_w, weight: 1.0 }] };
    let out = run(&[g_ok, g_nan], None, 2);
    assert_eq!(out[0], 0); // pixel 0: maximum(0.0, NaN) -> NaN -> 0
    assert_eq!(out[2], 65535); // pixel 1: the underflowed window gives inf -> 1
}
