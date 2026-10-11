//! A cancellation flag a long read checks between its pieces (plan R1 §2.6):
//! the caller keeps one end, the reader asks `wanted()`.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Clone, Debug, Default)]
pub struct Cancel(Arc<AtomicBool>);

impl Cancel {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }

    pub fn wanted(&self) -> bool {
        !self.0.load(Ordering::Acquire)
    }
}
