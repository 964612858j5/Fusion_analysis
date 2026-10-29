//! S0-B (docs/v17_step0_engine_plan.md §11.3): channels whose tiles Python
//! computes -- Step0's live TopHat / cuCIM preview. They are numbered in a
//! reserved range of the channel space, never a slide channel, never reused
//! within one core, and at most `MAX_LIVE` are registered at once (the one
//! shown and the one being prepared). Their tiles are float32 and count
//! against the corrected allowance. Pure bookkeeping, no threads, no GL.

use crate::plan::Ch;
use std::collections::HashSet;

/// The first number of the reserved range.
pub const FIRST: Ch = 0x8000;
/// Registered at once: the source shown and the one being prepared.
pub const MAX_LIVE: usize = 2;

#[derive(Default)]
pub struct PythonSources {
    live: HashSet<Ch>,
    /// the highest number ever registered: a new one must be above it
    highest: Option<Ch>,
    pub registered: u64,
    pub retired: u64,
}

impl PythonSources {
    pub fn is_python(c: Ch) -> bool {
        c >= FIRST
    }

    pub fn is_live(&self, c: Ch) -> bool {
        self.live.contains(&c)
    }

    pub fn live(&self) -> Vec<Ch> {
        let mut v: Vec<Ch> = self.live.iter().copied().collect();
        v.sort_unstable();
        v
    }

    /// Register `c`: in the range, above every number used before, and
    /// within `MAX_LIVE`. Err says why not (the caller falls back).
    pub fn add(&mut self, c: Ch) -> Result<(), String> {
        if !Self::is_python(c) {
            return Err(format!("source {c:#x} is not in the Python range"));
        }
        if self.highest.is_some_and(|h| c <= h) {
            return Err(format!("source {c:#x} was used before (numbers are never reused)"));
        }
        if self.live.len() >= MAX_LIVE {
            return Err(format!("{MAX_LIVE} Python sources are registered: retire one first"));
        }
        self.live.insert(c);
        self.highest = Some(c);
        self.registered += 1;
        Ok(())
    }

    /// Retire `c`; false when it was not registered.
    pub fn remove(&mut self, c: Ch) -> bool {
        let was = self.live.remove(&c);
        if was {
            self.retired += 1;
        }
        was
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_are_in_range_rising_and_at_most_two_at_once() {
        let mut s = PythonSources::default();
        assert!(s.add(3).is_err());
        s.add(FIRST).unwrap();
        s.add(FIRST + 1).unwrap();
        assert!(s.add(FIRST + 2).is_err(), "a third live source");
        assert!(s.remove(FIRST));
        assert!(s.add(FIRST).is_err(), "a number reused");
        s.add(FIRST + 2).unwrap();
        assert_eq!(s.live(), vec![FIRST + 1, FIRST + 2]);
        assert!(!s.remove(FIRST));
    }
}
