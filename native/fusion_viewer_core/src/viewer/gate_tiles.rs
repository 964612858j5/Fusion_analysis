//! §15.5 / 15.6 (docs/v17_step0_engine_plan.md): the tiles of one channel a
//! Step0 switch needs before its quality gate can open -- the coarsest
//! layer not delivered yet (the planner draws nothing of a channel before
//! its whole coarse has landed, `plan/draw.rs`), and the tiles of the gate
//! level that meet the view. The Python adapter asks for these first; the
//! rest of its demands wait until the new picture is published.

use super::ViewerCore;
use crate::geom::TileAt;
use crate::plan::Ch;

impl ViewerCore {
    /// `view`: the world rect on screen (x0, x1, y0, y1); `level`: the gate
    /// level. Sorted, no duplicates.
    pub fn gate_tiles(&self, c: Ch, view: [f64; 4], level: u8) -> Vec<TileAt> {
        let coarsest = self.pyr.coarsest();
        let mut out: Vec<TileAt> = Vec::new();
        if !self.planner.coarse_complete(c) {
            let (across, down) = self.pyr.grid(coarsest);
            out.extend((0..down).flat_map(|ty| (0..across).map(move |tx| TileAt { level: coarsest, tx, ty }))
                .filter(|t| !self.planner.coarse_has(c, *t)));
        }
        out.extend(meeting(&self.pyr, view, level.min(coarsest)));
        out.sort();
        out.dedup();
        out
    }
}

/// The tiles of `level` whose world rect meets `view` (an edge alone does not).
fn meeting(pyr: &crate::geom::Pyramid, view: [f64; 4], level: u8) -> Vec<TileAt> {
    let (across, down) = pyr.grid(level);
    (0..down).flat_map(|ty| (0..across).map(move |tx| TileAt { level, tx, ty }))
        .filter(|t| pyr.tile_world_rect(*t).is_some_and(|r| {
            r[0] < view[1] && view[0] < r[1] && r[2] < view[3] && view[2] < r[3]
        }))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::meeting;
    use crate::geom::{Pyramid, TileAt};

    #[test]
    fn only_the_tiles_of_the_level_that_meet_the_view() {
        let pyr = Pyramid { shapes: vec![(2048, 2048), (1024, 1024)] };
        let got = meeting(&pyr, [0.0, 600.0, 0.0, 100.0], 0);
        assert_eq!(got, vec![TileAt { level: 0, tx: 0, ty: 0 }, TileAt { level: 0, tx: 1, ty: 0 }]);
        let got = meeting(&pyr, [0.0, 600.0, 0.0, 100.0], 1);
        assert_eq!(got, vec![TileAt { level: 1, tx: 0, ty: 0 }], "a level-1 tile spans 1024 world px");
        assert!(meeting(&pyr, [512.0, 512.0, 0.0, 10.0], 0).is_empty(), "an empty view meets nothing");
    }
}
