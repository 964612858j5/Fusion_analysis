//! The programs one context draws with, made on demand and kept: the
//! single pass per sampler split (`vt_source(u, f)`; a split change picks
//! another, compiled once), the two final passes (`PASS_FINAL_OVERLAY`,
//! `PASS_FINAL_FUSION`) and the polygon mask with its vertex buffer
//! (`step1_gpu_mask.*`, `_ensure_mask_resources`).

use crate::shader;
use glow::HasContext;
use std::collections::HashMap;

pub const MASK_VERT: &str = include_str!("../../shaders/step1_gpu_mask.vert");
pub const MASK_FRAG: &str = include_str!("../../shaders/step1_gpu_mask.frag");

pub struct Programs {
    vt: HashMap<(usize, usize), glow::Program>,
    pub final_overlay: glow::Program,
    pub final_fusion: glow::Program,
    pub mask: glow::Program,
    pub mask_vao: glow::VertexArray,
    pub mask_vbo: glow::Buffer,
}

impl Programs {
    pub unsafe fn new(gl: &glow::Context) -> Result<Programs, String> {
        unsafe {
            let mut made: Vec<glow::Program> = Vec::new();
            let mut link = |vs: &str, fs: &str| -> Result<glow::Program, String> {
                let p = shader::link(gl, vs, fs)?;
                made.push(p);
                Ok(p)
            };
            let built = (|| -> Result<(glow::Program, glow::Program, glow::Program), String> {
                Ok((link(shader::VERT, &shader::with_define("PASS_FINAL_OVERLAY"))?,
                    link(shader::VERT, &shader::with_define("PASS_FINAL_FUSION"))?,
                    link(MASK_VERT, MASK_FRAG)?))
            })();
            let (final_overlay, final_fusion, mask) = match built {
                Ok(p) => p,
                Err(e) => {
                    for p in made { gl.delete_program(p); }
                    return Err(e);
                }
            };
            let (vao, vbo) = (gl.create_vertex_array()?, gl.create_buffer()?);
            gl.bind_vertex_array(Some(vao));
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            gl.enable_vertex_attrib_array(0);
            gl.vertex_attrib_pointer_f32(0, 2, glow::FLOAT, false, 0, 0);
            gl.bind_vertex_array(None);
            gl.bind_buffer(glow::ARRAY_BUFFER, None);
            Ok(Programs { vt: HashMap::new(), final_overlay, final_fusion, mask, mask_vao: vao, mask_vbo: vbo })
        }
    }

    /// The single pass for `split` (integer, float blocks), compiled once.
    pub unsafe fn vt(&mut self, gl: &glow::Context, split: (usize, usize)) -> Result<glow::Program, String> {
        if let Some(p) = self.vt.get(&split) {
            return Ok(*p);
        }
        let p = unsafe { shader::link(gl, shader::VERT, &shader::vt_source(split.0, split.1))? };
        self.vt.insert(split, p);
        Ok(p)
    }

    pub unsafe fn destroy(&mut self, gl: &glow::Context) {
        unsafe {
            for (_, p) in self.vt.drain() {
                gl.delete_program(p);
            }
            for p in [self.final_overlay, self.final_fusion, self.mask] {
                gl.delete_program(p);
            }
            gl.delete_vertex_array(self.mask_vao);
            gl.delete_buffer(self.mask_vbo);
        }
    }
}
