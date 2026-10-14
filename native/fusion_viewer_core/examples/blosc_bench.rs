//! Diagnostic only (not shipped): time Blosc decode stages on real chunks.
use std::time::Instant;
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("compare") {
        compare(&args[2..]);
        return;
    }
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

/// R7a §2 (diagnostic, not shipped): like-for-like decoder comparison.
/// `blosc_bench compare DIR...`: up to 20 real chunks per directory, each
/// decoded 20 times, single thread, release build, into the same 4 MiB
/// output. Whole chunk: ours (`blosc::decode_into`) vs C-Blosc
/// (`blosc_decompress_ctx`, 1 internal thread). LZ4 alone, on exactly the
/// same streams: ours (`lz4::decompress_into`) vs `lz4_flex`.
#[allow(dead_code)]
pub fn compare(dirs: &[String]) {
    use std::time::Instant;
    let mut chunks = Vec::new();
    for d in dirs {
        let mut n = 0;
        let mut names: Vec<_> = std::fs::read_dir(d).unwrap().map(|e| e.unwrap().path()).collect();
        names.sort();
        for p in names {
            if p.file_name().unwrap().to_string_lossy().starts_with('.') { continue; }
            chunks.push(std::fs::read(&p).unwrap());
            n += 1;
            if n >= 20 { break; }
        }
    }
    let med = |mut v: Vec<f64>| { v.sort_by(|a, b| a.partial_cmp(b).unwrap()); v[v.len() / 2] };
    let (mut ours, mut c, mut lz_ours, mut lz_flex) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut out_bytes = 0;
    for f in &chunks {
        let nbytes = u32::from_le_bytes(f[4..8].try_into().unwrap()) as usize;
        out_bytes = nbytes;
        let mut a = vec![0u8; nbytes];
        let mut b = vec![0u8; nbytes];
        let mut t1 = Vec::new();
        let mut t2 = Vec::new();
        for _ in 0..20 {
            let t = Instant::now();
            fusion_viewer_core::blosc::decode_into(f, &mut a).unwrap();
            t1.push(t.elapsed().as_secs_f64() * 1000.0);
            let t = Instant::now();
            let r = unsafe { blosc_src::blosc_decompress_ctx(f.as_ptr() as *const _, b.as_mut_ptr() as *mut _, nbytes, 1) };
            t2.push(t.elapsed().as_secs_f64() * 1000.0);
            assert!(r as usize == nbytes);
        }
        assert!(a == b, "decoders disagree");
        ours.push(med(t1));
        c.push(med(t2));
        // the LZ4 streams of this chunk (header: 16 bytes, then bstarts)
        let flags = f[2];
        let typesize = f[3] as usize;
        let blocksize = u32::from_le_bytes(f[8..12].try_into().unwrap()) as usize;
        let nblocks = nbytes.div_ceil(blocksize);
        let split = flags & 0x10 == 0 && typesize <= 16 && blocksize / typesize >= 128;
        let nsplits = if split { typesize } else { 1 };
        let mut streams = Vec::new();
        for bi in 0..nblocks {
            let bsize = blocksize.min(nbytes - bi * blocksize);
            if bsize != blocksize { continue; }
            let mut at = u32::from_le_bytes(f[16 + 4 * bi..20 + 4 * bi].try_into().unwrap()) as usize;
            for _ in 0..nsplits {
                let stored = u32::from_le_bytes(f[at..at + 4].try_into().unwrap()) as usize;
                at += 4;
                if stored != bsize / nsplits { streams.push((&f[at..at + stored], bsize / nsplits)); }
                at += stored;
            }
        }
        let mut dst = vec![0u8; blocksize];
        let mut s1 = Vec::new();
        let mut s2 = Vec::new();
        for _ in 0..20 {
            let t = Instant::now();
            for (src, n) in &streams { fusion_viewer_core::lz4::decompress_into(src, &mut dst[..*n]).unwrap(); }
            s1.push(t.elapsed().as_secs_f64() * 1000.0);
            let t = Instant::now();
            for (src, n) in &streams { lz4_flex::block::decompress_into(src, &mut dst[..*n]).unwrap(); }
            s2.push(t.elapsed().as_secs_f64() * 1000.0);
        }
        lz_ours.push(med(s1));
        lz_flex.push(med(s2));
    }
    let mb = out_bytes as f64 / 1e6;
    println!("chunks {} ({} per dir max), output {:.2} MB each", chunks.len(), 20, mb);
    for (name, v) in [("whole chunk ours", &ours), ("whole chunk C-Blosc 1 thread", &c),
                      ("LZ4 only ours", &lz_ours), ("LZ4 only lz4_flex", &lz_flex)] {
        let m = med(v.clone());
        println!("{name:30} median {m:.3} ms/chunk  ({:.0} MB/s)", mb / (m / 1000.0));
    }
}
