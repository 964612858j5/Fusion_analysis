//! R7a (docs/v16_5_r7a_timing_plan.md §2), measurement only and off unless
//! both the trace and `Config::draw_log` are on: where a paint's drawing
//! time goes, in the order the calls already run (nothing is moved):
//!   0 tables  `vt.sync_pages` + `vt.sync_meta` (CPU only)
//!   1 targets `render.ensure_targets`
//!   2 upload  `render.upload` (only when `final` is composed again)
//!   3 compose `render.compose` (one draw; the shader walks every channel)
//!   4 labels  `render.compose_labels`
//!   5 blit    `render.blit`
//! CPU time per segment always; GPU time for 1-5 from `GL_TIME_ELAPSED`
//! queries, collected without waiting: a result is read only once
//! `QUERY_RESULT_AVAILABLE` says so, on a later paint that happens anyway;
//! a full pool skips the query and counts it missing. No `glFinish`, no
//! extra paint.

use crate::trace::Trace;
use std::collections::VecDeque;
use std::time::Instant;

pub const SEGMENTS: usize = 6;
pub const TABLES: usize = 0;
pub const TARGETS: usize = 1;
pub const UPLOAD: usize = 2;
pub const COMPOSE: usize = 3;
pub const LABELS: usize = 4;
pub const BLIT: usize = 5;
/// queries alive at once per context (a frame uses at most five)
const POOL: usize = 64;

/// One context's timer queries (`Gpu::timers`): created on first use with
/// the context current, deleted by `destroy` (context current) or simply
/// forgotten with the context (`forget_gl`: no GL call).
#[derive(Default)]
pub struct GpuTimers {
    free: Vec<glow::Query>,
    /// (frame, segment, query), oldest first: results come in order
    pending: VecDeque<(u64, usize, glow::Query)>,
    created: usize,
    open: Option<glow::Query>,
    pub missing: u64,
}

impl GpuTimers {
    pub fn pending(&self) -> usize {
        self.pending.len() + usize::from(self.open.is_some())
    }

    /// Start timing `seg` of `frame`; false (counted missing) when the
    /// pool is exhausted.
    pub unsafe fn begin(&mut self, gl: &glow::Context, frame: u64, seg: usize) -> bool {
        use glow::HasContext;
        let q = match self.free.pop() {
            Some(q) => q,
            None if self.created < POOL => match unsafe { gl.create_query() } {
                Ok(q) => {
                    self.created += 1;
                    q
                }
                Err(_) => {
                    self.missing += 1;
                    return false;
                }
            },
            None => {
                self.missing += 1;
                return false;
            }
        };
        unsafe { gl.begin_query(glow::TIME_ELAPSED, q) };
        self.open = Some(q);
        self.pending.push_back((frame, seg, q));
        true
    }

    pub unsafe fn end(&mut self, gl: &glow::Context) {
        use glow::HasContext;
        if self.open.take().is_some() {
            unsafe { gl.end_query(glow::TIME_ELAPSED) };
        }
    }

    /// Results that are ready, as trace records `fphase_gpu`; stops at the
    /// first one that is not (they finish in order).
    pub unsafe fn collect(&mut self, gl: &glow::Context, trace: &Trace, viewer_frames: u64) {
        use glow::HasContext;
        while let Some(&(frame, seg, q)) = self.pending.front() {
            if self.open == Some(q) || unsafe { gl.get_query_parameter_u32(q, glow::QUERY_RESULT_AVAILABLE) } == 0 {
                break;
            }
            let ns = unsafe { gl.get_query_parameter_u64(q, glow::QUERY_RESULT) };
            self.pending.pop_front();
            self.free.push(q);
            trace.stamp("fphase_gpu", None, vec![("frame", frame as f64), ("seg", seg as f64),
                                                  ("ms", ns as f64 / 1.0e6),
                                                  ("age", (viewer_frames - frame) as f64)]);
        }
    }

    /// Delete every query (context current: `release_gl`).
    pub unsafe fn destroy(&mut self, gl: &glow::Context, trace: &Trace) {
        use glow::HasContext;
        unsafe { self.end(gl) };
        let lost = self.pending.len();
        for (_, _, q) in self.pending.drain(..) {
            unsafe { gl.delete_query(q) };
        }
        for q in self.free.drain(..) {
            unsafe { gl.delete_query(q) };
        }
        self.created = 0;
        if lost > 0 && trace.on() {
            trace.stamp("fphase_gpu_lost", None, vec![("n", lost as f64)]);
        }
    }

    /// The context is already gone: nothing may be called on it.
    pub fn forget(&mut self, trace: &Trace) {
        let lost = self.pending.len();
        self.pending.clear();
        self.free.clear();
        self.open = None;
        self.created = 0;
        if lost > 0 && trace.on() {
            trace.stamp("fphase_gpu_lost", None, vec![("n", lost as f64)]);
        }
    }
}

/// CPU time per segment of one paint's drawing.
pub struct PhaseClock {
    pub on: bool,
    /// GPU queries too (`BLOCK01_A9_GPU_TIMERS=0`: CPU segments only, for
    /// the overhead comparison of R7a §2)
    gpu: bool,
    pub frame: u64,
    pub cpu: [f64; SEGMENTS],
    t: Instant,
}

impl PhaseClock {
    pub fn new(on: bool, frame: u64) -> PhaseClock {
        let gpu = on && std::env::var("BLOCK01_A9_GPU_TIMERS").map_or(true, |v| v != "0");
        PhaseClock { on, gpu, frame, cpu: [0.0; SEGMENTS], t: Instant::now() }
    }

    /// Start a segment's CPU clock and, for a GPU segment, its query.
    pub unsafe fn begin(&mut self, gl: &glow::Context, timers: &mut GpuTimers, seg: usize) {
        if !self.on {
            return;
        }
        self.t = Instant::now();
        if self.gpu && seg != TABLES {
            unsafe { timers.begin(gl, self.frame, seg) };
        }
    }

    pub unsafe fn end(&mut self, gl: &glow::Context, timers: &mut GpuTimers, seg: usize) {
        if !self.on {
            return;
        }
        if self.gpu && seg != TABLES {
            unsafe { timers.end(gl) };
        }
        self.cpu[seg] += self.t.elapsed().as_secs_f64() * 1000.0;
    }
}

/// The `fphase` record of one paint (`frame::paint`): written when
/// `final` was composed again or the drawing took more than 20 ms;
/// `rest` = `draw_ms` minus the classified CPU segments.
pub fn record(trace: &Trace, pc: &PhaseClock, draw_ms: f64, composed: bool, made: bool,
              rows: usize, attaches: u32, missing: u64) {
    if !pc.on || !(composed || draw_ms > 20.0) {
        return;
    }
    let sum: f64 = pc.cpu.iter().sum();
    let names = ["tables", "targets", "upload", "compose", "labels", "blit"];
    let mut extra: Vec<(&'static str, f64)> = names.iter().zip(pc.cpu.iter()).map(|(n, v)| (*n, *v)).collect();
    extra.extend([("frame", pc.frame as f64), ("draw_ms", draw_ms), ("rest", draw_ms - sum),
                  ("composed", composed as i32 as f64), ("made", made as i32 as f64),
                  ("rows", rows as f64), ("attaches", attaches as f64), ("gpu_missing", missing as f64)]);
    trace.stamp("fphase", None, extra);
}
