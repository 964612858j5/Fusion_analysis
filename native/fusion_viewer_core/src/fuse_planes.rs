//! A plan fused over channel planes already in memory, one per slot (the
//! pixels of one unit, or a whole test image): resolve each window (an
//! open window over that slot's own plane, as Python per tile), build the
//! weighted tables for integer planes, run the kernel strip by strip.
//! Returns `2 * n` u16, interleaved (cyto, nucleus) -- the zarr chunk's
//! own layout. Reading the planes is the caller's (F5-2 `fuse_unit`).

use crate::fuse_kernel::{GroupTerms, Term, fuse_all};
use crate::fuse_lut;
use crate::fuse_plan::Plan;
use crate::fuse_window::{Window, finite_range};

/// Pixels per kernel strip: the scratch planes stay in L1/L2.
pub const STRIP: usize = 8192;

/// One slot's samples as read (the raw slide's type, or corrected f32).
pub enum Plane {
    U8(Vec<u8>),
    U16(Vec<u16>),
    F32(Vec<f32>),
}

impl Plane {
    pub fn len(&self) -> usize {
        match self {
            Plane::U8(v) => v.len(),
            Plane::U16(v) => v.len(),
            Plane::F32(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The finite (min, max), for an open window.
    fn range(&self) -> Option<(f64, f64)> {
        match self {
            Plane::U8(v) => finite_range(v.iter().map(|&x| x as f32)),
            Plane::U16(v) => finite_range(v.iter().map(|&x| x as f32)),
            Plane::F32(v) => finite_range(v.iter().copied()),
        }
    }
}

/// `planes[slot]` for every slot of `plan` (a slot no member or nucleus
/// uses may be an empty plane). Errors on a size mismatch.
pub fn fuse(plan: &Plan, planes: &[Plane]) -> Result<Vec<u16>, String> {
    if planes.len() != plan.channels.len() {
        return Err(format!("{} planes for {} channel slots", planes.len(), plan.channels.len()));
    }
    let uses = plan.uses();
    let n = uses.first().map(|&(s, _)| planes[s].len()).unwrap_or(0);
    if let Some(&(s, _)) = uses.iter().find(|&&(s, _)| planes[s].len() != n) {
        return Err(format!("plane {s} has {} pixels, expected {n}", planes[s].len()));
    }
    let n = if uses.is_empty() { planes.iter().map(Plane::len).max().unwrap_or(0) } else { n };
    let window = |s: usize| -> Window {
        let p = plan.channels[s].window.expect("a used slot has a window (Plan::new)");
        p.resolve(if p.open() { planes[s].range() } else { None })
    };
    // one weighted table per (slot, weight) of an integer plane
    let mut tables: Vec<((usize, u32), Vec<f32>)> = Vec::new();
    for &(s, w) in &uses {
        let values = match planes[s] {
            Plane::U8(_) => 256,
            Plane::U16(_) => 65536,
            Plane::F32(_) => continue,
        };
        if !tables.iter().any(|(k, _)| *k == (s, w.to_bits())) {
            tables.push(((s, w.to_bits()), fuse_lut::build(&window(s), w, values)));
        }
    }
    let table = |s: usize, w: f32| -> &[f32] {
        &tables.iter().find(|(k, _)| *k == (s, w.to_bits())).expect("built above").1
    };
    let term = |s: usize, w: f32| -> Term {
        match &planes[s] {
            Plane::U8(raw) => Term::U8 { raw, table: table(s, w) },
            Plane::U16(raw) => Term::U16 { raw, table: table(s, w) },
            Plane::F32(raw) => Term::F32 { raw, window: window(s), weight: w },
        }
    };
    let groups: Vec<GroupTerms> = plan.groups.iter().map(|g| GroupTerms {
        weight: g.weight,
        terms: g.members.iter().map(|&(s, w)| term(s, w)).collect(),
    }).collect();
    let nucleus = plan.nucleus.map(|(s, w)| term(s, w));
    let mut out = vec![0u16; n * 2];
    fuse_all(&groups, nucleus.as_ref(), n, STRIP, &mut out);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fuse_plan::Channel;
    use crate::fuse_window::Params;

    #[test]
    fn integer_and_float_planes_fuse_alike_and_sizes_are_checked() {
        let p = Params { min: Some(0.0), max: Some(200.0), gamma: 0.7, ..Params::default() };
        let plan = Plan::new(vec![Channel { window: Some(p) }, Channel { window: Some(p) }],
                             vec![(0.8, vec![(0, 1.0), (1, 0.5)])], Some((1, 1.0))).unwrap();
        let a: Vec<u8> = (0..=255).collect();
        let b: Vec<u16> = (0..256).map(|v| (255 - v) as u16).collect();
        let ints = fuse(&plan, &[Plane::U8(a.clone()), Plane::U16(b.clone())]).unwrap();
        let floats = fuse(&plan, &[Plane::F32(a.iter().map(|&v| v as f32).collect()),
                                   Plane::F32(b.iter().map(|&v| v as f32).collect())]).unwrap();
        assert_eq!(ints, floats);
        assert!(fuse(&plan, &[Plane::U8(a), Plane::U8(vec![0; 3])]).is_err());
        assert!(fuse(&plan, &[Plane::U8(vec![])]).is_err());
    }
}
