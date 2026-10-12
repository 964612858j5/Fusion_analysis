//! GL drawing in Qt's context: the single-pass virtual texture into an
//! RGBA32F `accum` (Overlay or Fusion), the final pass for the mode into
//! RGBA8 `final` (clipped to the region rectangle and, when there is one,
//! the region polygon through `final`'s stencil), and a NEAREST blit of
//! `final` to Qt's framebuffer -- `Step1GpuLayer._render_vt`, `_finalize`,
//! `paintGL`. Every state touched is put back to the GL default.

use crate::gpu_store::Store;
use crate::shader;
use crate::vt::{Vt, MAX_LEVELS};
use glow::HasContext;
use std::collections::HashMap;

mod finalize;
mod output;
mod programs;
mod upload;

/// How the final pass is cut to the region: the rectangle's pixels and,
/// for a polygon region, its points (level-0 world x, y).
#[derive(Clone, Debug, Default)]
pub struct Clip {
    pub scissor: Option<[i32; 4]>,
    pub polygon: Option<Vec<[f32; 2]>>,
}

struct Target {
    fbo: glow::Framebuffer,
    tex: glow::Texture,
    /// `final` only: its stencil (the polygon mask)
    stencil: Option<glow::Renderbuffer>,
}

pub struct Render {
    progs: programs::Programs,
    vao: glow::VertexArray,
    /// texture units the single pass may use for blocks (the rest hold the
    /// page, meta and parameter tables)
    pub units: usize,
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
            let mut progs = programs::Programs::new(gl)?;
            if let Err(e) = progs.vt(gl, (u, f)) {
                progs.destroy(gl);
                return Err(e);
            }
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
            Ok(Render { progs, vao, units: u + f, u_blocks: u, f_blocks: f, accum: None, fin: None,
                        size: (0, 0), last_rows: 0, pages: None, pages_shape: (0, 0, 0), meta: None, params: None,
                        params_rows: 0, params_last: Vec::new(), dummy_u, dummy_f, loc: HashMap::new() })
        }
    }

    /// Use `split` (integer, float blocks) from now on; its program is
    /// compiled the first time.
    pub unsafe fn set_split(&mut self, gl: &glow::Context, split: (usize, usize)) -> Result<(), String> {
        unsafe { self.progs.vt(gl, split)? };
        (self.u_blocks, self.f_blocks) = split;
        Ok(())
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
            Ok(Target { fbo, tex, stencil: None })
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
            let mut fin = Self::target(gl, glow::RGBA8, glow::RGBA, glow::UNSIGNED_BYTE, w, h)?;
            // the polygon mask lives on `final` only (`_ensure_targets`)
            let bound_rb = gl.get_parameter_i32(glow::RENDERBUFFER_BINDING);   // Qt's, put back
            let rb = gl.create_renderbuffer()?;
            gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
            gl.renderbuffer_storage(glow::RENDERBUFFER, glow::STENCIL_INDEX8, w, h);
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fin.fbo));
            gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::STENCIL_ATTACHMENT, glow::RENDERBUFFER, Some(rb));
            gl.bind_renderbuffer(glow::RENDERBUFFER,
                                 std::num::NonZeroU32::new(bound_rb as u32).map(glow::NativeRenderbuffer));
            fin.stencil = Some(rb);
            let ok = gl.check_framebuffer_status(glow::FRAMEBUFFER) == glow::FRAMEBUFFER_COMPLETE;
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            self.fin = Some(fin);
            if !ok {
                return Err("final framebuffer incomplete with its stencil".into());
            }
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
                if let Some(rb) = t.stencil {
                    gl.delete_renderbuffer(rb);
                }
            }
        }
        self.size = (0, 0);
    }

    pub fn target_bytes(&self) -> usize {
        (self.size.0.max(0) * self.size.1.max(0)) as usize * (16 + 4 + 1)
    }

    pub(super) fn uloc(&mut self, gl: &glow::Context, prog: glow::Program, name: &str) -> Option<glow::UniformLocation> {
        let key = format!("{}:{name}", prog.0);
        if let Some(l) = self.loc.get(&key) {
            return Some(l.clone());
        }
        let l = unsafe { gl.get_uniform_location(prog, name) }?;
        self.loc.insert(key, l.clone());
        Some(l)
    }

    /// The single pass into `accum` (Overlay, or Fusion when `fusion`),
    /// then the mode's final pass into `final`, cut by `clip` (`_finalize`).
    /// `view`: world rect (x0, x1, y0, y1).
    #[allow(clippy::too_many_arguments)]
    pub unsafe fn compose(&mut self, gl: &glow::Context, vt: &Vt, store: &Store, rows: usize,
                          view: [f32; 4], smooth: bool, fusion: bool, clip: &Clip) -> Result<(), String> {
        let (Some(accum), Some(fin)) = (self.accum.as_ref().map(|t| (t.fbo, t.tex)),
                                        self.fin.as_ref().map(|t| t.fbo)) else { return Ok(()) };
        let (w, h) = self.size;
        let p = unsafe { self.progs.vt(gl, (self.u_blocks, self.f_blocks))? };
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
            gl.uniform_1_i32(l.as_ref(), fusion as i32);
            let l = self.uloc(gl, p, "u_smooth");
            gl.uniform_1_i32(l.as_ref(), smooth as i32);
            let l = self.uloc(gl, p, "u_level_a");
            gl.uniform_4_f32_slice(l.as_ref(), &la[..MAX_LEVELS * 4]);
            let l = self.uloc(gl, p, "u_level_b");
            gl.uniform_2_i32_slice(l.as_ref(), &lb[..MAX_LEVELS * 2]);
            let mut units: Vec<(String, u32, glow::Texture)> = Vec::new();
            for i in 0..self.u_blocks {
                let t = store.texture_of(0, i).unwrap_or(self.dummy_u);
                units.push((format!("u_u{i}"), glow::TEXTURE_2D_ARRAY, t));
            }
            for i in 0..self.f_blocks {
                let t = store.texture_of(1, i).unwrap_or(self.dummy_f);
                units.push((format!("u_f{i}"), glow::TEXTURE_2D_ARRAY, t));
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
            gl.bind_vertex_array(None);
            gl.use_program(None);
            self.finalize(gl, fin, accum.1, view, fusion, clip);
        }
        Ok(())
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
            self.progs.destroy(gl);
        }
    }
}
