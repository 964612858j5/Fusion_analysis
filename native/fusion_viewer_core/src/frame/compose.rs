//! A paint's drawing: page tables up to date, the picture composed into
//! `final` -- unless nothing it is made from changed since the last time
//! (`ComposeKey`) -- Step3's masks over it into `shown` when there are any
//! and either changed, and the result blitted. So a new mask snapshot alone
//! composes no channel (`Step1GpuLayer.set_labels`: `image_submissions`
//! stays, `label_compositions` grows; R3 plan §2.1).

use super::phase_time::{self as pt, PhaseClock};
use super::{drawn_rows, shown_rows, FrameReport};
use crate::geom::{roi_scissor_box, TileAt};
use crate::plan::Ch;
use crate::viewer::ViewerCore;
use crate::vt::RowParams;

/// Time a paint may spend uploading Step3's label textures (R3b gate):
/// at least one goes, the rest wait for the next paint. A new texture
/// costs 0.5-2 ms, a reused one 0.3 ms: at 1 ms a screen of new tiles
/// took ~8 paints (~200 ms) to show; at 3 ms ~65 ms, Python's 80-110.
const LABEL_UPLOAD_MS: f64 = 3.0;

/// Everything `final` is composed from.
#[derive(Clone, Debug, PartialEq)]
pub struct ComposeKey {
    size: (i32, i32),
    view: [u64; 4],
    smooth: bool,
    fusion: bool,
    polygon: Option<Vec<[u32; 2]>>,
    rows: Vec<(Ch, Vec<TileAt>)>,
    params: Vec<(Ch, RowParams)>,
    rows_drawn: usize,
    generation: u64,
    split: (usize, usize),
    base_overflow: bool,
}

/// Steps 4 of `paint`; the rows drawn and whether the targets were made
/// anew, or None when GL failed (`rep.error` says why; nothing was
/// blitted). `pc` times the segments (R7a, `phase_time`; inert when off).
#[allow(clippy::too_many_arguments)]
pub(super) fn draw(core: &mut ViewerCore, rep: &mut FrameReport, fbo: u32, w: i32, h: i32,
                   view: [f64; 4], base_overflow: bool, pc: &mut PhaseClock)
                   -> Option<(Vec<(Ch, Vec<TileAt>)>, bool)> {
    // R7c step 3: the candidates, less the channels the gate keeps back
    let rows = if base_overflow { Vec::new() } else { let r = drawn_rows(core); shown_rows(core, r, view) };
    // S0-B: rows being prepared are fetched and uploaded, not composed
    let display = super::composed_display(core);
    let rows: Vec<_> = rows.into_iter().filter(|(c, _)| display.iter().any(|(d, _)| d == c)).collect();
    let viewer_frames = core.frames;
    let gpu = core.gpu.as_mut().unwrap();
    if pc.on {
        unsafe { gpu.timers.collect(&gpu.gl, &core.trace, viewer_frames) };
    }
    unsafe { pc.begin(&gpu.gl, &mut gpu.timers, pt::TABLES) };
    if !base_overflow {
        gpu.vt.sync_pages(&rows, &gpu.store);
    }
    gpu.vt.sync_meta(&mut gpu.store);
    unsafe { pc.end(&gpu.gl, &mut gpu.timers, pt::TABLES) };
    // the display's rows in order (a channel may have several, Fusion
    // groups), those of channels drawn now
    let drawn: std::collections::HashSet<_> = rows.iter().map(|(c, _)| *c).collect();
    let params_rows: Vec<_> = display.iter().filter(|(c, _)| drawn.contains(c)).copied().collect();
    let (gl, render, timers) = (&gpu.gl, &mut gpu.render, &mut gpu.timers);
    unsafe { pc.begin(gl, timers, pt::TARGETS) };
    let made = unsafe { render.ensure_targets(gl, w.max(1), h.max(1)) };
    unsafe { pc.end(gl, timers, pt::TARGETS) };
    let made = match made {
        Ok(m) => m,
        Err(e) => {
            rep.error = e;
            return None;
        }
    };
    let rows_drawn = if base_overflow { render.last_rows } else { params_rows.len() };
    let key = ComposeKey {
        size: render.size, view: view.map(f64::to_bits), smooth: core.smooth, fusion: core.fusion,
        polygon: core.polygon.as_ref().map(|p| p.iter().map(|q| [q[0].to_bits(), q[1].to_bits()]).collect()),
        rows: rows.clone(), params: params_rows.clone(), rows_drawn, generation: gpu.store.generation,
        split: (render.u_blocks, render.f_blocks), base_overflow };
    let v32 = [view[0] as f32, view[1] as f32, view[2] as f32, view[3] as f32];
    let clip = crate::render::Clip { scissor: roi_scissor_box(view, core.roi_world, render.size),
                                     polygon: core.polygon.clone() };
    let composed = made || gpu.composed.as_ref() != Some(&key);
    unsafe {
        if composed {
            if !base_overflow {
                let params = gpu.vt.params(&params_rows);
                pc.begin(gl, timers, pt::UPLOAD);
                let up = render.upload(gl, &mut gpu.vt, &params);
                pc.end(gl, timers, pt::UPLOAD);
                if let Err(e) = up {
                    rep.error = e;
                    return None;
                }
            }
            pc.begin(gl, timers, pt::COMPOSE);
            let done = render.compose(gl, &gpu.vt, &gpu.store, rows_drawn, v32, core.smooth, core.fusion, &clip);
            pc.end(gl, timers, pt::COMPOSE);
            if let Err(e) = done {
                rep.error = e;
                gpu.composed = None;
                return None;
            }
            render.last_rows = rows_drawn;
            gpu.composed = Some(key);
            gpu.composed_for = Some((v32, clip.clone()));
            core.labels.image_submissions += 1;
        }
        if composed || core.labels.dirty {
            let t = std::time::Instant::now();
            // label uploads within ~3 ms a paint: the rest come next paint
            let budget = Some(std::time::Duration::from_secs_f64(LABEL_UPLOAD_MS / 1000.0));
            pc.begin(gl, timers, pt::LABELS);
            let labelled = render.compose_labels(gl, &mut core.labels, v32, &clip, budget);
            pc.end(gl, timers, pt::LABELS);
            match labelled {
                Err(e) => {
                    rep.error = e;
                    return None;
                }
                Ok((_passes, left)) => {
                    core.labels.dirty = left > 0;
                    rep.deferred += left;
                    rep.label_deferred = left;
                }
            }
            let ms = t.elapsed().as_secs_f64() * 1000.0;
            core.lab.gui_ms += ms;
            core.lab.split_ms[2] += ms;
        }
        pc.begin(gl, timers, pt::BLIT);
        render.blit(gl, fbo, w, h, core.labels.shown);
        pc.end(gl, timers, pt::BLIT);
    }
    rep.composed = composed;
    Some((rows, made))
}

/// A new mask snapshot drawn over the picture as it stands, at once and
/// alone (`Step1GpuLayer.set_labels`): no tile taken, no channel composed;
/// `final` -> `shown` -> Qt's framebuffer. False when nothing is composed
/// yet (the next paint draws the masks with the picture).
pub fn draw_labels(core: &mut ViewerCore, fbo: u32, w: i32, h: i32) -> Result<bool, String> {
    let Some(gpu) = core.gpu.as_mut() else { return Ok(false) };
    let Some((view, clip)) = gpu.composed_for.clone().filter(|_| gpu.composed.is_some()) else { return Ok(false) };
    let (gl, render) = (&gpu.gl, &mut gpu.render);
    unsafe {
        let (_passes, left) = render.compose_labels(gl, &mut core.labels, view, &clip, None)?;
        core.labels.dirty = left > 0;
        render.blit(gl, fbo, w, h, core.labels.shown);
    }
    Ok(true)
}
