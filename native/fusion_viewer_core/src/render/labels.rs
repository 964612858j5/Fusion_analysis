//! Step3's masks over the picture (`Step1GpuLayer._compose_labels`, R3
//! plan §2.1): `final` is copied into `shown` (RGBA8, sharing `final`'s
//! stencil, so a mask is clipped by the very polygon the picture was);
//! then per layer the screen id image `ids` (R32UI) is drawn plane by plane
//! -- unclipped, unblended, a later plane over an earlier one, its zeros
//! too -- and drawn onto `shown` as outline or fill, inside the region's
//! stencil and rectangle, with separate alpha blending. One R32UI texture
//! per plane (`_LabelTextureLru`), in the label budget. The programs are
//! made the first time masks are drawn: a viewer without masks never pays.

use super::{Clip, Render, Target};
use crate::label_store::{Labels, Plane, LABEL_BUDGET};
use glow::HasContext;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

pub const LABELS_FRAG: &str = include_str!("../../shaders/step1_gpu_labels.frag");

pub struct LabelGpu {
    ids_prog: glow::Program,
    draw_prog: glow::Program,
    pub(super) ids: Option<Target>,
    pub(super) shown: Option<Target>,
    /// the size and `final` stencil the two targets were made for
    made_for: ((i32, i32), Option<glow::Renderbuffer>),
    textures: HashMap<u64, (glow::Texture, usize, u64)>,
    order: BTreeMap<u64, u64>,
    tick: u64,
    pub bytes: usize,
    pub uploads: u64,
    pub evictions: u64,
}

impl LabelGpu {
    unsafe fn new(gl: &glow::Context) -> Result<LabelGpu, String> {
        let src = |d: &str| LABELS_FRAG.replacen('\n', &format!("\n#define {d}\n"), 1);
        let ids_prog = unsafe { crate::shader::link(gl, crate::shader::VERT, &src("PASS_LABEL_IDS"))? };
        let draw_prog = match unsafe { crate::shader::link(gl, crate::shader::VERT, &src("PASS_LABEL_DRAW")) } {
            Ok(p) => p,
            Err(e) => {
                unsafe { gl.delete_program(ids_prog) };
                return Err(e);
            }
        };
        Ok(LabelGpu { ids_prog, draw_prog, ids: None, shown: None, made_for: ((0, 0), None),
                      textures: HashMap::new(), order: BTreeMap::new(), tick: 0, bytes: 0, uploads: 0,
                      evictions: 0 })
    }

    pub fn target_bytes(&self) -> usize {
        let (w, h) = self.made_for.0;
        if self.ids.is_some() { (w.max(0) * h.max(0)) as usize * (4 + 4) } else { 0 }
    }

    unsafe fn drop_targets(&mut self, gl: &glow::Context) {
        for t in [self.ids.take(), self.shown.take()].into_iter().flatten() {
            unsafe {
                gl.delete_framebuffer(t.fbo);
                gl.delete_texture(t.tex);
            }
        }
        self.made_for = ((0, 0), None);
    }

    unsafe fn ensure_targets(&mut self, gl: &glow::Context, size: (i32, i32),
                             stencil: Option<glow::Renderbuffer>) -> Result<(), String> {
        if self.made_for == (size, stencil) && self.ids.is_some() {
            return Ok(());
        }
        unsafe {
            self.drop_targets(gl);
            self.ids = Some(Render::target(gl, glow::R32UI, glow::RED_INTEGER, glow::UNSIGNED_INT, size.0, size.1)?);
            let shown = Render::target(gl, glow::RGBA8, glow::RGBA, glow::UNSIGNED_BYTE, size.0, size.1)?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(shown.fbo));
            gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::STENCIL_ATTACHMENT, glow::RENDERBUFFER, stencil);
            let ok = gl.check_framebuffer_status(glow::FRAMEBUFFER) == glow::FRAMEBUFFER_COMPLETE;
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            self.shown = Some(shown);
            if !ok {
                return Err("shown framebuffer incomplete with final's stencil".into());
            }
        }
        self.made_for = (size, stencil);
        Ok(())
    }

    /// Every plane of `needed` on the GPU; others leave, least recently
    /// used first, while over the budget (`_LabelTextureLru.prepare`).
    unsafe fn prepare(&mut self, gl: &glow::Context, needed: &[(u64, Arc<Plane>)]) -> Result<(), String> {
        let keep: HashSet<u64> = needed.iter().map(|(id, _)| *id).collect();
        let total: usize = needed.iter().map(|(_, p)| p.bytes()).sum();
        if total > LABEL_BUDGET {
            return Err(format!("label working set {total} bytes exceeds the label budget {LABEL_BUDGET}"));
        }
        let missing: usize = needed.iter().filter(|(id, _)| !self.textures.contains_key(id)).map(|(_, p)| p.bytes()).sum();
        while self.bytes + missing > LABEL_BUDGET {
            let Some((&t, &victim)) = self.order.iter().find(|(_, id)| !keep.contains(id)) else { break };
            self.order.remove(&t);
            if let Some((tex, b, _)) = self.textures.remove(&victim) {
                unsafe { gl.delete_texture(tex) };
                self.bytes -= b;
                self.evictions += 1;
            }
        }
        for (id, plane) in needed {
            self.tick += 1;
            if let Some(entry) = self.textures.get_mut(id) {
                self.order.remove(&entry.2);
                entry.2 = self.tick;
                self.order.insert(self.tick, *id);
                continue;
            }
            let tex = unsafe { upload(gl, plane)? };
            self.textures.insert(*id, (tex, plane.bytes(), self.tick));
            self.order.insert(self.tick, *id);
            self.bytes += plane.bytes();
            self.uploads += 1;
        }
        Ok(())
    }

    pub unsafe fn destroy(&mut self, gl: &glow::Context) {
        unsafe {
            self.drop_targets(gl);
            for (_, (tex, _, _)) in self.textures.drain() {
                gl.delete_texture(tex);
            }
            gl.delete_program(self.ids_prog);
            gl.delete_program(self.draw_prog);
        }
        self.order.clear();
        self.bytes = 0;
    }
}

unsafe fn upload(gl: &glow::Context, plane: &Plane) -> Result<glow::Texture, String> {
    unsafe {
        let tex = gl.create_texture()?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        for (p, v) in [(glow::TEXTURE_MIN_FILTER, glow::NEAREST), (glow::TEXTURE_MAG_FILTER, glow::NEAREST),
                       (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE)] {
            gl.tex_parameter_i32(glow::TEXTURE_2D, p, v as i32);
        }
        let bytes = std::slice::from_raw_parts(plane.ids.as_ptr() as *const u8, plane.ids.len() * 4);
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::R32UI as i32, plane.cols as i32, plane.rows as i32, 0,
                        glow::RED_INTEGER, glow::UNSIGNED_INT, glow::PixelUnpackData::Slice(Some(bytes)));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.bind_texture(glow::TEXTURE_2D, None);
        Ok(tex)
    }
}

impl Render {
    pub fn label_gpu(&self) -> Option<&LabelGpu> {
        self.labels.as_ref()
    }

    /// `_compose_labels` over the `final` just composed for `view` and
    /// `clip`. Without a visible layer `shown` is not drawn (`final` is the
    /// picture). Returns the passes drawn.
    pub unsafe fn compose_labels(&mut self, gl: &glow::Context, labels: &mut Labels, view: [f32; 4],
                                 clip: &Clip) -> Result<u64, String> {
        let fin = self.fin.as_ref().map(|t| (t.fbo, t.stencil));
        let Some((fin_fbo, stencil)) = fin.filter(|_| !labels.layers.is_empty()) else {
            labels.shown = false;
            return Ok(0);
        };
        if self.labels.is_none() {
            self.labels = Some(unsafe { LabelGpu::new(gl)? });
        }
        let ids: Vec<u64> = labels.layers.iter().flat_map(|l| l.planes.iter().map(|p| p.0)).collect();
        let mut needed = Vec::with_capacity(ids.len());
        for id in ids {
            let plane = labels.plane(id).ok_or_else(|| format!("label plane {id} was never handed over"))?;
            needed.push((id, plane));
        }
        let (w, h) = self.size;
        let lg = self.labels.as_mut().unwrap();
        unsafe {
            lg.prepare(gl, &needed)?;
            lg.ensure_targets(gl, (w, h), stencil)?;
        }
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
            return Ok(0);                       // the region is off screen: the picture is the answer
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
                    gl.active_texture(glow::TEXTURE0);
                    gl.bind_texture(glow::TEXTURE_2D, tex_of.get(id).copied());
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
        Ok(passes)
    }

    /// The masks' GPU names that `release_to_coarse` drops: the targets
    /// (the textures stay, as `Step1GpuLayer.release_to_coarse` keeps them).
    pub unsafe fn drop_label_targets(&mut self, gl: &glow::Context) {
        if let Some(lg) = self.labels.as_mut() {
            unsafe { lg.drop_targets(gl) };
        }
    }
}
