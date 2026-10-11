//! How many GPU blocks to hold ahead of need (docs §48): per drawn channel
//! its complete coarse plus the fine tiles of a transition -- the target
//! at about screen resolution and stand-ins one level finer (twice the
//! texels across) -- counted in 512x512 slots, plus one spare block, and
//! never more than the admission budget or the shader's blocks allow.
//! Design reference (no code copied): Odon sizes its tile cache from the
//! visible target set plus a quarter.

use crate::geom::TILE;

/// Tiles across x down that a `w` x `h` px screen covers at `scale` texels
/// per px, in the worst alignment.
fn screen_tiles(w: i32, h: i32, scale: usize) -> usize {
    let across = |px: i32| (px.max(1) as usize * scale).div_ceil(TILE) + 1;
    across(w) * across(h)
}

/// Slots one channel needs through a zoom: target plus finer stand-ins.
pub fn fine_slots(w: i32, h: i32) -> usize {
    screen_tiles(w, h, 1) + screen_tiles(w, h, 2)
}

/// Blocks to hold for `channels` drawn channels whose complete coarse is
/// `coarse_slots` tiles each, on a `w` x `h` px canvas.
pub fn target_blocks(channels: usize, coarse_slots: usize, w: i32, h: i32,
                     layers_per_block: usize, cap_blocks: usize) -> usize {
    if channels == 0 {
        return 0;
    }
    let slots = channels * (coarse_slots + fine_slots(w, h));
    (slots.div_ceil(layers_per_block.max(1)) + 1).min(cap_blocks)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twenty_channels_on_the_bench_canvas() {
        // 1307 x 788 px: target 4 x 3 = 12, stand-ins 7 x 5 = 35 tiles
        assert_eq!(fine_slots(1307, 788), 47);
        // 20 x (16 coarse + 47) = 1260 slots = 5 blocks of 256, + 1 spare
        assert_eq!(target_blocks(20, 16, 1307, 788, 256, 25), 6);
        assert_eq!(target_blocks(20, 16, 1307, 788, 256, 4), 4);
        assert_eq!(target_blocks(0, 16, 1307, 788, 256, 25), 0);
        assert_eq!(target_blocks(3, 16, 1307, 788, 256, 25), 2);
    }
}
