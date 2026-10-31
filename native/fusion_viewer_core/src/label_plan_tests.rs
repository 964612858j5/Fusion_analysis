//! Several slots (docs/v17_step3_multi_results_plan.md §3.4): tile-major
//! order, fair cutting, and which slots are asked for at all.
use super::*;

const MIB: usize = 1 << 20;

// 4096 x 4096 with a 2-level pyramid: 8x8 tiles of 512 (1 MiB each) on level 0
fn pyr() -> Pyramid {
    Pyramid { shapes: vec![(4096, 4096), (2048, 2048)] }
}

fn slots(n: u64) -> Vec<SlotPlan> {
    (1..=n).map(|stamp| SlotPlan { stamp, shown: true, pyramid: true }).collect()
}

fn run(level: u8, visible: &[(u32, u32)], slots: &[SlotPlan], resident: &[(LKey, [f64; 4])], budget: usize) -> PlanOut {
    plan(&PlanIn { pyr: &pyr(), level, visible, slots, resident, budget }).unwrap()
}

#[test]
fn every_slot_gets_a_tile_before_any_gets_the_next() {
    let out = run(0, &[(3, 3), (4, 3)], &slots(3), &[], 1 << 30);
    let firsts: Vec<u64> = out.wanted.iter().take(3).map(|k| k.0).collect();
    assert_eq!(firsts, vec![1, 2, 3], "the nearest tile goes to every slot first");
    let tile_of_first_three: Vec<(u32, u32)> = out.wanted.iter().take(3).map(|k| (k.2, k.3)).collect();
    assert!(tile_of_first_three.windows(2).all(|w| w[0] == w[1]));
}

#[test]
fn over_the_budget_every_slot_loses_its_far_tiles_together() {
    // 4 visible tiles x 3 slots = 12 MiB wanted; 7 MiB fit
    let visible = [(3, 3), (4, 3), (3, 4), (4, 4)];
    let out = run(0, &visible, &slots(3), &[], 7 * MIB);
    assert!(out.note.is_some(), "the budget note");
    let mut per_slot = std::collections::HashMap::new();
    for k in &out.wanted {
        *per_slot.entry(k.0).or_insert(0) += 1;
    }
    for stamp in 1..=3 {
        let n = per_slot.get(&stamp).copied().unwrap_or(0);
        assert!((2..=3).contains(&n), "slot {stamp} got {n} tiles: no slot is starved");
    }
}

#[test]
fn a_hidden_slot_or_one_without_pyramid_is_not_asked_for() {
    let s = [SlotPlan { stamp: 7, shown: false, pyramid: true },
             SlotPlan { stamp: 8, shown: true, pyramid: false },
             SlotPlan { stamp: 9, shown: true, pyramid: true }];
    let coarse = run(1, &[(1, 1)], &s, &[], 1 << 30);
    assert!(coarse.wanted.iter().all(|k| k.0 == 9), "{:?}", coarse.wanted);
    let fine = run(0, &[(1, 1)], &s, &[], 1 << 30);
    let asked: std::collections::BTreeSet<u64> = fine.wanted.iter().map(|k| k.0).collect();
    assert_eq!(asked.into_iter().collect::<Vec<_>>(), vec![8, 9], "level 0 needs no pyramid");
}

#[test]
fn old_level_tiles_stay_under_the_view_only_for_shown_slots() {
    let rect = [0.0, 1024.0, 0.0, 1024.0];
    let resident = [((1u64, 1u8, 0u32, 0u32), rect), ((2u64, 1u8, 0u32, 0u32), rect)];
    let s = [SlotPlan { stamp: 1, shown: true, pyramid: true }, SlotPlan { stamp: 2, shown: false, pyramid: true }];
    let out = run(0, &[(0, 0), (1, 1)], &s, &resident, 1 << 30);
    assert!(out.keep.contains(&(1, 1, 0, 0)), "a shown slot's coarse tile stays under the view");
    assert!(out.released.contains(&(2, 1, 0, 0)), "a hidden slot's tile goes");
}

#[test]
fn eight_and_sixteen_slots_are_cut_fairly() {
    let visible: Vec<(u32, u32)> = (2..6).flat_map(|x| (2..6).map(move |y| (x, y))).collect();  // 16 tiles
    for n in [8u64, 16] {
        // room for about half of what the visible tiles of every slot need
        let budget = (16 * n as usize / 2) * MIB;
        let out = run(0, &visible, &slots(n), &[], budget);
        assert!(out.note.is_some());
        let mut per_slot = std::collections::HashMap::new();
        for k in &out.wanted {
            *per_slot.entry(k.0).or_insert(0usize) += 1;
        }
        let (lo, hi) = (per_slot.values().min().copied().unwrap_or(0), per_slot.values().max().copied().unwrap_or(0));
        assert_eq!(per_slot.len() as u64, n, "{n} slots: one got nothing");
        assert!(hi - lo <= 1, "{n} slots: {lo}..{hi} tiles, not fair");
    }
}

#[test]
fn more_slots_than_the_budget_has_tiles_for_get_one_each_in_order() {
    let out = run(0, &[(3, 3)], &slots(5), &[], 3 * MIB);
    let got: Vec<u64> = out.wanted.iter().map(|k| k.0).collect();
    assert_eq!(got, vec![1, 2, 3], "the last tile group fits only in part, in slot order");
    assert!(out.note.is_some());
}
