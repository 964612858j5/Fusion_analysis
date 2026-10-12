//! What leaves the composition: `final` blitted to Qt's framebuffer, and
//! read back for tests.

use super::Render;
use glow::HasContext;

impl Render {
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
}
