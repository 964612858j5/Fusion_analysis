//! A channel's window tabulated for every value of an integer sample type,
//! already multiplied by the member's f32 weight: `table[v] = map(v) * w`.
//! Exact: NumPy's element-wise arithmetic gives the same f32 for the same
//! input wherever it sits, so one lookup equals the per-pixel computation
//! (plan §3, "查表").

use crate::fuse_window::Window;

/// 256 entries for u8 samples, 65536 for u16.
pub fn build(window: &Window, weight: f32, values: usize) -> Vec<f32> {
    (0..values).map(|v| window.map(v as f32) * weight).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fuse_window::Params;

    #[test]
    fn a_table_is_the_per_value_computation() {
        let p = Params { min: Some(1.0), max: Some(105.203125), brightness: 0.05, contrast: 1.3, gamma: 0.7 };
        let w = p.resolve(None);
        let t = build(&w, 0.33, 65536);
        for v in 0..65536usize {
            assert_eq!(t[v].to_bits(), (w.map(v as f32) * 0.33f32).to_bits());
        }
    }
}
