//! The planner's rules, one test each (coarse atomic, late tiles, stand-ins).
use super::*;

// 4096 x 4096, three levels: 8x8, 4x4, 2x2 tiles
fn planner() -> Planner {
    let pyr = Pyramid { shapes: vec![(4096, 4096), (2048, 2048), (1024, 1024)] };
    let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: 48 << 20,
                      total: Some(1 << 30), texel_bytes: 1 };
    Planner::new(pyr, b, 1024)
}

fn deliver_all(p: &mut Planner) {
    loop {
        let (coarse, fine) = p.wanted();
        if coarse.is_empty() && fine.is_empty() {
            break;
        }
        for (c, t) in coarse.into_iter().chain(fine) {
            p.deliver(c, t);
        }
    }
}

#[test]
fn coarse_is_drawn_only_when_complete() {
    let mut p = planner();
    p.set_active(vec![0]);
    p.set_view(View { level: 0, bbox_l0: [0, 0, 1024, 1024], epoch: 1 });
    let (coarse, _) = p.wanted();
    assert_eq!(coarse.len(), 4);
    for &(c, t) in &coarse[..3] {
        assert!(p.deliver(c, t));
    }
    assert!(p.drawn(0, &|_, _| true).is_empty());
    p.deliver(coarse[3].0, coarse[3].1);
    assert_eq!(p.drawn(0, &|_, _| true).len(), 4);
}

#[test]
fn a_late_tile_of_an_old_view_is_not_taken() {
    let mut p = planner();
    p.set_active(vec![0]);
    p.set_view(View { level: 0, bbox_l0: [0, 0, 1024, 1024], epoch: 1 });
    deliver_all(&mut p);
    p.set_view(View { level: 0, bbox_l0: [3072, 3072, 4096, 4096], epoch: 2 });
    assert!(!p.deliver(0, TileAt { level: 0, tx: 1, ty: 1 }) || p.keeps(0, TileAt { level: 0, tx: 1, ty: 1 }));
    assert!(!p.deliver(0, TileAt { level: 0, tx: 0, ty: 7 }));
}

#[test]
fn finer_stand_in_is_dropped_once_the_target_is_on_the_gpu() {
    let mut p = planner();
    p.set_active(vec![0]);
    // level 0 view, all delivered
    p.set_view(View { level: 0, bbox_l0: [0, 0, 1024, 1024], epoch: 1 });
    deliver_all(&mut p);
    // zoom out to level 1 over the same place: level-0 tiles are stand-ins
    p.set_view(View { level: 1, bbox_l0: [0, 0, 1024, 1024], epoch: 2 });
    let fine_l1 = TileAt { level: 1, tx: 0, ty: 0 };
    let l0 = |t: &TileAt| t.level == 0;
    // the target is not on the GPU yet: the level-0 stand-ins are drawn
    assert!(p.drawn(0, &|_, _| false).iter().any(l0));
    assert!(p.deliver(0, fine_l1));
    // once it is, they are not
    assert!(!p.drawn(0, &|_, t| t == fine_l1).iter().any(l0));
    // still kept (memory is the admission's business)
    assert!(p.keeps(0, TileAt { level: 0, tx: 0, ty: 0 }));
}

#[test]
fn stand_ins_share_the_carry_nearest_level_first() {
    let pyr = Pyramid { shapes: vec![(4096, 4096), (2048, 2048), (1024, 1024)] };
    // carry room for exactly one 512x512 tile beyond the target
    let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: (2 * 512 * 512) as u64,
                      total: None, texel_bytes: 1 };
    let mut p = Planner::new(pyr, b, 1024);
    p.set_active(vec![0]);
    p.set_view(View { level: 0, bbox_l0: [0, 0, 1024, 512], epoch: 1 });   // two level-0 tiles
    deliver_all(&mut p);
    p.set_view(View { level: 1, bbox_l0: [0, 0, 1024, 1024], epoch: 2 });  // one level-1 tile
    let kept: Vec<_> = (0..2).filter(|ty| p.keeps(0, TileAt { level: 0, tx: 0, ty: *ty })).collect();
    assert_eq!(kept, vec![0], "one stand-in, the first in (level, ty, tx) order");
}
