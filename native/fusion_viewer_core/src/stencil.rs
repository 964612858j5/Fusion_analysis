//! The ROI polygon as a stencil (`Step1GpuLayer._write_polygon_stencil`):
//! a triangle fan over the polygon's points with GL_INVERT on the low
//! stencil bit leaves bit 0 set exactly inside it -- the even-odd rule,
//! concave or not, either winding. The colour mask is off while it is
//! drawn; afterwards only pixels with bit 0 pass. `restore` hands every
//! clip state back as `_restore_clip_state` does, and the scissor box too.

use glow::HasContext;

/// Stamp `points` (level-0 world x, y) for the view `view` (x0, x1, y0, y1)
/// into the bound framebuffer's stencil, then test against it.
pub unsafe fn write_polygon(gl: &glow::Context, program: glow::Program, vao: glow::VertexArray,
                            vbo: glow::Buffer, points: &[[f32; 2]], view: [f32; 4]) {
    let data: Vec<u8> = points.iter().flat_map(|p| p.iter().flat_map(|v| v.to_ne_bytes())).collect();
    unsafe {
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, &data, glow::DYNAMIC_DRAW);
        gl.bind_buffer(glow::ARRAY_BUFFER, None);
        gl.enable(glow::STENCIL_TEST);
        gl.stencil_mask(0x01);
        gl.stencil_func(glow::ALWAYS, 0, 0x01);
        gl.stencil_op(glow::KEEP, glow::KEEP, glow::INVERT);
        gl.color_mask(false, false, false, false);
        gl.disable(glow::BLEND);
        gl.use_program(Some(program));
        let l = gl.get_uniform_location(program, "u_view_rect");
        gl.uniform_4_f32(l.as_ref(), view[0], view[1], view[2], view[3]);
        gl.bind_vertex_array(Some(vao));
        gl.draw_arrays(glow::TRIANGLE_FAN, 0, points.len() as i32);
        gl.bind_vertex_array(None);
        gl.use_program(None);
        gl.color_mask(true, true, true, true);
        gl.stencil_mask(0x00);
        gl.stencil_func(glow::EQUAL, 0x01, 0x01);
        gl.stencil_op(glow::KEEP, glow::KEEP, glow::KEEP);
    }
}

/// Every clip state back to the GL default (all-ones stencil masks, as a
/// fresh context has them; `scissor_box` as it was before the clip).
pub unsafe fn restore(gl: &glow::Context, scissor_box: [i32; 4]) {
    unsafe {
        gl.disable(glow::SCISSOR_TEST);
        gl.scissor(scissor_box[0], scissor_box[1], scissor_box[2], scissor_box[3]);
        gl.disable(glow::STENCIL_TEST);
        gl.stencil_mask(!0);
        gl.stencil_func(glow::ALWAYS, 0, !0);
        gl.stencil_op(glow::KEEP, glow::KEEP, glow::KEEP);
        gl.color_mask(true, true, true, true);
    }
}
