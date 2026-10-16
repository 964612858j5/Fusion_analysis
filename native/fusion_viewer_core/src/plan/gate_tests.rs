//! R7c step 3: the first-appearance gate (hybrid) and its exact coverage.
use super::super::levels::Order;
use super::super::*;
use super::covered;
use crate::admit::Budgets;
use crate::geom::Pyramid;

const R: [f64; 4] = [0.0, 100.0, 0.0, 100.0];

#[test]
fn one_rect_over_the_region_covers_it() {
    assert!(covered(R, &[[-10.0, 110.0, -10.0, 110.0]]));
}

#[test]
fn abutting_rects_cover_a_gap_does_not() {
    assert!(covered(R, &[[0.0, 50.0, 0.0, 100.0], [50.0, 100.0, 0.0, 100.0]]));
    assert!(!covered(R, &[[0.0, 49.5, 0.0, 100.0], [50.0, 100.0, 0.0, 100.0]]), "a narrow slit");
}

#[test]
fn a_missing_corner_is_found() {
    let rects = [[0.0, 100.0, 0.0, 50.0], [0.0, 50.0, 50.0, 100.0]];
    assert!(!covered(R, &rects));
    assert!(covered(R, &[rects[0], rects[1], [50.0, 100.0, 50.0, 100.0]]));
}

#[test]
fn rects_touching_only_at_a_corner_leave_the_rest_open() {
    assert!(!covered(R, &[[0.0, 50.0, 0.0, 50.0], [50.0, 100.0, 50.0, 100.0]]));
}

#[test]
fn an_empty_region_is_never_covered() {
    assert!(!covered([10.0, 10.0, 0.0, 100.0], &[[-1e9, 1e9, -1e9, 1e9]]));
    assert!(!covered(R, &[]));
}

fn hybrid() -> Planner {
    let pyr = Pyramid { shapes: vec![(4096, 4096), (2048, 2048), (1024, 1024)] };
    let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: 48 << 20, total: Some(1 << 30),
                      max_coarse_bytes: None };
    let mut p = Planner::new(pyr, b, 1024);
    p.order = Order::Hybrid;
    p.set_active(vec![0, 1]);
    p
}

#[test]
fn hybrid_opens_once_covered_stays_open_and_resets_when_turned_off() {
    let mut p = hybrid();
    assert!(!p.shown(0, R, &|| vec![[0.0, 50.0, 0.0, 100.0]]));
    assert!(p.shown(0, R, &|| vec![[0.0, 100.0, 0.0, 100.0]]));
    assert!(p.shown(0, R, &Vec::new), "stays open across moves");
    assert!(!p.shown(1, R, &Vec::new), "per channel");
    p.set_active(vec![1]);
    p.set_active(vec![0, 1]);
    assert!(!p.shown(0, R, &Vec::new), "turned off and on: closed again");
}

#[test]
fn cur_and_odon_are_never_held_back() {
    for order in [Order::Cur, Order::Odon] {
        let mut p = hybrid();
        p.order = order;
        assert!(p.shown(0, R, &Vec::new));
    }
}
