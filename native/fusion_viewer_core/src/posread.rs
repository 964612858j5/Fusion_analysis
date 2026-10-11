//! Positioned reads: the ONE place that knows how each OS reads a byte
//! range without a shared file offset.
//!
//! Many reader threads share one `File`. A positioned read touches no
//! shared cursor, so no lock is needed (Linux `pread`, Windows `ReadFile`
//! with an OVERLAPPED offset). Windows' `seek_read` DOES move the handle's
//! cursor, which is harmless here because nothing else uses the cursor.

use std::fs::File;
use std::io;

/// Fill `buf` from `offset`, or fail; a short file is an error.
pub fn read_exact_at(file: &File, buf: &mut [u8], offset: u64) -> io::Result<()> {
    let mut done = 0usize;
    while done < buf.len() {
        let n = read_at(file, &mut buf[done..], offset + done as u64)?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("file ended at {} while reading {} bytes", offset + done as u64, buf.len()),
            ));
        }
        done += n;
    }
    Ok(())
}

#[cfg(unix)]
fn read_at(file: &File, buf: &mut [u8], offset: u64) -> io::Result<usize> {
    use std::os::unix::fs::FileExt;
    loop {
        match file.read_at(buf, offset) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            other => return other,
        }
    }
}

#[cfg(windows)]
fn read_at(file: &File, buf: &mut [u8], offset: u64) -> io::Result<usize> {
    use std::os::windows::fs::FileExt;
    file.seek_read(buf, offset)
}
