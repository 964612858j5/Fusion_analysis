//! R7c step 2: the read-ahead's rules, one test each.
use super::super::*;
use super::Mode;
use crate::admit::Budgets;
use crate::geom::Pyramid;

// 8192 square, four levels: 16x16, 8x8, 4x4, 2x2 tiles
fn planner(total: u64, channels: u16) -> Planner {
    let pyr = Pyramid { shapes: vec![(8192, 8192), (4096, 4096), (2048, 2048), (1024, 1024)] };
    let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: 256 << 20, total: Some(total),
                      max_coarse_bytes: None };
    let mut p = Planner::new(pyr, b, 1024);
    p.set_texels((0..channels).map(|c| (c, 4)).collect());
    p.set_active((0..channels).collect());
    // a 2 x 2 tile view in the middle of level 0
    p.set_view(View { level: 0, bbox_l0: [3072, 3072, 4096, 4096], epoch: 1 });
    p
}

#[test]
fn off_reads_nothing_ahead() {
    let mut p = planner(1 << 32, 2);
    let a = p.ahead(Mode::Off, 100_000, 6);
    assert!(a.with_visible.is_empty() && a.after.is_empty());
}

#[test]
fn odon_reads_the_first_ring_with_the_visible_and_the_second_after() {
    let mut p = planner(1 << 32, 2);
    let a = p.ahead(Mode::Odon, 100_000, 6);
    // visible 2x2 -> pad1 4x4 = 12 more positions; 16 positions <= 16: ring 2 (6x6 - 16 = 20)
    assert_eq!(a.pad1_positions, 16);
    assert_eq!(a.with_visible.len(), 12 * 2);
    assert_eq!(a.after.len(), 20 * 2);
    assert!(a.with_visible.iter().all(|(_, t)| t.level == 0));
    assert_eq!(a.in_flight, None);
}

#[test]
fn odon_leaves_the_second_ring_out_with_many_channels_or_a_small_cache() {
    let mut p = planner(1 << 40, 16);
    let a = p.ahead(Mode::Odon, 100_000, 6);
    assert_eq!(a.with_visible.len(), 12 * 16);
    assert!(a.after.is_empty());
    let mut p = planner(1 << 32, 2);
    let a = p.ahead(Mode::Odon, 20, 6);                   // 16 positions x 2 channels > 20 slots
    assert!(!a.with_visible.is_empty() && a.after.is_empty());
}

#[test]
fn limited_reads_one_ring_after_the_visible_at_most_48_one_at_a_time() {
    let mut p = planner(1 << 32, 6);
    let a = p.ahead(Mode::Limited, 100_000, 100);          // (no backlog: 24 missing < 2 x 100)
    assert!(a.with_visible.is_empty());
    assert_eq!(a.after.len(), 48);                        // 12 positions x 6 channels = 72, cut to 48
    assert_eq!(a.in_flight, Some(1));
    let first: Vec<_> = a.after.iter().take(6).map(|(_, t)| (t.tx, t.ty)).collect();
    assert!(first.iter().all(|p| *p == first[0]), "channels of one position together, nearest first");
}

#[test]
fn limited_stops_without_room() {
    // a total that admits the view but leaves no stand-in allowance
    let mut p = planner(4 * 512 * 512 * 4 * 2 + 4 * 512 * 512 * 4 * 2, 2);
    let a = p.ahead(Mode::Limited, 100_000, 100);
    assert!(a.after.is_empty() && a.skipped.contains("allowance"), "{}", a.skipped);
}

#[test]
fn limited_waits_while_the_visible_tiles_are_backlogged() {
    let mut p = planner(1 << 32, 6);                      // 4 positions x 6 channels missing
    let a = p.ahead(Mode::Limited, 100_000, 6);           // > 2 x 6 readers
    assert!(a.after.is_empty() && a.skipped.contains("backlog"), "{}", a.skipped);
}
