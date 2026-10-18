//! `MultiFinder::locate_tokens` (ARCHITECTURE.md §1.5): which keys and scalars of JSON
//! records hold a needle occurrence, with their paths. Pure Rust, no PyO3.
//!
//! One validating pass per span (the grammar and nesting limit of the Scanner /
//! PathExtractor, §1.1). The occurrences are found lazily while the tokens go by: `occ`
//! is the leftmost occurrence lying entirely inside the span that starts at or after the
//! end of the last token examined. A token `[ts, te)` (string with its quotes, number or
//! literal) is located when `ts <= occ < te`; an `occ` before `ts` started on whitespace
//! or structure (the only bytes between two tokens) and is ignored, so the search resumes
//! at `ts`. After a located token the search resumes at `te`: any later occurrence
//! starting inside the same token would locate it again. Every occurrence is accounted for
//! with one search per located token plus one per occurrence ignored, and the searches
//! never scan a byte twice.
//!
//! While a span has an occurrence at all, every object's keys are checked for duplicates
//! (compared after decoding escapes, exactly: small objects pairwise, larger ones with a
//! hash set), and a span with located tokens and a duplicate key anywhere gets one
//! [`KIND_CONFIRM`] entry after its tokens.

use std::collections::HashSet;

use crate::finder::MultiFinder;
use crate::util::{decode_json_str, load8, str_special, utf8_seq, STR_SPECIAL};

/// A string value.
pub const KIND_STRING: u8 = 0;
/// An object key (its path is the path of the member value).
pub const KIND_KEY: u8 = 1;
/// A number.
pub const KIND_NUMBER: u8 = 2;
/// `true`, `false` or `null`.
pub const KIND_LITERAL: u8 = 3;
/// The member's own key of a member span (`member_spans`); empty path.
pub const KIND_MEMBER_KEY: u8 = 4;
/// "Confirm this record by parsing": the record has duplicate keys somewhere.
pub const KIND_CONFIRM: u8 = 5;

/// Container nesting accepted inside one record (the Scanner's default `max_depth`).
const MAX_DEPTH: usize = crate::scanner::DEFAULT_MAX_DEPTH;
/// "No further occurrence" (larger than every token end).
const NONE: usize = usize::MAX;
/// Objects with more keys than this check duplicates with a hash set.
const SMALL_OBJECT: usize = 16;

/// One path segment: an object key (raw content between the quotes, `esc` = it has
/// backslash escapes) or an array index.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Seg {
    Key { start: usize, end: usize, esc: bool },
    Index(u64),
}

/// A located token: `start..end` absolute in `buf` (quotes included), its path is
/// `Located::segs[path.0..path.1]`. `KIND_CONFIRM` entries have `start == end == 0` and an
/// empty path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Token {
    pub span: usize,
    pub kind: u8,
    pub start: usize,
    pub end: usize,
    pub path: (usize, usize),
}

/// Result of [`MultiFinder::locate_tokens`].
#[derive(Default, Debug)]
pub struct Located {
    pub tokens: Vec<Token>,
    pub segs: Vec<Seg>,
}

struct Frame {
    is_obj: bool,
    /// current member key / element index
    seg: Seg,
    /// elements seen so far (arrays)
    count: u64,
    /// this object's entries in `Scratch::keys` / `Scratch::arena` start here
    keys_base: usize,
    arena_base: usize,
    /// decoded keys of an object with more than `SMALL_OBJECT` keys
    set: Option<HashSet<Box<[u8]>>>,
}

/// Decoded key of the duplicate check: `buf[start..end]` (no escapes) or
/// `arena[start..end]`.
#[derive(Clone, Copy)]
struct KeyRef {
    start: usize,
    end: usize,
    in_arena: bool,
}

/// Per-call scratch state (so `locate_tokens` only needs `&self`).
#[derive(Default)]
struct Scratch {
    frames: Vec<Frame>,
    keys: Vec<KeyRef>,
    arena: Vec<u8>,
    tmp: Vec<u8>,
    /// the current span ASCII-lowercased (a case-folding finder searches it) and its start
    low: Vec<u8>,
    low_base: usize,
    /// next occurrence (absolute) or `NONE`
    occ: usize,
    /// the current span has an occurrence: check duplicate keys
    track: bool,
    dup: bool,
}

#[inline(always)]
fn ws(buf: &[u8], mut i: usize, e: usize) -> usize {
    while i < e {
        match buf[i] {
            b' ' | b'\n' | b'\r' | b'\t' => i += 1,
            _ => break,
        }
    }
    i
}

/// Validate a string whose content starts at `i`; returns (index after the closing
/// quote, had_escape) or the offset of the first error.
#[inline(always)]
fn scan_string(buf: &[u8], mut i: usize, e: usize) -> Result<(usize, bool), usize> {
    let mut esc = false;
    loop {
        while i + 8 <= e {
            let m = str_special(load8(buf, i));
            if m != 0 {
                i += (m.trailing_zeros() >> 3) as usize;
                break;
            }
            i += 8;
        }
        while i < e && !STR_SPECIAL[buf[i] as usize] {
            i += 1;
        }
        if i >= e {
            return Err(e);
        }
        let b = buf[i];
        if b == b'"' {
            return Ok((i + 1, esc));
        }
        if b == b'\\' {
            esc = true;
            if i + 1 >= e {
                return Err(e);
            }
            match buf[i + 1] {
                b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => i += 2,
                b'u' => {
                    for k in 2..6 {
                        if i + k >= e {
                            return Err(e);
                        }
                        if !buf[i + k].is_ascii_hexdigit() {
                            return Err(i + k);
                        }
                    }
                    i += 6;
                }
                _ => return Err(i + 1),
            }
            continue;
        }
        if b < 0x20 {
            return Err(i);
        }
        match utf8_seq(buf, i, e) {
            Ok(l) => i += l,
            Err(_) => return Err(i),
        }
    }
}

#[inline(always)]
fn scan_number(buf: &[u8], i: usize, e: usize) -> Result<usize, usize> {
    let mut j = i;
    if buf[j] == b'-' {
        j += 1;
    }
    if j >= e {
        return Err(j);
    }
    match buf[j] {
        b'0' => j += 1,
        b'1'..=b'9' => {
            j += 1;
            while j < e && buf[j].is_ascii_digit() {
                j += 1;
            }
        }
        _ => return Err(j),
    }
    if j < e && buf[j] == b'.' {
        j += 1;
        if j >= e || !buf[j].is_ascii_digit() {
            return Err(j);
        }
        while j < e && buf[j].is_ascii_digit() {
            j += 1;
        }
    }
    if j < e && (buf[j] == b'e' || buf[j] == b'E') {
        j += 1;
        if j < e && (buf[j] == b'+' || buf[j] == b'-') {
            j += 1;
        }
        if j >= e || !buf[j].is_ascii_digit() {
            return Err(j);
        }
        while j < e && buf[j].is_ascii_digit() {
            j += 1;
        }
    }
    Ok(j)
}

#[inline(always)]
fn key_slice<'a>(buf: &'a [u8], arena: &'a [u8], k: KeyRef) -> &'a [u8] {
    if k.in_arena {
        &arena[k.start..k.end]
    } else {
        &buf[k.start..k.end]
    }
}

impl Scratch {
    /// Start of the leftmost occurrence lying entirely inside `buf[s..e]` (within the
    /// current span), or `NONE`.
    #[inline(always)]
    fn find(&self, f: &MultiFinder, buf: &[u8], s: usize, e: usize) -> usize {
        let found = if f.folds() {
            let b = self.low_base;
            f.search(&self.low, s - b, e - b).map(|p| p + b)
        } else {
            f.search(buf, s, e)
        };
        found.unwrap_or(NONE)
    }

    /// Is the token `[ts, te)` located? Advances the next occurrence past the token.
    #[inline(always)]
    fn hit(&mut self, f: &MultiFinder, buf: &[u8], e: usize, ts: usize, te: usize) -> bool {
        if self.occ >= te {
            return false;
        }
        if self.occ < ts {
            // started on whitespace / structure: the next one from the token on
            self.occ = self.find(f, buf, ts, e);
            if self.occ >= te {
                return false;
            }
        }
        self.occ = self.find(f, buf, te, e);
        true
    }

    fn push_token(&self, out: &mut Located, span: usize, kind: u8, ts: usize, te: usize, with_path: bool) {
        let p0 = out.segs.len();
        if with_path {
            out.segs.extend(self.frames.iter().map(|fr| fr.seg));
        }
        out.tokens.push(Token { span, kind, start: ts, end: te, path: (p0, out.segs.len()) });
    }

    fn push_frame(&mut self, is_obj: bool) {
        self.frames.push(Frame {
            is_obj,
            seg: Seg::Index(0),
            count: 0,
            keys_base: self.keys.len(),
            arena_base: self.arena.len(),
            set: None,
        });
    }

    fn pop_frame(&mut self) {
        if let Some(fr) = self.frames.pop() {
            self.keys.truncate(fr.keys_base);
            self.arena.truncate(fr.arena_base);
        }
    }

    /// Duplicate check of a new key of the innermost object (content `buf[ks..ke]`).
    fn add_key(&mut self, buf: &[u8], ks: usize, ke: usize, esc: bool) {
        let k = if esc {
            decode_json_str(&buf[ks..ke], &mut self.tmp);
            let a0 = self.arena.len();
            self.arena.extend_from_slice(&self.tmp);
            KeyRef { start: a0, end: self.arena.len(), in_arena: true }
        } else {
            KeyRef { start: ks, end: ke, in_arena: false }
        };
        let fr = match self.frames.last_mut() {
            Some(fr) => fr,
            None => return,
        };
        let new = key_slice(buf, &self.arena, k);
        if let Some(set) = fr.set.as_mut() {
            if !set.insert(new.into()) {
                self.dup = true;
            }
            return;
        }
        let base = fr.keys_base;
        for &old in &self.keys[base..] {
            if key_slice(buf, &self.arena, old) == new {
                self.dup = true;
                return;
            }
        }
        self.keys.push(k);
        if self.keys.len() - base > SMALL_OBJECT {
            let mut set: HashSet<Box<[u8]>> = HashSet::with_capacity(4 * SMALL_OBJECT);
            for &old in &self.keys[base..] {
                set.insert(key_slice(buf, &self.arena, old).into());
            }
            fr.set = Some(set);
        }
    }

    /// The key whose opening quote is at `i` (innermost frame is an object), then the
    /// colon; returns the start of the value.
    #[inline(always)]
    fn key(&mut self, f: &MultiFinder, buf: &[u8], e: usize, i: usize, span: usize, out: &mut Located)
           -> Result<usize, usize> {
        let (j, esc) = scan_string(buf, i + 1, e)?;
        if let Some(fr) = self.frames.last_mut() {
            fr.seg = Seg::Key { start: i + 1, end: j - 1, esc };
        }
        if self.track && !self.dup {
            self.add_key(buf, i + 1, j - 1, esc);
        }
        if self.hit(f, buf, e, i, j) {
            self.push_token(out, span, KIND_KEY, i, j, true);
        }
        let k = ws(buf, j, e);
        if k >= e || buf[k] != b':' {
            return Err(k);
        }
        Ok(ws(buf, k + 1, e))
    }

    /// Validate and locate the span `buf[range]` (number `span`); `Err(offset)` on
    /// invalid JSON.
    fn span(&mut self, f: &MultiFinder, buf: &[u8], range: (usize, usize), member: bool, span: usize,
            out: &mut Located) -> Result<(), usize> {
        let (s, e) = range;
        self.frames.clear();
        self.keys.clear();
        self.arena.clear();
        if f.folds() {
            crate::finder::lower_into(&buf[s..e], &mut self.low);
            self.low_base = s;
        }
        self.occ = self.find(f, buf, s, e);
        self.track = self.occ != NONE;
        self.dup = false;
        let first = out.tokens.len();
        let mut i = ws(buf, s, e);
        if member {
            if i >= e || buf[i] != b'"' {
                return Err(i);
            }
            let (j, _) = scan_string(buf, i + 1, e)?;
            if self.hit(f, buf, e, i, j) {
                self.push_token(out, span, KIND_MEMBER_KEY, i, j, false);
            }
            i = ws(buf, j, e);
            if i >= e || buf[i] != b':' {
                return Err(i);
            }
            i = ws(buf, i + 1, e);
        }
        'value: loop {
            if i >= e {
                return Err(i);
            }
            if let Some(fr) = self.frames.last_mut() {
                if !fr.is_obj {
                    fr.seg = Seg::Index(fr.count);
                    fr.count += 1;
                }
            }
            let b = buf[i];
            match b {
                b'{' | b'[' => {
                    if self.frames.len() >= MAX_DEPTH {
                        return Err(i); // same limit as the Scanner (depth_limit)
                    }
                    let is_obj = b == b'{';
                    self.push_frame(is_obj);
                    i = ws(buf, i + 1, e);
                    if i >= e {
                        return Err(i);
                    }
                    let c = buf[i];
                    if (!is_obj && c == b']') || (is_obj && c == b'}') {
                        self.pop_frame();
                        i += 1;
                    } else if is_obj {
                        if c != b'"' {
                            return Err(i);
                        }
                        i = self.key(f, buf, e, i, span, out)?;
                        continue 'value;
                    } else {
                        continue 'value;
                    }
                }
                b'"' => {
                    let (j, _) = scan_string(buf, i + 1, e)?;
                    if self.hit(f, buf, e, i, j) {
                        self.push_token(out, span, KIND_STRING, i, j, true);
                    }
                    i = j;
                }
                b'-' | b'0'..=b'9' => {
                    let j = scan_number(buf, i, e)?;
                    if self.hit(f, buf, e, i, j) {
                        self.push_token(out, span, KIND_NUMBER, i, j, true);
                    }
                    i = j;
                }
                b't' | b'f' | b'n' => {
                    let lit: &[u8] = match b {
                        b't' => b"true",
                        b'f' => b"false",
                        _ => b"null",
                    };
                    let l = lit.len();
                    if i + l > e || &buf[i..i + l] != lit {
                        let mut k = 0;
                        while i + k < e && k < l && buf[i + k] == lit[k] {
                            k += 1;
                        }
                        return Err(i + k);
                    }
                    if self.hit(f, buf, e, i, i + l) {
                        self.push_token(out, span, KIND_LITERAL, i, i + l, true);
                    }
                    i += l;
                }
                _ => return Err(i),
            }
            // after a complete value
            loop {
                let top_is_obj = match self.frames.last() {
                    Some(fr) => fr.is_obj,
                    None => break 'value,
                };
                i = ws(buf, i, e);
                if i >= e {
                    return Err(i);
                }
                let c = buf[i];
                if c == b',' {
                    i = ws(buf, i + 1, e);
                    if top_is_obj {
                        if i >= e || buf[i] != b'"' {
                            return Err(i);
                        }
                        i = self.key(f, buf, e, i, span, out)?;
                    }
                    continue 'value;
                }
                if (c == b']' && !top_is_obj) || (c == b'}' && top_is_obj) {
                    self.pop_frame();
                    i += 1;
                    continue;
                }
                return Err(i);
            }
        }
        let i = ws(buf, i, e);
        if i != e {
            return Err(i);
        }
        if self.dup && out.tokens.len() > first {
            let p = out.segs.len();
            out.tokens.push(Token { span, kind: KIND_CONFIRM, start: 0, end: 0, path: (p, p) });
        }
        Ok(())
    }
}

impl MultiFinder {
    /// Every key/scalar token of every span (u64 pairs relative to `buf`, each one JSON
    /// value, or an object member `"key": value` with `member`) in which a needle
    /// occurrence lying entirely inside the span starts, once, in document order (see the
    /// module docs); `Err` for an out-of-bounds span or invalid JSON in a span.
    pub fn locate_tokens(&self, buf: &[u8], spans: &[(u64, u64)], member: bool, out: &mut Located)
                         -> Result<(), String> {
        let len = buf.len() as u64;
        for (k, &(s, e)) in spans.iter().enumerate() {
            if s > e || e > len {
                return Err(format!("span {k} ({s}, {e}) is out of bounds for a buffer of {len} bytes"));
            }
        }
        out.tokens.clear();
        out.segs.clear();
        let mut sc = Scratch::default();
        for (k, &(s, e)) in spans.iter().enumerate() {
            let (s, e) = (s as usize, e as usize);
            if let Err(off) = sc.span(self, buf, (s, e), member, k, out) {
                return Err(format!("invalid JSON in span {k} ({s}, {e}) at offset {off}"));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    type Tok = (usize, u8, String, Vec<String>);

    fn seg_text(buf: &[u8], s: &Seg) -> String {
        match *s {
            Seg::Index(i) => format!("#{i}"),
            Seg::Key { start, end, esc } => {
                let mut v = Vec::new();
                if esc {
                    decode_json_str(&buf[start..end], &mut v);
                } else {
                    v.extend_from_slice(&buf[start..end]);
                }
                String::from_utf8_lossy(&v).into_owned()
            }
        }
    }

    fn run(needles: &[&[u8]], buf: &[u8], spans: &[(u64, u64)], member: bool) -> Result<Vec<Tok>, String> {
        let f = MultiFinder::new(needles.iter().map(|n| n.to_vec()).collect()).unwrap();
        let mut out = Located::default();
        f.locate_tokens(buf, spans, member, &mut out)?;
        Ok(out
            .tokens
            .iter()
            .map(|t| {
                let text = String::from_utf8_lossy(&buf[t.start..t.end]).into_owned();
                let path = out.segs[t.path.0..t.path.1].iter().map(|s| seg_text(buf, s)).collect();
                (t.span, t.kind, text, path)
            })
            .collect())
    }

    fn one(needles: &[&[u8]], rec: &[u8]) -> Vec<Tok> {
        run(needles, rec, &[(0, rec.len() as u64)], false).unwrap()
    }

    fn t(span: usize, kind: u8, text: &str, path: &[&str]) -> Tok {
        (span, kind, text.to_string(), path.iter().map(|s| s.to_string()).collect())
    }

    #[test]
    fn kinds_and_paths() {
        let rec = br#"{"ab": "xab", "n": [1, 2.5e3, {"k": true}], "ab\n": null, "z": "q"}"#;
        assert_eq!(
            one(&[b"ab"], rec),
            vec![t(0, 1, "\"ab\"", &["ab"]), t(0, 0, "\"xab\"", &["ab"]), t(0, 1, "\"ab\\n\"", &["ab\n"])]
        );
        assert_eq!(one(&[b"5e"], rec), vec![t(0, 2, "2.5e3", &["n", "#1"])]);
        assert_eq!(one(&[b"ru", b"ul"], rec), vec![t(0, 3, "true", &["n", "#2", "k"]), t(0, 3, "null", &["ab\n"])]);
        assert_eq!(one(&[b"1"], br#"[[1], 21, [], {"a": [3, 1]}]"#), vec![
            t(0, 2, "1", &["#0", "#0"]),
            t(0, 2, "21", &["#1"]),
            t(0, 2, "1", &["#3", "a", "#1"]),
        ]);
        // root scalars have the empty path
        assert_eq!(one(&[b"x"], br#" "x" "#), vec![t(0, 0, "\"x\"", &[])]);
        assert_eq!(one(&[b"7"], b"17"), vec![t(0, 2, "17", &[])]);
    }

    #[test]
    fn structural_and_crossing_occurrences() {
        // occurrences starting on whitespace / structure are ignored
        assert_eq!(one(&[b"{\""], br#"{"a": {"b": 1}}"#), vec![]);
        assert_eq!(one(&[b", "], br#"[1, 2]"#), vec![]);
        // an occurrence starting in token A and ending in token B locates A only
        assert_eq!(one(&[b"1, 2"], br#"[1, 2]"#), vec![t(0, 2, "1", &["#0"])]);
        assert_eq!(one(&[b"a\": \"b"], br#"{"a": "b"}"#), vec![t(0, 1, "\"a\"", &["a"])]);
        // the quotes belong to the token
        assert_eq!(one(&[b"\"b"], br#"{"a": "b"}"#), vec![t(0, 0, "\"b\"", &["a"])]);
        // overlapping needles / occurrences: every token once
        assert_eq!(one(&[b"aa", b"aaa"], br#"["aaaa", "a", "aa"]"#),
                   vec![t(0, 0, "\"aaaa\"", &["#0"]), t(0, 0, "\"aa\"", &["#2"])]);
        // occurrences crossing the span end do not count
        let buf = br#"["xy"]["xy"]"#;
        assert_eq!(run(&[b"y\"]"], buf, &[(0, 6), (6, 12)], false).unwrap(),
                   vec![t(0, 0, "\"xy\"", &["#0"]), t(1, 0, "\"xy\"", &["#0"])]);  // ends at the span end
        assert_eq!(run(&[b"y\"]["], buf, &[(0, 6), (6, 12)], false).unwrap(), vec![]);
        assert_eq!(run(&[b"y\"]["], buf, &[(0, 12)], false), Err("invalid JSON in span 0 (0, 12) at offset 6".into()));
    }

    #[test]
    fn duplicates() {
        // duplicate flag after the span's tokens, only when something is located
        assert_eq!(one(&[b"x"], br#"{"a": "x", "a": 1}"#), vec![t(0, 0, "\"x\"", &["a"]), t(0, 5, "", &[])]);
        assert_eq!(one(&[b"q"], br#"{"a": "x", "a": 1}"#), vec![]);
        // escapes are decoded before comparing; deeper objects count too
        assert_eq!(one(&[b"x"], br#"["x", {"b": {"a": 1, "\u0061": 2}}]"#),
                   vec![t(0, 0, "\"x\"", &["#0"]), t(0, 5, "", &[])]);
        assert_eq!(one(&[b"x"], br#"["x", {"b": {"a": 1, "\u0062": 2}}, {"a": 1}, {"a": 2}]"#),
                   vec![t(0, 0, "\"x\"", &["#0"])]);
        // large objects (hash set path)
        let mut big = String::from("{");
        for i in 0..40 {
            big.push_str(&format!("\"k{i}\": {i}, "));
        }
        big.push_str("\"x\": \"x\"}");
        let x2 = vec![t(0, 1, "\"x\"", &["x"]), t(0, 0, "\"x\"", &["x"])];
        assert_eq!(one(&[b"\"x\""], big.as_bytes()), x2);
        let dup = big.replace("\"x\": \"x\"}", "\"x\": \"x\", \"k\\u0033\": 0}");
        let x3 = vec![x2[0].clone(), x2[1].clone(), t(0, 5, "", &[])];
        assert_eq!(one(&[b"\"x\""], dup.as_bytes()), x3);
        // a duplicate before the first located token still counts
        let late = big.replace("{\"k0\": 0, ", "{\"k0\": 0, \"k0\": 1, ");
        assert_eq!(one(&[b"\"x\""], late.as_bytes()), x3);
    }

    #[test]
    fn member_spans_and_escaped_paths() {
        let doc = br#"{"ab": {"c\"d": ["ab"]}, "x" : 1}"#;
        let spans = [(1, 23), (25, 32)];
        assert_eq!(run(&[b"ab"], doc, &spans, true).unwrap(), vec![
            t(0, 4, "\"ab\"", &[]),
            t(0, 0, "\"ab\"", &["c\"d", "#0"]),
        ]);
        assert_eq!(run(&[b"x", b"1"], doc, &spans, true).unwrap(), vec![t(1, 4, "\"x\"", &[]), t(1, 2, "1", &[])]);
        assert!(run(&[b"x"], doc, &spans, false).is_err());
        assert!(run(&[b"x"], b"[1]", &[(0, 3)], true).is_err());
        let surrogate = b"{\"\\ud83d\\ude00\\ud800\": \"a\"}";
        let got = one(&[b"a"], surrogate);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].0, 0);
    }

    #[test]
    fn invalid_and_bounds() {
        for bad in [
            &br#"{"a":1,}"#[..],
            b"[1 2]",
            b"[01]",
            b"[1.]",
            b"\"\x01\"",
            b"\"\xc3\"",
            b"\"\\x\"",
            b"tru",
            b"{} x",
            b"",
            b"  ",
            b"[",
            b"{\"a\" 1}",
            b"[-]",
            b"{\"a\":1}}",
        ] {
            // with and without an occurrence in the span
            for n in [&b"a"[..], b"zz"] {
                assert!(run(&[n], bad, &[(0, bad.len() as u64)], false).is_err(), "{bad:?}");
            }
        }
        assert!(run(&[b"a"], b"[1]", &[(0, 4)], false).is_err());
        assert!(run(&[b"a"], b"[1]", &[(2, 1)], false).is_err());
        assert_eq!(run(&[b"a"], b"[1]", &[], false).unwrap(), vec![]);
        let deep = format!("{}{}", "[".repeat(MAX_DEPTH + 1), "]".repeat(MAX_DEPTH + 1));
        assert!(run(&[b"a"], deep.as_bytes(), &[(0, deep.len() as u64)], false).is_err());
        let ok = format!("{}\"a\"{}", "[".repeat(MAX_DEPTH), "]".repeat(MAX_DEPTH));
        let got = run(&[b"a"], ok.as_bytes(), &[(0, ok.len() as u64)], false).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].3.len(), MAX_DEPTH);
    }

    // ---- brute force comparison --------------------------------------------------------
    /// All leaf tokens of a valid record: (kind, start, end, path), by a simple recursive
    /// descent (no validation; records come from the generator below).
    /// (kind, start, end, path) of a token.
    type BruteTok = (u8, usize, usize, Vec<String>);

    fn all_tokens(buf: &[u8], s: usize, e: usize, member: bool) -> (Vec<BruteTok>, bool) {
        struct P<'a> {
            b: &'a [u8],
            i: usize,
            out: Vec<BruteTok>,
            path: Vec<String>,
            dup: bool,
        }
        impl P<'_> {
            fn ws(&mut self) {
                while self.i < self.b.len() && b" \t\r\n".contains(&self.b[self.i]) {
                    self.i += 1;
                }
            }
            fn string(&mut self) -> (usize, usize) {
                let s = self.i;
                self.i += 1;
                while self.b[self.i] != b'"' {
                    self.i += if self.b[self.i] == b'\\' { 2 } else { 1 };
                }
                self.i += 1;
                (s, self.i)
            }
            fn value(&mut self) {
                self.ws();
                let c = self.b[self.i];
                if c == b'{' {
                    self.i += 1;
                    let mut keys: Vec<Vec<u8>> = Vec::new();
                    loop {
                        self.ws();
                        if self.b[self.i] == b'}' {
                            self.i += 1;
                            return;
                        }
                        if self.b[self.i] == b',' {
                            self.i += 1;
                            self.ws();
                        }
                        let (ks, ke) = self.string();
                        let mut k = Vec::new();
                        decode_json_str(&self.b[ks + 1..ke - 1], &mut k);
                        if keys.contains(&k) {
                            self.dup = true;
                        }
                        keys.push(k.clone());
                        self.path.push(format!("{}", String::from_utf8_lossy(&k)));
                        self.out.push((KIND_KEY, ks, ke, self.path.clone()));
                        self.ws();
                        self.i += 1; // ':'
                        self.value();
                        self.path.pop();
                    }
                } else if c == b'[' {
                    self.i += 1;
                    let mut n = 0;
                    loop {
                        self.ws();
                        if self.b[self.i] == b']' {
                            self.i += 1;
                            return;
                        }
                        if self.b[self.i] == b',' {
                            self.i += 1;
                        }
                        self.path.push(format!("#{n}"));
                        self.value();
                        self.path.pop();
                        n += 1;
                    }
                } else if c == b'"' {
                    let (s, e) = self.string();
                    self.out.push((KIND_STRING, s, e, self.path.clone()));
                } else {
                    let s = self.i;
                    while self.i < self.b.len() && !b" \t\r\n,]}".contains(&self.b[self.i]) {
                        self.i += 1;
                    }
                    let kind = if b"tfn".contains(&c) { KIND_LITERAL } else { KIND_NUMBER };
                    self.out.push((kind, s, self.i, self.path.clone()));
                }
            }
        }
        let mut p = P { b: &buf[..e], i: s, out: Vec::new(), path: Vec::new(), dup: false };
        if member {
            p.ws();
            let (ks, ke) = p.string();
            p.out.push((KIND_MEMBER_KEY, ks, ke, Vec::new()));
            p.ws();
            p.i += 1;
        }
        p.value();
        (p.out, p.dup)
    }

    fn brute(needles: &[Vec<u8>], buf: &[u8], spans: &[(u64, u64)], member: bool) -> Vec<Tok> {
        let mut res = Vec::new();
        for (k, &(s, e)) in spans.iter().enumerate() {
            let (s, e) = (s as usize, e as usize);
            let (toks, dup) = all_tokens(buf, s, e, member);
            let starts: Vec<usize> = (s..e)
                .filter(|&p| needles.iter().any(|n| p + n.len() <= e && buf[p..p + n.len()] == n[..]))
                .collect();
            let mut any = false;
            for (kind, ts, te, path) in toks {
                if starts.iter().any(|&p| ts <= p && p < te) {
                    any = true;
                    res.push((k, kind, String::from_utf8_lossy(&buf[ts..te]).into_owned(), path));
                }
            }
            if any && dup {
                res.push((k, KIND_CONFIRM, String::new(), Vec::new()));
            }
        }
        res
    }

    #[test]
    fn random_against_brute_force() {
        let mut seed = 0x9e37_79b9_7f4a_7c15u64;
        let mut rnd = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        fn gen(r: &mut dyn FnMut() -> u64, depth: usize, out: &mut String) {
            let ws = |r: &mut dyn FnMut() -> u64| [" ", "", "", "\n", "\t"][(r() % 5) as usize];
            let strs = ["a", "ab", "b", "a\\\"b", "\\u0061", "x y", "ba", "aab", "", "\\\\a", "1", "ab\\nb"];
            match if depth > 3 { r() % 4 } else { r() % 6 } {
                0 => out.push_str(&format!("\"{}\"", strs[(r() % strs.len() as u64) as usize])),
                1 => out.push_str(["1", "-0", "12.5", "1e5", "10", "0.1e-2", "21"][(r() % 7) as usize]),
                2 => out.push_str(["true", "false", "null"][(r() % 3) as usize]),
                3 => out.push_str(&format!("\"{}{}\"", strs[(r() % 12) as usize], strs[(r() % 12) as usize])),
                4 => {
                    out.push('[');
                    let n = r() % 4;
                    for k in 0..n {
                        if k > 0 {
                            out.push_str(ws(r));
                            out.push(',');
                        }
                        out.push_str(ws(r));
                        gen(r, depth + 1, out);
                    }
                    out.push_str(ws(r));
                    out.push(']');
                }
                _ => {
                    out.push('{');
                    let n = r() % 4;
                    for k in 0..n {
                        if k > 0 {
                            out.push(',');
                        }
                        out.push_str(ws(r));
                        out.push_str(&format!("\"{}\"", strs[(r() % strs.len() as u64) as usize]));
                        out.push_str(ws(r));
                        out.push(':');
                        out.push_str(ws(r));
                        gen(r, depth + 1, out);
                    }
                    out.push('}');
                }
            }
        }
        let pool: [&[u8]; 10] = [b"a", b"ab", b"b\"", b"1", b"\"a", b", ", b"e", b"\\", b"ba", b"\":"];
        let (mut located, mut confirms) = (0, 0);
        for round in 0..3000 {
            let member = round % 3 == 0;
            let mut buf = String::new();
            let mut spans = Vec::new();
            for _ in 0..1 + rnd() % 4 {
                let s = buf.len();
                if member {
                    buf.push_str(&format!("\"{}\" : ", ["a", "k", "ab", "x\\u0061"][(rnd() % 4) as usize]));
                }
                gen(&mut rnd, 0, &mut buf);
                spans.push((s as u64, buf.len() as u64));
                buf.push_str(", ");
            }
            let needles: Vec<Vec<u8>> = (0..1 + rnd() % 3).map(|_| pool[(rnd() % 10) as usize].to_vec()).collect();
            let refs: Vec<&[u8]> = needles.iter().map(|n| &n[..]).collect();
            let got = run(&refs, buf.as_bytes(), &spans, member).unwrap();
            assert_eq!(got, brute(&needles, buf.as_bytes(), &spans, member), "{buf} {needles:?}");
            located += got.len();
            confirms += got.iter().filter(|t| t.1 == KIND_CONFIRM).count();
        }
        assert!(located > 5000 && confirms > 100, "{located} {confirms}");
    }

    /// Timing on real data (not a test): `LOCATE_BENCH_FILE=path/to/records.json cargo test
    /// --release --no-default-features locate_bench -- --ignored --nocapture`; one record
    /// per line (a JSON array written one record per line, like bench/data/syn_2g.json).
    #[test]
    #[ignore]
    fn locate_bench() {
        use std::io::Read;
        use std::time::Instant;
        let path = match std::env::var("LOCATE_BENCH_FILE") {
            Ok(p) => p,
            Err(_) => return,
        };
        let mut buf = Vec::new();
        std::fs::File::open(path).unwrap().take(400 << 20).read_to_end(&mut buf).unwrap();
        let mut spans = Vec::new();
        let mut pos = 0;
        while let Some(nl) = memchr::memchr(b'\n', &buf[pos..]) {
            let line = &buf[pos..pos + nl];
            if line.len() > 2 && line[0] == b'{' {
                let mut end = pos + nl;
                while buf[end - 1] == b',' || buf[end - 1] == b'\r' {
                    end -= 1;
                }
                spans.push((pos as u64, end as u64));
            }
            pos += nl + 1;
        }
        let mb = buf.len() as f64 / 1e6;
        println!("{} records, {mb:.0} MB", spans.len());
        let mut cases: Vec<(&str, Vec<Vec<u8>>)> = vec![
            ("validate only", vec![b"\x00zz".to_vec()]),
            ("ko'chasi 1 needle", vec![b"ko'chasi".to_vec()]),
            ("chasi 32 case variants", Vec::new()),
            ("children 1 needle", vec![b"children".to_vec()]),
        ];
        for b in 0..32u32 {
            cases[2].1.push(
                b"chasi".iter().enumerate().map(|(k, &c)| if b & (1 << k) != 0 { c.to_ascii_uppercase() } else { c }).collect(),
            );
        }
        // more needle sets: LOCATE_BENCH_NEEDLES = file with "name: hex hex ..." lines
        let extra = std::env::var("LOCATE_BENCH_NEEDLES").ok().and_then(|p| std::fs::read_to_string(p).ok());
        let names: Vec<String> = extra.iter().flat_map(|s| s.lines().map(|l| l.to_string())).collect();
        for line in &names {
            let (name, hexes) = line.split_once(':').unwrap();
            let needles = hexes
                .split_whitespace()
                .map(|h| (0..h.len()).step_by(2).map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap()).collect())
                .collect();
            cases.push((name, needles));
        }
        for (name, needles) in cases {
            let f = MultiFinder::new(needles).unwrap();
            let mut out = Located::default();
            let mut fis = Vec::new();
            for _ in 0..2 {
                let t0 = Instant::now();
                f.find_in_spans(&buf, &spans, &mut fis).unwrap();
                let t1 = Instant::now();
                f.locate_tokens(&buf, &spans, false, &mut out).unwrap();
                let t2 = Instant::now();
                let (a, b) = ((t1 - t0).as_secs_f64(), (t2 - t1).as_secs_f64());
                println!(
                    "{name:24} find_in_spans {:6.0} MB/s  locate {:6.0} MB/s  tokens {}",
                    mb / a,
                    mb / b,
                    out.tokens.len()
                );
            }
        }
    }
}
