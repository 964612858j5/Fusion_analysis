//! S0-A (docs/v17_step0_engine_plan.md §10.5.2): is the view a paint just
//! drew wholly covered, for every channel the display asks for? A read-only
//! question the Step0 host asks before it publishes a new source's picture.
//!
//! Recomputed for the view given, never the hybrid gate's remembered answer
//! (`plan::gate`), and only from the tiles this frame's composition would
//! use (`Planner::drawn` among the GPU-resident ones): a tile the planner
//! has dropped but the GPU store still holds is not counted. Every display
//! channel is required -- one with no usable tile makes the answer false.
//! The tile edges are widened to the slide's edge as the gate does
//! (`shown::reach`); that is geometry, the shader still samples the real
//! valid rectangles.

use super::shown::reach;
use crate::geom::TileAt;
use crate::plan::gate::covered;
use crate::plan::{Ch, Planner};
use crate::viewer::ViewerCore;

/// `view`: the world rect drawn (x0, x1, y0, y1).
pub(crate) fn view_covered(core: &mut ViewerCore, view: [f64; 4]) -> bool {
    let mut required: Vec<Ch> = core.display.iter().map(|(c, _)| *c).collect();
    required.sort_unstable();
    required.dedup();
    let (h, w) = core.pyr.shapes[0];
    let outer = core.roi_world.map_or([0.0, w as f64, 0.0, h as f64],
                                      |r| [r[0].max(0.0), r[1].min(w as f64), r[2].max(0.0), r[3].min(h as f64)]);
    let region = [view[0].max(outer[0]), view[1].min(outer[1]), view[2].max(outer[2]), view[3].min(outer[3])];
    let slack = {
        let (ds_y, ds_x) = core.pyr.ds_yx(core.pyr.coarsest());
        (ds_x, ds_y)
    };
    let Some(gpu) = core.gpu.as_ref() else { return false };
    let store = &gpu.store;
    let on_gpu = |c, t| store.is_resident(&(c, t));
    let planner = &mut core.planner;
    all_covered(&required, region, |c| {
        usable(planner, c, &on_gpu).into_iter()
            .filter_map(|t| store.meta.get(&(c, t)).and_then(|m| m.valid))
            .map(|r| reach(r, outer, slack))
            .collect()
    })
}

/// The tiles of `c` this frame's composition would use: the planner's
/// choice, and of it only what is on the GPU.
fn usable(planner: &mut Planner, c: Ch, on_gpu: &dyn Fn(Ch, TileAt) -> bool) -> Vec<TileAt> {
    planner.drawn(c, on_gpu).into_iter().filter(|t| on_gpu(c, *t)).collect()
}

/// Every channel of `required` covers `region` with its `rects`; false for
/// no channel at all or an empty region.
fn all_covered(required: &[Ch], region: [f64; 4], mut rects: impl FnMut(Ch) -> Vec<[f64; 4]>) -> bool {
    !required.is_empty() && required.iter().all(|c| covered(region, &rects(*c)))
}

#[cfg(test)]
mod tests {
    use super::{all_covered, usable};
    use crate::geom::{Pyramid, TileAt};
    use crate::admit::Budgets;
    use crate::plan::{Planner, View};
    use std::collections::HashSet;

    /// codex S0-A code review 2: tiles of view A, dropped by the planner
    /// after a pan to B but still on the GPU, are not counted when the
    /// view comes back to A before they are delivered again.
    #[test]
    fn dropped_tiles_still_on_the_gpu_are_not_counted() {
        let pyr = Pyramid { shapes: vec![(4096, 4096), (2048, 2048), (1024, 1024)] };
        let b = Budgets { max_fine_tiles: 256, max_fine_bytes_per_channel: 48 << 20,
                          total: Some(1 << 30), max_coarse_bytes: None };
        let mut p = Planner::new(pyr, b, 1024);
        let mut resident: HashSet<TileAt> = HashSet::new();
        let deliver_all = |p: &mut Planner, resident: &mut HashSet<TileAt>| loop {
            let (coarse, fine) = p.wanted();
            if coarse.is_empty() && fine.is_empty() {
                break;
            }
            for (c, t) in coarse.into_iter().chain(fine) {
                p.deliver(c, t);
                resident.insert(t);
            }
        };
        p.set_active(vec![0]);
        let a = View { level: 0, bbox_l0: [0, 0, 1024, 1024], epoch: 1 };
        p.set_view(a);
        deliver_all(&mut p, &mut resident);
        let a_tile = TileAt { level: 0, tx: 0, ty: 0 };
        assert!(resident.contains(&a_tile));
        p.set_view(View { level: 0, bbox_l0: [3072, 3072, 4096, 4096], epoch: 2 });
        deliver_all(&mut p, &mut resident);
        p.set_view(View { epoch: 3, ..a });              // back to A, nothing delivered again
        let on_gpu = |_c, t| resident.contains(&t);
        let got = usable(&mut p, 0, &on_gpu);
        assert!(resident.contains(&a_tile), "still on the GPU");
        assert!(!got.iter().any(|t| t.level == 0 && t.tx < 2 && t.ty < 2),
                "a dropped level-0 tile of A was counted: {got:?}");
        assert!(got.iter().all(|t| on_gpu(0, *t)));
    }

    const VIEW: [f64; 4] = [0.0, 1024.0, 0.0, 1024.0];

    #[test]
    fn every_required_channel_must_cover() {
        let full = vec![[0.0, 1024.0, 0.0, 1024.0]];
        let half = vec![[0.0, 512.0, 0.0, 1024.0]];
        assert!(all_covered(&[0, 1], VIEW, |_| full.clone()));
        assert!(!all_covered(&[0, 1], VIEW, |c| if c == 0 { full.clone() } else { half.clone() }));
    }

    #[test]
    fn a_channel_with_no_drawable_tile_is_not_covered() {
        assert!(!all_covered(&[0, 1], VIEW, |c| if c == 0 { vec![[0.0, 1024.0, 0.0, 1024.0]] } else { vec![] }));
    }

    #[test]
    fn nothing_required_is_not_ready() {
        assert!(!all_covered(&[], VIEW, |_| vec![[0.0, 1024.0, 0.0, 1024.0]]));
    }

    #[test]
    fn tiles_of_several_levels_together_cover() {
        let rects = vec![[0.0, 512.0, 0.0, 1024.0], [512.0, 768.0, 0.0, 1024.0], [768.0, 1024.0, 0.0, 512.0],
                         [768.0, 1024.0, 512.0, 1024.0]];
        assert!(all_covered(&[3], VIEW, |_| rects.clone()));
        assert!(!all_covered(&[3], VIEW, |_| rects[..3].to_vec()));
    }

    #[test]
    fn an_empty_region_is_not_covered() {
        assert!(!all_covered(&[0], [10.0, 10.0, 0.0, 5.0], |_| vec![[0.0, 1024.0, 0.0, 1024.0]]));
    }
}
