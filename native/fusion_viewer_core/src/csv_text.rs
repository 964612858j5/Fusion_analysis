//! Step4's CSV rows as text, the same characters as NumPy's `savetxt`
//! with `%d` / `%.17g` (plan `docs/v17_step4_x_stream_plan.md` §1 B).
//!
//! `%.17g` takes the 17 correctly rounded significant digits of the
//! double's exact value (integer arithmetic; ties to even, as glibc and
//! Python), laid out by C's `%g` rules, so the text does not depend on the
//! platform's printf. Values whose exact arithmetic does not fit 128 bits
//! (beyond roughly 1e-6 .. 1e21) take Rust's `{:.16e}`, slower but equal.
//! Row blocks are formatted on several threads; the output keeps row order.

const P17: u64 = 100_000_000_000_000_000;        // 10^17
const P16: u64 = 10_000_000_000_000_000;         // 10^16

/// One cell as C's `%.17g` (Python's `'%.17g' % x`): `nan`, `inf`, `-inf`,
/// `-0`, `1e+20`, `1.0000000000000001e-05`, `0.10000000000000001`.
pub fn push_g17(out: &mut String, x: f64) {
    if x.is_nan() {
        out.push_str("nan");
        return;
    }
    if x.is_infinite() {
        out.push_str(if x < 0.0 { "-inf" } else { "inf" });
        return;
    }
    if x.is_sign_negative() {
        out.push('-');
    }
    let a = x.abs();
    if a == 0.0 {
        out.push('0');
        return;
    }
    let (digits, exp) = digits17(a).unwrap_or_else(|| slow_digits17(a));
    lay_out(out, digits, exp);
}

/// (D, X): 10^16 <= D < 10^17 and |x| ~ D * 10^(X-16), D correctly rounded.
fn digits17(a: f64) -> Option<(u64, i32)> {
    let bits = a.to_bits();
    let (be, frac) = (((bits >> 52) & 0x7ff) as i32, bits & ((1u64 << 52) - 1));
    let (m, e) = if be == 0 { (frac, -1074) } else { (frac | (1u64 << 52), be - 1075) };
    let mut x = a.log10().floor() as i32;
    for _ in 0..3 {
        // the exponent from the exact (unrounded) quotient; rounding may
        // then carry into 10^17, which C prints as 1 followed by zeros
        let (floor, d) = scaled(m, e, x - 16)?;
        if floor >= P17 as u128 {
            x += 1;
        } else if floor < P16 as u128 {
            x -= 1;
        } else if d == P17 {
            return Some((P16, x + 1));
        } else {
            return Some((d, x));
        }
    }
    None
}

/// (floor, round_half_even) of m * 2^e / 10^q, or None when it does not
/// fit u128.
fn scaled(m: u64, e: i32, q: i32) -> Option<(u128, u64)> {
    let p10 = |k: i32| 10u128.checked_pow(k as u32);
    let (num, den) = match (e >= 0, q >= 0) {
        (true, true) => ((m as u128).checked_shl(e as u32).filter(|v| v >> e == m as u128)?, p10(q)?),
        (true, false) => ((m as u128).checked_shl(e as u32).filter(|v| v >> e == m as u128)?
                              .checked_mul(p10(-q)?)?, 1),
        (false, true) => (m as u128, p10(q)?.checked_mul(1u128.checked_shl((-e) as u32)?)?),
        (false, false) => ((m as u128).checked_mul(p10(-q)?)?, 1u128.checked_shl((-e) as u32)?),
    };
    let (floor, r) = (num / den, num % den);
    let rest = den - r;                          // compare r with den / 2 without overflow
    let d = if r > rest || (r == rest && floor % 2 == 1) { floor + 1 } else { floor };
    Some((floor, u64::try_from(d).ok()?))
}

fn slow_digits17(a: f64) -> (u64, i32) {
    let sci = format!("{a:.16e}");             // "d.dddddddddddddddde-5"
    let (mant, exp) = sci.split_once('e').expect("{:e} has an exponent");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    (digits.parse().expect("17 digits"), exp.parse().expect("an exponent"))
}

fn s(b: &[u8]) -> &str {
    std::str::from_utf8(b).expect("ascii digits")
}

/// C's `%g` layout of D * 10^(X-16), trailing zeros dropped.
fn lay_out(out: &mut String, d: u64, x: i32) {
    let mut buf = [0u8; 17];
    let mut v = d;
    for i in (0..17).rev() {
        buf[i] = b'0' + (v % 10) as u8;
        v /= 10;
    }
    let mut end = 17;
    while end > 1 && buf[end - 1] == b'0' {
        end -= 1;
    }
    if (-4..17).contains(&x) {
        if x >= 0 {
            let int = x as usize + 1;
            out.push_str(s(&buf[..int]));
            if end > int {
                out.push('.');
                out.push_str(s(&buf[int..end]));
            }
        } else {
            out.push_str("0.");
            for _ in 0..(-x - 1) {
                out.push('0');
            }
            out.push_str(s(&buf[..end]));
        }
    } else {
        out.push(buf[0] as char);
        if end > 1 {
            out.push('.');
            out.push_str(s(&buf[1..end]));
        }
        out.push('e');
        out.push(if x < 0 { '-' } else { '+' });
        let e = x.unsigned_abs();
        if e < 10 {
            out.push('0');
        }
        out.push_str(&e.to_string());
    }
}

/// One cell as Python's `'%d' % x` for a float: truncated toward zero.
/// NaN and infinities have no integer (Python raises): `Err`.
pub fn push_d(out: &mut String, x: f64) -> Result<(), String> {
    if !x.is_finite() {
        return Err(format!("cannot write {x} as an integer"));
    }
    let t = x.trunc();
    if t.abs() < 9.0e15 {
        let mut v = t as i64;
        if v < 0 {
            out.push('-');
            v = -v;
        }
        let mut buf = [0u8; 20];
        let mut i = buf.len();
        loop {
            i -= 1;
            buf[i] = b'0' + (v % 10) as u8;
            v /= 10;
            if v == 0 {
                break;
            }
        }
        out.push_str(std::str::from_utf8(&buf[i..]).expect("ascii digits"));
    } else {
        // beyond 2^53 every float is an integer: its exact decimal value
        out.push_str(&format!("{t:.0}"));
    }
    Ok(())
}

/// Rows of a row-major `rows x cols` table as CSV lines, each ended by
/// `newline`. `integer[k]`: column k as `%d`, else `%.17g`. `extra`: text
/// columns put right after column 0 (`extra[j][row]`). Formatted on
/// `threads` threads; the parts, concatenated in order, are the text.
pub fn format_rows(values: &[f64], cols: usize, integer: &[bool], extra: &[Vec<String>],
                   newline: &str, threads: usize) -> Result<Vec<String>, String> {
    if cols == 0 || integer.len() != cols || values.len() % cols != 0 {
        return Err(format!("table of {} values does not have {cols} columns", values.len()));
    }
    let rows = values.len() / cols;
    if extra.iter().any(|e| e.len() != rows) {
        return Err(format!("extra text columns must have {rows} rows"));
    }
    let threads = threads.clamp(1, 64).min(rows.max(1));
    let per = rows.div_ceil(threads.max(1)).max(1);
    let parts: Vec<Result<String, String>> = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..rows).step_by(per).map(|r0| {
            let r1 = (r0 + per).min(rows);
            scope.spawn(move || {
                let mut out = String::with_capacity((r1 - r0) * cols * 20);
                for r in r0..r1 {
                    let row = &values[r * cols..(r + 1) * cols];
                    for (k, &x) in row.iter().enumerate() {
                        if k > 0 {
                            out.push(',');
                        }
                        if integer[k] { push_d(&mut out, x)? } else { push_g17(&mut out, x) }
                        if k == 0 {
                            for e in extra {
                                out.push(',');
                                out.push_str(&e[r]);
                            }
                        }
                    }
                    out.push_str(newline);
                }
                Ok(out)
            })
        }).collect();
        handles.into_iter().map(|h| h.join().unwrap_or_else(|_| Err("CSV formatter panicked".into())))
            .collect()
    });
    parts.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn g(x: f64) -> String {
        let mut s = String::new();
        push_g17(&mut s, x);
        s
    }

    #[test]
    fn g17_matches_c_printf() {
        // expected strings: Python's '%.17g' % x
        for (x, want) in [(0.0, "0"), (-0.0, "-0"), (1.0, "1"), (0.1, "0.10000000000000001"),
                          (1e-5, "1.0000000000000001e-05"), (1e-4, "0.0001"),
                          (123456.0, "123456"), (1e16, "10000000000000000"),
                          (1e17, "1e+17"), (2.5e-300, "2.5e-300"),
                          (f64::NAN, "nan"), (f64::INFINITY, "inf"),
                          (f64::NEG_INFINITY, "-inf"), (-1.5, "-1.5"),
                          (1.7976931348623157e308, "1.7976931348623157e+308"),
                          (5e-324, "4.9406564584124654e-324"), (1e-6, "9.9999999999999995e-07"),
                          (99999999999999999.0, "1e+17")] {
            assert_eq!(g(x), want, "{x:e}");
        }
    }

    #[test]
    fn d_truncates_and_refuses_nan() {
        let mut s = String::new();
        push_d(&mut s, 3.7).unwrap();
        s.push(' ');
        push_d(&mut s, -3.7).unwrap();
        s.push(' ');
        push_d(&mut s, 1e17).unwrap();
        assert_eq!(s, "3 -3 100000000000000000");
        assert!(push_d(&mut s, f64::NAN).is_err());
    }

    #[test]
    fn rows_keep_order_and_put_extra_after_the_id() {
        let v: Vec<f64> = (0..30).map(|i| i as f64 * 0.5).collect();
        let extra = vec![(0..10).map(|r| format!("t{r}")).collect::<Vec<_>>()];
        let one = format_rows(&v, 3, &[true, false, false], &extra, "\n", 1).unwrap().concat();
        let many = format_rows(&v, 3, &[true, false, false], &extra, "\n", 7).unwrap().concat();
        assert_eq!(one, many);
        assert!(one.starts_with("0,t0,0.5,1\n1,t1,2,2.5\n"));
        assert_eq!(one.lines().count(), 10);
    }
}
