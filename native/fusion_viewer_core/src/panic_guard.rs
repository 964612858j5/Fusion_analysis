//! A reader's panic becomes that read's error (docs/v17_error_guard_plan.md
//! §3): the thread lives on and the existing failure path (key released,
//! failure counted, the GUI told) takes it. Only the read itself runs here;
//! queue and cache work stays outside, as before.

use std::any::Any;
use std::panic::{catch_unwind, AssertUnwindSafe};

/// `f()`, or the panic's message.
pub fn run<T>(f: impl FnOnce() -> T) -> Result<T, String> {
    // AssertUnwindSafe: a read touches no shared state but through locks
    // it takes and releases itself, so nothing is left half-changed
    catch_unwind(AssertUnwindSafe(f)).map_err(|p| message(&*p))
}

fn message(payload: &(dyn Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "a panic without a message".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_value_passes_and_a_panic_becomes_its_message() {
        assert_eq!(run(|| 7), Ok(7));
        assert_eq!(run(|| -> u8 { panic!("decode broke") }), Err("decode broke".to_string()));
        let n = 3;
        assert_eq!(run(|| -> u8 { panic!("tile {n}") }), Err("tile 3".to_string()));
    }
}
