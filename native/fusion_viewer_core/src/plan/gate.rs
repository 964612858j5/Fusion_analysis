//! R7c step 3 (docs/v16_5_r7c3_order_plan.md §2.4): when a channel may first
//! be drawn. `cur`: once its whole coarsest level has landed (`drawn`
//! returns nothing before). `odon`: at once. `hybrid`: the first time the
//! view's region is wholly covered by tiles of the channel on the GPU (any
//! level); then it stays open until the channel is turned off.
//!
//! Coverage is geometric and exact: the region and every tile rect cut the
//! region into cells along all their edges; each cell lies wholly inside
//! or wholly outside each tile, so testing its centre decides it.

use super::levels::Order;
use super::{Ch, Planner};

/// Is `region` (x0, x1, y0, y1) wholly covered by the union of `rects`?
/// An empty region is not.
pub fn covered(region: [f64; 4], rects: &[[f64; 4]]) -> bool {
    let [x0, x1, y0, y1] = region;
    if x1 <= x0 || y1 <= y0 {
        return false;
    }
    let inside = |r: &[f64; 4]| r[1] > x0 && r[0] < x1 && r[3] > y0 && r[2] < y1;
    let rects: Vec<[f64; 4]> = rects.iter().filter(|r| inside(r)).copied().collect();
    let cuts = |lo: f64, hi: f64, a: usize, b: usize| {
        let mut v: Vec<f64> = vec![lo, hi];
        v.extend(rects.iter().flat_map(|r| [r[a], r[b]]).filter(|&e| e > lo && e < hi));
        v.sort_by(f64::total_cmp);
        v.dedup();
        v
    };
    let (xs, ys) = (cuts(x0, x1, 0, 1), cuts(y0, y1, 2, 3));
    for wy in ys.windows(2) {
        let cy = (wy[0] + wy[1]) / 2.0;
        for wx in xs.windows(2) {
            let cx = (wx[0] + wx[1]) / 2.0;
            if !rects.iter().any(|r| r[0] <= cx && cx < r[1] && r[2] <= cy && cy < r[3]) {
                return false;
            }
        }
    }
    true
}

impl Planner {
    /// May `c` be drawn? `region`: what of the slide is on screen (view
    /// within the slide and the ROI's box); `rects`: the valid world rects
    /// of `c`'s candidate tiles now on the GPU.
    pub fn shown(&mut self, c: Ch, region: [f64; 4], rects: &dyn Fn() -> Vec<[f64; 4]>) -> bool {
        match self.order {
            Order::Cur | Order::Odon => true,
            Order::Hybrid => {
                if self.opened.contains(&c) {
                    return true;
                }
                let open = covered(region, &rects());
                if open {
                    self.opened.insert(c);
                }
                open
            }
        }
    }
}

#[cfg(test)]
#[path = "gate_tests.rs"]
mod tests;
