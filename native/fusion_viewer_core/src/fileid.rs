//! File identity: size, modification time and the OS file number.
//!
//! The manifest records what Python saw when it parsed the file; Rust checks
//! the file it actually opened against it, so the two never read different
//! versions of a path. Values match Python's `os.stat` on each OS:
//! `st_size`, `st_mtime_ns`, `st_ino`. On Windows `st_ino` is the 64-bit
//! file index up to Python 3.11 and may be the 128-bit file id from 3.12
//! (ReFS); both are accepted (they agree on NTFS).

use std::fs::File;
use std::io;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileId {
    pub size: u64,
    pub mtime_ns: i128,
    pub file_number: u128,
    /// Windows only: the 128-bit file id, when the file system has one.
    pub alt_number: Option<u128>,
}

impl FileId {
    /// Is this (the opened file) the file `expected` describes?
    pub fn matches(&self, expected: &FileId) -> bool {
        self.size == expected.size
            && self.mtime_ns == expected.mtime_ns
            && (self.file_number == expected.file_number
                || self.alt_number == Some(expected.file_number))
    }
}

#[cfg(unix)]
pub fn of(file: &File) -> io::Result<FileId> {
    use std::os::unix::fs::MetadataExt;
    let m = file.metadata()?;
    Ok(FileId {
        size: m.size(),
        mtime_ns: m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128,
        file_number: m.ino() as u128,
        alt_number: None,
    })
}

#[cfg(windows)]
pub fn of(file: &File) -> io::Result<FileId> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_ID_INFO, FileIdInfo, GetFileInformationByHandle,
        GetFileInformationByHandleEx,
    };
    let handle = file.as_raw_handle() as _;
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: a valid open handle and a valid out-pointer.
    if unsafe { GetFileInformationByHandle(handle, &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut id: FILE_ID_INFO = unsafe { std::mem::zeroed() };
    // SAFETY: as above; the size is that of the out struct.
    let alt_number = (unsafe {
        GetFileInformationByHandleEx(handle, FileIdInfo, &mut id as *mut _ as *mut _,
                                     std::mem::size_of::<FILE_ID_INFO>() as u32)
    } != 0)
        .then(|| u128::from_le_bytes(id.FileId.Identifier));
    let ticks = ((info.ftLastWriteTime.dwHighDateTime as i128) << 32)
        | info.ftLastWriteTime.dwLowDateTime as i128;
    // FILETIME counts 100 ns from 1601; Python's st_mtime_ns counts from 1970
    let mtime_ns = (ticks - 116_444_736_000_000_000) * 100;
    Ok(FileId {
        size: ((info.nFileSizeHigh as u64) << 32) | info.nFileSizeLow as u64,
        mtime_ns,
        file_number: ((info.nFileIndexHigh as u128) << 32) | info.nFileIndexLow as u128,
        alt_number,
    })
}
