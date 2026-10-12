//! One GPU block: a 512x512 array texture of one storage format
//! (`step1_gpu_layer._ArrayBlock`), its layers, which are free and which
//! tile owns each; and the GL calls that make one and fill a layer.
//! Context current, GUI thread.

use crate::cache::{Key, Tile};
use crate::family::Format;
use crate::geom::TILE;
use glow::HasContext;
use std::collections::HashMap;

/// Bytes a block is made of at most (`ARRAY_BLOCK_BYTES`).
pub const BLOCK_BYTES: usize = 64 * 1024 * 1024;
/// Pixels of one layer.
pub const SLOT_PIXELS: usize = TILE * TILE;

pub struct Block {
    pub texture: glow::Texture,
    pub format: Format,
    pub layers: u32,
    /// its number among the blocks of its sampler family (the shader's
    /// `u_u<i>` / `u_f<i>`), the lowest free one when it was made
    pub vt_index: usize,
    pub free: Vec<u32>,
    pub owners: HashMap<u32, Key>,
}

impl Block {
    pub fn bytes(&self) -> usize {
        self.layers as usize * SLOT_PIXELS * self.format.bytes()
    }
}

/// `_block_layers`: a block of at most `BLOCK_BYTES`, the GL layer limit,
/// and at most a quarter of the budget.
pub fn block_layers(max_bytes: usize, max_gl_layers: usize, format: Format) -> u32 {
    let slot = SLOT_PIXELS * format.bytes();
    (BLOCK_BYTES / slot).min(max_gl_layers).min(max_bytes / (4 * slot)).max(1) as u32
}

/// An empty array texture of `layers` layers in `format`, NEAREST and
/// clamped, allocated with glTexImage3D or (`storage`) glTexStorage3D.
pub unsafe fn create_texture(gl: &glow::Context, format: Format, layers: u32, storage: bool)
                             -> Result<glow::Texture, String> {
    let (internal, fmt, typ) = format.gl();
    unsafe {
        let tex = gl.create_texture()?;
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(tex));
        for (p, v) in [(glow::TEXTURE_MIN_FILTER, glow::NEAREST), (glow::TEXTURE_MAG_FILTER, glow::NEAREST),
                       (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE), (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE)] {
            gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, p, v as i32);
        }
        gl.tex_parameter_i32(glow::TEXTURE_2D_ARRAY, glow::TEXTURE_MAX_LEVEL, 0);
        if storage {
            gl.tex_storage_3d(glow::TEXTURE_2D_ARRAY, 1, internal, TILE as i32, TILE as i32, layers as i32);
        } else {
            gl.tex_image_3d(glow::TEXTURE_2D_ARRAY, 0, internal as i32, TILE as i32, TILE as i32,
                            layers as i32, 0, fmt, typ, glow::PixelUnpackData::Slice(None));
        }
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
        Ok(tex)
    }
}

/// Write `tile` (its own format and edge-cut shape) into `layer`.
pub unsafe fn upload(gl: &glow::Context, texture: glow::Texture, layer: u32, tile: &Tile) {
    let (_internal, fmt, typ) = tile.format.gl();
    unsafe {
        // set every time: creating a block resets it, and an edge tile's
        // rows are not 4-byte multiples
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, Some(texture));
        gl.tex_sub_image_3d(glow::TEXTURE_2D_ARRAY, 0, 0, 0, layer as i32, tile.cols as i32,
                            tile.rows as i32, 1, fmt, typ, glow::PixelUnpackData::Slice(Some(&tile.data)));
        gl.bind_texture(glow::TEXTURE_2D_ARRAY, None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layers_per_format_follow_python() {
        // 1536 MiB budget, 2048 GL layers: 64 MiB per block
        let b = 1536 * 1024 * 1024;
        assert_eq!(block_layers(b, 2048, Format::U8), 256);
        assert_eq!(block_layers(b, 2048, Format::U16), 128);
        assert_eq!(block_layers(b, 2048, Format::F32), 64);
        // a small budget: a quarter of it per block
        assert_eq!(block_layers(16 * 1024 * 1024, 2048, Format::F32), 4);
    }
}
