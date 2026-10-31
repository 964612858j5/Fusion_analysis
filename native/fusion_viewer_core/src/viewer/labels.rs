//! Step3's masks planned and read by the core (R3b plan §2): the binding's
//! state (`Step3LabelBinding`) on the GUI thread -- the slots (one per
//! drawn mask, docs/v17_step3_multi_results_plan.md §3.3) with their
//! styles, what is held (in arrival order), what is wanted -- planned at
//! every camera change (`label_plan`), read by the label thread
//! (`label_pool`), taken without waiting by the paint and handed to the
//! drawing in `_push`'s order. Held tiles ARE the drawing's copies
//! (`label_store`): released, they go at once. A new slot list keeps the
//! tiles of every slot still in it (by stamp): adding or removing one
//! result does not blank the others.

use super::ViewerCore;
use crate::label_plan::{plan, tile_bytes, LKey, PlanIn, SlotPlan};
use crate::label_pool::{LabelPool, Sources};
use crate::label_read::Read;
use crate::label_source::Slot;
use crate::label_store::{Layer, Plane};
use std::collections::{BTreeMap, HashMap, HashSet};

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
    /// dashed outline (the trial of plan §4)
    pub dash: bool,
}

impl Default for Style {
    fn default() -> Self {
        Style { visible: true, color: [0.0, 1.0, 0.0], alpha: 0.75, radius: 1, fill: false, dash: false }
    }
}

#[derive(Default)]
pub struct LabelState {
    pub pool: Option<LabelPool>,
    /// slot lists handed over so far (stats)
    pub generation: u64,
    /// the slots in drawing order (None: none)
    pub sources: Option<Sources>,
    /// per stamp
    pub styles: HashMap<u64, Style>,
    /// held: key, world rect, the drawing copy's id -- in arrival order
    pub resident: Vec<(LKey, [f64; 4], u64)>,
    pub wanted: HashSet<LKey>,
    pub target: Option<u8>,
    pub note: Option<String>,
    /// per stamp: the last read error of that slot (gone with the slot)
    pub errors: BTreeMap<u64, String>,
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
    fn has_slot(&self, stamp: u64) -> bool {
        self.lab.sources.as_ref().is_some_and(|s| s.iter().any(|slot| slot.stamp == stamp))
    }

    fn release_label_tile(&mut self, key: LKey, id: u64) {
        self.labels.remove(id);
        let (rows, cols) = self.pyr.tile_shape(crate::geom::TileAt { level: key.1, tx: key.2, ty: key.3 });
        if let Some(g) = self.gpu.as_mut() {
            g.render.release_label(id, cols, rows);   // its texture is reused
        }
    }

    /// New slots, in drawing order, each with its style (empty: none). The
    /// tiles, jobs and errors of a stamp still listed stay; the others go.
    pub fn set_label_slots(&mut self, slots: Vec<(Slot, Style)>) {
        if self.lab.pool.is_none() {
            self.lab.pool = Some(LabelPool::start(self.pyr.clone(), LABEL_BUDGET, self.notifier.clone(),
                                                  std::sync::Arc::clone(&self.faults)));
        }
        self.lab.generation += 1;
        let stamps: HashSet<u64> = slots.iter().map(|(slot, _)| slot.stamp).collect();
        let (stay, gone): (Vec<_>, Vec<_>) = std::mem::take(&mut self.lab.resident).into_iter()
            .partition(|r| stamps.contains(&r.0 .0));
        self.lab.resident = stay;
        for (key, _, id) in gone {
            self.release_label_tile(key, id);
        }
        self.lab.wanted.retain(|k| stamps.contains(&k.0));
        self.lab.errors.retain(|stamp, _| stamps.contains(stamp));
        self.lab.note = None;
        self.lab.styles = slots.iter().map(|(slot, style)| (slot.stamp, *style)).collect();
        let list: Vec<Slot> = slots.into_iter().map(|(slot, _)| slot).collect();
        self.lab.sources = (!list.is_empty()).then(|| std::sync::Arc::new(list));
        self.lab.read_bound_seen = self.lab.read_bound_seen.max(self.sources_read_bound());
        let held = self.held_label_bytes();
        let pool = self.lab.pool.as_ref().unwrap();
        pool.set_sources(self.lab.sources.clone(), held);
        self.plan_labels();
        self.push_labels();
    }

    /// One slot's style; showing a hidden slot plans again (it may need
    /// tiles). False: no such slot.
    pub fn set_label_slot_style(&mut self, stamp: u64, style: Style) -> bool {
        let Some(before) = self.lab.styles.get(&stamp).copied() else { return false };
        self.lab.styles.insert(stamp, style);
        if style.visible && !before.visible {
            self.plan_labels();
        }
        self.push_labels();
        true
    }

    /// `_plan` for the camera now; jobs to the label thread.
    pub fn plan_labels(&mut self) {
        let (Some(pool), Some(view)) = (self.lab.pool.as_ref(), self.planner.view()) else { return };
        if self.paused || self.lab.sources.is_none() {
            return;
        }
        let started = std::time::Instant::now();
        let visible = self.pyr.cover(view.bbox_l0, view.level);
        let slots: Vec<SlotPlan> = self.lab.sources.as_ref().map_or(Vec::new(), |s| s.iter().map(|slot| SlotPlan {
            stamp: slot.stamp,
            shown: self.lab.styles.get(&slot.stamp).is_some_and(|st| st.visible),
            pyramid: slot.source.has_pyramid(),
        }).collect());
        let resident: Vec<(LKey, [f64; 4])> = self.lab.resident.iter().map(|(k, r, _)| (*k, *r)).collect();
        let Some(out) = plan(&PlanIn { pyr: &self.pyr, level: view.level, visible: &visible, slots: &slots,
                                       resident: &resident, budget: LABEL_BUDGET }) else { return };
        let kept: HashSet<LKey> = out.keep.iter().copied().collect();
        let gone: Vec<(LKey, u64)> = self.lab.resident.iter().filter(|r| !kept.contains(&r.0)).map(|r| (r.0, r.2)).collect();
        self.lab.resident.retain(|r| kept.contains(&r.0));
        for (key, id) in gone {
            self.labels.remove(id);
            let (rows, cols) = self.pyr.tile_shape(crate::geom::TileAt { level: key.1, tx: key.2, ty: key.3 });
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
            if !self.has_slot(key.0) {                   // a slot that left: errors too
                self.lab.counts[3] += 1;
                continue;
            }
            let tile = match result {
                Err(e) => {
                    self.lab.errors.insert(key.0, format!("label tile ({}, {}, {}) could not be read: {e}",
                                                          key.1, key.2, key.3));
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

    /// `_push`: per shown slot, in drawing order, its held tiles -- other
    /// levels first, farthest level first, the target last; by row, column.
    pub fn push_labels(&mut self) {
        if self.lab.pool.is_none() {
            return;
        }
        let target = self.lab.target;
        let t = target.unwrap_or(0) as i64;
        let mut layers = Vec::new();
        for slot in self.lab.sources.iter().flat_map(|s| s.iter()) {
            let Some(st) = self.lab.styles.get(&slot.stamp).copied().filter(|st| st.visible) else { continue };
            let mut mine: Vec<&(LKey, [f64; 4], u64)> = self.lab.resident.iter().filter(|r| r.0 .0 == slot.stamp).collect();
            mine.sort_by_key(|r| (Some(r.0 .1) == target, -(r.0 .1 as i64 - t).abs(), r.0 .3, r.0 .2));
            layers.push(Layer { planes: mine.iter().map(|r| (r.2, r.1)).collect(), color: st.color,
                                alpha: st.alpha, radius: st.radius, fill: st.fill, dash: st.dash });
        }
        self.labels.set(layers);
    }

    pub fn close_labels(&mut self) {
        if let Some(pool) = self.lab.pool.as_ref() {
            pool.close();
        }
    }

    fn sources_read_bound(&self) -> usize {
        self.lab.sources.as_ref().map_or(0, |s| s.iter()
            .map(|slot| slot.source.read_bytes_bound(crate::geom::TILE)).max().unwrap_or(0))
    }

    /// Temporary bytes the label reader may hold (the memory bound): the
    /// most any sources handed to this viewer needed -- it never shrinks,
    /// so what an old source's read or scratch still holds stays counted.
    pub fn label_read_bound(&self) -> usize {
        self.lab.read_bound_seen
    }
}
