//! fusion_viewer_core: the Step1 viewer's performance core (v16_5).
//!
//! Small single-purpose modules (user ruling 2026-10-11): each core module
//! does one thing and knows nothing of Python; `py/` is the thin binding
//! layer and holds no logic of its own.

pub mod admit;
pub mod batch;
pub mod cache;
pub mod clock;
pub mod decode;
pub mod error;
pub mod fileid;
pub mod frame;
pub mod geom;
pub mod gpu_store;
pub mod gpu_warm;
pub mod gl;
pub mod manifest;
pub mod notify;
pub mod plan;
pub mod pool;
pub mod posread;
pub mod reader;
pub mod render;
pub mod shader;
pub mod trace;
pub mod viewer;
pub mod vt;

mod py;
