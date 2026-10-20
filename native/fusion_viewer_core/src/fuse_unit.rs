//! One output unit (`FullFusionWorker._compute_unit`): read every channel
//! the plan uses, each once, from its source (`fuse_source`), fuse them
//! (`fuse_planes`), zero what lies outside the polygon. The cancel flag is
//! asked before each channel read (and between the chunks of a corrected
//! read). Returns `2 * h * w` u16, row-major, (cyto, nucleus) interleaved
//! -- the zarr chunk's own layout -- or None once cancelled.
//!
//! Only the slots the plan uses are read (plan §1 ruling 7: a channel
//! without a committed window, or used by nothing, is not read).

use crate::cancel::Cancel;
use crate::error::{CoreError, Result};
use crate::fuse_plan::Plan;
use crate::fuse_planes::{self, Plane};
use crate::fuse_source::Source;

pub struct UnitFuser {
    plan: Plan,
    /// one per plan slot; None for a slot the plan does not use
    sources: Vec<Option<Source>>,
    /// the channel name of each slot, for error messages
    names: Vec<String>,
    cancel: Cancel,
}

/// `e` with the channel and the unit in front (plan §6: a runtime failure
/// says which channel, which unit and why); the kind is kept.
fn in_context(e: CoreError, name: &str, unit: [usize; 4]) -> CoreError {
    let at = |m: String| format!("channel {name:?}, unit {unit:?}: {m}");
    match e {
        CoreError::Io(m) => CoreError::Io(at(m)),
        CoreError::Decode(m) => CoreError::Decode(at(m)),
        CoreError::Identity(m) => CoreError::Identity(at(m)),
        CoreError::Bounds(m) => CoreError::Bounds(at(m)),
        CoreError::Manifest(m) => CoreError::Manifest(at(m)),
    }
}

impl UnitFuser {
    /// Every slot the plan uses must have a source; one name per slot.
    pub fn new(plan: Plan, sources: Vec<Option<Source>>, names: Vec<String>) -> Result<Self> {
        if sources.len() != plan.channels.len() || names.len() != plan.channels.len() {
            return Err(CoreError::Manifest(format!(
                "{} sources and {} names for {} channel slots", sources.len(), names.len(), plan.channels.len())));
        }
        if let Some((s, _)) = plan.uses().into_iter().find(|&(s, _)| sources[s].is_none()) {
            return Err(CoreError::Manifest(format!("channel slot {s} is used but has no source")));
        }
        Ok(UnitFuser { plan, sources, names, cancel: Cancel::new() })
    }

    /// Stop the reads in flight and refuse new units.
    pub fn cancel(&self) {
        self.cancel.cancel();
    }

    /// `mask`: one byte per pixel of the unit, 0 = outside the polygon.
    pub fn fuse_unit(&self, y0: usize, y1: usize, x0: usize, x1: usize, mask: Option<&[u8]>)
                     -> Result<Option<Vec<u16>>> {
        if y1 < y0 || x1 < x0 {
            return Err(CoreError::Bounds(format!("unit [{y0}, {y1}, {x0}, {x1}]")));
        }
        let n = (y1 - y0) * (x1 - x0);
        if let Some(m) = mask {
            if m.len() != n {
                return Err(CoreError::Bounds(format!("mask of {} pixels for a unit of {n}", m.len())));
            }
        }
        let wanted = || self.cancel.wanted();
        let mut planes: Vec<Plane> = (0..self.sources.len()).map(|_| Plane::U8(Vec::new())).collect();
        let mut read = vec![false; self.sources.len()];
        for (s, _) in self.plan.uses() {
            if read[s] {
                continue;
            }
            if !wanted() {
                return Ok(None);
            }
            let source = self.sources[s].as_ref().expect("checked in new");
            let got = source.read(y0, y1, x0, x1, &wanted)
                .map_err(|e| in_context(e, &self.names[s], [y0, y1, x0, x1]))?;
            match got {
                Some(p) => planes[s] = p,
                None => return Ok(None),
            }
            read[s] = true;
        }
        let mut out = fuse_planes::fuse(&self.plan, &planes).map_err(CoreError::Bounds)?;
        if self.plan.uses().is_empty() {
            out = vec![0u16; n * 2]; // nothing to fuse: zeros of the unit's size
        }
        if let Some(m) = mask {
            for (px, &inside) in out.chunks_exact_mut(2).zip(m) {
                if inside == 0 {
                    px[0] = 0;
                    px[1] = 0;
                }
            }
        }
        Ok(Some(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_read_error_names_the_channel_and_the_unit() {
        let e = in_context(CoreError::Decode("lzw: bad code".into()), "CD8", [0, 64, 128, 256]);
        assert!(matches!(e, CoreError::Decode(_)));
        assert_eq!(e.to_string(), "decode: channel \"CD8\", unit [0, 64, 128, 256]: lzw: bad code");
    }
}
