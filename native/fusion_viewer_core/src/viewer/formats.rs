//! What a channel's tiles are stored as, and the admission total that
//! follows from the formats drawn (`_total_budget`).

use super::ViewerCore;
use crate::plan::Ch;

impl ViewerCore {
    /// How a channel's tiles are stored (None: missing, never drawn).
    pub fn format_of(&self, c: Ch) -> Option<crate::family::Format> {
        use crate::corrected::Source;
        use crate::family::Format;
        use crate::step1_tile::RawKind;
        match self.sources.of(c)? {
            Source::Raw { .. } => Some(match self.raw_kind {
                RawKind::U8 => Format::U8,
                RawKind::U16 => Format::U16,
                _ => Format::F32,
            }),
            Source::Corrected { u16: true, .. } => Some(Format::U16),          // R7c 1E
            Source::Corrected { .. } => Some(Format::F32),
            Source::DisplayTiff { kind, .. } => Some(match kind {         // R7c 1B
                RawKind::U8 => Format::U8,
                RawKind::U16 => Format::U16,
                _ => Format::F32,
            }),
            Source::Missing { .. } => None,
        }
    }

    /// The admission total (`_total_budget`): the budget less one block
    /// of each format the drawn channels use.
    pub(super) fn update_total(&mut self) {
        let Some(g) = self.gpu.as_ref() else { return };
        // `_total_budget`: one block of the slide's own format, and one of
        // float32 when any drawn channel is not raw
        let raw = match self.raw_kind {
            crate::step1_tile::RawKind::U8 => crate::family::Format::U8,
            crate::step1_tile::RawKind::U16 => crate::family::Format::U16,
            _ => crate::family::Format::F32,
        };
        let mut formats = vec![raw];
        // one block of every format a drawn non-raw channel uses (R7c 1B, 1E)
        for (c, _) in &self.display {
            let non_raw = matches!(self.sources.of(*c), Some(crate::corrected::Source::Corrected { .. })
                                                     | Some(crate::corrected::Source::DisplayTiff { .. }));
            if let (true, Some(f)) = (non_raw, self.format_of(*c)) {
                if !formats.contains(&f) {
                    formats.push(f);
                }
            }
        }
        let total = self.cfg.max_raw_texture_bytes.saturating_sub(g.store.slack_bytes(&formats));
        self.planner.set_total(Some(total as u64));
    }
}
