//! Small shared helpers (no PyO3 types): SWAR byte classification, UTF-8 lead-byte
//! table (Unicode Table 3-7), decoding of JSON string content into "surrogatepass"
//! UTF-8 (the byte form Python produces with `str.encode("utf-8", "surrogatepass")`),
//! little-endian span encoding and Python-style slice index normalisation.

pub const LO: u64 = 0x0101_0101_0101_0101;
pub const HI: u64 = 0x8080_8080_8080_8080;

/// Unaligned little-endian load of 8 bytes at `i` (caller guarantees `i + 8 <= buf.len()`).
#[inline(always)]
pub fn load8(buf: &[u8], i: usize) -> u64 {
    debug_assert!(i + 8 <= buf.len());
    let b: [u8; 8] = buf[i..i + 8].try_into().unwrap();
    u64::from_le_bytes(b)
}

/// High bit set in every byte lane that is zero (lowest flagged lane is exact; higher
/// lanes may be false positives caused by borrows, which never matters because callers
/// only use the lowest set bit).
#[inline(always)]
pub fn has_zero(x: u64) -> u64 {
    x.wrapping_sub(LO) & !x & HI
}

/// Lanes equal to `b`.
#[inline(always)]
pub fn has_byte(x: u64, b: u8) -> u64 {
    has_zero(x ^ LO.wrapping_mul(b as u64))
}

/// Lanes holding a byte `< n` (n <= 0x80); lanes `>= 0x80` are never flagged.
#[inline(always)]
pub fn has_less(x: u64, n: u8) -> u64 {
    x.wrapping_sub(LO.wrapping_mul(n as u64)) & !x & HI
}

/// Lanes that interrupt a plain run of string content: `"`, `\`, `< 0x20`, `>= 0x80`.
#[inline(always)]
pub fn str_special(x: u64) -> u64 {
    has_byte(x, b'"') | has_byte(x, b'\\') | has_less(x, 0x20) | (x & HI)
}

/// Lanes holding `"` or `\`.
#[inline(always)]
pub fn quote_or_backslash(x: u64) -> u64 {
    has_byte(x, b'"') | has_byte(x, b'\\')
}

/// Byte-at-a-time counterpart of [`str_special`].
pub static STR_SPECIAL: [bool; 256] = {
    let mut t = [false; 256];
    let mut i = 0;
    while i < 256 {
        t[i] = i < 0x20 || i == b'"' as usize || i == b'\\' as usize || i >= 0x80;
        i += 1;
    }
    t
};

/// JSON whitespace (RFC 8259): space, tab, LF, CR.
#[inline(always)]
pub fn is_ws(b: u8) -> bool {
    b == b' ' || b == b'\n' || b == b'\r' || b == b'\t'
}

/// Bytes that may start a JSON value.
pub static VALUE_START: [bool; 256] = {
    let mut t = [false; 256];
    t[b'{' as usize] = true;
    t[b'[' as usize] = true;
    t[b'"' as usize] = true;
    t[b'-' as usize] = true;
    t[b't' as usize] = true;
    t[b'f' as usize] = true;
    t[b'n' as usize] = true;
    let mut d = b'0';
    while d <= b'9' {
        t[d as usize] = true;
        d += 1;
    }
    t
};

/// UTF-8 lead byte → (number of continuation bytes, allowed range of the FIRST
/// continuation byte). `None` for bytes that cannot start a multi-byte sequence
/// (continuation bytes, overlong leads C0/C1, F5..FF). Ranges follow Unicode Table 3-7, so
/// overlongs, UTF-8 encoded surrogates and code points above U+10FFFF are rejected.
#[inline(always)]
pub fn utf8_lead(b: u8) -> Option<(u8, u8, u8)> {
    match b {
        0xC2..=0xDF => Some((1, 0x80, 0xBF)),
        0xE0 => Some((2, 0xA0, 0xBF)),
        0xE1..=0xEC | 0xEE..=0xEF => Some((2, 0x80, 0xBF)),
        0xED => Some((2, 0x80, 0x9F)),
        0xF0 => Some((3, 0x90, 0xBF)),
        0xF1..=0xF3 => Some((3, 0x80, 0xBF)),
        0xF4 => Some((3, 0x80, 0x8F)),
        _ => None,
    }
}

/// Validate the multi-byte sequence starting at `buf[i]` (a byte >= 0x80) when it lies
/// completely inside `buf[..end]`.
/// Returns `Ok(len)` (sequence length), `Err(true)` if invalid, `Err(false)` if the
/// buffer ends (at `end`) before the sequence is complete but what is there is valid.
#[inline(always)]
pub fn utf8_seq(buf: &[u8], i: usize, end: usize) -> Result<usize, bool> {
    let (need, lo, hi) = match utf8_lead(buf[i]) {
        Some(t) => t,
        None => return Err(true),
    };
    let need = need as usize;
    let mut j = i + 1;
    let mut lo = lo;
    let mut hi = hi;
    while j <= i + need {
        if j >= end {
            return Err(false);
        }
        let c = buf[j];
        if c < lo || c > hi {
            return Err(true);
        }
        lo = 0x80;
        hi = 0xBF;
        j += 1;
    }
    Ok(need + 1)
}

#[inline(always)]
pub fn hex_val(b: u8) -> Option<u32> {
    match b {
        b'0'..=b'9' => Some((b - b'0') as u32),
        b'a'..=b'f' => Some((b - b'a' + 10) as u32),
        b'A'..=b'F' => Some((b - b'A' + 10) as u32),
        _ => None,
    }
}

fn hex4(raw: &[u8], i: usize) -> Option<u32> {
    if i + 4 > raw.len() {
        return None;
    }
    let mut v = 0u32;
    for k in 0..4 {
        v = (v << 4) | hex_val(raw[i + k])?;
    }
    Some(v)
}

/// Append code point `cp` (any value <= 0x10FFFF, surrogates included) as UTF-8;
/// surrogates get the 3-byte form exactly like Python's "surrogatepass" handler.
pub fn push_cp(out: &mut Vec<u8>, cp: u32) {
    if cp < 0x80 {
        out.push(cp as u8);
    } else if cp < 0x800 {
        out.push(0xC0 | (cp >> 6) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else if cp < 0x10000 {
        out.push(0xE0 | (cp >> 12) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else {
        out.push(0xF0 | (cp >> 18) as u8);
        out.push(0x80 | ((cp >> 12) & 0x3F) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    }
}

/// Decode the content of a JSON string (bytes between the quotes, already validated)
/// into `out` as "surrogatepass" UTF-8, matching Python's `json` decoder: a high surrogate
/// escape immediately followed by a low surrogate escape is joined into one code point,
/// any other surrogate escape stays a lone surrogate. Never panics on malformed input.
pub fn decode_json_str(raw: &[u8], out: &mut Vec<u8>) {
    out.clear();
    let n = raw.len();
    let mut i = 0;
    while i < n {
        let j = match memchr::memchr(b'\\', &raw[i..]) {
            Some(k) => i + k,
            None => n,
        };
        out.extend_from_slice(&raw[i..j]);
        if j >= n {
            break;
        }
        // escape at j
        let e = match raw.get(j + 1) {
            Some(&e) => e,
            None => break,
        };
        i = j + 2;
        match e {
            b'b' => out.push(0x08),
            b'f' => out.push(0x0C),
            b'n' => out.push(b'\n'),
            b'r' => out.push(b'\r'),
            b't' => out.push(b'\t'),
            b'u' => {
                let mut cp = match hex4(raw, i) {
                    Some(v) => v,
                    None => break,
                };
                i += 4;
                if (0xD800..0xDC00).contains(&cp)
                    && i + 6 <= n
                    && raw[i] == b'\\'
                    && raw[i + 1] == b'u'
                {
                    if let Some(lo) = hex4(raw, i + 2) {
                        if (0xDC00..0xE000).contains(&lo) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            i += 6;
                        }
                    }
                }
                push_cp(out, cp);
            }
            other => out.push(other), // '"', '\\', '/'
        }
    }
}

/// Append a little-endian u64.
#[inline(always)]
pub fn push_u64(out: &mut Vec<u8>, v: u64) {
    out.extend_from_slice(&v.to_le_bytes());
}

/// Read u64 LE pairs from a span array. `Err` when the length is not a multiple of 16.
pub fn read_pairs(spans: &[u8]) -> Result<Vec<(u64, u64)>, String> {
    if !spans.len().is_multiple_of(16) {
        return Err(format!(
            "span array length {} is not a multiple of 16 bytes",
            spans.len()
        ));
    }
    let mut v = Vec::with_capacity(spans.len() / 16);
    for c in spans.chunks_exact(16) {
        let s = u64::from_le_bytes(c[0..8].try_into().unwrap());
        let e = u64::from_le_bytes(c[8..16].try_into().unwrap());
        v.push((s, e));
    }
    Ok(v)
}

/// Python slice-index normalisation (as used by `bytes.find(sub, start, end)`).
pub fn norm_index(idx: i64, len: usize) -> usize {
    let len_i = len as i64;
    if idx < 0 {
        let v = idx + len_i;
        if v < 0 {
            0
        } else {
            v as usize
        }
    } else if idx > len_i {
        len
    } else {
        idx as usize
    }
}

/// `Some(n)` when `s` is a canonical non-negative decimal (`0` or `[1-9][0-9]*`) that fits
/// in u64; canonical decimals too large for u64 give `Some(u64::MAX)` (they can never be a
/// valid index anyway); `None` otherwise.
pub fn canonical_index(s: &[u8]) -> Option<u64> {
    if s.is_empty() || !s.iter().all(|c| c.is_ascii_digit()) {
        return None;
    }
    if s.len() > 1 && s[0] == b'0' {
        return None;
    }
    let mut v: u64 = 0;
    for &c in s {
        v = match v.checked_mul(10).and_then(|x| x.checked_add((c - b'0') as u64)) {
            Some(x) => x,
            None => return Some(u64::MAX),
        };
    }
    Some(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn special_first(bytes: &[u8]) -> Option<usize> {
        let x = load8(bytes, 0);
        let m = str_special(x);
        if m == 0 {
            None
        } else {
            Some((m.trailing_zeros() / 8) as usize)
        }
    }

    #[test]
    fn swar_special() {
        assert_eq!(special_first(b"abcdefgh"), None);
        assert_eq!(special_first(b"abc\"efgh"), Some(3));
        assert_eq!(special_first(b"abc\\efgh"), Some(3));
        assert_eq!(special_first(b"\x1fbcdefgh"), Some(0));
        assert_eq!(special_first(b"abcdefg\x80"), Some(7));
        assert_eq!(special_first(b"ab\x00\x20\"fgh"), Some(2));
        assert_eq!(special_first(b"  \x01!\"\x7f~ "), Some(2));
        // borrow false positives never precede the first true hit
        for b in 0u16..256 {
            for pos in 0..8 {
                let mut a = *b"ABCDEFGH";
                a[pos] = b as u8;
                let want = if STR_SPECIAL[b as usize] { Some(pos) } else { None };
                assert_eq!(special_first(&a), want, "byte {b} at {pos}");
            }
        }
    }

    #[test]
    fn utf8_sequences() {
        assert_eq!(utf8_seq(b"\xc3\xa9", 0, 2), Ok(2));
        assert_eq!(utf8_seq(b"\xc3", 0, 1), Err(false));
        assert_eq!(utf8_seq(b"\xc3(", 0, 2), Err(true));
        assert_eq!(utf8_seq(b"\xe0\x80\x80", 0, 3), Err(true)); // overlong
        assert_eq!(utf8_seq(b"\xed\xa0\x80", 0, 3), Err(true)); // surrogate
        assert_eq!(utf8_seq(b"\xf4\x90\x80\x80", 0, 4), Err(true)); // > U+10FFFF
        assert_eq!(utf8_seq(b"\xf0\x9f\x98\x80", 0, 4), Ok(4));
        assert_eq!(utf8_seq(b"\xc0\xaf", 0, 2), Err(true));
        assert_eq!(utf8_seq(b"\x80", 0, 1), Err(true));
    }

    #[test]
    fn decode_strings() {
        let mut out = Vec::new();
        decode_json_str(br#"a\"b\\c\/d\b\f\n\r\t"#, &mut out);
        assert_eq!(out, b"a\"b\\c/d\x08\x0c\n\r\t");
        // (written with \x5c = backslash so no editor/tool turns the escapes into text)
        decode_json_str(b"\x5cu0041\x5cu00e9", &mut out);
        assert_eq!(out, b"A\xc3\xa9");
        decode_json_str(b"\x5cuD83D\x5cude00", &mut out);
        assert_eq!(out, b"\xf0\x9f\x98\x80");
        decode_json_str(b"\x5cud800x", &mut out);
        assert_eq!(out, b"\xed\xa0\x80x");
        decode_json_str(b"\x5cud800\x5cu0041", &mut out);
        assert_eq!(out, b"\xed\xa0\x80A");
        decode_json_str(b"\x5cudc00\x5cud800", &mut out);
        assert_eq!(out, b"\xed\xb0\x80\xed\xa0\x80");
        decode_json_str(b"\x5cud800", &mut out);
        assert_eq!(out, b"\xed\xa0\x80");
    }

    #[test]
    fn indices() {
        assert_eq!(norm_index(-1, 10), 9);
        assert_eq!(norm_index(-20, 10), 0);
        assert_eq!(norm_index(20, 10), 10);
        assert_eq!(canonical_index(b"0"), Some(0));
        assert_eq!(canonical_index(b"12"), Some(12));
        assert_eq!(canonical_index(b"012"), None);
        assert_eq!(canonical_index(b""), None);
        assert_eq!(canonical_index(b"-1"), None);
        assert_eq!(canonical_index(b"99999999999999999999999"), Some(u64::MAX));
    }
}
