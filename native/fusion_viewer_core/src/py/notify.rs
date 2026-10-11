//! `Notifier`: the redraw notice (ownership and order: see core notify.rs).

use pyo3::prelude::*;
use std::sync::Arc;
use std::sync::atomic::Ordering;

#[pyclass(frozen)]
pub struct Notifier {
    inner: Arc<crate::notify::Notifier>,
}

#[pymethods]
impl Notifier {
    /// Borrow the write end's `fileno()`; Python keeps owning the socket.
    #[new]
    fn new(write_handle: u64) -> PyResult<Self> {
        Ok(Notifier { inner: Arc::new(crate::notify::Notifier::borrow(write_handle)?) })
    }

    fn notify(&self) {
        self.inner.notify();
    }

    fn ack(&self) {
        self.inner.ack();
    }

    fn close(&self, py: Python<'_>) {
        py.detach(|| self.inner.close());
    }

    /// (bytes written, notices coalesced)
    fn counts(&self) -> (u64, u64) {
        (self.inner.writes.load(Ordering::Relaxed), self.inner.coalesced.load(Ordering::Relaxed))
    }

    /// Test aid: `threads` Rust threads each send `count` notices,
    /// `gap_us` apart, while Python keeps running. Returns at once.
    fn storm(&self, threads: usize, count: usize, gap_us: u64) {
        for _ in 0..threads {
            let n = Arc::clone(&self.inner);
            std::thread::spawn(move || {
                for _ in 0..count {
                    n.notify();
                    std::thread::sleep(std::time::Duration::from_micros(gap_us));
                }
            });
        }
    }
}
