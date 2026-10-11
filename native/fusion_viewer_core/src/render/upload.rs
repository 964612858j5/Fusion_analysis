//! Uploading the virtual texture's tables: the page table (R32I array),
//! the meta table and the per-row parameters (RGBA32F), only the parts
//! `vt.rs` marked dirty (`VirtualCompositor._upload_pages`, `_sync_meta`,
//! `upload_params`).

use super::{f4, Render};
use crate::vt::{Vt, PARAM_TEXELS};
use glow::HasContext;

impl Render {
    /// Upload the page table, meta and parameters `vt` marked dirty.
    pub unsafe fn upload(&mut self, gl: &glow::Context, vt: &mut Vt, params: &[[f32; 4]]) -> Result<(), String> {
        unsafe {
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            let shape = (vt.layers(), vt.height, vt.width);
            let bytes = |v: &[i32]| std::slice::from_raw_parts(v.as_ptr() as *const u8, v.len() * 4);
            if self.pages.is_none() || vt.pages_resized || self.pages_shape != shape {
                let t = match self.pages { Some(t) => t, None => gl.create_texture()? };
                self.pages = Some(t);
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(t));
                gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
                gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
                let need = shape.0 * shape.1 * shape.2;
                if vt.pages.len() < need {
                    vt.pages.resize(need, -1);
                }
                gl.tex_image_3d(glow::TEXTURE_2D_ARRAY, 0, glow::R32I as i32, shape.2 as i32, shape.1 as i32,
                                shape.0 as i32, 0, glow::RED_INTEGER, glow::INT,
                                glow::PixelUnpackData::Slice(Some(bytes(&vt.pages[..need]))));
                self.pages_shape = shape;
                vt.pages_resized = false;
                vt.dirty_pages.clear();
            } else if !vt.dirty_pages.is_empty() {
                gl.bind_texture(glow::TEXTURE_2D_ARRAY, self.pages);
                for (layer, r0, r1) in std::mem::take(&mut vt.dirty_pages) {
                    let at = (layer * vt.height + r0) * vt.width;
                    let end = (layer * vt.height + r1) * vt.width;
                    gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, r0 as i32, layer as i32, vt.width as i32,
                                        (r1 - r0) as i32, 1, glow::RED_INTEGER, glow::INT,
                                        glow::PixelUnpackData::Slice(Some(bytes(&vt.pages[at..end]))));
                }
            }
            gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
            // meta: per block three rows, one column per layer
            if self.meta.is_none() || vt.meta_resized {
                let t = match self.meta { Some(t) => t, None => gl.create_texture()? };
                self.meta = Some(t);
                gl.bind_texture(glow::TEXTURE_2D, Some(t));
                gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
                gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
                gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA32F as i32, vt.meta_width as i32,
                                vt.meta_rows as i32, 0, glow::RGBA, glow::FLOAT,
                                glow::PixelUnpackData::Slice(Some(f4(&vt.meta))));
                vt.meta_resized = false;
                vt.meta_dirty_rows.clear();
            } else if !vt.meta_dirty_rows.is_empty() {
                let mut rows = std::mem::take(&mut vt.meta_dirty_rows);
                rows.sort();
                rows.dedup();
                gl.bind_texture(glow::TEXTURE_2D, self.meta);
                for row in rows {
                    let s = &vt.meta[row * vt.meta_width..(row + 3) * vt.meta_width];
                    gl.tex_sub_image_2d(glow::TEXTURE_2D, 0, 0, row as i32, vt.meta_width as i32, 3,
                                        glow::RGBA, glow::FLOAT, glow::PixelUnpackData::Slice(Some(f4(s))));
                }
            }
            gl.bind_texture(glow::TEXTURE_2D, None);
            // parameters: allocated once (grown when needed), then only written
            if params != self.params_last.as_slice() {
                let rows = params.len() / PARAM_TEXELS;
                if self.params.is_none() || rows > self.params_rows {
                    let t = match self.params { Some(t) => t, None => gl.create_texture()? };
                    self.params = Some(t);
                    self.params_rows = rows.next_power_of_two().max(64);
                    gl.bind_texture(glow::TEXTURE_2D, Some(t));
                    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::NEAREST as i32);
                    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::NEAREST as i32);
                    gl.tex_image_2d(glow::TEXTURE_2D, 0, glow::RGBA32F as i32, PARAM_TEXELS as i32,
                                    self.params_rows as i32, 0, glow::RGBA, glow::FLOAT,
                                    glow::PixelUnpackData::Slice(None));
                }
                gl.bind_texture(glow::TEXTURE_2D, self.params);
                gl.tex_sub_image_2d(glow::TEXTURE_2D, 0, 0, 0, PARAM_TEXELS as i32, rows as i32,
                                    glow::RGBA, glow::FLOAT, glow::PixelUnpackData::Slice(Some(f4(params))));
                gl.bind_texture(glow::TEXTURE_2D, None);
                self.params_last = params.to_vec();
            }
        }
        Ok(())
    }

}
