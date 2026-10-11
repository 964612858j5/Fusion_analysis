//! Diagnostic: where does creating a GPU block cost its time? Times each
//! stage of `gpu_store::new_block` separately (docs §47.4: the 100-150 ms
//! GUI stall when the store grows). No product code calls it.

use crate::gpu_store::create_block_texture;
use crate::geom::TILE;
use glow::HasContext;
use std::time::Instant;

/// How a new block's first use is paid in the probe (Touch = the store's
/// `gpu_touch`; the sample-only and attach-only steps measured on the way
/// are in docs §48).
#[derive(Clone, Copy, PartialEq)]
pub enum Warm { None, Old, Touch }

/// One block's stages, ms: [drain queued work (glFinish before), allocate
/// (the GL call), its glFinish, warm, then three real uses, each a full
/// tile upload into a layer + a draw sampling that layer + glFinish:
/// layer 0, the middle layer, the last layer; then the same into the
/// middle layer again, and into a fresh layer after it].
pub type Stages = [f64; 9];

const VS: &str = "#version 330 core\nvoid main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);\n  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }\n";
const FS: &str = "#version 330 core\nuniform usampler2DArray u_t; uniform int u_layer; out vec4 o;\nvoid main() { ivec2 a = ivec2(gl_FragCoord.xy) * 8;\n  o = vec4(float(texelFetch(u_t, ivec3(a, u_layer), 0).r) / 255.0); }\n";

/// A 64x64 RGBA8 target and the sampling program.
struct Sampler {
    program: glow::Program,
    vao: glow::VertexArray,
    fbo: glow::Framebuffer,
    color: glow::Texture,
}

unsafe fn sampler(gl: &glow::Context) -> Result<Sampler, String> {
    unsafe {
        let program = crate::shader::link(gl, VS, FS)?;
        let vao = gl.create_vertex_array()?;
        let color = gl.create_texture()?;
        gl.bind_texture(glow::TEXTURE_2D, Some(color));
        gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA8 as i32, 64, 64, 0, glow::RGBA,
                        glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(None));
        gl.bind_texture(glow::TEXTURE_2D, None);
        let fbo = gl.create_framebuffer()?;
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
        gl.framebuffer_texture_2d(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(color), 0);
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        Ok(Sampler { program, vao, fbo, color })
    }
}

unsafe fn draw(gl: &glow::Context, s: &Sampler, tex: glow::Texture, layer: i32) {
    unsafe {
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(s.fbo));
        gl.viewport(0, 0, 64, 64);
        gl.use_program(Some(s.program));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
        gl.uniform_1_i32(gl.get_uniform_location(s.program, "u_t").as_ref(), 0);
        gl.uniform_1_i32(gl.get_uniform_location(s.program, "u_layer").as_ref(), layer);
        gl.bind_vertex_array(Some(s.vao));
        gl.draw_arrays(glow::TRIANGLES, 0, 3);
        gl.bind_vertex_array(None);
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
        gl.use_program(None);
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.finish();
    }
}

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1000.0
}

/// Create `count` blocks of `layers` layers one after another (kept alive
/// until all are made, as the store would), allocated with
/// glTexStorage3D (`storage`) or glTexImage3D, first use paid by `warm`.
pub unsafe fn run(gl: &glow::Context, layers: u32, storage: bool, warm: Warm, count: usize)
                  -> Vec<Stages> {
    let tile = vec![7u8; TILE * TILE];
    let mut made = Vec::new();
    let mut out = Vec::new();
    unsafe {
        let Ok(probe) = sampler(gl) else { return out };
        let Ok(kit) = crate::gpu_touch::TouchKit::new(gl) else { return out };
        for _ in 0..count {
            let mut s = [0.0; 9];
            let t = Instant::now();
            gl.finish();
            s[0] = ms(t);
            let t = Instant::now();
            let Ok(tex) = create_block_texture(gl, layers, storage) else { break };
            s[1] = ms(t);
            let t = Instant::now();
            gl.finish();
            s[2] = ms(t);
            let t = Instant::now();
            match warm {
                Warm::Old => crate::gpu_warm::warm(gl, tex, layers),
                Warm::Touch => { kit.touch(gl, tex, layers); }
                Warm::None => {}
            }
            s[3] = ms(t);
            let mid = layers as i32 / 2;
            for (k, layer) in [0, mid, layers as i32 - 1, mid, mid + 1].into_iter().enumerate() {
                let t = Instant::now();
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
                gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, layer, TILE as i32, TILE as i32, 1,
                                    glow::RED_INTEGER, glow::UNSIGNED_BYTE,
                                    glow::PixelUnpackData::Slice(Some(&tile)));
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
                draw(gl, &probe, tex, layer);
                s[4 + k] = ms(t);
            }
            made.push(tex);
            out.push(s);
        }
        for tex in made {
            gl.delete_texture(tex);
        }
        kit.destroy(gl);
        gl.delete_program(probe.program);
        gl.delete_vertex_array(probe.vao);
        gl.delete_framebuffer(probe.fbo);
        gl.delete_texture(probe.color);
        gl.finish();
        while gl.get_error() != glow::NO_ERROR {}
    }
    out
}
