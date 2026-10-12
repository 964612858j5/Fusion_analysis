//! The three storage formats of a tile on the CPU and the GPU, and their
//! two sampler families (`step1_gpu_layer._array_format`, `step1_gpu_vt
//! ._family`): u8 -> R8UI and u16 -> R16UI share the integer family (0),
//! everything else is f32 -> R32F, the float family (1), invalid where NaN.

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Format {
    U8,
    U16,
    F32,
}

pub const ALL: [Format; 3] = [Format::U8, Format::U16, Format::F32];

impl Format {
    pub fn bytes(self) -> usize {
        match self {
            Format::U8 => 1,
            Format::U16 => 2,
            Format::F32 => 4,
        }
    }

    /// 0: integer samplers (usampler2DArray), 1: float samplers
    pub fn family(self) -> usize {
        (self == Format::F32) as usize
    }

    /// (internal format, format, type) for glTexImage3D / glTexSubImage3D
    pub fn gl(self) -> (u32, u32, u32) {
        match self {
            Format::U8 => (glow::R8UI, glow::RED_INTEGER, glow::UNSIGNED_BYTE),
            Format::U16 => (glow::R16UI, glow::RED_INTEGER, glow::UNSIGNED_SHORT),
            Format::F32 => (glow::R32F, glow::RED, glow::FLOAT),
        }
    }
}
