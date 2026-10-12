//! What the GPU store should hold ahead of need for the channels drawn
//! now (R2 plan §2.3, docs §48): blocks per storage format, and how the
//! single pass's block samplers are split between the integer and the
//! float family -- by need, not by a fixed share (Python's `sampler_split`
//! gives the float family 4 of 29; it then falls back to drawing channel
//! by channel, which this core does not do). The split changes only how
//! samplers are assigned, never a pixel.

use crate::family::{Format, ALL};
use std::collections::HashMap;

pub struct Need {
    /// drawn channels per format
    pub channels: HashMap<Format, usize>,
    /// complete coarse tiles per channel
    pub coarse_slots: usize,
    /// canvas, device pixels
    pub w: i32,
    pub h: i32,
}

pub struct StorePlan {
    pub targets: HashMap<Format, usize>,
    /// blocks per family the shader binds: [integer, float]
    pub caps: [usize; 2],
}

/// `layers` and `block_bytes` per format, `room` = bytes the blocks may use
/// (budget less slack), `units` = samplers for blocks in all.
pub fn plan(need: &Need, layers: &dyn Fn(Format) -> usize, block_bytes: &dyn Fn(Format) -> usize,
            room: usize, units: usize) -> StorePlan {
    let mut want: HashMap<Format, usize> = HashMap::new();
    for f in ALL {
        let n = need.channels.get(&f).copied().unwrap_or(0);
        if n > 0 {
            want.insert(f, crate::reserve::target_blocks(n, need.coarse_slots, need.w, need.h, layers(f), usize::MAX));
        }
    }
    // within the budget: scale every format down alike, keep one each
    let bytes: usize = want.iter().map(|(f, n)| n * block_bytes(*f)).sum();
    if bytes > room && bytes > 0 {
        for n in want.values_mut() {
            *n = ((*n as u128 * room as u128 / bytes as u128) as usize).max(1);
        }
    }
    let demand = |want: &HashMap<Format, usize>, family: usize| -> usize {
        want.iter().filter(|(f, _)| f.family() == family).map(|(_, n)| *n).sum()
    };
    let (d0, d1) = (demand(&want, 0), demand(&want, 1));
    let units = units.max(2);
    let f = if d0 + d1 <= units {
        d1.clamp(1, units - 1)
    } else {
        ((units * d1).div_ceil(d0 + d1)).clamp(1, units - 1)
    };
    let caps = [units - f, f];
    // no family may want more blocks than it has samplers
    for family in 0..2 {
        let mut over = demand(&want, family).saturating_sub(caps[family]);
        for fmt in ALL.iter().rev().filter(|x| x.family() == family) {
            if let Some(n) = want.get_mut(fmt) {
                let cut = over.min(n.saturating_sub(1));
                *n -= cut;
                over -= cut;
            }
        }
    }
    StorePlan { targets: want, caps }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bytes(_f: Format) -> usize {
        64 << 20                                  // every block 64 MiB
    }

    fn layers(f: Format) -> usize {
        256 / f.bytes()
    }

    #[test]
    fn twenty_raw_channels_take_the_integer_family() {
        let need = Need { channels: HashMap::from([(Format::U8, 20)]), coarse_slots: 16, w: 1307, h: 788 };
        let p = plan(&need, &layers, &bytes, 1536 << 20, 29);
        assert_eq!(p.targets[&Format::U8], 6);
        assert_eq!(p.caps, [28, 1]);
    }

    #[test]
    fn corrected_channels_get_float_samplers() {
        // 3 raw + 3 corrected: f32 needs 3 x 63 slots / 64 per block
        let need = Need { channels: HashMap::from([(Format::U8, 3), (Format::F32, 3)]),
                          coarse_slots: 16, w: 1307, h: 788 };
        let p = plan(&need, &layers, &bytes, 1536 << 20, 29);
        assert_eq!(p.targets[&Format::F32], 4);
        assert_eq!(p.caps[1], 4);
        assert!(p.caps[0] + p.caps[1] == 29);
    }

    #[test]
    fn over_the_samplers_the_targets_are_cut() {
        let need = Need { channels: HashMap::from([(Format::F32, 40)]), coarse_slots: 16, w: 1307, h: 788 };
        let p = plan(&need, &layers, &bytes, 1 << 40, 29);
        assert_eq!(p.caps, [1, 28]);
        assert_eq!(p.targets[&Format::F32], 28);
    }
}
