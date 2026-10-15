//! Step1's per-channel sources, as Python resolved them (plan R1 §2,
//! §4.5): `step1_core_adapter.build_corrected_manifest` turns a
//! `Step1SourceTable` into this, and Rust never asks Python again.
//!
//! A corrected channel names the array `open_corrected_channel_array`
//! opened and the persisted coarse planes Python accepted, each with the
//! identity of its `.zarray` / `.zattrs` as Python saw them; `open` checks
//! them and refuses a product that is not the one described. Assumed
//! unchanged while it is viewed (a Save republishes the handoff and the
//! manifest is rebuilt); a rewrite of chunks alone is not detected, as it
//! is not for Python's zarr reads.

use crate::error::{CoreError, Result};
use crate::fileid::FileId;
use crate::reduce::CorrectedRegion;
use crate::zarr_v2::ArrayMeta;
use std::collections::HashMap;
use std::path::Path;

/// One zarr array plus the identity of its two metadata files.
pub struct ArraySpec {
    pub meta: ArrayMeta,
    pub zarray: FileId,
    pub zattrs: Option<FileId>,
}

/// A persisted coarse plane for one stride (`CoarsePlane`): blocks of the
/// level-0-anchored grid starting at `origin` (block coordinates).
pub struct Plane {
    pub array: ArrayMeta,
    pub origin: [i64; 2],
    /// R7b B2: a uint16 display layer Python accepted, not the float32
    /// coarse plane
    pub display: bool,
}

pub enum Source {
    Raw { index: usize },
    Corrected { region: CorrectedRegion, planes: HashMap<usize, Plane> },
    /// R7c 1B: a corrected channel drawn from a second, already-built tiled
    /// pyramid (the merged OME-TIFF), read like a raw channel of that file
    DisplayTiff { src: std::sync::Arc<crate::reader::RawSource>, kind: crate::step1_tile::RawKind, index: usize },
    Missing { reason: String },
}

pub struct Step1Sources {
    pub channels: HashMap<String, Source>,
    /// the slide's channel index -> its name in `channels`
    pub names: HashMap<u16, String>,
    /// the analysis region (y0, y1, x0, x1), level 0; None = whole slide
    pub roi: Option<[i64; 4]>,
    /// `Step1SourceTable.identity_token()`: cache keys carry it
    pub revision: String,
}

impl Step1Sources {
    /// Every channel raw, as the thin slice read them (no table given).
    pub fn all_raw(channels: usize, roi: Option<[i64; 4]>) -> Self {
        let names: HashMap<u16, String> = (0..channels).map(|i| (i as u16, i.to_string())).collect();
        let channels = names.iter().map(|(i, n)| (n.clone(), Source::Raw { index: *i as usize })).collect();
        Step1Sources { channels, names, roi, revision: String::new() }
    }

    pub fn of(&self, ch: u16) -> Option<&Source> {
        self.names.get(&ch).and_then(|n| self.channels.get(n))
    }

    /// Why `ch` cannot be drawn, if its product is missing.
    pub fn missing(&self, ch: u16) -> Option<&str> {
        match self.of(ch) {
            Some(Source::Missing { reason }) => Some(reason),
            _ => None,
        }
    }
}

/// Check that `spec`'s metadata files are still the ones Python described.
pub fn verify(spec: &ArraySpec) -> Result<()> {
    let check = |name: &str, expected: &FileId| -> Result<()> {
        let path = spec.meta.dir.join(name);
        let file = std::fs::File::open(&path)
            .map_err(|e| CoreError::Identity(format!("{}: {e}", path.display())))?;
        let seen = crate::fileid::of(&file).map_err(|e| CoreError::Identity(format!("{}: {e}", path.display())))?;
        if !seen.matches(expected) {
            return Err(CoreError::Identity(format!(
                "{} changed after the manifest was built", path.display())));
        }
        Ok(())
    };
    check(".zarray", &spec.zarray)?;
    if let Some(z) = &spec.zattrs {
        check(".zattrs", z)?;
    }
    Ok(())
}

/// A corrected channel's sources, verified.
pub fn corrected(product: ArraySpec, bbox: [i64; 4], planes: Vec<(usize, ArraySpec, [i64; 2], bool)>)
                 -> Result<Source> {
    verify(&product)?;
    let mut by_stride = HashMap::new();
    for (stride, spec, origin, display) in planes {
        verify(&spec)?;
        by_stride.insert(stride, Plane { array: spec.meta, origin, display });
    }
    Ok(Source::Corrected { region: CorrectedRegion { array: product.meta, bbox }, planes: by_stride })
}

pub fn exists(dir: &Path) -> bool {
    dir.join(".zarray").is_file()
}
