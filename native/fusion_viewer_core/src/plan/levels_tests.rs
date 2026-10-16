//! R7c step 3: the helper levels' rules, one test each.
use super::super::*;
use super::Order;
use crate::admit::Budgets;
use crate::geom::Pyramid;

const TILE_BYTES: u64 = 512 * 512; // one byte per texel below

// 8192 square, four levels: 16x16, 8x8, 4x4, 2x2 tiles (level 3 the coarsest)
fn planner(order: Order, total: u64) -> Planner {
    let pyr = Pyramid { shapes: vec![(8192, 8192), (4096, 4096), (2048, 2048), (1024, 1024)] };
    let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: 256 << 20, total: Some(total),
                      max_coarse_bytes: None };
    let mut p = Planner::new(pyr, b, 1024);
    p.order = order;
    p.set_texels([(0, 1)].into_iter().collect());
    p.set_active(vec![0]);
    p
}

// a 2 x 2 tile view in the middle of level 0
fn middle(epoch: u64) -> View {
    View { level: 0, bbox_l0: [3072, 3072, 4096, 4096], epoch }
}

// the helper tiles still asked for (reserved)
fn helpers(p: &Planner) -> Vec<TileAt> {
    let mut v: Vec<TileAt> = p.plans.get(&0).map_or(Vec::new(), |f| f.aux.iter().copied().collect());
    v.sort_by_key(|t| (t.level, t.ty, t.tx));
    v
}

#[test]
fn cur_asks_for_no_helper_level() {
    let mut p = planner(Order::Cur, 1 << 32);
    p.set_view(middle(1));
    assert!(helpers(&p).is_empty());
    assert_eq!(p.wanted().1.len(), 4);
}

#[test]
fn odon_asks_for_target_plus_one_around_the_view() {
    let mut p = planner(Order::Odon, 1 << 32);
    p.set_view(middle(1));
    // level 1 covers the view with tile (3, 3); grown by one: 3 x 3
    let h = helpers(&p);
    assert_eq!(h.len(), 9);
    assert!(h.iter().all(|t| t.level == 1 && (2..=4).contains(&t.tx) && (2..=4).contains(&t.ty)));
}

#[test]
fn the_sticky_ceiling_rises_on_zoom_in_and_drops_on_zoom_out() {
    let mut p = planner(Order::Odon, 1 << 32);
    p.set_view(View { level: 2, bbox_l0: [0, 0, 4096, 4096], epoch: 1 });
    assert!(helpers(&p).is_empty(), "target 2: target+1 is the coarsest");
    p.set_view(middle(2));                                 // 2 -> 0: ceiling 2
    let levels: std::collections::BTreeSet<u8> = helpers(&p).iter().map(|t| t.level).collect();
    assert_eq!(levels.into_iter().collect::<Vec<_>>(), vec![1, 2]);
    p.set_view(View { level: 1, bbox_l0: [3072, 3072, 4096, 4096], epoch: 3 });   // 0 -> 1: ceiling 1
    assert_eq!(p.ceiling, Some(1));
    p.set_view(View { level: 1, bbox_l0: [3072, 3584, 4096, 4608], epoch: 4 });   // same level: kept
    assert_eq!(p.ceiling, Some(1));
}

#[test]
fn helpers_are_paid_from_what_the_allowance_leaves_nearest_first() {
    // the coarse (4 tiles) and the target (4 tiles) fit; three tiles more
    let mut p = planner(Order::Odon, 8 * TILE_BYTES + 3 * TILE_BYTES);
    p.set_view(middle(1));
    let h = helpers(&p);
    assert_eq!(h.len(), 3);
    let nearest: Vec<(u32, u32)> = p.padded(1, 1).into_iter().take(3).collect();
    assert!(h.iter().all(|t| nearest.contains(&(t.tx, t.ty))), "{h:?} vs {nearest:?}");
}

#[test]
fn no_allowance_no_helper() {
    let mut p = planner(Order::Odon, 8 * TILE_BYTES);
    p.set_view(middle(1));
    assert!(helpers(&p).is_empty());
}

#[test]
fn a_landed_helper_frees_nothing_to_hand_out_again() {
    let mut p = planner(Order::Odon, 8 * TILE_BYTES + 3 * TILE_BYTES);
    p.set_view(middle(1));
    let before = helpers(&p);
    assert!(p.deliver(0, before[0]));
    let after = helpers(&p);
    assert_eq!(after.len(), 2);
    assert!(after.iter().all(|t| before.contains(t)), "no new helper key");
}

#[test]
fn hybrid_withdraws_its_helpers_once_the_target_is_complete() {
    let mut p = planner(Order::Hybrid, 1 << 32);
    p.set_view(middle(1));
    assert_eq!(helpers(&p).len(), 9);
    assert!(!p.take_reissue());
    let target: Vec<TileAt> = p.wanted().1.into_iter().map(|(_, t)| t).filter(|t| t.level == 0).collect();
    for t in target {
        p.deliver(0, t);
    }
    assert!(helpers(&p).is_empty());
    assert!(p.take_reissue(), "the withdrawal is issued");
    assert!(!p.take_reissue(), "once");
}

#[test]
fn hybrid_never_climbs_a_ladder() {
    let mut p = planner(Order::Hybrid, 1 << 32);
    p.set_view(View { level: 2, bbox_l0: [0, 0, 4096, 4096], epoch: 1 });
    p.set_view(middle(2));
    assert!(helpers(&p).iter().all(|t| t.level == 1));
}

#[test]
fn coarse_only_asks_for_no_helper() {
    let pyr = Pyramid { shapes: vec![(8192, 8192), (4096, 4096), (2048, 2048), (1024, 1024)] };
    let b = Budgets { max_fine_tiles: 0, max_fine_bytes_per_channel: 256 << 20, total: Some(1 << 32),
                      max_coarse_bytes: None };
    let mut p = Planner::new(pyr, b, 1024);
    p.order = Order::Odon;
    p.set_active(vec![0]);
    p.set_view(middle(1));
    assert!(p.wanted().1.is_empty());
}

#[test]
fn odon_draws_the_coarse_tiles_landed_so_far_cur_waits_for_all() {
    for (order, early) in [(Order::Cur, false), (Order::Odon, true), (Order::Hybrid, true)] {
        let mut p = planner(order, 1 << 32);
        p.set_view(middle(1));
        let (coarse, _) = p.wanted();
        assert_eq!(coarse.len(), 4);
        p.deliver(coarse[0].0, coarse[0].1);
        let drawn = p.drawn(0, &|_, _| true);
        assert_eq!(!drawn.is_empty(), early, "{order:?}");
        if early {
            assert_eq!(drawn[0], coarse[0].1);
            assert!(drawn.iter().filter(|t| t.level == 3).count() == 1);
        }
    }
}

#[test]
fn the_ceiling_follows_admission_moved_by_the_channels() {
    // 1 channel admits level 0; 8 channels only level 2 (41 tiles in all)
    let mut p = planner(Order::Odon, 41 * TILE_BYTES);
    p.set_texels((0..8).map(|c| (c, 1)).collect());
    let view = View { level: 0, bbox_l0: [2048, 2048, 4096, 4096], epoch: 1 };
    p.set_view(view);
    assert_eq!(p.admission_level(), Some(0));
    p.set_active((0..8).collect());
    assert_eq!(p.admission_level(), Some(2));
    p.set_active(vec![0]);
    assert_eq!(p.admission_level(), Some(0));
    assert_eq!(p.ceiling, Some(2), "back finer: the ladder keeps level 2");
    let levels: std::collections::BTreeSet<u8> = helpers(&p).iter().map(|t| t.level).collect();
    assert!(levels.contains(&2), "{levels:?}");
}
