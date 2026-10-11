//! GL drawing in Qt's context: the single-pass virtual texture into an
//! RGBA32F `accum`, Overlay's final pass into RGBA8 `final` (clipped to
//! the region rectangle when there is one), and a NEAREST blit of `final`
//! to Qt's framebuffer -- `Step1GpuLayer._render_vt`, `_finalize`,
//! `paintGL`. Every state touched is put back to the GL default.

use crate::gpu_store::Store;
use crate::shader;
use crate::vt::{Vt, MAX_LEVELS};
use glow::HasContext;
use std::collections::HashMap;

mod upload;

struct Target {
    fbo: glow::Framebuffer,
    tex: glow::Texture,
}

pub struct Render {
    vt_prog: glow::Program,
    final_prog: glow::Program,
    vao: glow::VertexArray,
    pub u_blocks: usize,
    pub f_blocks: usize,
    accum: Option<Target>,
    fin: Option<Target>,
    pub size: (i32, i32),
    /// rows the last composed frame drew (kept while the base overflows)
    pub last_rows: usize,
    pages: Option<glow::Texture>,
    pages_shape: (usize, usize, usize),
    meta: Option<glow::Texture>,
    params: Option<glow::Texture>,
    params_rows: usize,
    params_last: Vec<[f32; 4]>,
    dummy_u: glow::Texture,
    dummy_f: glow::Texture,
    loc: HashMap<String, glow::UniformLocation>,
}

pub(crate) fn f4(v: &[[f32; 4]]) -> &[u8] {
    unsafe { std::slice::from_raw_parts(v.as_ptr() as *const u8, std::mem::size_of_val(v)) }
}

impl Render {
    pub unsafe fn new(gl: &glow::Context) -> Result<Render, String> {
        unsafe {
            let units = gl.get_parameter_i32(glow::MAX_TEXTURE_IMAGE_UNITS);
            let (u, f) = shader::sampler_split(units);
            let vt_prog = shader::link(gl, shader::VERT, &shader::vt_source(u, f))?;
            let final_prog = match shader::link(gl, shader::VERT, &shader::with_define("PASS_FINAL_OVERLAY")) {
                Ok(p) => p,
                Err(e) => { gl.delete_program(vt_prog); return Err(e); }
            };
            let vao = gl.create_vertex_array()?;
            let dummy = |internal: u32, fmt: u32, typ: u32, bytes: &[u8]| -> Result<glow::Texture, String> {
                let t = gl.create_texture()?;
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(t));
                gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
                gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
                gl.tex_image_3d(glow::TEXTURE_2D_ARRAY, 0, internal as i32, 1, 1, 1, 0, fmt, typ,
                                glow::PixelUnpackData::Slice(Some(bytes)));
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
                Ok(t)
            };
            let dummy_u = dummy(glow::R8UI, glow::RED_INTEGER, glow::UNSIGNED_BYTE, &[0])?;
            let dummy_f = dummy(glow::R32F, glow::RED, glow::FLOAT, &[0, 0, 0, 0])?;
            Ok(Render { vt_prog, final_prog, vao, u_blocks: u, f_blocks: f, accum: None, fin: None,
                        size: (0, 0), last_rows: 0, pages: None, pages_shape: (0, 0, 0), meta: None, params: None,
                        params_rows: 0, params_last: Vec::new(), dummy_u, dummy_f, loc: HashMap::new() })
        }
    }

    unsafe fn target(gl: &glow::Context, internal: u32, fmt: u32, typ: u32, w: i32, h: i32) -> Result<Target, String> {
        unsafe {
            let tex = gl.create_texture()?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
            gl.tex_image_2d(glow::TEXTURE_2D, 0, internal as i32, w, h, 0, fmt, typ, glow::PixelUnpackData::Slice(None));
            gl.bind_texture(glow::TEXTURE_2D, None);
            let fbo = gl.create_framebuffer()?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(tex), 0);
            let ok = gl.check_framebuffer_status(glow::FRAMEBUFFER) == glow::FRAMEBUFFER_COMPLETE;
            if !ok {
                return Err("composition framebuffer incomplete".into());
            }
            Ok(Target { fbo, tex })
        }
    }

    /// (Re)allocate `accum` and `final` at the device size.
    pub unsafe fn ensure_targets(&mut self, gl: &glow::Context, w: i32, h: i32) -> Result<(), String> {
        if (w, h) == self.size && self.fin.is_some() {
            return Ok(());
        }
        unsafe {
            self.drop_targets(gl);
            self.accum = Some(Self::target(gl, glow::RGBA32F, glow::RGBA, glow::FLOAT, w, h)?);
            self.fin = Some(Self::target(gl, glow::RGBA8, glow::RGBA, glow::UNSIGNED_BYTE, w, h)?);
        }
        self.size = (w, h);
        Ok(())
    }

    /// Delete the composition targets (rebuilt by the next paint).
    pub unsafe fn drop_targets(&mut self, gl: &glow::Context) {
        for t in [self.accum.take(), self.fin.take()].into_iter().flatten() {
            unsafe {
                gl.delete_framebuffer(t.fbo);
                gl.delete_texture(t.tex);
            }
        }
        self.size = (0, 0);
    }

    pub fn target_bytes(&self) -> usize {
        (self.size.0.max(0) * self.size.1.max(0)) as usize * (16 + 4)
    }

    fn uloc(&mut self, gl: &glow::Context, prog: glow::Program, name: &str) -> Option<glow::UniformLocation> {
        let key = format!("{}:{name}", prog.0);
        if let Some(l) = self.loc.get(&key) {
            return Some(l.clone());
        }
        let l = unsafe { gl.get_uniform_location(prog, name) }?;
        self.loc.insert(key, l.clone());
        Some(l)
    }

    /// The single pass into `accum`, then the final pass into `final`.
    /// `view`: world rect (x0, x1, y0, y1); `scissor`: the region's pixels.
    pub unsafe fn compose(&mut self, gl: &glow::Context, vt: &Vt, store: &Store, rows: usize,
                          view: [f32; 4], smooth: bool, scissor: Option<[i32; 4]>) {
        let (Some(accum), Some(fin)) = (self.accum.as_ref().map(|t| (t.fbo, t.tex)),
                                        self.fin.as_ref().map(|t| t.fbo)) else { return };
        let (w, h) = self.size;
        let p = self.vt_prog;
        let (la, lb) = vt.level_uniforms();
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(accum.0));
            gl.viewport(0, 0, w, h);
            gl.disable(glow::BLEND);
            gl.use_program(Some(p));
            let l = self.uloc(gl, p, "u_view_rect");
            gl.uniform_4_f32(l.as_ref(), view[0], view[1], view[2], view[3]);
            let l = self.uloc(gl, p, "u_target_size");
            gl.uniform_2_f32(l.as_ref(), w as f32, h as f32);
            let l = self.uloc(gl, p, "u_rows");
            gl.uniform_1_i32(l.as_ref(), rows as i32);
            let l = self.uloc(gl, p, "u_fusion");
            gl.uniform_1_i32(l.as_ref(), 0);
            let l = self.uloc(gl, p, "u_smooth");
            gl.uniform_1_i32(l.as_ref(), smooth as i32);
            let l = self.uloc(gl, p, "u_level_a");
            gl.uniform_4_f32_slice(l.as_ref(), &la[..MAX_LEVELS * 4]);
            let l = self.uloc(gl, p, "u_level_b");
            gl.uniform_2_i32_slice(l.as_ref(), &lb[..MAX_LEVELS * 2]);
            let mut units: Vec<(String, u32, glow::Texture)> = Vec::new();
            for i in 0..self.u_blocks {
                let t = store.texture(i).unwrap_or(self.dummy_u);
                units.push((format!("u_u{i}"), glow::TEXTURE_2D_ARRAY, t));
            }
            for i in 0..self.f_blocks {
                units.push((format!("u_f{i}"), glow::TEXTURE_2D_ARRAY, self.dummy_f));
            }
            for (name, kind, t) in [("u_pages", glow::TEXTURE_2D_ARRAY, self.pages),
                                    ("u_meta", glow::TEXTURE_2D, self.meta),
                                    ("u_params", glow::TEXTURE_2D, self.params)] {
                if let Some(t) = t {
                    units.push((name.to_string(), kind, t));
                }
            }
            for (unit, (name, kind, t)) in units.iter().enumerate() {
                gl.active_texture(glow::TEXTURE0 + unit as u32);
                gl.bind_texture(*kind, Some(*t));
                let l = self.uloc(gl, p, name);
                gl.uniform_1_i32(l.as_ref(), unit as i32);
            }
            gl.bind_vertex_array(Some(self.vao));
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
            for (unit, (_n, kind, _t)) in units.iter().enumerate() {
                gl.active_texture(glow::TEXTURE0 + unit as u32);
                gl.bind_texture(*kind, None);
            }
            gl.active_texture(glow::TEXTURE0);
            // final: Overlay's clamp + opaque alpha, cut to the region
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fin));
            gl.viewport(0, 0, w, h);
            if let Some(s) = scissor {
                gl.clear_color(0.0, 0.0, 0.0, 0.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                if s[2] <= 0 || s[3] <= 0 {
                    gl.bind_vertex_array(None);
                    gl.use_program(None);
                    return;
                }
                gl.enable(glow::SCISSOR_TEST);
                gl.scissor(s[0], s[1], s[2], s[3]);
            }
            let fp = self.final_prog;
            gl.use_program(Some(fp));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(accum.1));
            let l = self.uloc(gl, fp, "u_input");
            gl.uniform_1_i32(l.as_ref(), 0);
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.disable(glow::SCISSOR_TEST);
            gl.bind_vertex_array(None);
            gl.use_program(None);
        }
    }

    /// `final` to Qt's framebuffer at device size (NEAREST), leaving the
    /// draw and read framebuffers bound to `fbo`.
    pub unsafe fn blit(&self, gl: &glow::Context, fbo: u32, w: i32, h: i32) {
        let Some(fin) = &self.fin else { return };
        let target = std::num::NonZeroU32::new(fbo).map(glow::NativeFramebuffer);
        unsafe {
            gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(fin.fbo));
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, target);
            gl.blit_framebuffer(0, 0, self.size.0, self.size.1, 0, 0, w, h,
                                glow::COLOR_BUFFER_BIT, glow::NEAREST);
            gl.bind_framebuffer(glow::FRAMEBUFFER, target);
        }
    }

    /// `final` as RGBA8 rows, top row first (`readback_rgba_for_test`).
    pub unsafe fn readback(&self, gl: &glow::Context) -> Vec<u8> {
        let Some(fin) = &self.fin else { return Vec::new() };
        let (w, h) = (self.size.0 as usize, self.size.1 as usize);
        let mut px = vec![0u8; w * h * 4];
        unsafe {
            gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(fin.fbo));
            gl.pixel_store_i32(glow::PACK_ALIGNMENT, 1);
            gl.read_pixels(0, 0, w as i32, h as i32, glow::RGBA, glow::UNSIGNED_BYTE,
                           glow::PixelPackData::Slice(Some(&mut px)));
            gl.pixel_store_i32(glow::PACK_ALIGNMENT, 4);
            gl.bind_framebuffer(glow::READ_FRAMEBUFFER, None);
        }
        let mut out = vec![0u8; px.len()];
        for y in 0..h {
            out[y * w * 4..(y + 1) * w * 4].copy_from_slice(&px[(h - 1 - y) * w * 4..(h - y) * w * 4]);
        }
        out
    }

    /// Delete every name (context current).
    pub unsafe fn destroy(&mut self, gl: &glow::Context) {
        unsafe {
            self.drop_targets(gl);
            for t in [self.pages.take(), self.meta.take(), self.params.take()].into_iter().flatten() {
                gl.delete_texture(t);
            }
            gl.delete_texture(self.dummy_u);
            gl.delete_texture(self.dummy_f);
            gl.delete_vertex_array(self.vao);
            gl.delete_program(self.vt_prog);
            gl.delete_program(self.final_prog);
        }
    }
}
