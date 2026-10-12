//! What Step3's masks need now (`Step3LabelBinding._plan`, R3b plan §2.2),
//! step for step: the camera level's visible tiles and one ring around
//! them, nearest the visible centre first (ties: row, then column -- the
//! Python binding sorts the same way since R3b), visible-cell, visible-
//! nucleus, ring-cell, ring-nucleus; other levels' tiles stay while they
//! lie under the view; over the budget the farthest old levels go first,
//! then the ring, then the visible tiles farthest from the centre, with a
//! note. Pure: the reader (`label_pool`) and the planner's state live
//! elsewhere; a test drives this and Python's `_plan` with the same events.

use crate::geom::{Pyramid, TILE};
use crate::label_source::KINDS;

/// (generation, kind, level, tx, ty)
pub type LKey = (u64, usize, u8, u32, u32);

pub struct PlanIn<'a> {
    pub pyr: &'a Pyramid,
    /// the camera's level and the tiles it shows there (`cover`)
    pub level: u8,
    pub visible: &'a [(u32, u32)],
    pub generation: u64,
    /// per kind: it has a source and is shown
    pub shown: [bool; 2],
    /// per kind: its pyramid is there (coarse levels can be read)
    pub pyramid: [bool; 2],
    /// what is held now, in the order it arrived, with its world rect
    pub resident: &'a [(LKey, [f64; 4])],
    pub budget: usize,
}

#[derive(Debug, Default, PartialEq)]
pub struct PlanOut {
    /// wanted, in reading order
    pub wanted: Vec<LKey>,
    /// what stays held, in arrival order
    pub keep: Vec<LKey>,
    pub released: Vec<LKey>,
    pub note: Option<String>,
}

pub fn tile_bytes(pyr: &Pyramid, key: &LKey) -> usize {
    let (rows, cols) = pyr.tile_shape(crate::geom::TileAt { level: key.2, tx: key.3, ty: key.4 });
    rows * cols * 4
}

/// The reader's new job list: the wanted tiles, in order, neither held
/// (`keep`) nor being read (`_plan`'s queue).
pub fn jobs(out: &PlanOut, reading: &std::collections::HashSet<LKey>) -> Vec<LKey> {
    let kept: std::collections::HashSet<&LKey> = out.keep.iter().collect();
    out.wanted.iter().filter(|k| !kept.contains(k) && !reading.contains(k)).copied().collect()
}

fn overlaps(a: &[f64; 4], b: &[f64; 4]) -> bool {
    a[0] < b[1] && b[0] < a[1] && a[2] < b[3] && b[2] < a[3]
}

/// None: the level is not one of the viewer's (nothing changes).
pub fn plan(p: &PlanIn) -> Option<PlanOut> {
    let shapes = &p.pyr.shapes;
    if p.level as usize >= shapes.len() {
        return None;
    }
    let (h, w) = shapes[p.level as usize];
    let (nx, ny) = (w.div_ceil(TILE) as u32, h.div_ceil(TILE) as u32);
    let mut visible: Vec<(u32, u32)> = p.visible.iter().copied().filter(|t| t.0 < nx && t.1 < ny).collect();
    visible.sort_unstable();
    visible.dedup();
    let vis_set: std::collections::HashSet<(u32, u32)> = visible.iter().copied().collect();
    let mut ring = std::collections::BTreeSet::new();
    for &(tx, ty) in &visible {
        for dx in -1i64..=1 {
            for dy in -1i64..=1 {
                let q = (tx as i64 + dx, ty as i64 + dy);
                if q.0 >= 0 && q.1 >= 0 && (q.0 as u32) < nx && (q.1 as u32) < ny
                    && !vis_set.contains(&(q.0 as u32, q.1 as u32)) {
                    ring.insert((q.0 as u32, q.1 as u32));
                }
            }
        }
    }
    let (cx, cy, view) = if visible.is_empty() {
        (0.0, 0.0, None)
    } else {
        let (minx, maxx) = (visible.iter().map(|t| t.0).min().unwrap(), visible.iter().map(|t| t.0).max().unwrap());
        let (miny, maxy) = (visible.iter().map(|t| t.1).min().unwrap(), visible.iter().map(|t| t.1).max().unwrap());
        let rects: Vec<[f64; 4]> = visible.iter()
            .filter_map(|&(tx, ty)| p.pyr.tile_world_rect(crate::geom::TileAt { level: p.level, tx, ty })).collect();
        let fold = |f: fn(f64, f64) -> f64, i: usize, init: f64| rects.iter().map(|r| r[i]).fold(init, f);
        ((minx + maxx) as f64 / 2.0, (miny + maxy) as f64 / 2.0,
         Some([fold(f64::min, 0, f64::INFINITY), fold(f64::max, 1, f64::NEG_INFINITY),
               fold(f64::min, 2, f64::INFINITY), fold(f64::max, 3, f64::NEG_INFINITY)]))
    };
    let order = |t: &(u32, u32)| (t.0 as f64 - cx).powi(2) + (t.1 as f64 - cy).powi(2);
    let sorted = |set: Vec<(u32, u32)>| {
        let mut v = set;
        v.sort_by(|a, b| order(a).partial_cmp(&order(b)).unwrap().then(a.1.cmp(&b.1)).then(a.0.cmp(&b.0)));
        v
    };
    let (vis_sorted, ring_sorted) = (sorted(visible.clone()), sorted(ring.into_iter().collect()));
    let generation = p.generation;
    let (mut target_vis, mut target_ring) = (Vec::new(), Vec::new());
    for kind in KINDS {
        if !p.shown[kind] || (p.level > 0 && !p.pyramid[kind]) {
            continue;                         // not requested, not drawn (status says why)
        }
        target_vis.extend(vis_sorted.iter().map(|&(tx, ty)| (generation, kind, p.level, tx, ty)));
        target_ring.extend(ring_sorted.iter().map(|&(tx, ty)| (generation, kind, p.level, tx, ty)));
    }
    let target: std::collections::HashSet<LKey> = target_vis.iter().chain(&target_ring).copied().collect();
    // what stays: the target, and other levels' tiles still under the view
    let mut keep: Vec<LKey> = Vec::new();
    for (key, rect) in p.resident {
        let under = key.0 == generation && key.2 != p.level && p.shown[key.1]
            && view.as_ref().is_some_and(|v| overlaps(rect, v));
        if target.contains(key) || under {
            keep.push(*key);
        }
    }
    // the budget: old levels first, then the ring, then the far visible
    let size = |k: &LKey| tile_bytes(p.pyr, k);
    let mut old: Vec<LKey> = keep.iter().copied().filter(|k| k.2 != p.level).collect();
    old.sort_by_key(|k| (k.2 as i64 - p.level as i64).abs());   // stable, as Python's sorted
    let (mut wanted_vis, mut wanted_ring) = (target_vis, target_ring);
    let total = |keep: &Vec<LKey>, wv: &Vec<LKey>, wr: &Vec<LKey>| {
        let held: std::collections::HashSet<&LKey> = keep.iter().collect();
        keep.iter().map(size).sum::<usize>()
            + wv.iter().chain(wr).filter(|k| !held.contains(k)).map(size).sum::<usize>()
    };
    while total(&keep, &wanted_vis, &wanted_ring) > p.budget {
        let Some(gone) = old.pop() else { break };      // farthest level first
        keep.retain(|k| *k != gone);
    }
    if total(&keep, &wanted_vis, &wanted_ring) > p.budget {
        let ring: std::collections::HashSet<LKey> = wanted_ring.drain(..).collect();
        keep.retain(|k| !ring.contains(k));
    }
    let mut note = None;
    if total(&keep, &wanted_vis, &wanted_ring) > p.budget {
        let (mut fitted, mut used) = (0, 0);
        for k in &wanted_vis {
            if used + size(k) > p.budget {
                break;
            }
            fitted += 1;
            used += size(k);
        }
        let dropped: std::collections::HashSet<LKey> = wanted_vis[fitted..].iter().copied().collect();
        keep.retain(|k| !dropped.contains(k));
        let need: usize = wanted_vis.iter().map(size).sum();
        wanted_vis.truncate(fitted);
        note = Some(format!("mask memory is full: part of the view has no mask (needs {} MB, limit {} MB)",
                            need >> 20, p.budget >> 20));
    }
    let kept: std::collections::HashSet<LKey> = keep.iter().copied().collect();
    let released = p.resident.iter().map(|(k, _)| *k).filter(|k| !kept.contains(k)).collect();
    wanted_vis.extend(wanted_ring);
    Some(PlanOut { wanted: wanted_vis, keep, released, note })
}
