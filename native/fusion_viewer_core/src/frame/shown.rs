//! R7c step 3 (`plan::gate`): of a frame's candidate rows, those whose
//! channel may be drawn now (hybrid's first-appearance gate).

use crate::geom::TileAt;
use crate::plan::Ch;
use crate::viewer::ViewerCore;

/// A level's tiles end on its own pixel grid, which may stop short of the
/// slide's / ROI's edge by part of a pixel (codex review): a tile edge
/// within `slack` (one coarsest-level pixel) of that OUTER edge counts as
/// reaching it. The region itself is never shrunk, so it stays non-empty.
pub(crate) fn reach(mut r: [f64; 4], outer: [f64; 4], slack: (f64, f64)) -> [f64; 4] {
    let (sx, sy) = slack;
    if (r[0] - outer[0]).abs() <= sx { r[0] = r[0].min(outer[0]); }
    if (r[1] - outer[1]).abs() <= sx { r[1] = r[1].max(outer[1]); }
    if (r[2] - outer[2]).abs() <= sy { r[2] = r[2].min(outer[2]); }
    if (r[3] - outer[3]).abs() <= sy { r[3] = r[3].max(outer[3]); }
    r
}

/// `view`: the world rect on screen (x0, x1, y0, y1).
pub(crate) fn shown_rows(core: &mut ViewerCore, rows: Vec<(Ch, Vec<TileAt>)>, view: [f64; 4]) -> Vec<(Ch, Vec<TileAt>)> {
    // coarse only / over budget: cur's gate (the whole coarse level) holds
    let plain = core.planner.admission().is_none_or(|a| a.coarse_only || a.base_overflow);
    if core.planner.order != crate::plan::levels::Order::Hybrid || plain {
        return rows;
    }
    let Some(gpu) = core.gpu.as_ref() else { return rows };
    let (h, w) = core.pyr.shapes[0];
    let outer = core.roi_world.map_or([0.0, w as f64, 0.0, h as f64],
                                      |r| [r[0].max(0.0), r[1].min(w as f64), r[2].max(0.0), r[3].min(h as f64)]);
    let region = [view[0].max(outer[0]), view[1].min(outer[1]), view[2].max(outer[2]), view[3].min(outer[3])];
    let (ds_y, ds_x) = core.pyr.ds_yx(core.pyr.coarsest());
    let store = &gpu.store;
    let planner = &mut core.planner;
    rows.into_iter().filter(|(c, tiles)| {
        let rects = || tiles.iter().filter_map(|t| store.meta.get(&(*c, *t)).and_then(|m| m.valid))
            .map(|r| reach(r, outer, (ds_x, ds_y))).collect();
        planner.shown(*c, region, &rects)
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::reach;
    use crate::plan::gate::covered;

    const OUTER: [f64; 4] = [0.0, 4096.0, 0.0, 4096.0];

    #[test]
    fn a_tile_short_of_the_slide_edge_by_part_of_a_pixel_reaches_it() {
        let r = reach([2048.0, 4095.75, 0.0, 4096.0], OUTER, (8.0, 8.0));
        assert_eq!(r[1], 4096.0);
        assert!(covered([4092.0, 4096.0, 0.0, 100.0], &[r]), "a narrow strip at the edge opens");
    }

    #[test]
    fn inner_edges_are_not_stretched() {
        let r = reach([1000.0, 2000.0, 1000.0, 2000.0], OUTER, (8.0, 8.0));
        assert_eq!(r, [1000.0, 2000.0, 1000.0, 2000.0]);
        assert!(!covered([1000.0, 2004.0, 1000.0, 2000.0], &[r]));
    }
}
