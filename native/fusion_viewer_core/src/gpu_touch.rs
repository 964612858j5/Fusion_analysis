//! A new GPU block's first-use cost, paid at creation (docs §48). On the
//! server's driver a fresh array texture costs, once: ~0.35 ms per MiB at
//! the first draw that samples it, and ~0.16 ms at the first write of each
//! layer -- unless it was attached to a framebuffer before, which makes
//! every later write as cheap as a rewrite. So the block's last layer is
//! attached and detached (no clear), one texel of layer 0 is initialised
//! and read by a 1x1 draw, and the GPU is waited for: ~55 ms per 64 MiB,
//! against ~75 ms for the old warm (`gpu_warm`: clear, blit, two finishes'
//! worth of work), with the same cheap writes afterwards.
//! Every GL binding touched (and the scissor test, off for the draw) is
//! restored; Qt's framebuffer is not 0.

use glow::HasContext;

const VS: &str = "#version 330 core\nvoid main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);\n  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }\n";
const FS: &str = "#version 330 core\nuniform usampler2DArray u_t; out vec4 o;\nvoid main() { o = vec4(float(texelFetch(u_t, ivec3(0, 0, 0), 0).r) / 255.0); }\n";
/// the same for the float family (R32F blocks)
const FS_F: &str = "#version 330 core\nuniform sampler2DArray u_t; out vec4 o;\nvoid main() { o = vec4(texelFetch(u_t, ivec3(0, 0, 0), 0).r); }\n";

/// The 1x1 target and the program, made once per context.
pub struct TouchKit {
    program: glow::Program,
    /// the float family's program
    program_f: glow::Program,
    vao: glow::VertexArray,
    fbo: glow::Framebuffer,
    color: glow::Texture,
    /// the framebuffer a new block is attached to (and detached from)
    attach: glow::Framebuffer,
}

impl TouchKit {
    pub unsafe fn new(gl: &glow::Context) -> Result<TouchKit, String> {
        unsafe {
            let saved = Saved::take(gl);
            let made = Self::build(gl);
            saved.restore(gl);
            made
        }
    }

    /// Every object made so far is deleted again when a later step fails,
    /// and the 1x1 target must be complete, or the caller falls back.
    unsafe fn build(gl: &glow::Context) -> Result<TouchKit, String> {
        unsafe {
            let program = crate::shader::link(gl, VS, FS)?;
            let program_f = match crate::shader::link(gl, VS, FS_F) {
                Ok(p) => p,
                Err(e) => { gl.delete_program(program); return Err(e); }
            };
            let vao = gl.create_vertex_array();
            let color = gl.create_texture();
            let fbo = gl.create_framebuffer();
            let attach = gl.create_framebuffer();
            let (Ok(vao), Ok(color), Ok(fbo), Ok(attach)) =
                (vao.clone(), color.clone(), fbo.clone(), attach.clone()) else {
                gl.delete_program(program);
                gl.delete_program(program_f);
                if let Ok(v) = vao { gl.delete_vertex_array(v); }
                if let Ok(t) = color { gl.delete_texture(t); }
                if let Ok(f) = fbo { gl.delete_framebuffer(f); }
                if let Ok(f) = attach { gl.delete_framebuffer(f); }
                return Err("the first-use draw could not be built".into());
            };
            let kit = TouchKit { program, program_f, vao, fbo, color, attach };
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(color));
            gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA8 as i32, 1, 1, 0, glow::RGBA,
                            glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(None));
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(fbo));
            gl.framebuffer_texture_2d(glow::DRAW_FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D,
                                      Some(color), 0);
            let complete = gl.check_framebuffer_status(glow::DRAW_FRAMEBUFFER) == glow::FRAMEBUFFER_COMPLETE;
            for p in [program, program_f] {
                gl.use_program(Some(p));
                gl.uniform_1_i32(gl.get_uniform_location(p, "u_t").as_ref(), 0);
            }
            if !complete || gl.get_error() != glow::NO_ERROR {
                while gl.get_error() != glow::NO_ERROR {}
                kit.destroy(gl);
                return Err("the first-use draw's target is not usable".into());
            }
            Ok(kit)
        }
    }

    /// Attach the last of `layers` layers of `tex` to a framebuffer and
    /// detach it, initialise texel (0, 0) of layer 0, sample it once, finish.
    /// Returns what it saw, for the record (errors are then cleared): the
    /// attachment's framebuffer status (complete or not; nothing renders
    /// into it) and the first GL error raised while touching (0: none).
    pub unsafe fn touch(&self, gl: &glow::Context, tex: glow::Texture, layers: u32,
                        format: crate::family::Format) -> (u32, u32) {
        let (_internal, fmt, typ) = format.gl();
        let zero = [0u8; 4];
        unsafe {
            let saved = Saved::take(gl);
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(self.attach));
            gl.framebuffer_texture_layer(glow::DRAW_FRAMEBUFFER, glow::COLOR_ATTACHMENT0, Some(tex), 0,
                                         layers.max(1) as i32 - 1);
            let status = gl.check_framebuffer_status(glow::DRAW_FRAMEBUFFER);
            gl.framebuffer_texture_layer(glow::DRAW_FRAMEBUFFER, glow::COLOR_ATTACHMENT0, None, 0, 0);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, 0, 1, 1, 1, fmt, typ,
                                glow::PixelUnpackData::Slice(Some(&zero[..format.bytes()])));
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(self.fbo));
            gl.viewport(0, 0, 1, 1);
            gl.disable(glow::SCISSOR_TEST);
            gl.use_program(Some(if format.family() == 1 { self.program_f } else { self.program }));
            gl.bind_vertex_array(Some(self.vao));
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
            gl.finish();
            saved.restore(gl);
            let first = gl.get_error();
            while gl.get_error() != glow::NO_ERROR {}
            (status, first)
        }
    }

    pub unsafe fn destroy(self, gl: &glow::Context) {
        unsafe {
            gl.delete_program(self.program);
            gl.delete_program(self.program_f);
            gl.delete_vertex_array(self.vao);
            gl.delete_framebuffer(self.fbo);
            gl.delete_framebuffer(self.attach);
            gl.delete_texture(self.color);
        }
    }
}

/// The bindings `touch` changes, as they were.
struct Saved {
    program: i32,
    vao: i32,
    draw_fbo: i32,
    viewport: [i32; 4],
    unit: i32,
    array0: i32,
    tex2d0: i32,
    unpack: i32,
    scissor: bool,
}

impl Saved {
    unsafe fn take(gl: &glow::Context) -> Saved {
        unsafe {
            let unit = gl.get_parameter_i32(glow::ACTIVE_TEXTURE);
            gl.active_texture(glow::TEXTURE0);
            let mut viewport = [0; 4];
            gl.get_parameter_i32_slice(glow::VIEWPORT, &mut viewport);
            Saved {
                program: gl.get_parameter_i32(glow::CURRENT_PROGRAM),
                vao: gl.get_parameter_i32(glow::VERTEX_ARRAY_BINDING),
                draw_fbo: gl.get_parameter_i32(glow::DRAW_FRAMEBUFFER_BINDING),
                viewport,
                unit,
                array0: gl.get_parameter_i32(glow::TEXTURE_BINDING_2D_ARRAY),
                tex2d0: gl.get_parameter_i32(glow::TEXTURE_BINDING_2D),
                unpack: gl.get_parameter_i32(glow::UNPACK_ALIGNMENT),
                scissor: gl.is_enabled(glow::SCISSOR_TEST),
            }
        }
    }

    unsafe fn restore(&self, gl: &glow::Context) {
        let name = |v: i32| std::num::NonZeroU32::new(v as u32);
        unsafe {
            gl.use_program(name(self.program).map(glow::NativeProgram));
            gl.bind_vertex_array(name(self.vao).map(glow::NativeVertexArray));
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, name(self.draw_fbo).map(glow::NativeFramebuffer));
            gl.viewport(self.viewport[0], self.viewport[1], self.viewport[2], self.viewport[3]);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, name(self.array0).map(glow::NativeTexture));
            gl.bind_texture(glow::TEXTURE_2D, name(self.tex2d0).map(glow::NativeTexture));
            gl.active_texture(self.unit as u32);
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, self.unpack);
            if self.scissor { gl.enable(glow::SCISSOR_TEST); } else { gl.disable(glow::SCISSOR_TEST); }
        }
    }
}
