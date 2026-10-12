//! Step3's masks over the picture (`Step1GpuLayer._compose_labels`, R3
//! plan §2.1): `final` is copied into `shown` (RGBA8, sharing `final`'s
//! stencil, so a mask is clipped by the very polygon the picture was);
//! then per layer the screen id image `ids` (R32UI) is drawn plane by plane
//! -- unclipped, unblended, a later plane over an earlier one, its zeros
//! too -- and drawn onto `shown` as outline or fill, inside the region's
//! stencil and rectangle, with separate alpha blending. One R32UI texture
//! per plane (`_LabelTextureLru`), in the label budget. The programs are
//! made the first time masks are drawn: a viewer without masks never pays.

use super::label_textures::LabelGpu;
use super::{Clip, Render};
use crate::label_store::Labels;
use glow::HasContext;
use std::collections::HashMap;


impl Render {
    pub fn label_gpu(&self) -> Option<&LabelGpu> {
        self.labels.as_ref()
    }

    /// `_compose_labels` over the `final` just composed for `view` and
    /// `clip`. Without a visible layer `shown` is not drawn (`final` is the
    /// picture). Returns the passes drawn.
    /// `budget`: the upload time this paint may spend (None: everything
    /// now, as `set_labels` draws). Returns (passes, planes left for later).
    pub unsafe fn compose_labels(&mut self, gl: &glow::Context, labels: &mut Labels, view: [f32; 4],
                                 clip: &Clip, budget: Option<std::time::Duration>) -> Result<(u64, usize), String> {
        if let Some(lg) = self.labels.as_mut() {
            unsafe { lg.flush_orphans(gl) };             // released without a context
        }
        let fin = self.fin.as_ref().map(|t| (t.fbo, t.stencil));
        let Some((fin_fbo, stencil)) = fin.filter(|_| !labels.layers.is_empty()) else {
            labels.shown = false;
            return Ok((0, 0));
        };
        if self.labels.is_none() {
            self.labels = Some(unsafe { LabelGpu::new(gl)? });
        }
        // upload order: each layer's planes from its last (the target
        // level, drawn last) back, every layer's n-th-from-last together
        let mut ranked: Vec<(usize, u64)> = labels.layers.iter()
            .flat_map(|l| l.planes.iter().rev().enumerate().map(|(rank, p)| (rank, p.0))).collect();
        ranked.sort_by_key(|r| r.0);
        let mut needed = Vec::with_capacity(ranked.len());
        for (_, id) in ranked {
            let plane = labels.plane(id).ok_or_else(|| format!("label plane {id} was never handed over"))?;
            needed.push((id, plane));
        }
        let (w, h) = self.size;
        let lg = self.labels.as_mut().unwrap();
        let deferred = unsafe {
            let d = lg.prepare(gl, &needed, budget)?;
            lg.ensure_targets(gl, (w, h), stencil)?;
            d
        };
        let (ids_fbo, ids_tex) = lg.ids.as_ref().map(|t| (t.fbo, t.tex)).unwrap();
        let shown_fbo = lg.shown.as_ref().map(|t| t.fbo).unwrap();
        let (ids_prog, draw_prog) = (lg.ids_prog, lg.draw_prog);
        let tex_of: HashMap<u64, glow::Texture> = lg.textures.iter().map(|(id, (t, _, _))| (*id, *t)).collect();
        let mut passes = 0;
        unsafe {
            gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(fin_fbo));
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(shown_fbo));
            gl.blit_framebuffer(0, 0, w, h, 0, 0, w, h, glow::COLOR_BUFFER_BIT, glow::NEAREST);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        }
        labels.shown = true;
        labels.label_compositions += 1;
        if clip.scissor.is_some_and(|s| s[2] <= 0 || s[3] <= 0) {
            return Ok((0, deferred));           // the region is off screen: the picture is the answer
        }
        let mut scissor_box = [0i32; 4];
        unsafe { gl.get_parameter_i32_slice(glow::SCISSOR_BOX, &mut scissor_box) };
        for layer in labels.layers.clone() {
            unsafe {
                // the id image: cleared, every plane in order, unclipped, unblended
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(ids_fbo));
                gl.viewport(0, 0, w, h);
                gl.disable(glow::BLEND);
                gl.disable(glow::SCISSOR_TEST);           // unclipped, whatever came before
                gl.disable(glow::STENCIL_TEST);
                gl.clear_buffer_u32_slice(glow::COLOR, 0, &[0, 0, 0, 0]);
                gl.use_program(Some(ids_prog));
                let l = self.uloc(gl, ids_prog, "u_view_rect");
                gl.uniform_4_f32(l.as_ref(), view[0], view[1], view[2], view[3]);
                let l = self.uloc(gl, ids_prog, "u_target_size");
                gl.uniform_2_f32(l.as_ref(), w as f32, h as f32);
                gl.bind_vertex_array(Some(self.vao));
                for (id, rect) in &layer.planes {
                    let Some(tex) = tex_of.get(id).copied() else { continue };   // uploaded by a later paint
                    gl.active_texture(glow::TEXTURE0);
                    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
                    let l = self.uloc(gl, ids_prog, "u_labels");
                    gl.uniform_1_i32(l.as_ref(), 0);
                    let l = self.uloc(gl, ids_prog, "u_plane_rect");
                    gl.uniform_4_f32(l.as_ref(), rect[0] as f32, rect[1] as f32, rect[2] as f32, rect[3] as f32);
                    gl.draw_arrays(glow::TRIANGLES, 0, 3);
                    passes += 1;
                }
                gl.use_program(None);
                // onto `shown`: inside the region, blended
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(shown_fbo));
                gl.viewport(0, 0, w, h);
                if clip.polygon.is_some() {
                    gl.enable(glow::STENCIL_TEST);
                    gl.stencil_mask(0x00);
                    gl.stencil_func(glow::EQUAL, 0x01, 0x01);
                    gl.stencil_op(glow::KEEP, glow::KEEP, glow::KEEP);
                }
                if let Some(s) = clip.scissor {
                    gl.enable(glow::SCISSOR_TEST);
                    gl.scissor(s[0], s[1], s[2], s[3]);
                }
                gl.enable(glow::BLEND);
                gl.blend_equation(glow::FUNC_ADD);
                gl.blend_func_separate(glow::SRC_ALPHA, glow::ONE_MINUS_SRC_ALPHA, glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
                gl.use_program(Some(draw_prog));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(ids_tex));
                for (name, v) in [("u_ids", 0), ("u_mode", layer.fill as i32), ("u_radius", layer.radius)] {
                    let l = self.uloc(gl, draw_prog, name);
                    gl.uniform_1_i32(l.as_ref(), v);
                }
                let l = self.uloc(gl, draw_prog, "u_color");
                gl.uniform_3_f32(l.as_ref(), layer.color[0], layer.color[1], layer.color[2]);
                let l = self.uloc(gl, draw_prog, "u_alpha");
                gl.uniform_1_f32(l.as_ref(), layer.alpha);
                gl.draw_arrays(glow::TRIANGLES, 0, 3);
                passes += 1;
                gl.bind_texture(glow::TEXTURE_2D, None);
                gl.bind_vertex_array(None);
                gl.use_program(None);
                gl.disable(glow::BLEND);
                gl.blend_func(glow::ONE, glow::ZERO);
                crate::stencil::restore(gl, scissor_box);
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            }
        }
        labels.label_passes += passes;
        Ok((passes, deferred))
    }

    /// The core's planner let plane `id` go: its texture is reused (R3b).
    pub fn release_label(&mut self, id: u64, cols: usize, rows: usize) {
        if let Some(lg) = self.labels.as_mut() {
            lg.release(id, cols, rows);
        }
    }

    /// The masks' GPU names that `release_to_coarse` drops: the targets
    /// (the textures stay, as `Step1GpuLayer.release_to_coarse` keeps them).
    pub unsafe fn drop_label_targets(&mut self, gl: &glow::Context) {
        if let Some(lg) = self.labels.as_mut() {
            unsafe { lg.drop_targets(gl) };
        }
    }
}
