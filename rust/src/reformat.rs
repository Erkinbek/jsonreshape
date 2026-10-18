//! Streaming whitespace normaliser (ARCHITECTURE.md §1.4). Pure Rust, no PyO3.
//!
//! Tokens are copied byte-for-byte; only whitespace outside strings is replaced. With an
//! indent, an opening bracket is held back until the next significant byte shows whether
//! the container is empty (`{}` / `[]`), so `finish()` may still emit a pending `{`/`[`;
//! compact output never holds anything back (same per-call output as the reference).

use crate::util::quote_or_backslash;

pub struct Reformatter {
    /// `None` = compact; `Some(unit)` = one indentation unit per level (may be empty for
    /// `indent=0`: newlines without indentation).
    unit: Option<Vec<u8>>,
    base_level: i64,
    level: i64,
    in_str: bool,
    esc: bool,
    pending: u8,
    finished: bool,
    out: Vec<u8>,
}

/// Bytes that are copied verbatim outside strings (number/literal characters); every
/// other byte is whitespace, a structural character or a quote.
static PLAIN: [bool; 256] = {
    let mut t = [true; 256];
    t[b' ' as usize] = false;
    t[b'\t' as usize] = false;
    t[b'\n' as usize] = false;
    t[b'\r' as usize] = false;
    t[b'{' as usize] = false;
    t[b'}' as usize] = false;
    t[b'[' as usize] = false;
    t[b']' as usize] = false;
    t[b',' as usize] = false;
    t[b':' as usize] = false;
    t[b'"' as usize] = false;
    t
};

impl Reformatter {
    pub fn new(unit: Option<Vec<u8>>, base_level: i64) -> Reformatter {
        Reformatter {
            unit,
            base_level,
            level: 0,
            in_str: false,
            esc: false,
            pending: 0,
            finished: false,
            out: Vec::new(),
        }
    }

    /// `finish()` has been called (the binding then rejects `feed()` with ValueError).
    pub fn finished(&self) -> bool {
        self.finished
    }

    #[inline]
    fn newline(&mut self) {
        if let Some(unit) = &self.unit {
            self.out.push(b'\n');
            let k = self.base_level + self.level;
            if k > 0 && !unit.is_empty() {
                for _ in 0..k {
                    self.out.extend_from_slice(unit);
                }
            }
        }
    }

    #[inline(always)]
    fn flush_pending(&mut self) {
        if self.pending != 0 {
            self.out.push(self.pending);
            self.pending = 0;
            self.newline();
        }
    }

    pub fn feed(&mut self, buf: &[u8]) -> &[u8] {
        self.out.clear();
        self.out.reserve(buf.len() + buf.len() / 4);
        let n = buf.len();
        let mut i = 0;
        while i < n {
            if self.in_str {
                if self.esc {
                    self.out.push(buf[i]);
                    self.esc = false;
                    i += 1;
                    continue;
                }
                // copy up to (and including) the next quote or backslash
                let mut j = i;
                while j + 8 <= n {
                    let m = quote_or_backslash(crate::util::load8(buf, j));
                    if m != 0 {
                        j += (m.trailing_zeros() >> 3) as usize;
                        break;
                    }
                    j += 8;
                }
                while j < n && buf[j] != b'"' && buf[j] != b'\\' {
                    j += 1;
                }
                if j >= n {
                    self.out.extend_from_slice(&buf[i..n]);
                    return &self.out;
                }
                self.out.extend_from_slice(&buf[i..=j]);
                if buf[j] == b'"' {
                    self.in_str = false;
                } else {
                    self.esc = true;
                }
                i = j + 1;
                continue;
            }
            let b = buf[i];
            match b {
                b' ' | b'\t' | b'\n' | b'\r' => {
                    i += 1;
                }
                b'"' => {
                    self.flush_pending();
                    self.out.push(b'"');
                    self.in_str = true;
                    i += 1;
                }
                b'{' | b'[' => {
                    self.flush_pending();
                    if self.unit.is_some() {
                        self.pending = b;
                    } else {
                        self.out.push(b);
                    }
                    self.level += 1;
                    i += 1;
                }
                b'}' | b']' => {
                    self.level -= 1;
                    if self.pending != 0 {
                        self.out.push(self.pending);
                        self.pending = 0;
                    } else {
                        self.newline();
                    }
                    self.out.push(b);
                    i += 1;
                }
                b',' => {
                    self.flush_pending();
                    self.out.push(b',');
                    self.newline();
                    i += 1;
                }
                b':' => {
                    self.flush_pending();
                    self.out.push(b':');
                    if self.unit.is_some() {
                        self.out.push(b' ');
                    }
                    i += 1;
                }
                _ => {
                    self.flush_pending();
                    let mut j = i + 1;
                    while j < n && PLAIN[buf[j] as usize] {
                        j += 1;
                    }
                    self.out.extend_from_slice(&buf[i..j]);
                    i = j;
                }
            }
        }
        &self.out
    }

    /// Output of the last `feed()`/`finish()` call.
    pub fn output(&self) -> &[u8] {
        &self.out
    }

    pub fn finish(&mut self) -> &[u8] {
        self.out.clear();
        self.finished = true;
        if self.pending != 0 {
            self.out.push(self.pending);
            self.pending = 0;
        }
        &self.out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fmt(unit: Option<&[u8]>, base: i64, chunks: &[&[u8]]) -> Vec<u8> {
        let mut r = Reformatter::new(unit.map(|u| u.to_vec()), base);
        let mut out = Vec::new();
        for c in chunks {
            out.extend_from_slice(r.feed(c));
        }
        out.extend_from_slice(r.finish());
        out
    }

    #[test]
    fn compact() {
        assert_eq!(fmt(None, 0, &[b"{ \"a\" : 1 ,\n \"b\" : [ 1 , 2 ] }"]), b"{\"a\":1,\"b\":[1,2]}");
        assert_eq!(fmt(None, 0, &[b"\"a b\\\" c\""]), b"\"a b\\\" c\"");
    }

    #[test]
    fn indented() {
        let want = b"{\n  \"a\": 1,\n  \"b\": [\n    1,\n    2\n  ],\n  \"c\": {},\n  \"d\": []\n}";
        assert_eq!(fmt(Some(b"  "), 0, &[b"{\"a\":1,\"b\":[1,2],\"c\":{ },\"d\":[]}"]), want.to_vec());
        // split everywhere
        let src = b"{\"a\":1,\"b\":[1,2],\"c\":{ },\"d\":[]}";
        let chunks: Vec<&[u8]> = src.chunks(1).collect();
        assert_eq!(fmt(Some(b"  "), 0, &chunks), want.to_vec());
        assert_eq!(fmt(Some(b"\t"), 1, &[b"[1]"]), b"[\n\t\t1\n\t]".to_vec());
        assert_eq!(fmt(Some(b""), 0, &[b"[1,2]"]), b"[\n1,\n2\n]".to_vec());
    }

    #[test]
    fn pending_open_flushed_at_finish() {
        assert_eq!(fmt(Some(b"  "), 0, &[b"{"]), b"{".to_vec());
        let mut r = Reformatter::new(Some(b"  ".to_vec()), 0);
        assert_eq!(r.feed(b"["), b"");
        assert_eq!(r.feed(b"]"), b"[]");
    }
}
