//! What one fusion combines, with `fuse_channels`' weight rules applied
//! once (plan §3): a member counts only when its channel has a committed
//! window and its weight is > 0; weights are clipped to [0, 1] in f64
//! (`float(np.clip(w, 0, 1))`, NaN kept) and used as f32; the group weight
//! (default 1) likewise; the nucleus counts when its channel has a window
//! and its weight is > 0.

use crate::fuse_window::{Params, clip01_f64};

/// One channel slot: its committed window, or None (takes no part).
#[derive(Clone, Debug)]
pub struct Channel {
    pub window: Option<Params>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Group {
    pub weight: f32,
    /// (slot, f32 weight) in the configuration's order
    pub members: Vec<(usize, f32)>,
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub channels: Vec<Channel>,
    pub groups: Vec<Group>,
    pub nucleus: Option<(usize, f32)>,
}

impl Plan {
    /// `groups`: (group weight, [(slot, weight)]) as Python floats;
    /// `nucleus`: (slot, weight). A slot outside `channels` is an error.
    pub fn new(channels: Vec<Channel>, groups: Vec<(f64, Vec<(usize, f64)>)>,
               nucleus: Option<(usize, f64)>) -> Result<Self, String> {
        let usable = |slot: usize| -> Result<bool, String> {
            channels.get(slot).map(|c| c.window.is_some())
                .ok_or_else(|| format!("channel slot {slot} out of range ({} channels)", channels.len()))
        };
        let mut out = Vec::with_capacity(groups.len());
        for (gw, members) in groups {
            let mut m = Vec::new();
            for (slot, w) in members {
                if usable(slot)? && w > 0.0 {
                    m.push((slot, clip01_f64(w) as f32));
                }
            }
            out.push(Group { weight: clip01_f64(gw) as f32, members: m });
        }
        let nucleus = match nucleus {
            Some((slot, w)) if usable(slot)? && w > 0.0 => Some((slot, clip01_f64(w) as f32)),
            _ => None,
        };
        Ok(Plan { channels, groups: out, nucleus })
    }

    /// Every (slot, weight) a term is built for: members, then the nucleus.
    pub fn uses(&self) -> Vec<(usize, f32)> {
        let mut v: Vec<(usize, f32)> = self.groups.iter().flat_map(|g| g.members.iter().copied()).collect();
        v.extend(self.nucleus);
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ch(w: bool) -> Channel {
        Channel { window: w.then(Params::default) }
    }

    #[test]
    fn weights_follow_fuse_channels() {
        let p = Plan::new(vec![ch(true), ch(false), ch(true)],
                          vec![(1.5, vec![(0, 0.5), (1, 1.0), (2, 0.0), (2, -1.0), (0, 2.0)]), (f64::NAN, vec![])],
                          Some((2, 0.9))).unwrap();
        assert_eq!(p.groups[0], Group { weight: 1.0, members: vec![(0, 0.5), (0, 1.0)] });
        assert!(p.groups[1].weight.is_nan());
        assert_eq!(p.nucleus, Some((2, 0.9f64 as f32)));
        assert!(Plan::new(vec![ch(true)], vec![], Some((1, 1.0))).is_err());
        let none = Plan::new(vec![ch(false)], vec![], Some((0, 1.0))).unwrap();
        assert_eq!(none.nucleus, None);
        let nan_w = Plan::new(vec![ch(true)], vec![(1.0, vec![(0, f64::NAN)])], Some((0, 0.0))).unwrap();
        assert!(nan_w.groups[0].members.is_empty() && nan_w.nucleus.is_none());
    }
}
