//! The crate's one error type. Every failure says what was being done, so
//! the Python side can log it and fall back to the old path.

use std::fmt;

#[derive(Debug)]
pub enum CoreError {
    /// The OS refused a read or an open.
    Io(String),
    /// Compressed bytes did not decode to the block the manifest promised.
    Decode(String),
    /// The opened file is not the one the manifest describes.
    Identity(String),
    /// A request outside the manifest (channel, level, block).
    Bounds(String),
    /// The manifest itself is malformed.
    Manifest(String),
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoreError::Io(m) => write!(f, "io: {m}"),
            CoreError::Decode(m) => write!(f, "decode: {m}"),
            CoreError::Identity(m) => write!(f, "identity: {m}"),
            CoreError::Bounds(m) => write!(f, "bounds: {m}"),
            CoreError::Manifest(m) => write!(f, "manifest: {m}"),
        }
    }
}

impl std::error::Error for CoreError {}

pub type Result<T> = std::result::Result<T, CoreError>;
