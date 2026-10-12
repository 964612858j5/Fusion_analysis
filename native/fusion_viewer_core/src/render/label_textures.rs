//! Step3's label textures and targets (`_LabelTextureLru`, R3 / R3b): one
//! R32UI texture per plane in the label budget, uploaded within a paint's
//! time when asked (the target level first), textures of released planes
//! reused for the next of their size; the `ids` (R32UI) and `shown` (RGBA8,
//! on `final`'s stencil) targets; the two label programs, made the first
//! time masks are drawn.

use super::{Render, Target};
use crate::label_store::{Plane, LABEL_BUDGET};
use glow::HasContext;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

pub const LABELS_FRAG: &str = include_str!("../../shaders/step1_gpu_labels.frag");
/// free label textures kept for reuse (R3b): enough for a screen's tiles
const FREE_TEXTURES: usize = 64;

pub struct LabelGpu {
    pub(super) ids_prog: glow::Program,
    pub(super) draw_prog: glow::Program,
    pub(super) ids: Option<Target>,
    pub(super) shown: Option<Target>,
    /// the size and `final` stencil the two targets were made for
    made_for: ((i32, i32), Option<glow::Renderbuffer>),
    pub(super) textures: HashMap<u64, (glow::Texture, usize, u64)>,
    order: BTreeMap<u64, u64>,
    /// textures of released or evicted planes, kept for the next plane of
    /// the same (cols, rows): an upload into them allocates nothing (R3b)
    free: Vec<(usize, usize, glow::Texture)>,
    pub free_bytes: usize,
    /// textures released without a context, deleted by the next paint
    orphans: Vec<glow::Texture>,
    pub orphan_bytes: usize,
    tick: u64,
    pub bytes: usize,
    pub uploads: u64,
    pub evictions: u64,
}

impl LabelGpu {
    pub(super) unsafe fn new(gl: &glow::Context) -> Result<LabelGpu, String> {
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
                      textures: HashMap::new(), order: BTreeMap::new(), free: Vec::new(), free_bytes: 0, orphans: Vec::new(), orphan_bytes: 0,
                      tick: 0, bytes: 0, uploads: 0, evictions: 0 })
    }

    pub fn target_bytes(&self) -> usize {
        let (w, h) = self.made_for.0;
        let targets = if self.ids.is_some() { (w.max(0) * h.max(0)) as usize * (4 + 4) } else { 0 };
        targets + self.free_bytes + self.orphan_bytes
    }

    pub(super) unsafe fn drop_targets(&mut self, gl: &glow::Context) {
        for t in [self.ids.take(), self.shown.take()].into_iter().flatten() {
            unsafe {
                gl.delete_framebuffer(t.fbo);
                gl.delete_texture(t.tex);
            }
        }
        self.made_for = ((0, 0), None);
    }

    pub(super) unsafe fn ensure_targets(&mut self, gl: &glow::Context, size: (i32, i32),
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

    /// Plane `id` is no longer held by the core's planner: its texture is
    /// free for the next plane of its size (R3b).
    pub fn release(&mut self, id: u64, cols: usize, rows: usize) {
        if let Some((tex, b, t)) = self.textures.remove(&id) {
            self.order.remove(&t);
            self.bytes -= b;
            if self.free.len() < FREE_TEXTURES {
                self.free.push((cols, rows, tex));
                self.free_bytes += b;
            } else {
                self.orphans.push(tex);
                self.orphan_bytes += b;
            }
        }
    }

    /// Delete what was released without a context (context current).
    pub(super) unsafe fn flush_orphans(&mut self, gl: &glow::Context) {
        for tex in self.orphans.drain(..) {
            unsafe { gl.delete_texture(tex) };
        }
        self.orphan_bytes = 0;
    }

    /// Every plane of `needed` on the GPU; others leave, least recently
    /// used first, while over the budget (`_LabelTextureLru.prepare`).
    /// With `budget`, uploading stops once that time has passed (one upload
    /// at least); `needed` comes in upload order (the caller puts the target
    /// level first): the planes left out are counted, drawn by a later paint.
    pub(super) unsafe fn prepare(&mut self, gl: &glow::Context, needed: &[(u64, Arc<Plane>)],
                      budget: Option<std::time::Duration>) -> Result<usize, String> {
        unsafe { self.flush_orphans(gl) };
        let keep: HashSet<u64> = needed.iter().map(|(id, _)| *id).collect();
        let total: usize = needed.iter().map(|(_, p)| p.bytes()).sum();
        if total > LABEL_BUDGET {
            return Err(format!("label working set {total} bytes exceeds the label budget {LABEL_BUDGET}"));
        }
        let missing: usize = needed.iter().filter(|(id, _)| !self.textures.contains_key(id)).map(|(_, p)| p.bytes()).sum();
        // free textures count against the budget too: they go first
        while self.bytes + self.free_bytes + missing > LABEL_BUDGET {
            let Some((cols, rows, tex)) = self.free.pop() else { break };
            unsafe { gl.delete_texture(tex) };
            self.free_bytes -= cols * rows * 4;
        }
        while self.bytes + missing > LABEL_BUDGET {
            let Some((&t, &victim)) = self.order.iter().find(|(_, id)| !keep.contains(id)) else { break };
            self.order.remove(&t);
            if let Some((tex, b, _)) = self.textures.remove(&victim) {
                unsafe { gl.delete_texture(tex) };
                self.bytes -= b;
                self.evictions += 1;
            }
        }
        let started = std::time::Instant::now();
        let mut deferred = 0;
        let mut uploaded = 0;
        for (id, plane) in needed {
            self.tick += 1;
            if let Some(entry) = self.textures.get_mut(id) {
                self.order.remove(&entry.2);
                entry.2 = self.tick;
                self.order.insert(self.tick, *id);
                continue;
            }
            if uploaded > 0 && budget.is_some_and(|b| started.elapsed() >= b) {
                deferred += 1;
                continue;
            }
            let reuse = self.free.iter().position(|f| f.0 == plane.cols && f.1 == plane.rows)
                .map(|i| self.free.swap_remove(i).2);
            if reuse.is_some() {
                self.free_bytes -= plane.bytes();
            }
            let tex = unsafe { upload(gl, plane, reuse)? };
            self.textures.insert(*id, (tex, plane.bytes(), self.tick));
            self.order.insert(self.tick, *id);
            self.bytes += plane.bytes();
            self.uploads += 1;
            uploaded += 1;
        }
        Ok(deferred)
    }

    pub unsafe fn destroy(&mut self, gl: &glow::Context) {
        unsafe {
            self.drop_targets(gl);
            for (_, (tex, _, _)) in self.textures.drain() {
                gl.delete_texture(tex);
            }
            for (_, _, tex) in self.free.drain(..) {
                gl.delete_texture(tex);
            }
            for tex in self.orphans.drain(..) {
                gl.delete_texture(tex);
            }
            gl.delete_program(self.ids_prog);
            gl.delete_program(self.draw_prog);
        }
        self.order.clear();
        self.bytes = 0;
    }
}

/// `plane` into `reuse` (a free texture of its size: no allocation) or a
/// new texture.
unsafe fn upload(gl: &glow::Context, plane: &Plane, reuse: Option<glow::Texture>) -> Result<glow::Texture, String> {
    unsafe {
        let bytes = std::slice::from_raw_parts(plane.ids.as_ptr() as *const u8, plane.ids.len() * 4);
        if let Some(tex) = reuse {
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_sub_image_2d(glow::TEXTURE_2D, 0, 0, 0, plane.cols as i32, plane.rows as i32,
                                glow::RED_INTEGER, glow::UNSIGNED_INT, glow::PixelUnpackData::Slice(Some(bytes)));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            gl.bind_texture(glow::TEXTURE_2D, None);
            return Ok(tex);
        }
        let tex = gl.create_texture()?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        for (p, v) in [(glow::TEXTURE_MIN_FILTER, glow::NEAREST), (glow::TEXTURE_MAG_FILTER, glow::NEAREST),
                       (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE)] {
            gl.tex_parameter_i32(glow::TEXTURE_2D, p, v as i32);
        }
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::R32UI as i32, plane.cols as i32, plane.rows as i32, 0,
                        glow::RED_INTEGER, glow::UNSIGNED_INT, glow::PixelUnpackData::Slice(Some(bytes)));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.bind_texture(glow::TEXTURE_2D, None);
        Ok(tex)
    }
}

