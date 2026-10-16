//! What the planner owes the readers and what each channel draws
//! (`Step1GpuBinding._publish_body`, `_drawn_fine`).

use super::{Ch, Planner};
use crate::geom::TileAt;
use std::collections::HashSet;

impl Planner {
    /// Everything still owed, coarse first, then fine -- the target's and
    /// (R7c step 3) the helper levels' -- unordered within.
    pub fn wanted(&self) -> (Vec<(Ch, TileAt)>, Vec<(Ch, TileAt)>) {
        let coarsest = self.pyr.coarsest();
        let (across, down) = self.pyr.grid(coarsest);
        let mut coarse = Vec::new();
        for &c in &self.active {
            if let Some(k) = self.coarse.get(&c) {
                for ty in 0..down {
                    for tx in 0..across {
                        let t = TileAt { level: coarsest, tx, ty };
                        if !k.got.contains(&t) {
                            coarse.push((c, t));
                        }
                    }
                }
            }
        }
        let mut fine = Vec::new();
        for &c in &self.active {
            if let Some(p) = self.plans.get(&c) {
                fine.extend(p.expected.iter().chain(&p.aux).map(|&t| (c, t)));
            }
        }
        (coarse, fine)
    }

    /// The tiles of channel `c` worth drawing, coarse first then fine
    /// coarsest-level first (`_publish_body` + `_drawn_fine`): a finer
    /// stand-in is left out once every target cell it touches is on the
    /// GPU. Empty while the channel's coarse is incomplete -- except under
    /// R7c step 3's odon / hybrid, where the coarse tiles landed so far are
    /// candidates at once (whether the channel is shown is `gate`'s call).
    pub fn drawn(&mut self, c: Ch, on_gpu: &dyn Fn(Ch, TileAt) -> bool) -> Vec<TileAt> {
        let a = self.admission().cloned();
        let early = self.order != super::levels::Order::Cur
            && a.as_ref().is_some_and(|a| !a.coarse_only && !a.base_overflow);
        if !self.coarse_complete(c) && !early {
            return Vec::new();
        }
        let coarsest = self.pyr.coarsest();
        let (across, down) = self.pyr.grid(coarsest);
        let got = self.coarse.get(&c);
        let mut out: Vec<TileAt> = (0..down)
            .flat_map(|ty| (0..across).map(move |tx| TileAt { level: coarsest, tx, ty }))
            .filter(|t| got.is_some_and(|k| k.got.contains(t))).collect();
        let Some(fine) = self.fine.get(&c) else { return out };
        let mut tiles: Vec<TileAt> = fine.keys().copied().collect();
        tiles.sort_by_key(|t| (std::cmp::Reverse(t.level), t.ty, t.tx));
        if let Some(a) = a.filter(|a| !a.coarse_only) {
            let covered: HashSet<(u32, u32)> = tiles.iter()
                .filter(|t| t.level == a.level && on_gpu(c, **t)).map(|t| (t.tx, t.ty)).collect();
            if !covered.is_empty() {
                let (ds_y, ds_x) = self.pyr.ds_yx(a.level);
                let (cw, ch) = (crate::geom::TILE as f64 * ds_x, crate::geom::TILE as f64 * ds_y);
                tiles.retain(|t| {
                    if t.level >= a.level {
                        return true;
                    }
                    let r = self.pyr.tile_world_rect(*t).unwrap();
                    let (cx0, cx1) = ((r[0] / cw).floor() as i64, (r[1] / cw).ceil() as i64 - 1);
                    let (cy0, cy1) = ((r[2] / ch).floor() as i64, (r[3] / ch).ceil() as i64 - 1);
                    !(cx0..=cx1).all(|x| (cy0..=cy1).all(|y| {
                        x >= 0 && y >= 0 && covered.contains(&(x as u32, y as u32))
                    }))
                });
            }
        }
        out.extend(tiles);
        out
    }
}
