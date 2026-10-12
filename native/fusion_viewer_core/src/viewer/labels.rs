//! Step3's masks planned and read by the core (R3b plan §2): the binding's
//! state (`Step3LabelBinding`) on the GUI thread -- sources and their
//! generation, styles, what is held (in arrival order), what is wanted --
//! planned at every camera change (`label_plan`), read by the label thread
//! (`label_pool`), taken without waiting by the paint and handed to the
//! drawing in `_push`'s order. Held tiles ARE the drawing's copies
//! (`label_store`): released, they go at once.

use super::ViewerCore;
use crate::label_plan::{plan, tile_bytes, LKey, PlanIn};
use crate::label_pool::{LabelPool, Sources};
use crate::label_read::Read;
use crate::label_source::{KindSource, KINDS};
use crate::label_store::{Layer, Plane};
use std::collections::HashSet;

pub const LABEL_BUDGET: usize = crate::label_store::LABEL_BUDGET;
/// The core numbers its own planes from here: never one of the numbers the
/// Python side gives (`CoreLabelSurface._label_id`, L1 counts from 1), so a
/// plane switched between the two never meets the other's copy or texture.
const CORE_ID_BASE: u64 = 1 << 48;

#[derive(Clone, Copy, Debug)]
pub struct Style {
    pub visible: bool,
    pub color: [f32; 3],
    pub alpha: f32,
    pub radius: i32,
    pub fill: bool,
}

impl Default for Style {
    fn default() -> Self {
        Style { visible: true, color: [0.0, 1.0, 0.0], alpha: 0.75, radius: 1, fill: false }
    }
}

#[derive(Default)]
pub struct LabelState {
    pub pool: Option<LabelPool>,
    pub generation: u64,
    pub sources: Option<Sources>,
    pub styles: [Style; 2],
    /// held: key, world rect, the drawing copy's id -- in arrival order
    pub resident: Vec<(LKey, [f64; 4], u64)>,
    pub wanted: HashSet<LKey>,
    pub target: Option<u8>,
    pub note: Option<String>,
    pub last_error: Option<String>,
    /// requested, read, accepted, late_dropped, pushes
    pub counts: [u64; 5],
    /// GUI-thread time spent on the masks since the last paint report
    /// (planning, taking results, uploading and composing them), ms
    pub gui_ms: f64,
    /// the same, split and summed for good: planning, taking, composing (ms)
    pub split_ms: [f64; 3],
    /// the largest read allowance of any sources handed over: the reader
    /// thread keeps Blosc's scratch, and a read in flight its old source,
    /// past a change of sources (codex R3b round 3)
    pub read_bound_seen: usize,
    next_id: u64,
}

impl ViewerCore {
    fn has_source(&self, kind: usize) -> bool {
        self.lab.sources.as_ref().is_some_and(|s| s[kind].is_some())
    }

    /// New masks (None for none): a new generation, the old one's tiles go.
    pub fn set_label_sources(&mut self, sources: Option<[Option<KindSource>; 2]>) {
        if self.lab.pool.is_none() {
            self.lab.pool = Some(LabelPool::start(self.pyr.clone(), LABEL_BUDGET, self.notifier.clone(),
                                                  std::sync::Arc::clone(&self.faults)));
        }
        self.lab.generation += 1;
        for (key, _, id) in std::mem::take(&mut self.lab.resident) {
            self.labels.remove(id);
            let (rows, cols) = self.pyr.tile_shape(crate::geom::TileAt { level: key.2, tx: key.3, ty: key.4 });
            if let Some(g) = self.gpu.as_mut() {
                g.render.release_label(id, cols, rows);
            }
        }
        self.lab.wanted.clear();
        self.lab.note = None;
        self.lab.last_error = None;
        self.lab.sources = sources.map(std::sync::Arc::new);
        self.lab.read_bound_seen = self.lab.read_bound_seen.max(self.sources_read_bound());
        let pool = self.lab.pool.as_ref().unwrap();
        pool.set_sources(self.lab.generation, self.lab.sources.clone());
        self.plan_labels();
        self.push_labels();
    }

    /// One mask's style; showing a hidden mask plans again (it may need tiles).
    pub fn set_label_style(&mut self, kind: usize, style: Style) {
        let shown_now = style.visible && !self.lab.styles[kind].visible;
        self.lab.styles[kind] = style;
        if shown_now {
            self.plan_labels();
        }
        self.push_labels();
    }

    /// `_plan` for the camera now; jobs to the label thread.
    pub fn plan_labels(&mut self) {
        let (Some(pool), Some(view)) = (self.lab.pool.as_ref(), self.planner.view()) else { return };
        if self.paused || self.lab.sources.is_none() {
            return;
        }
        let started = std::time::Instant::now();
        let visible = self.pyr.cover(view.bbox_l0, view.level);
        let shown = KINDS.map(|k| self.has_source(k) && self.lab.styles[k].visible);
        let pyramid = KINDS.map(|k| self.lab.sources.as_ref().and_then(|s| s[k].as_ref()).is_some_and(|s| s.has_pyramid()));
        let resident: Vec<(LKey, [f64; 4])> = self.lab.resident.iter().map(|(k, r, _)| (*k, *r)).collect();
        let Some(out) = plan(&PlanIn { pyr: &self.pyr, level: view.level, visible: &visible,
                                       generation: self.lab.generation, shown, pyramid, resident: &resident,
                                       budget: LABEL_BUDGET }) else { return };
        let kept: HashSet<LKey> = out.keep.iter().copied().collect();
        let gone: Vec<(LKey, u64)> = self.lab.resident.iter().filter(|r| !kept.contains(&r.0)).map(|r| (r.0, r.2)).collect();
        self.lab.resident.retain(|r| kept.contains(&r.0));
        for (key, id) in gone {
            self.labels.remove(id);
            let (rows, cols) = self.pyr.tile_shape(crate::geom::TileAt { level: key.2, tx: key.3, ty: key.4 });
            if let Some(g) = self.gpu.as_mut() {
                g.render.release_label(id, cols, rows);   // its texture is reused
            }
        }
        self.lab.target = Some(view.level);
        self.lab.note = out.note.clone();
        self.lab.wanted = out.wanted.iter().copied().collect();
        let reading = pool.reading();
        let jobs: Vec<(LKey, usize)> = crate::label_plan::jobs(&out, &reading).into_iter()
            .map(|k| (k, tile_bytes(&self.pyr, &k))).collect();
        let held = self.held_label_bytes();
        let (new, _dropped) = pool.set_jobs(jobs, &self.lab.wanted, held);
        self.lab.counts[0] += new as u64;
        self.push_labels();
        let ms = started.elapsed().as_secs_f64() * 1000.0;
        self.lab.gui_ms += ms;
        self.lab.split_ms[0] += ms;
    }

    fn held_label_bytes(&self) -> usize {
        self.lab.resident.iter().map(|(k, _, _)| tile_bytes(&self.pyr, k)).sum()
    }

    /// Results in hand (paint; never waits): `_accept` for each.
    pub fn drain_labels(&mut self) {
        let Some(pool) = self.lab.pool.as_ref() else { return };
        let started = std::time::Instant::now();
        let results = pool.take();
        if results.is_empty() {
            return;
        }
        let mut changed = false;
        for (key, result) in results {
            self.lab.counts[1] += 1;
            if key.0 != self.lab.generation {             // the generation first, errors too
                self.lab.counts[3] += 1;
                continue;
            }
            let tile = match result {
                Err(e) => {
                    self.lab.last_error = Some(format!("label tile ({}, {}, {}) could not be read: {e}",
                                                      key.2, key.3, key.4));
                    continue;
                }
                Ok(_) if !self.lab.wanted.contains(&key) || self.lab.resident.iter().any(|r| r.0 == key) => {
                    self.lab.counts[3] += 1;              // no longer wanted, or already held
                    continue;
                }
                Ok(Read::Unavailable(_)) => continue,
                Ok(Read::Tile(t)) => t,
            };
            self.lab.next_id += 1;
            let id = CORE_ID_BASE + self.lab.next_id;
            let keep: HashSet<u64> = self.lab.resident.iter().map(|r| r.2).collect();
            self.labels.insert(id, Plane { rows: tile.rows, cols: tile.cols, ids: tile.ids }, &keep);
            self.lab.resident.push((key, tile.rect, id));
            self.lab.counts[2] += 1;
            changed = true;
        }
        let held = self.held_label_bytes();
        pool.set_held(held);
        if changed {
            self.push_labels();
            self.lab.counts[4] += 1;                     // `pushes`: new tiles handed to the drawing
        }
        let ms = started.elapsed().as_secs_f64() * 1000.0;
        self.lab.gui_ms += ms;
        self.lab.split_ms[1] += ms;
    }

    /// `_push`: per kind with a source, its held tiles -- other levels
    /// first, farthest level first, the target last; by row, column.
    pub fn push_labels(&mut self) {
        if self.lab.pool.is_none() {
            return;
        }
        let target = self.lab.target;
        let mut layers = Vec::new();
        for kind in KINDS {
            if !self.has_source(kind) || !self.lab.styles[kind].visible {
                continue;
            }
            let t = target.unwrap_or(0) as i64;
            let mut mine: Vec<&(LKey, [f64; 4], u64)> = self.lab.resident.iter().filter(|r| r.0 .1 == kind).collect();
            mine.sort_by_key(|r| (Some(r.0 .2) == target, -(r.0 .2 as i64 - t).abs(), r.0 .4, r.0 .3));
            let st = self.lab.styles[kind];
            layers.push(Layer { planes: mine.iter().map(|r| (r.2, r.1)).collect(), color: st.color,
                                alpha: st.alpha, radius: st.radius, fill: st.fill });
        }
        self.labels.set(layers);
    }

    pub fn close_labels(&mut self) {
        if let Some(pool) = self.lab.pool.as_ref() {
            pool.close();
        }
    }

    fn sources_read_bound(&self) -> usize {
        self.lab.sources.as_ref().map_or(0, |s| s.iter().flatten()
            .map(|k| k.read_bytes_bound(crate::geom::TILE)).max().unwrap_or(0))
    }

    /// Temporary bytes the label reader may hold (the memory bound): the
    /// most any sources handed to this viewer needed -- it never shrinks,
    /// so what an old source's read or scratch still holds stays counted.
    pub fn label_read_bound(&self) -> usize {
        self.lab.read_bound_seen
    }
}
