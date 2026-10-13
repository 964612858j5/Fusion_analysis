//! R5 plan §2: when a frame may reserve GPU blocks and how long its
//! ordinary uploads may run (the input and upload hot windows, apart).

use std::time::{Duration, Instant};

/// R5 plan §2: one frame's pacing, `since` s after the last input. The
/// input window keeps blocks from being reserved ahead (`top_up`); the
/// upload window, apart from it, cuts ordinary uploads `hot_upload_ms`
/// after they start, else `frame_budget_ms` after the frame started.
#[derive(Clone, Copy, Debug)]
pub struct Pacing {
    pub since: f64,
    pub input_hot_s: f64,
    pub upload_hot_s: f64,
    pub hot_upload_ms: f64,
    pub frame_budget_ms: f64,
}

impl Pacing {
    /// the config's upload window: the input window when not given
    pub fn upload_window(given: Option<f64>, input_hot_s: f64) -> f64 {
        given.unwrap_or(input_hot_s)
    }

    pub fn input_hot(&self) -> bool {
        self.since < self.input_hot_s
    }

    pub fn deadline(&self, started: Instant, t_up: Instant) -> Instant {
        if self.since < self.upload_hot_s {
            t_up + Duration::from_secs_f64(self.hot_upload_ms / 1000.0)
        } else {
            started + Duration::from_secs_f64(self.frame_budget_ms / 1000.0)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(since: f64, upload_hot_s: f64, hot_upload_ms: f64) -> Pacing {
        Pacing { since, input_hot_s: 0.060, upload_hot_s, hot_upload_ms, frame_budget_ms: 12.0 }
    }

    fn cut_ms(p: Pacing) -> (f64, f64) {      // (after the frame start, after the upload start)
        let started = Instant::now();
        let t_up = started + Duration::from_millis(2);
        let d = p.deadline(started, t_up);
        ((d - started).as_secs_f64() * 1e3, (d - t_up).as_secs_f64() * 1e3)
    }

    #[test]
    fn upload_cold_while_input_hot_takes_the_frame_budget_and_reserves_nothing() {
        let p = at(0.030, 0.020, 1.5);                       // B, 30 ms after the input
        assert!(p.input_hot());
        assert!((cut_ms(p).0 - 12.0).abs() < 1e-6);
    }

    #[test]
    fn both_hot_cut_after_the_hot_upload() {
        for ms in [1.5, 4.0] {                               // A/B, C/D
            for window in [0.060, 0.020] {
                let p = at(0.010, window, ms);
                assert!(p.input_hot());
                assert!((cut_ms(p).1 - ms).abs() < 1e-6);
            }
        }
    }

    #[test]
    fn both_cold_take_the_frame_budget_and_may_reserve() {
        let p = at(0.100, 0.020, 4.0);
        assert!(!p.input_hot());
        assert!((cut_ms(p).0 - 12.0).abs() < 1e-6);
    }

    #[test]
    fn equal_windows_are_the_old_rule() {                    // A: one window, as before R5
        for since in [0.0, 0.030, 0.059, 0.060, 0.2] {
            let p = at(since, Pacing::upload_window(None, 0.060), 1.5);
            let hot = since < 0.060;
            assert_eq!(p.input_hot(), hot);
            let (from_start, from_up) = cut_ms(p);
            if hot { assert!((from_up - 1.5).abs() < 1e-6) } else { assert!((from_start - 12.0).abs() < 1e-6) }
        }
        assert_eq!(Pacing::upload_window(Some(0.020), 0.060), 0.020);
    }
}
