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
    /// `max_coarse_plane_bytes_per_channel` (None: no limit)
    pub max_coarse_bytes_per_channel: Option<u64>,
    pub max_coarse_tiles: usize,
    /// the CPU cache's two allowances (`STEP1_VIEWER_RAW_CACHE_BYTES`,
    /// `STEP1_VIEWER_CORRECTED_CACHE_BYTES`)
    pub cpu_cache_bytes: usize,
    pub corrected_cache_bytes: usize,
    pub threads: usize,
    pub input_hot_s: f64,
    pub hot_upload_ms: f64,
    pub frame_budget_ms: f64,
    pub growth_quiet_s: f64,
    /// byte bound of the reader result queue (`pool::RESULT_BYTES`)
    pub result_bytes: usize,
    /// test only: delay before each read (`pool::Shared::read_delay`)
    pub test_read_delay_ms: f64,
    /// test only: bind at most this many blocks in the single pass (to
    /// reach the sampler-capacity fallback on a small project)
    pub test_block_units: Option<usize>,
    /// test only: request fine tiles channel by channel (the old order)
    pub legacy_order: bool,
}

mod failures;
mod gl_life;
mod memory;

pub struct Gpu {
    pub gl: glow::Context,
    pub store: Store,
    pub vt: Vt,
    pub render: Render,
    /// what the picture in `final` was composed from (`frame::compose`)
    pub composed: Option<crate::frame::ComposeKey>,
    /// the view and clip `final` was composed for (masks drawn alone use them)
    pub composed_for: Option<([f32; 4], crate::render::Clip)>,
}

pub struct ViewerCore {
    pub cfg: Config,
    /// what each channel's pixels are (raw / corrected / missing)
    pub sources: Arc<crate::corrected::Step1Sources>,
    pub pyr: Pyramid,
    /// the region `(y0, y1, x0, x1)` (level 0) and as a world rect
    pub roi_world: Option<[f64; 4]>,
    pub planner: Planner,
    pub pool: Pool,
    pub notifier: Option<Arc<Notifier>>,
    pub trace: Arc<Trace>,
    pub gpu: Option<Gpu>,
    /// the rows drawn, in order (Fusion: a channel may have several)
    pub display: Vec<(Ch, RowParams)>,
    pub smooth: bool,
    /// Fusion (else Overlay)
    pub fusion: bool,
    /// the region polygon (level-0 world x, y), already checked by Python
    /// (`sanitize_roi_polygon`); None: the rectangle alone clips
    pub polygon: Option<Vec<[f32; 2]>>,
    /// the slide's sample type (what a raw channel is stored as)
    pub raw_kind: crate::step1_tile::RawKind,
    pub last_input: f64,
    pub paused: bool,
    /// tiles delivered to the planner and still held by it
    pub held: HashMap<Key, Arc<Tile>>,
    pub frames: u64,
    pub last_error: String,
    /// keys asked for and still owed: only a NEW want is a trace "req"
    issued: std::collections::HashSet<Key>,
    /// test-only fault injection (`fault`); inert in the product
    pub faults: Arc<crate::fault::Faults>,
    /// GL initialisations so far (`attach_gl`)
    pub attaches: u32,
    /// Step3's masks (`label_store`)
    pub labels: crate::label_store::Labels,
}

impl ViewerCore {
    /// `sources`: per channel raw / corrected / missing and the region
    /// (`build_corrected_manifest`); `raw_kind`: the slide's sample type.
    #[allow(clippy::too_many_arguments)]
    pub fn new(source: RawSource, cfg: Config, sources: crate::corrected::Step1Sources,
               raw_kind: crate::step1_tile::RawKind, budget: usize,
               notifier: Option<Arc<Notifier>>, trace: Arc<Trace>) -> ViewerCore {
        let roi = sources.roi;
        let missing: Vec<Ch> = sources.names.keys().copied().filter(|c| sources.missing(*c).is_some()).collect();
        let shapes = source.manifest().levels.iter().map(|l| (l.height, l.width)).collect();
        let pyr = Pyramid { shapes };
        let budgets = Budgets { max_fine_tiles: cfg.max_fine_tiles,
                                max_fine_bytes_per_channel: cfg.max_fine_bytes_per_channel,
                                total: Some(cfg.max_raw_texture_bytes as u64),
                                max_coarse_bytes: cfg.max_coarse_bytes_per_channel };
        let planner = Planner::new(pyr.clone(), budgets, cfg.max_coarse_tiles);
        let mut planner = planner;
        planner.unavailable.extend(missing);              // drawn by nobody, read by nobody
        let sources = Arc::new(sources);
        let faults = Arc::new(crate::fault::Faults::from_env());
        let pool = Pool::start(Arc::new(source), pyr.clone(), Arc::clone(&sources), raw_kind, budget,
                               [cfg.cpu_cache_bytes, cfg.corrected_cache_bytes], cfg.threads, cfg.result_bytes, cfg.test_read_delay_ms,
                               notifier.clone(), Arc::clone(&trace), Arc::clone(&faults));
        let roi_world = roi.map(|[y0, y1, x0, x1]| [x0 as f64, x1 as f64, y0 as f64, y1 as f64]);
        ViewerCore { cfg, pyr, roi_world, planner, pool, sources, notifier, trace, gpu: None,
                     display: Vec::new(), smooth: true, fusion: false, polygon: None, raw_kind,
                     last_input: -1e9, paused: false,
                     held: HashMap::new(), frames: 0, last_error: String::new(),
                     issued: Default::default(), faults, attaches: 0, labels: Default::default() }
    }

    /// The rows drawn and their display parameters (Overlay: one per
    /// channel; Fusion: groups in order, then the nucleus), the mode.
    pub fn set_display(&mut self, rows: Vec<(Ch, RowParams)>, smooth: bool, fusion: bool) {
        self.smooth = smooth;
        self.fusion = fusion;
        let mut active: Vec<Ch> = Vec::new();
        for (c, _) in &rows {
            if !active.contains(c) {
                active.push(*c);                     // a channel is read once
            }
        }
        self.display = rows;
        let texels = active.iter().filter_map(|c| self.format_of(*c).map(|f| (*c, f.bytes() as u64))).collect();
        self.planner.set_texels(texels);
        self.update_total();
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

    /// How a channel's tiles are stored (None: missing, never drawn).
    pub fn format_of(&self, c: Ch) -> Option<crate::family::Format> {
        use crate::corrected::Source;
        use crate::family::Format;
        use crate::step1_tile::RawKind;
        match self.sources.of(c)? {
            Source::Raw { .. } => Some(match self.raw_kind {
                RawKind::U8 => Format::U8,
                RawKind::U16 => Format::U16,
                _ => Format::F32,
            }),
            Source::Corrected { .. } => Some(Format::F32),
            Source::Missing { .. } => None,
        }
    }

    /// The admission total (`_total_budget`): the budget less one block
    /// of each format the drawn channels use.
    fn update_total(&mut self) {
        let Some(g) = self.gpu.as_ref() else { return };
        // `_total_budget`: one block of the slide's own format, and one of
        // float32 when any drawn channel is not raw
        let raw = match self.raw_kind {
            crate::step1_tile::RawKind::U8 => crate::family::Format::U8,
            crate::step1_tile::RawKind::U16 => crate::family::Format::U16,
            _ => crate::family::Format::F32,
        };
        let mut formats = vec![raw];
        if self.display.iter().any(|(c, _)| !matches!(self.sources.of(*c), Some(crate::corrected::Source::Raw { .. }))) {
            formats.push(crate::family::Format::F32);
        }
        let total = self.cfg.max_raw_texture_bytes.saturating_sub(g.store.slack_bytes(&formats));
        self.planner.set_total(Some(total as u64));
    }

    pub fn resume(&mut self) {
        self.paused = false;
        let mut active: Vec<Ch> = Vec::new();
        for (c, _) in &self.display {
            if !active.contains(c) {
                active.push(*c);
            }
        }
        self.planner.set_active(active);
        self.issue();
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
