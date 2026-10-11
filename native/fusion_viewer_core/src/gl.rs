//! Drawing inside Qt's own GL context (R0: a probe picture only).
//!
//! Contract (plan §3): every call here runs on the GUI thread with Qt's
//! context current (initializeGL / resizeGL / paintGL, or between
//! makeCurrent/doneCurrent). GL functions come from Qt's
//! `QOpenGLContext.getProcAddress`, never GLX/WGL/EGL directly. The target
//! is the framebuffer Qt names (`defaultFramebufferObject()`).
//!
//! State: on the way out of `paint` every state this module touched is
//! put back to its GL default (program 0, VAO 0, array buffer 0, texture
//! unit 0 with nothing bound, blend/scissor/stencil/depth off, unpack
//! alignment 4). The draw framebuffer stays the one Qt gave us. Nothing is
//! queried on the hot path.
//!
//! Lifetime: `release()` deletes our GL names (context current, e.g. from
//! `aboutToBeDestroyed`); `forget()` drops them without GL calls when the
//! context is already gone. A new context gets a new `GlCore`.

use glow::HasContext;

const VERT: &str = r#"#version 330 core
layout(location = 0) in vec2 a_pos;
out vec2 v_uv;
void main() {
    v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"#;

const FRAG: &str = r#"#version 330 core
in vec2 v_uv;
out vec4 out_rgba;
uniform sampler2D u_tex;
uniform vec3 u_color;
void main() {
    out_rgba = vec4(u_color * texture(u_tex, v_uv).r, 1.0);
}
"#;

struct Names {
    program: glow::Program,
    vao: glow::VertexArray,
    vbo: glow::Buffer,
    texture: Option<glow::Texture>,
    color_loc: Option<glow::UniformLocation>,
}

pub struct GlCore {
    gl: glow::Context,
    names: Option<Names>,
    pub info: String,
}

impl GlCore {
    /// Load GL through `loader` (Qt's getProcAddress) and build the probe
    /// program. The context must be current.
    pub fn load(loader: impl FnMut(&str) -> *const std::ffi::c_void) -> Result<Self, String> {
        // SAFETY: the caller guarantees a current context whose functions
        // `loader` returns.
        let gl = unsafe { glow::Context::from_loader_function(loader) };
        let info = unsafe {
            format!("{} | {} | {}", gl.get_parameter_string(glow::VENDOR),
                    gl.get_parameter_string(glow::RENDERER), gl.get_parameter_string(glow::VERSION))
        };
        let names = unsafe { build(&gl)? };
        Ok(GlCore { gl, names: Some(names), info })
    }

    pub fn alive(&self) -> bool {
        self.names.is_some()
    }

    /// Our live GL names (program, VAO, VBO, texture).
    pub fn live_names(&self) -> usize {
        self.names.as_ref().map_or(0, |n| 3 + n.texture.is_some() as usize)
    }

    /// Upload one R8 picture (`rows x cols`), replacing the previous one.
    pub fn upload_r8(&mut self, pixels: &[u8], rows: i32, cols: i32) -> Result<(), String> {
        let gl = &self.gl;
        let names = self.names.as_mut().ok_or("released")?;
        unsafe {
            let tex = match names.texture {
                Some(t) => t,
                None => gl.create_texture()?,
            };
            names.texture = Some(tex);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::R8 as i32, cols, rows, 0, glow::RED,
                            glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(Some(pixels)));
            for (p, v) in [(glow::TEXTURE_MIN_FILTER, glow::NEAREST), (glow::TEXTURE_MAG_FILTER, glow::NEAREST),
                           (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE)] {
                gl.tex_parameter_i32(glow::TEXTURE_2D, p, v as i32);
            }
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            gl.bind_texture(glow::TEXTURE_2D, None);
        }
        Ok(())
    }

    /// Draw the picture over `width x height` device pixels of `fbo`.
    pub fn paint(&self, fbo: u32, width: i32, height: i32, color: [f32; 3]) -> Result<(), String> {
        let gl = &self.gl;
        let names = self.names.as_ref().ok_or("released")?;
        unsafe {
            gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, framebuffer(fbo));
            gl.viewport(0, 0, width, height);
            for cap in [glow::BLEND, glow::SCISSOR_TEST, glow::STENCIL_TEST, glow::DEPTH_TEST] {
                gl.disable(cap);
            }
            gl.use_program(Some(names.program));
            gl.uniform_3_f32(names.color_loc.as_ref(), color[0], color[1], color[2]);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, names.texture);
            gl.bind_vertex_array(Some(names.vao));
            gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
            // back to defaults (module doc); the framebuffer stays Qt's
            gl.bind_vertex_array(None);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.use_program(None);
        }
        Ok(())
    }

    /// GL error flag, for tests (a synchronous query: never on the hot path).
    pub fn error(&self) -> u32 {
        unsafe { self.gl.get_error() }
    }

    /// Delete every name we own. The context must be current.
    pub fn release(&mut self) {
        if let Some(n) = self.names.take() {
            unsafe {
                if let Some(t) = n.texture {
                    self.gl.delete_texture(t);
                }
                self.gl.delete_buffer(n.vbo);
                self.gl.delete_vertex_array(n.vao);
                self.gl.delete_program(n.program);
            }
        }
    }

    /// The context is gone: drop our names without touching GL.
    pub fn forget(&mut self) {
        self.names = None;
    }
}

fn framebuffer(fbo: u32) -> Option<glow::Framebuffer> {
    std::num::NonZeroU32::new(fbo).map(glow::NativeFramebuffer)
}

/// What `build` has made so far, deleted again if a later step fails
/// (the context is current: `load` runs inside initializeGL).
#[derive(Default)]
struct Partial {
    program: Option<glow::Program>,
    shaders: Vec<glow::Shader>,
    vao: Option<glow::VertexArray>,
    vbo: Option<glow::Buffer>,
}

impl Partial {
    unsafe fn delete(self, gl: &glow::Context) {
        unsafe {
            for s in self.shaders {
                gl.delete_shader(s);
            }
            if let Some(v) = self.vbo {
                gl.delete_buffer(v);
            }
            if let Some(v) = self.vao {
                gl.delete_vertex_array(v);
            }
            if let Some(p) = self.program {
                gl.delete_program(p);
            }
        }
    }
}

unsafe fn build(gl: &glow::Context) -> Result<Names, String> {
    let mut made = Partial::default();
    match unsafe { build_into(gl, &mut made) } {
        Ok(names) => Ok(names),
        Err(e) => {
            unsafe { made.delete(gl) };
            Err(e)
        }
    }
}

unsafe fn build_into(gl: &glow::Context, made: &mut Partial) -> Result<Names, String> {
    unsafe {
        let program = gl.create_program()?;
        made.program = Some(program);
        for (kind, src) in [(glow::VERTEX_SHADER, VERT), (glow::FRAGMENT_SHADER, FRAG)] {
            let s = gl.create_shader(kind)?;
            made.shaders.push(s);
            gl.shader_source(s, src);
            gl.compile_shader(s);
            if !gl.get_shader_compile_status(s) {
                return Err(gl.get_shader_info_log(s));
            }
            gl.attach_shader(program, s);
        }
        gl.link_program(program);
        for s in made.shaders.drain(..) {
            gl.detach_shader(program, s);
            gl.delete_shader(s);
        }
        if !gl.get_program_link_status(program) {
            return Err(gl.get_program_info_log(program));
        }
        let color_loc = gl.get_uniform_location(program, "u_color");
        let quad: [f32; 8] = [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0];
        let bytes: Vec<u8> = quad.iter().flat_map(|v| v.to_ne_bytes()).collect();
        let vao = gl.create_vertex_array()?;
        made.vao = Some(vao);
        let vbo = gl.create_buffer()?;
        made.vbo = Some(vbo);
        gl.bind_vertex_array(Some(vao));
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, &bytes, glow::STATIC_DRAW);
        gl.enable_vertex_attrib_array(0);
        gl.vertex_attrib_pointer_f32(0, 2, glow::FLOAT, false, 8, 0);
        gl.bind_vertex_array(None);
        gl.bind_buffer(glow::ARRAY_BUFFER, None);
        // sampler unit 0 is the program default; set it once anyway
        gl.use_program(Some(program));
        gl.uniform_1_i32(gl.get_uniform_location(program, "u_tex").as_ref(), 0);
        gl.use_program(None);
        Ok(Names { program, vao, vbo, texture: None, color_loc })
    }
}
