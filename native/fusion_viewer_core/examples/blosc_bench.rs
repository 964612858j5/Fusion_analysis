//! Diagnostic only (not shipped): time Blosc decode stages on real chunks.
use std::time::Instant;
fn main() {
    let dir = std::env::args().nth(1).expect("chunk dir");
    let mut frames = Vec::new();
    for e in std::fs::read_dir(&dir).unwrap() {
        let p = e.unwrap().path();
        if p.file_name().unwrap().to_string_lossy().starts_with('.') { continue; }
        frames.push(std::fs::read(p).unwrap());
        if frames.len() >= 100 { break; }
    }
    let t = Instant::now();
    let mut total = 0usize;
    for f in &frames { total += fusion_viewer_core::blosc::decode(f).unwrap().len(); }
    let el = t.elapsed().as_secs_f64() * 1000.0 / frames.len() as f64;
    println!("decode {el:.2} ms/chunk ({} MB)", total / frames.len() / 1_000_000);
    // lz4 alone on the split streams of frame 0 layout
    let f = &frames[0];
    let nbytes = u32::from_le_bytes(f[4..8].try_into().unwrap()) as usize;
    let blocksize = u32::from_le_bytes(f[8..12].try_into().unwrap()) as usize;
    println!("flags {:#x} typesize {} nbytes {} blocksize {}", f[2], f[3], nbytes, blocksize);
    let t = Instant::now();
    let mut v = vec![0f32; nbytes / 4];
    for _ in 0..100 { let b = fusion_viewer_core::blosc::decode(f).unwrap(); for (o, c) in v.iter_mut().zip(b.chunks_exact(4)) { *o = f32::from_le_bytes(c.try_into().unwrap()); } }
    println!("decode+f32 {:.2} ms", t.elapsed().as_secs_f64() * 10.0);
}
