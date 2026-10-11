//! A new GPU block's first-use cost, paid when it is created
//! (`_TileArrayStore._warm`): measured 22-70 ms otherwise, mid-gesture.

use glow::HasContext;

/// Pay a new block's first-use cost now (`_TileArrayStore._warm`): clear
/// one layer, write a texel, blit it on the GPU, finish, write again.
pub unsafe fn warm(gl: &glow::Context, tex: glow::Texture, layers: u32) {
    unsafe {
        let (Ok(read), Ok(draw), Ok(rb)) = (gl.create_framebuffer(), gl.create_framebuffer(),
                                            gl.create_renderbuffer()) else { return };
        let last = layers as i32 - 1;
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(read));
        gl.framebuffer_texture_layer(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, Some(tex), 0, last);
        gl.clear_buffer_u32_slice(glow::COLOR, 0, &[0, 0, 0, 0]);
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        let zero = [0u8];
        gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, last, 1, 1, 1, glow::RED_INTEGER,
                            glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(Some(&zero)));
        gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
        gl.renderbuffer_storage(glow::RENDERBUFFER, glow::R8UI, 1, 1);
        gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(draw));
        gl.framebuffer_renderbuffer(glow::DRAW_FRAMEBUFFER, glow::COLOR_ATTACHMENT0,
                                    glow::RENDERBUFFER, Some(rb));
        gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(read));
        gl.blit_framebuffer(0, 0, 1, 1, 0, 0, 1, 1, glow::COLOR_BUFFER_BIT, glow::NEAREST);
        gl.finish();
        gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, last, 1, 1, 1, glow::RED_INTEGER,
                            glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(Some(&zero)));
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.bind_renderbuffer(glow::RENDERBUFFER, None);
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.delete_framebuffer(read);
        gl.delete_framebuffer(draw);
        gl.delete_renderbuffer(rb);
        while gl.get_error() != glow::NO_ERROR {}
    }
}
