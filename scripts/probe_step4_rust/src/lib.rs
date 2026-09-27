//! Block S4-1P: the fused per-label accumulator, Rust / Rayon version.
//!
//! One pass over a tile updates, per cell label and per channel:
//! count, sum, sum of squares, min, max -- and, when `geom` is set, the
//! geometry moments (count, Σx, Σy, Σx², Σy², Σxy) and the bounding box.
//! Each thread owns private accumulators for the whole label space and
//! works on its own band of rows; `acc_merge` reduces them once at the end.
//! Label 0 is background. Coordinates are global (tile origin added).
//!
//! The same contract as the Numba and CuPy kernels of the probe script.

use rayon::prelude::*;

struct Local {
    cnt: Vec<i64>,
    sx: Vec<f64>,
    sy: Vec<f64>,
    sxx: Vec<f64>,
    syy: Vec<f64>,
    sxy: Vec<f64>,
    bb: Vec<i32>, // per label: ymin, ymax, xmin, xmax
    s: Vec<f64>,  // per label x channel
    ss: Vec<f64>,
    mn: Vec<f32>,
    mx: Vec<f32>,
}

pub struct Acc {
    n: usize, // labels 0..n-1 (n = max label + 1)
    c: usize, // channels in total
    locals: Vec<Local>,
    pool: rayon::ThreadPool,
}

impl Local {
    fn new(n: usize, c: usize) -> Local {
        let mut bb = vec![0i32; n * 4];
        for l in 0..n {
            bb[l * 4] = i32::MAX;
            bb[l * 4 + 1] = i32::MIN;
            bb[l * 4 + 2] = i32::MAX;
            bb[l * 4 + 3] = i32::MIN;
        }
        Local {
            cnt: vec![0; n],
            sx: vec![0.0; n],
            sy: vec![0.0; n],
            sxx: vec![0.0; n],
            syy: vec![0.0; n],
            sxy: vec![0.0; n],
            bb,
            s: vec![0.0; n * c],
            ss: vec![0.0; n * c],
            mn: vec![f32::INFINITY; n * c],
            mx: vec![f32::NEG_INFINITY; n * c],
        }
    }
}

#[no_mangle]
pub extern "C" fn acc_new(threads: usize, n: usize, c: usize) -> *mut Acc {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(threads).build().unwrap();
    let locals = (0..threads).map(|_| Local::new(n, c)).collect();
    Box::into_raw(Box::new(Acc { n, c, locals, pool }))
}

#[no_mangle]
pub extern "C" fn acc_free(acc: *mut Acc) {
    if !acc.is_null() {
        unsafe { drop(Box::from_raw(acc)) };
    }
}

/// `labels`: h x w uint32, C order. `img`: cb x h x w float32, C order,
/// channels c0..c0+cb of the accumulator.
#[no_mangle]
pub extern "C" fn acc_add_tile(
    acc: *mut Acc,
    labels: *const u32,
    h: usize,
    w: usize,
    img: *const f32,
    cb: usize,
    y0: i64,
    x0: i64,
    c0: usize,
    geom: i32,
) {
    let acc = unsafe { &mut *acc };
    let labels = unsafe { std::slice::from_raw_parts(labels, h * w) };
    let img = unsafe { std::slice::from_raw_parts(img, cb * h * w) };
    let (n, c) = (acc.n, acc.c);
    let t = acc.locals.len();
    let plane = h * w;
    let geom = geom != 0;
    let locals = &mut acc.locals;
    acc.pool.install(|| {
        locals.par_iter_mut().enumerate().for_each(|(ti, loc)| {
            let r0 = ti * h / t;
            let r1 = (ti + 1) * h / t;
            for r in r0..r1 {
                let gy = (y0 + r as i64) as f64;
                let row = r * w;
                for q in 0..w {
                    let l = labels[row + q] as usize;
                    if l == 0 || l >= n {
                        continue;
                    }
                    if geom {
                        let gx = (x0 + q as i64) as f64;
                        loc.cnt[l] += 1;
                        loc.sx[l] += gx;
                        loc.sy[l] += gy;
                        loc.sxx[l] += gx * gx;
                        loc.syy[l] += gy * gy;
                        loc.sxy[l] += gx * gy;
                        let (iy, ix) = ((y0 + r as i64) as i32, (x0 + q as i64) as i32);
                        let b = &mut loc.bb[l * 4..l * 4 + 4];
                        if iy < b[0] { b[0] = iy; }
                        if iy > b[1] { b[1] = iy; }
                        if ix < b[2] { b[2] = ix; }
                        if ix > b[3] { b[3] = ix; }
                    }
                    let base = l * c + c0;
                    for k in 0..cb {
                        let v = img[k * plane + row + q];
                        let vd = v as f64;
                        loc.s[base + k] += vd;
                        loc.ss[base + k] += vd * vd;
                        if v < loc.mn[base + k] { loc.mn[base + k] = v; }
                        if v > loc.mx[base + k] { loc.mx[base + k] = v; }
                    }
                }
            }
        });
    });
}

/// Reduce the threads' accumulators into the caller's arrays (n, n*4 and
/// n*c long).
#[no_mangle]
pub extern "C" fn acc_merge(
    acc: *mut Acc,
    cnt: *mut i64,
    sx: *mut f64,
    sy: *mut f64,
    sxx: *mut f64,
    syy: *mut f64,
    sxy: *mut f64,
    bb: *mut i32,
    s: *mut f64,
    ss: *mut f64,
    mn: *mut f32,
    mx: *mut f32,
) {
    let acc = unsafe { &mut *acc };
    let (n, c) = (acc.n, acc.c);
    let o = |p: *mut f64, len: usize| unsafe { std::slice::from_raw_parts_mut(p, len) };
    let cnt = unsafe { std::slice::from_raw_parts_mut(cnt, n) };
    let (sx, sy, sxx, syy, sxy) = (o(sx, n), o(sy, n), o(sxx, n), o(syy, n), o(sxy, n));
    let bb = unsafe { std::slice::from_raw_parts_mut(bb, n * 4) };
    let (s, ss) = (o(s, n * c), o(ss, n * c));
    let mn = unsafe { std::slice::from_raw_parts_mut(mn, n * c) };
    let mx = unsafe { std::slice::from_raw_parts_mut(mx, n * c) };
    let locals = &acc.locals;
    acc.pool.install(|| {
        // per label, in parallel over labels
        cnt.par_iter_mut().enumerate().for_each(|(l, v)| *v = locals.iter().map(|x| x.cnt[l]).sum());
        for (dst, get) in [
            (sx, (|x: &Local, l: usize| x.sx[l]) as fn(&Local, usize) -> f64),
            (sy, |x: &Local, l: usize| x.sy[l]),
            (sxx, |x: &Local, l: usize| x.sxx[l]),
            (syy, |x: &Local, l: usize| x.syy[l]),
            (sxy, |x: &Local, l: usize| x.sxy[l]),
        ] {
            dst.par_iter_mut().enumerate().for_each(|(l, v)| *v = locals.iter().map(|x| get(x, l)).sum());
        }
        bb.par_chunks_mut(4).enumerate().for_each(|(l, b)| {
            b[0] = locals.iter().map(|x| x.bb[l * 4]).min().unwrap();
            b[1] = locals.iter().map(|x| x.bb[l * 4 + 1]).max().unwrap();
            b[2] = locals.iter().map(|x| x.bb[l * 4 + 2]).min().unwrap();
            b[3] = locals.iter().map(|x| x.bb[l * 4 + 3]).max().unwrap();
        });
        s.par_iter_mut().enumerate().for_each(|(i, v)| *v = locals.iter().map(|x| x.s[i]).sum());
        ss.par_iter_mut().enumerate().for_each(|(i, v)| *v = locals.iter().map(|x| x.ss[i]).sum());
        mn.par_iter_mut().enumerate().for_each(|(i, v)| {
            *v = locals.iter().map(|x| x.mn[i]).fold(f32::INFINITY, f32::min)
        });
        mx.par_iter_mut().enumerate().for_each(|(i, v)| {
            *v = locals.iter().map(|x| x.mx[i]).fold(f32::NEG_INFINITY, f32::max)
        });
    });
}
