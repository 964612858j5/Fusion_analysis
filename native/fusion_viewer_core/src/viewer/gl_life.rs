//! The core's GL side over a context's life: built in `initializeGL`,
//! deleted with the context current (`aboutToBeDestroyed`), forgotten when
//! the context is already gone, and released down to the coarse when the
//! viewer is hidden (`gpu_memory`).

use super::{Gpu, ViewerCore};
use crate::cache::Key;
use crate::gpu_store::Store;
use crate::render::Render;
use crate::vt::Vt;

impl ViewerCore {
    /// GL resources for the current context (`initializeGL`).
    pub fn attach_gl(&mut self, gl: glow::Context, max_layers: usize) -> Result<(), String> {
        let mut render = unsafe { Render::new(&gl)? };
        if let Some(n) = self.cfg.test_block_units.filter(|n| *n >= 2 && *n < render.units) {
            render.units = n;
            unsafe { render.set_split(&gl, (n - 1, 1))? };
        }
        let caps = [render.u_blocks, render.f_blocks];
        let store = Store::new(self.cfg.max_raw_texture_bytes, max_layers, caps);
        let width = crate::family::ALL.iter().map(|f| store.layers(*f) as usize).max().unwrap_or(1);
        let vt = Vt::new(&self.pyr, caps, width);
        self.gpu = Some(Gpu { gl, store, vt, render });
        self.update_total();
        Ok(())
    }

    /// Delete every GL name (context current: `aboutToBeDestroyed`).
    pub fn release_gl(&mut self) {
        if let Some(mut g) = self.gpu.take() {
            g.store.clear(&g.gl);
            unsafe { g.render.destroy(&g.gl) };
        }
    }

    /// The context is already gone: forget the names without GL calls.
    pub fn forget_gl(&mut self) {
        self.gpu = None;
    }

    pub fn gpu_bytes(&self) -> usize {
        self.gpu.as_ref().map_or(0, |g| g.store.allocated_bytes() + g.render.target_bytes())
    }

    /// Keep only the complete coarse on the GPU (`release_to_coarse`).
    pub fn release_to_coarse(&mut self) -> usize {
        let before = self.gpu_bytes();
        let coarsest = self.pyr.coarsest();
        if let Some(g) = self.gpu.as_mut() {
            let keep: std::collections::HashSet<Key> =
                g.store.slots.keys().filter(|k| k.1.level == coarsest).copied().collect();
            g.store.release_except(&g.gl, &keep);
            g.vt.forget();
            unsafe { g.render.drop_targets(&g.gl) };
            g.render.last_rows = 0;            // nothing old is redrawn from released slots
        }
        before.saturating_sub(self.gpu_bytes())
    }
}
