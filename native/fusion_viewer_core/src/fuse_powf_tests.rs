//! `fuse_powf` against the platform's glibc `powf` (Linux only: the
//! reference NumPy 1.26 calls). The exhaustive sweep is `#[ignore]`d:
//! `cargo test --release --lib fuse_powf -- --ignored` (about 10 minutes
//! on 16 threads).
use super::*;

#[cfg(target_os = "linux")]
unsafe extern "C" {
    #[link_name = "powf"]
    fn glibc_powf(x: f32, y: f32) -> f32;
}

#[cfg(target_os = "linux")]
fn reference(x: f32, y: f32) -> f32 {
    // SAFETY: a pure libm function
    unsafe { glibc_powf(std::hint::black_box(x), std::hint::black_box(y)) }
}

/// The spinbox's gammas: 0.10 .. 3.00 in steps of 0.01 (`k / 100`).
fn ui_gammas() -> Vec<f32> {
    (10..=300).map(|k| (k as f64 / 100.0) as f32).collect()
}

/// 200 deterministic exponents, log-uniform in [1e-3, 300] (plan §3 step 5).
fn random_gammas() -> Vec<f32> {
    let mut s = 0x2026_1020_f5f5_0001u64;
    (0..200).map(|_| {
        let u = (rng(&mut s) >> 11) as f64 / (1u64 << 53) as f64;
        10f64.powf(-3.0 + u * (300f64.log10() + 3.0)) as f32
    }).collect()
}

/// FNV-1a over the result bits of `f` on a fixed grid of (x, gamma).
fn grid_hash(f: impl Fn(f32, f32) -> f32) -> u64 {
    let gammas = [0.1f32, 0.33, 0.7, 1.3, 1.5, 2.2, 2.97, 0.123456, 5.0, 180.6949];
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    for g in gammas {
        let mut b = 0u32;
        while b <= 0x3f80_0000 {
            for byte in f(f32::from_bits(b), g).to_bits().to_le_bytes() {
                h = (h ^ byte as u64).wrapping_mul(0x0000_0100_0000_01b3);
            }
            b += 1009;
        }
    }
    h
}

/// `grid_hash` of glibc 2.39's `powf` on the reference machine (checked
/// against glibc by `the_grid_hash_is_glibcs` on Linux). This test runs on
/// every platform: on Windows it is the bit-identity check (plan §9 F5-1).
const GLIBC_GRID_HASH: u64 = 7_423_969_108_811_886_722;

#[test]
fn both_paths_reproduce_the_reference_grid_on_this_platform() {
    assert_eq!(grid_hash(powf), GLIBC_GRID_HASH, "hardware-dispatched path");
    assert_eq!(grid_hash(powf_portable), GLIBC_GRID_HASH, "portable (software fma) path");
}

#[cfg(target_os = "linux")]
#[test]
fn the_grid_hash_is_glibcs() {
    assert_eq!(grid_hash(reference), GLIBC_GRID_HASH);
}

/// xorshift: deterministic, no dependency
fn rng(seed: &mut u64) -> u64 {
    *seed ^= *seed << 13;
    *seed ^= *seed >> 7;
    *seed ^= *seed << 17;
    *seed
}

#[test]
fn special_values_follow_the_algorithm() {
    assert_eq!(powf(0.0, 0.7).to_bits(), 0);
    assert_eq!(powf(-0.0, 3.0).to_bits(), 0x8000_0000); // odd integer keeps the sign
    assert_eq!(powf(-0.0, 2.5).to_bits(), 0);
    assert_eq!(powf(1.0, 0.7), 1.0);
    assert!(powf(f32::NAN, 0.7).is_nan());
    assert_eq!(powf(f32::NAN, 0.0), 1.0);
    assert_eq!(powf(0.25, 0.5), 0.5);
    assert_eq!(powf(f32::from_bits(1), 3.0), 0.0); // underflow
}

/// Known sensitive cases: the FMA and non-FMA glibc builds differ at the
/// first; glibc is not correctly rounded at the other two. The expected
/// bits are checked on every platform, glibc itself only on Linux.
const KNOWN: [(u32, u32, u32); 3] = [(0x3f51a2a9, 0x4334b1e5, 0x2570f441),
                                     (0x06db0fc6, 0x3f333333, 0x17d62c6d),
                                     (0x3e828000, 0x40400000, 0x3c87a5bf)]; // 261/1024 ** 3

#[test]
fn known_cases_on_every_platform() {
    for (x, y, want) in KNOWN {
        let (x, y) = (f32::from_bits(x), f32::from_bits(y));
        assert_eq!(powf(x, y).to_bits(), want);
        assert_eq!(powf_portable(x, y).to_bits(), want);
    }
}

#[cfg(target_os = "linux")]
#[test]
fn known_cases_are_glibcs() {
    assert_eq!((261.0f32 / 1024.0).to_bits(), 0x3e82_8000);
    for (x, y, want) in KNOWN {
        assert_eq!(reference(f32::from_bits(x), f32::from_bits(y)).to_bits(), want,
                   "the machine's glibc is not the reference build");
    }
}

#[cfg(target_os = "linux")]
#[test]
fn random_pairs_match_glibc_and_both_paths_agree() {
    let mut s = 0x9e37_79b9_7f4a_7c15u64;
    for _ in 0..2_000_000 {
        let x = f32::from_bits((rng(&mut s) % 0x3f80_0001) as u32);
        let y = f32::from_bits(((rng(&mut s) >> 11) as u32 % 0x4400_0000).max(1)); // (0, 512)
        let want = reference(x, y).to_bits();
        assert_eq!(powf(x, y).to_bits(), want, "x {:#x} y {:#x}", x.to_bits(), y.to_bits());
        assert_eq!(powf_portable(x, y).to_bits(), want, "software fma, x {:#x} y {:#x}", x.to_bits(), y.to_bits());
    }
}

/// Every f32 in [0, 1] (and -0.0) for every UI gamma and the 200 random
/// exponents, 16 threads.
#[cfg(target_os = "linux")]
#[test]
#[ignore]
fn exhaustive_unit_interval_for_every_ui_gamma() {
    let mut gammas = ui_gammas();
    gammas.extend(random_gammas());
    let top: u32 = 0x3f80_0000;
    let threads = 16u32;
    let bad = std::sync::atomic::AtomicU64::new(0);
    std::thread::scope(|sc| {
        for t in 0..threads {
            let (gammas, bad) = (&gammas, &bad);
            sc.spawn(move || {
                for &g in gammas {
                    let mut b = t;
                    while b <= top {
                        let x = f32::from_bits(b);
                        if powf(x, g).to_bits() != reference(x, g).to_bits() {
                            bad.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        }
                        b += threads;
                    }
                    if t == 0 && powf(-0.0, g).to_bits() != reference(-0.0, g).to_bits() {
                        bad.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                }
            });
        }
    });
    assert_eq!(bad.into_inner(), 0);
}
