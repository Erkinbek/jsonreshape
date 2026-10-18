//! `numbers_plain` (ARCHITECTURE.md §1.3): are all number tokens outside strings
//! reproduced exactly by `orjson.dumps(orjson.loads(tok))`? Pure Rust, no PyO3.
//!
//! For valid JSON this is exactly the spec rule. For any other input the result equals
//! the reference implementation's regular expression, which is
//! `(?:STRING|[^"\-0-9]++|NUMBER)*+` matched against the whole buffer:
//!
//! * STRING `"(?:[^"\\]++|\\.)*+"` – an unterminated string fails the match;
//! * NUMBER = optional `-`, then the maximal run `T` of `[0-9.]`; the byte after `T` must
//!   not be `e`/`E`, and `T` must be one of the plain shapes (see [`plain_run`]).
//!
//! It never panics and never reads out of bounds.

use crate::util::{load8, quote_or_backslash};

const C_OTHER: u8 = 0;
const C_QUOTE: u8 = 1;
const C_NUM: u8 = 2;

static CLASS: [u8; 256] = {
    let mut t = [C_OTHER; 256];
    t[b'"' as usize] = C_QUOTE;
    t[b'-' as usize] = C_NUM;
    let mut d = b'0';
    while d <= b'9' {
        t[d as usize] = C_NUM;
        d += 1;
    }
    t
};

/// Index just after the closing quote of the string whose content starts at `i`;
/// `None` when the string is unterminated (a trailing lone backslash included).
#[inline(always)]
fn skip_string(buf: &[u8], mut i: usize) -> Option<usize> {
    let n = buf.len();
    loop {
        while i + 8 <= n {
            let m = quote_or_backslash(load8(buf, i));
            if m != 0 {
                i += (m.trailing_zeros() >> 3) as usize;
                break;
            }
            i += 8;
        }
        while i < n && buf[i] != b'"' && buf[i] != b'\\' {
            i += 1;
        }
        if i >= n {
            return None;
        }
        if buf[i] == b'"' {
            return Some(i + 1);
        }
        // backslash + any byte
        if i + 1 >= n {
            return None;
        }
        i += 2;
    }
}

/// Is the `[0-9.]` run `t` (after an optional minus, `neg`) a plain number token?
/// Shapes (the reference regex alternatives): `0.0`; `I.0` with `I` = `[1-9]` + up to 14
/// digits, or 16 digits ending in `0`; `I.F` with `F` ending in `[1-9]` and at most 16
/// characters in total; `0.F` with at most 4 leading zeros and at most 15 further digits
/// ending in `[1-9]`; `0` (not after a minus); integers `[1-9][0-9]{0,17}`.
fn plain_run(t: &[u8], neg: bool) -> bool {
    if t.is_empty() {
        return false;
    }
    if t == b"0.0" {
        return true;
    }
    if t == b"0" {
        return !neg;
    }
    let dot = match t.iter().position(|&c| c == b'.') {
        None => return t[0] != b'0' && t.len() <= 18,
        Some(p) => p,
    };
    let (ip, fp) = (&t[..dot], &t[dot + 1..]);
    if fp.is_empty() || fp.contains(&b'.') || ip.is_empty() {
        return false;
    }
    if ip[0] != b'0' {
        // I.0
        if fp == b"0" && (ip.len() <= 15 || (ip.len() == 16 && ip[15] == b'0')) {
            return true;
        }
        // I.F
        return t.len() <= 16 && fp[fp.len() - 1] != b'0';
    }
    if ip != b"0" {
        return false;
    }
    // 0.F
    let z = fp.iter().take_while(|&&c| c == b'0').count();
    let r = &fp[z..];
    z <= 4 && !r.is_empty() && r.len() <= 15 && r[r.len() - 1] != b'0'
}

/// True iff every number token outside strings in `buf` is plain.
pub fn numbers_plain(buf: &[u8]) -> bool {
    let n = buf.len();
    let mut i = 0;
    while i < n {
        match CLASS[buf[i] as usize] {
            C_QUOTE => match skip_string(buf, i + 1) {
                Some(j) => i = j,
                None => return false,
            },
            C_NUM => {
                let neg = buf[i] == b'-';
                let ts = if neg { i + 1 } else { i };
                let mut j = ts;
                while j < n && (buf[j].is_ascii_digit() || buf[j] == b'.') {
                    j += 1;
                }
                if j < n && (buf[j] == b'e' || buf[j] == b'E') {
                    return false;
                }
                if !plain_run(&buf[ts..j], neg) {
                    return false;
                }
                i = j;
            }
            _ => i += 1,
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens() {
        for (tok, want) in [
            ("0", true),
            ("-0", false),
            ("1", true),
            ("-1", true),
            ("0.1", true),
            ("1.10", false),
            ("-0.0", true),
            ("0.0", true),
            ("100.0", true),
            ("1e400", false),
            ("1E5", false),
            ("2.5E+3", false),
            ("12345678901234567890", false),
            ("123456789012345678", true),
            ("1234567890123456789", false),
            ("-123456789012345678", true),
            ("123456789.123456789", false),
            ("0.00001", true),
            ("0.000001", false),
            ("3.141592653589793", false),
            ("3.14159265358979", true),
            ("1000000000000000.0", true),
            ("9999999999999999.0", false),
            ("12345678901234567.0", false),
            ("0.123456789012345", true),
            ("0.1234567890123456", false),
            ("0.00", false),
            ("00", false),
            ("01", false),
            ("1.", false),
            ("1.5.3", false),
            (".5", true),
        ] {
            assert_eq!(numbers_plain(tok.as_bytes()), want, "{tok}");
        }
    }

    #[test]
    fn strings_are_skipped() {
        assert!(numbers_plain(br#"{"a\"1e5":"1.10","b":[1,2.5,"-0"]}"#));
        assert!(!numbers_plain(br#"{"a":[1,2.50]}"#));
        assert!(numbers_plain(br#""k" : 12"#));
        assert!(!numbers_plain(b"\"unterminated 1e5"));
        assert!(!numbers_plain(b"\"ends with backslash\\"));
        assert!(numbers_plain(b""));
        assert!(!numbers_plain(b"-"));
        assert!(numbers_plain(b"1-2"));
    }
}
