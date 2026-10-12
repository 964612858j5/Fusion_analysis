//! The final pass (`Step1GpuLayer._finalize`): the mode's final program
//! from `accum` into `final`, cut to the region rectangle and polygon.

use super::{Clip, Render};
use glow::HasContext;

impl Render {
    /// `_finalize`: with a region, `final` is cleared (colour and stencil)
    /// with no clip, then the polygon stamped and the rectangle set, and
    /// the final pass drawn inside them; a region wholly off screen leaves
    /// the cleared target. Every clip state is restored.
    pub(super) unsafe fn finalize(&mut self, gl: &glow::Context, fin: glow::Framebuffer, input: glow::Texture,
                       view: [f32; 4], fusion: bool, clip: &Clip) {
        let (w, h) = self.size;
        let clipped = clip.scissor.is_some() || clip.polygon.is_some();
        let mut scissor_box = [0i32; 4];
        unsafe {
            if clipped {
                gl.get_parameter_i32_slice(glow::SCISSOR_BOX, &mut scissor_box);   // one query, clipped frames only
            }
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fin));
            gl.viewport(0, 0, w, h);
            gl.disable(glow::BLEND);
            if clipped {
                gl.disable(glow::SCISSOR_TEST);
                gl.disable(glow::STENCIL_TEST);
                gl.stencil_mask(0xFF);
                gl.clear_color(0.0, 0.0, 0.0, 0.0);
                gl.clear_stencil(0);
                gl.clear(glow::COLOR_BUFFER_BIT | glow::STENCIL_BUFFER_BIT);
                if clip.scissor.is_some_and(|s| s[2] <= 0 || s[3] <= 0) {
                    crate::stencil::restore(gl, scissor_box);
                    return;
                }
                match &clip.polygon {
                    Some(points) => crate::stencil::write_polygon(gl, self.progs.mask, self.progs.mask_vao,
                                                                  self.progs.mask_vbo, points, view),
                    None => gl.stencil_mask(0x00),
                }
                if let Some(s) = clip.scissor {
                    gl.enable(glow::SCISSOR_TEST);
                    gl.scissor(s[0], s[1], s[2], s[3]);
                }
            }
            let fp = if fusion { self.progs.final_fusion } else { self.progs.final_overlay };
            gl.use_program(Some(fp));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(input));
            let l = self.uloc(gl, fp, "u_input");
            gl.uniform_1_i32(l.as_ref(), 0);
            gl.bind_vertex_array(Some(self.vao));
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.bind_vertex_array(None);
            gl.use_program(None);
            if clipped {
                crate::stencil::restore(gl, scissor_box);
            }
        }
    }
}
