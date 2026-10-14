//! fusion_viewer_core: the Step1 viewer's performance core (v16_5).
//!
//! Small single-purpose modules (user ruling 2026-10-11): each core module
//! does one thing and knows nothing of Python; `py/` is the thin binding
//! layer and holds no logic of its own.

pub mod admit;
pub mod batch;
pub mod blosc;
pub mod cache;
pub mod cancel;
pub mod chunk_cache;
pub mod clock;
pub mod corrected;
pub mod decode;
pub mod error;
pub mod fileid;
pub mod family;
pub mod fault;
pub mod frame;
pub mod geom;
pub mod gpu_block;
pub mod gpu_store;
pub mod gpu_touch;
pub mod gpu_warm;
pub mod label_plan;
pub mod label_pool;
pub mod label_read;
pub mod label_source;
pub mod label_store;
pub mod grow_probe;
pub mod gl;
pub mod lz4;
pub mod manifest;
pub mod notify;
pub mod plan;
pub mod pool;
pub mod posread;
pub mod reader;
pub mod read_split;
pub mod reduce;
pub mod render;
pub mod reserve;
pub mod shader;
pub mod stencil;
pub mod store_plan;
pub mod step1_tile;
pub mod trace;
pub mod viewer;
pub mod vt;
pub mod zarr_v2;

mod py;
