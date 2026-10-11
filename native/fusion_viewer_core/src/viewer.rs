//! `ViewerCore`: one Step1 viewer's Rust side, for one raw source. It
//! wires the planner, the reader pool, the CPU cache and the GPU parts
//! together; each of those does its own job (see their modules).
//!
//! Threads: everything here runs on the GUI thread with the GIL held,
//! except the pool's readers. GL calls only happen inside `frame::paint`,
//! `attach_gl` / `release_gl`, with Qt's context current.

use crate::admit::Budgets;
use crate::cache::{Key, Tile};
use crate::clock::monotonic;
use crate::geom::{Pyramid, TileAt};
use crate::gpu_store::Store;
use crate::notify::Notifier;
use crate::plan::{Ch, Planner, View};
use crate::pool::Pool;
use crate::reader::RawSource;
use crate::render::Render;
use crate::trace::Trace;
use crate::vt::{RowParams, Vt};
use std::collections::HashMap;
use std::sync::Arc;

/// Tunables the Python side passes (budgets from `demo_budgets()` and the
/// resource tier; timings from the binding's constants).
#[derive(Clone, Debug)]
pub struct Config {
    pub max_raw_texture_bytes: usize,
    pub max_fine_tiles: usize,
    pub max_fine_bytes_per_channel: u64,
    pub max_coarse_tiles: usize,
    pub cpu_cache_bytes: usize,
    pub threads: usize,
    pub input_hot_s: f64,
    pub hot_upload_ms: f64,
    pub frame_budget_ms: f64,
    pub growth_quiet_s: f64,
    /// test only: request fine tiles channel by channel (the old order)
    pub legacy_order: bool,
}

pub struct Gpu {
    pub gl: glow::Context,
    pub store: Store,
    pub vt: Vt,
    pub render: Render,
}

pub struct ViewerCore {
    pub cfg: Config,
    pub pyr: Pyramid,
    /// the region `(y0, y1, x0, x1)` (level 0) and as a world rect
    pub roi_world: Option<[f64; 4]>,
    pub planner: Planner,
    pub pool: Pool,
    pub notifier: Option<Arc<Notifier>>,
    pub trace: Arc<Trace>,
    pub gpu: Option<Gpu>,
    pub display: Vec<(Ch, RowParams)>,
    pub smooth: bool,
    pub last_input: f64,
    pub paused: bool,
    /// tiles delivered to the planner and still held by it
    pub held: HashMap<Key, Arc<Tile>>,
    pub frames: u64,
    pub last_error: String,
    /// keys asked for and still owed: only a NEW want is a trace "req"
    issued: std::collections::HashSet<Key>,
}

impl ViewerCore {
    pub fn new(source: RawSource, cfg: Config, roi: Option<[i64; 4]>,
               notifier: Option<Arc<Notifier>>, trace: Arc<Trace>) -> ViewerCore {
        let shapes = source.manifest().levels.iter().map(|l| (l.height, l.width)).collect();
        let pyr = Pyramid { shapes };
        let budgets = Budgets { max_fine_tiles: cfg.max_fine_tiles,
                                max_fine_bytes_per_channel: cfg.max_fine_bytes_per_channel,
                                total: Some(cfg.max_raw_texture_bytes as u64), texel_bytes: 1 };
        let planner = Planner::new(pyr.clone(), budgets, cfg.max_coarse_tiles);
        let pool = Pool::start(Arc::new(source), pyr.clone(), roi, cfg.cpu_cache_bytes, cfg.threads,
                               notifier.clone(), Arc::clone(&trace));
        let roi_world = roi.map(|[y0, y1, x0, x1]| [x0 as f64, x1 as f64, y0 as f64, y1 as f64]);
        ViewerCore { cfg, pyr, roi_world, planner, pool, notifier, trace, gpu: None,
                     display: Vec::new(), smooth: true, last_input: -1e9, paused: false,
                     held: HashMap::new(), frames: 0, last_error: String::new(),
                     issued: Default::default() }
    }

    /// The drawn channels (sorted by name) and their display parameters.
    pub fn set_display(&mut self, rows: Vec<(Ch, RowParams)>, smooth: bool) {
        self.smooth = smooth;
        let active: Vec<Ch> = rows.iter().map(|r| r.0).collect();
        self.display = rows;
        if !self.paused {
            self.planner.set_active(active);
            self.issue();
        }
    }

    /// The controller's camera position; `input` true for a user gesture.
    pub fn set_view(&mut self, view: View, input: bool) {
        if input {
            self.last_input = monotonic();
        }
        if self.paused {
            return;
        }
        self.planner.set_view(view);
        self.issue();
    }

    pub fn input_hot(&self) -> bool {
        monotonic() - self.last_input < self.cfg.input_hot_s
    }

    /// Hand the planner's wants to the pool: cache hits are delivered at
    /// once (one picture, as the binding's `immediate` batch), the rest are
    /// queued coarse first, then fine near-to-far, all channels of one
    /// position together.
    pub fn issue(&mut self) {
        let (coarse, mut fine) = self.planner.wanted();
        let order: HashMap<Ch, usize> = self.planner.active().iter().enumerate().map(|(i, c)| (*c, i)).collect();
        if self.cfg.legacy_order {
            fine.sort_by_key(|(c, t)| (order.get(c).copied(), t.level, t.ty, t.tx));
        } else if let Some(v) = self.planner.view() {
            let cy = (v.bbox_l0[0] + v.bbox_l0[2]) as f64 / 2.0;
            let cx = (v.bbox_l0[1] + v.bbox_l0[3]) as f64 / 2.0;
            let pyr = &self.pyr;
            let dist = |t: &TileAt| {
                let r = pyr.tile_world_rect(*t).unwrap_or([0.0; 4]);
                let (dx, dy) = ((r[0] + r[1]) / 2.0 - cx, (r[2] + r[3]) / 2.0 - cy);
                ((dx * dx + dy * dy).sqrt() * 1000.0) as u64
            };
            fine.sort_by_key(|(c, t)| (dist(t), t.ty, t.tx, order.get(c).copied()));
        }
        let mut jobs = Vec::with_capacity(coarse.len() + fine.len());
        let mut hits = Vec::new();
        let mut now_issued = std::collections::HashSet::with_capacity(coarse.len() + fine.len());
        {
            let mut cache = self.pool.shared().cache.lock().unwrap();
            let tiers = coarse.into_iter().map(|k| (k, 0.0)).chain(fine.into_iter().map(|k| (k, 1.0)));
            for (key, fine) in tiers {
                if !self.issued.contains(&key) {
                    self.trace.stamp("req", Some(key), vec![("fine", fine)]);
                }
                now_issued.insert(key);
                match cache.get(&key) {
                    Some(tile) => hits.push((key, tile)),
                    None => jobs.push(key),
                }
            }
        }
        self.issued = now_issued;
        for (key, tile) in hits {
            self.deliver(key, tile);
        }
        self.pool.set_jobs(jobs);
    }

    /// A tile in hand: the planner takes it or not (late / not wanted).
    pub fn deliver(&mut self, key: Key, tile: Arc<Tile>) -> bool {
        // a coarsest-level tile while that channel's coarse is loading is
        // the coarse tier's; anything else is fine
        let coarse = key.1.level == self.pyr.coarsest() && !self.planner.coarse_complete(key.0);
        let ok = self.planner.deliver(key.0, key.1);
        self.trace.stamp("recv", Some(key), vec![("ok", ok as i32 as f64), ("fine", !coarse as i32 as f64)]);
        if ok {
            self.held.insert(key, tile);
        }
        ok
    }

    /// Drop the tiles the planner no longer holds.
    pub fn prune_held(&mut self) {
        let planner = &self.planner;
        self.held.retain(|k, _| planner.keeps(k.0, k.1));
    }

    pub fn pause(&mut self) {
        self.paused = true;
        self.pool.set_jobs(Vec::new());
    }

    pub fn resume(&mut self) {
        self.paused = false;
        let active: Vec<Ch> = self.display.iter().map(|r| r.0).collect();
        self.planner.set_active(active);
        self.issue();
    }

    /// GL resources for the current context (`initializeGL`).
    pub fn attach_gl(&mut self, gl: glow::Context, max_layers: usize) -> Result<(), String> {
        let render = unsafe { Render::new(&gl)? };
        let store = Store::new(self.cfg.max_raw_texture_bytes, max_layers, render.u_blocks);
        let vt = Vt::new(&self.pyr, render.u_blocks, store.layers_per_block as usize);
        let total = self.cfg.max_raw_texture_bytes.saturating_sub(store.slack_bytes());
        self.planner.set_total(Some(total as u64));
        self.gpu = Some(Gpu { gl, store, vt, render });
        Ok(())
    }

    /// Delete every GL name (context current: `aboutToBeDestroyed`).
    pub fn release_gl(&mut self) {
        if let Some(mut g) = self.gpu.take() {
            g.store.clear(&g.gl);
            unsafe { g.render.destroy(&g.gl) };
        }
    }

    /// The context is already gone: forget the names without GL calls.
    pub fn forget_gl(&mut self) {
        self.gpu = None;
    }

    pub fn gpu_bytes(&self) -> usize {
        self.gpu.as_ref().map_or(0, |g| g.store.allocated_bytes() + g.render.target_bytes())
    }

    /// Keep only the complete coarse on the GPU (`release_to_coarse`).
    pub fn release_to_coarse(&mut self) -> usize {
        let before = self.gpu_bytes();
        let coarsest = self.pyr.coarsest();
        if let Some(g) = self.gpu.as_mut() {
            let keep: std::collections::HashSet<Key> =
                g.store.slots.keys().filter(|k| k.1.level == coarsest).copied().collect();
            g.store.release_except(&g.gl, &keep);
            g.vt.forget();
            unsafe { g.render.drop_targets(&g.gl) };
            g.render.last_rows = 0;            // nothing old is redrawn from released slots
        }
        before.saturating_sub(self.gpu_bytes())
    }

    /// Stop: no notice, no read after this (the Python side then closes
    /// the socket pair). Never waits for a read under way.
    pub fn close(&mut self) {
        self.pool.close();
        if let Some(n) = &self.notifier {
            n.close();
        }
    }
}
