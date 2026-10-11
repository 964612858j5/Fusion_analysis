//! Monotonic clock on the same time base as Python's `time.monotonic()`.
//!
//! Records from Rust (tile stages, presents) must line up with the Python
//! perf log and the injector, which all stamp `time.monotonic()`. CPython
//! reads `CLOCK_MONOTONIC` on Linux and `QueryPerformanceCounter` on
//! Windows; this module reads the same source, so no offset is needed.

/// Seconds on the `time.monotonic()` time base.
#[cfg(unix)]
pub fn monotonic() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: `ts` is a valid out-pointer; CLOCK_MONOTONIC always exists.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

#[cfg(windows)]
pub fn monotonic() -> f64 {
    use windows_sys::Win32::System::Performance::{
        QueryPerformanceCounter, QueryPerformanceFrequency,
    };
    let (mut count, mut freq) = (0i64, 0i64);
    // SAFETY: both are valid out-pointers; the calls cannot fail on XP+.
    unsafe {
        QueryPerformanceCounter(&mut count);
        QueryPerformanceFrequency(&mut freq);
    }
    count as f64 / freq as f64
}

/// CPU seconds this thread has used (the reader's `cpu_ms` in the trace);
/// wall time where the OS has no per-thread clock here.
#[cfg(unix)]
pub fn thread_cpu() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: valid out-pointer; the clock exists on Linux.
    unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

#[cfg(windows)]
pub fn thread_cpu() -> f64 {
    monotonic()
}
