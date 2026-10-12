//! A paint's drawing: page tables up to date, the picture composed into
//! `final` -- unless nothing it is made from changed since the last time
//! (`ComposeKey`) -- Step3's masks over it into `shown` when there are any
//! and either changed, and the result blitted. So a new mask snapshot alone
//! composes no channel (`Step1GpuLayer.set_labels`: `image_submissions`
//! stays, `label_compositions` grows; R3 plan §2.1).

use super::{drawn_rows, FrameReport};
use crate::geom::{roi_scissor_box, TileAt};
use crate::plan::Ch;
use crate::viewer::ViewerCore;
use crate::vt::RowParams;

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

/// Steps 4 of `paint`; the rows drawn, or None when GL failed (`rep.error`
/// says why; nothing was blitted).
pub(super) fn draw(core: &mut ViewerCore, rep: &mut FrameReport, fbo: u32, w: i32, h: i32,
                   view: [f64; 4], base_overflow: bool) -> Option<Vec<(Ch, Vec<TileAt>)>> {
    let rows = if base_overflow { Vec::new() } else { drawn_rows(core) };
    let gpu = core.gpu.as_mut().unwrap();
    if !base_overflow {
        gpu.vt.sync_pages(&rows, &gpu.store);
    }
    gpu.vt.sync_meta(&mut gpu.store);
    // the display's rows in order (a channel may have several, Fusion
    // groups), those of channels drawn now
    let drawn: std::collections::HashSet<_> = rows.iter().map(|(c, _)| *c).collect();
    let params_rows: Vec<_> = core.display.iter().filter(|(c, _)| drawn.contains(c)).copied().collect();
    let (gl, render) = (&gpu.gl, &mut gpu.render);
    let made = match unsafe { render.ensure_targets(gl, w.max(1), h.max(1)) } {
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
                if let Err(e) = render.upload(gl, &mut gpu.vt, &params) {
                    rep.error = e;
                    return None;
                }
            }
            if let Err(e) = render.compose(gl, &gpu.vt, &gpu.store, rows_drawn, v32, core.smooth, core.fusion, &clip) {
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
            if let Err(e) = render.compose_labels(gl, &mut core.labels, v32, &clip) {
                rep.error = e;
                return None;
            }
            core.labels.dirty = false;
        }
        render.blit(gl, fbo, w, h, core.labels.shown);
    }
    rep.composed = composed;
    Some(rows)
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
        render.compose_labels(gl, &mut core.labels, view, &clip)?;
        core.labels.dirty = false;
        render.blit(gl, fbo, w, h, core.labels.shown);
    }
    Ok(true)
}
