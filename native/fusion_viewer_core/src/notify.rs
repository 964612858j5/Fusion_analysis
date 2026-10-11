//! "Please redraw" notices to the GUI thread, through a socket pair.
//!
//! Ownership: Python creates `socket.socketpair()` and owns BOTH ends. Rust
//! only borrows the write end's OS handle (fd on Linux, SOCKET on Windows)
//! and never closes it. Close order: `Notifier::close()` (no more writes),
//! then Python closes the two sockets.
//!
//! Coalescing: a byte is written only when the state goes from "nothing
//! pending" to "pending". The GUI side, on readable: drain the socket
//! until it would block, THEN `ack()`, then pull the work. Any notice
//! raised after `ack()` writes a new byte (so none is lost); a notice
//! raised between the drain and `ack()` is covered because the work is
//! pulled after `ack()`. (Acking BEFORE draining loses a wake-up: a
//! notice after the ack writes a byte the drain then swallows, and the
//! next notice sees "pending" and writes nothing -- found in review.) A
//! byte written between drain and ack only causes one spare wake-up.
//! Writes never block: a full socket buffer already holds an unread byte.
//!
//! A notice means "something may have changed, redraw", never "on screen".

use socket2::Socket;
use std::io::{ErrorKind, Write};
use std::mem::ManuallyDrop;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

/// The OS handle of the write end, as Python's `sock.fileno()` gives it.
pub type RawSocket = u64;

pub struct Notifier {
    socket: ManuallyDrop<Socket>,
    pending: AtomicBool,
    closed: AtomicBool,
    /// notices between their `closed` check and the end of their write
    in_flight: AtomicUsize,
    pub writes: AtomicU64,
    pub coalesced: AtomicU64,
}

impl Notifier {
    /// Borrow `handle`; Python keeps ownership and must keep it open until
    /// `close()` has returned.
    pub fn borrow(handle: RawSocket) -> std::io::Result<Self> {
        let socket = ManuallyDrop::new(from_raw(handle));
        // the write end is Rust's alone to use; Python only owns its lifetime
        socket.set_nonblocking(true)?;
        Ok(Notifier {
            socket,
            pending: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            in_flight: AtomicUsize::new(0),
            writes: AtomicU64::new(0),
            coalesced: AtomicU64::new(0),
        })
    }

    /// Ask for a redraw. Cheap, never blocks, safe from any thread.
    pub fn notify(&self) {
        self.in_flight.fetch_add(1, Ordering::SeqCst);
        self.notify_inner();
        self.in_flight.fetch_sub(1, Ordering::SeqCst);
    }

    fn notify_inner(&self) {
        if self.closed.load(Ordering::SeqCst) {
            return;
        }
        if self.pending.swap(true, Ordering::AcqRel) {
            self.coalesced.fetch_add(1, Ordering::Relaxed);
            return;
        }
        match (&*self.socket).write(&[1u8]) {
            Ok(_) => {
                self.writes.fetch_add(1, Ordering::Relaxed);
            }
            // full buffer: an unread byte is already there
            Err(e) if e.kind() == ErrorKind::WouldBlock => {}
            // peer gone (closing): nothing to wake
            Err(_) => {}
        }
    }

    /// The GUI side has drained the socket and is about to pull the work;
    /// the next notice writes again.
    pub fn ack(&self) {
        self.pending.store(false, Ordering::Release);
    }

    /// No write after this returns (the caller then closes the sockets):
    /// waits out a write already under way -- a few microseconds, since
    /// writes never block -- so a closed fd number is never written to.
    pub fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        while self.in_flight.load(Ordering::SeqCst) != 0 {
            std::hint::spin_loop();
        }
    }
}

#[cfg(unix)]
fn from_raw(handle: RawSocket) -> Socket {
    use std::os::fd::FromRawFd;
    // SAFETY: Python keeps the fd open (see `borrow`); ManuallyDrop means
    // this Socket never closes it.
    unsafe { Socket::from_raw_fd(handle as i32) }
}

#[cfg(windows)]
fn from_raw(handle: RawSocket) -> Socket {
    use std::os::windows::io::FromRawSocket;
    // SAFETY: as above; SOCKET is 64-bit on Win64 and passed whole.
    unsafe { Socket::from_raw_socket(handle as _) }
}
