//! Value spans at paths without building Python objects (ARCHITECTURE.md §1.6).
//! Pure Rust, no PyO3.
//!
//! Each record span is fully validated (same grammar as the scanner, §1.1, including its
//! default nesting limit of 100000 containers) while a small
//! "tape" of nodes is built for the values that paths can reach (depth <= longest path);
//! deeper values are only validated. Paths are then resolved on the tape with Python
//! dict semantics: last duplicate key wins, `any_key` yields one value per distinct key in
//! order of first appearance (value of the last occurrence).

use std::borrow::Cow;
use std::collections::HashMap;

use crate::util::{canonical_index, decode_json_str, load8, str_special, utf8_seq, STR_SPECIAL};

/// Path segment. `Key` holds the decoded key as "surrogatepass" UTF-8 bytes plus its
/// value as an array index when it is a canonical non-negative decimal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PSeg {
    Key(Vec<u8>, Option<u64>),
    Index(u64),
    AnyIndex,
    AnyKey,
}

impl PSeg {
    pub fn key(k: Vec<u8>) -> PSeg {
        let idx = canonical_index(&k);
        PSeg::Key(k, idx)
    }
}

const NIL: u32 = u32::MAX;
/// Container nesting accepted inside one record (the Scanner's default `max_depth`).
const MAX_DEPTH: usize = crate::scanner::DEFAULT_MAX_DEPTH;
const K_SCALAR: u8 = 0;
const K_ARR: u8 = 1;
const K_OBJ: u8 = 2;

#[derive(Clone, Copy)]
struct Node {
    start: usize,
    end: usize,
    kind: u8,
    key_esc: bool,
    key_start: usize,
    key_end: usize,
    first: u32,
    next: u32,
    count: u32,
}

#[derive(Clone, Copy)]
struct Frame {
    kind: u8,
    node: u32,
    last: u32,
}

#[derive(Default)]
struct Tape {
    nodes: Vec<Node>,
    stack: Vec<Frame>,
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

impl Tape {
    #[inline(always)]
    fn add(&mut self, start: usize, end: usize, kind: u8, key: (usize, usize, bool)) -> u32 {
        let idx = self.nodes.len() as u32;
        self.nodes.push(Node {
            start,
            end,
            kind,
            key_esc: key.2,
            key_start: key.0,
            key_end: key.1,
            first: NIL,
            next: NIL,
            count: 0,
        });
        if let Some(parent) = self.stack.last_mut() {
            if parent.node != NIL {
                if parent.last == NIL {
                    self.nodes[parent.node as usize].first = idx;
                } else {
                    self.nodes[parent.last as usize].next = idx;
                }
                parent.last = idx;
                self.nodes[parent.node as usize].count += 1;
            }
        }
        idx
    }

    /// Parse and validate `buf[s..e]`; nodes are created for depth <= `need`.
    /// The root value is node 0. `Err(offset)` on invalid JSON.
    fn parse(&mut self, buf: &[u8], s: usize, e: usize, member: bool, need: usize) -> Result<(), usize> {
        self.nodes.clear();
        self.stack.clear();
        let mut i = ws(buf, s, e);
        let mut key = (0usize, 0usize, false);
        if member {
            if i >= e || buf[i] != b'"' {
                return Err(i);
            }
            let (j, _) = scan_string(buf, i + 1, e)?;
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
            let d = self.stack.len();
            let track = d <= need;
            let b = buf[i];
            match b {
                b'{' | b'[' => {
                    if d >= MAX_DEPTH {
                        return Err(i); // same limit as the Scanner (depth_limit)
                    }
                    let kind = if b == b'{' { K_OBJ } else { K_ARR };
                    let node = if track { self.add(i, i, kind, key) } else { NIL };
                    self.stack.push(Frame { kind, node, last: NIL });
                    i = ws(buf, i + 1, e);
                    if i >= e {
                        return Err(i);
                    }
                    let c = buf[i];
                    if (kind == K_ARR && c == b']') || (kind == K_OBJ && c == b'}') {
                        self.stack.pop();
                        if node != NIL {
                            self.nodes[node as usize].end = i + 1;
                        }
                        i += 1;
                    } else if kind == K_OBJ {
                        if c != b'"' {
                            return Err(i);
                        }
                        let (j, esc) = scan_string(buf, i + 1, e)?;
                        key = (i + 1, j - 1, esc);
                        i = ws(buf, j, e);
                        if i >= e || buf[i] != b':' {
                            return Err(i);
                        }
                        i = ws(buf, i + 1, e);
                        continue 'value;
                    } else {
                        continue 'value;
                    }
                }
                b'"' => {
                    let (j, _) = scan_string(buf, i + 1, e)?;
                    if track {
                        self.add(i, j, K_SCALAR, key);
                    }
                    i = j;
                }
                b'-' | b'0'..=b'9' => {
                    let j = scan_number(buf, i, e)?;
                    if track {
                        self.add(i, j, K_SCALAR, key);
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
                    if track {
                        self.add(i, i + l, K_SCALAR, key);
                    }
                    i += l;
                }
                _ => return Err(i),
            }
            // after a complete value
            loop {
                let top = match self.stack.last() {
                    Some(f) => *f,
                    None => break 'value,
                };
                i = ws(buf, i, e);
                if i >= e {
                    return Err(i);
                }
                let c = buf[i];
                if c == b',' {
                    i = ws(buf, i + 1, e);
                    if top.kind == K_OBJ {
                        if i >= e || buf[i] != b'"' {
                            return Err(i);
                        }
                        let (j, esc) = scan_string(buf, i + 1, e)?;
                        key = (i + 1, j - 1, esc);
                        i = ws(buf, j, e);
                        if i >= e || buf[i] != b':' {
                            return Err(i);
                        }
                        i = ws(buf, i + 1, e);
                    }
                    continue 'value;
                }
                if (c == b']' && top.kind == K_ARR) || (c == b'}' && top.kind == K_OBJ) {
                    self.stack.pop();
                    if top.node != NIL {
                        self.nodes[top.node as usize].end = i + 1;
                    }
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
        Ok(())
    }

    #[inline]
    fn children(&self, nd: u32) -> ChildIter<'_> {
        ChildIter { tape: self, cur: self.nodes[nd as usize].first }
    }

    fn nth_child(&self, nd: u32, ix: u64) -> Option<u32> {
        if ix >= self.nodes[nd as usize].count as u64 {
            return None;
        }
        self.children(nd).nth(ix as usize)
    }

    fn key_of<'a>(&self, buf: &'a [u8], nd: u32) -> Cow<'a, [u8]> {
        let n = &self.nodes[nd as usize];
        let raw = &buf[n.key_start..n.key_end];
        if n.key_esc {
            let mut v = Vec::with_capacity(raw.len());
            decode_json_str(raw, &mut v);
            Cow::Owned(v)
        } else {
            Cow::Borrowed(raw)
        }
    }
}

struct ChildIter<'a> {
    tape: &'a Tape,
    cur: u32,
}

impl Iterator for ChildIter<'_> {
    type Item = u32;
    #[inline]
    fn next(&mut self) -> Option<u32> {
        if self.cur == NIL {
            return None;
        }
        let c = self.cur;
        self.cur = self.tape.nodes[c as usize].next;
        Some(c)
    }
}

/// Per-call scratch buffers (so `extract` needs only `&self` and one extractor can be
/// shared between threads).
#[derive(Default)]
struct Scratch {
    tape: Tape,
    cur: Vec<u32>,
    next: Vec<u32>,
    dec: Vec<u8>,
}

pub struct PathExtractor {
    paths: Vec<Vec<PSeg>>,
    member_spans: bool,
    need: usize,
}

impl PathExtractor {
    pub fn new(paths: Vec<Vec<PSeg>>, member_spans: bool) -> PathExtractor {
        let need = paths.iter().map(|p| p.len()).max().unwrap_or(0);
        PathExtractor { paths, member_spans, need }
    }

    /// Quads `(span_idx, path_idx, start, end)` as u64 LE into `out`.
    /// `Err` (message) for out-of-bounds spans or invalid JSON inside a span.
    pub fn extract(&self, buf: &[u8], spans: &[(u64, u64)], out: &mut Vec<u8>) -> Result<(), String> {
        out.clear();
        let mut sc = Scratch::default();
        let len = buf.len() as u64;
        for (si, &(s, e)) in spans.iter().enumerate() {
            if s > e || e > len {
                return Err(format!("span {si} ({s}, {e}) is out of bounds for a buffer of {len} bytes"));
            }
            let (s, e) = (s as usize, e as usize);
            if let Err(off) = sc.tape.parse(buf, s, e, self.member_spans, self.need) {
                return Err(format!("invalid JSON in span {si} ({s}, {e}) at offset {off}"));
            }
            for (pi, path) in self.paths.iter().enumerate() {
                resolve(path, &mut sc, buf);
                for &nd in &sc.cur {
                    let n = &sc.tape.nodes[nd as usize];
                    out.extend_from_slice(&(si as u64).to_le_bytes());
                    out.extend_from_slice(&(pi as u64).to_le_bytes());
                    out.extend_from_slice(&(n.start as u64).to_le_bytes());
                    out.extend_from_slice(&(n.end as u64).to_le_bytes());
                }
            }
        }
        Ok(())
    }
}

/// Resolve `path` on the current tape; results (node ids, in order) end up in `sc.cur`.
fn resolve(path: &[PSeg], sc: &mut Scratch, buf: &[u8]) {
    let tape = &sc.tape;
    let mut cur = std::mem::take(&mut sc.cur);
    let mut next = std::mem::take(&mut sc.next);
    cur.clear();
    cur.push(0);
    for seg in path {
        next.clear();
        for &nd in &cur {
            let node = &tape.nodes[nd as usize];
            match seg {
                PSeg::Key(k, idx) => {
                    if node.kind == K_OBJ {
                        let mut found = NIL;
                        for c in tape.children(nd) {
                            let cn = &tape.nodes[c as usize];
                            let eq = if cn.key_esc {
                                decode_json_str(&buf[cn.key_start..cn.key_end], &mut sc.dec);
                                sc.dec == *k
                            } else {
                                buf[cn.key_start..cn.key_end] == k[..]
                            };
                            if eq {
                                found = c;
                            }
                        }
                        if found != NIL {
                            next.push(found);
                        }
                    } else if node.kind == K_ARR {
                        if let Some(ix) = idx {
                            if let Some(c) = tape.nth_child(nd, *ix) {
                                next.push(c);
                            }
                        }
                    }
                }
                PSeg::Index(ix) => {
                    if node.kind == K_ARR {
                        if let Some(c) = tape.nth_child(nd, *ix) {
                            next.push(c);
                        }
                    }
                }
                PSeg::AnyIndex => {
                    if node.kind == K_ARR {
                        next.extend(tape.children(nd));
                    }
                }
                PSeg::AnyKey => {
                    if node.kind == K_OBJ {
                        any_key(tape, buf, nd, &mut next);
                    }
                }
            }
        }
        std::mem::swap(&mut cur, &mut next);
        if cur.is_empty() {
            break;
        }
    }
    sc.cur = cur;
    sc.next = next;
}

/// One value per distinct key (order of first appearance, value of the last occurrence).
fn any_key(tape: &Tape, buf: &[u8], nd: u32, next: &mut Vec<u32>) {
    let count = tape.nodes[nd as usize].count as usize;
    let base = next.len();
    if count <= 16 {
        let mut keys: Vec<Cow<[u8]>> = Vec::with_capacity(count);
        for c in tape.children(nd) {
            let k = tape.key_of(buf, c);
            match keys.iter().position(|x| *x == k) {
                Some(p) => next[base + p] = c,
                None => {
                    keys.push(k);
                    next.push(c);
                }
            }
        }
    } else {
        let mut seen: HashMap<Cow<[u8]>, usize> = HashMap::with_capacity(count);
        for c in tape.children(nd) {
            let k = tape.key_of(buf, c);
            match seen.get(&k) {
                Some(&p) => next[base + p] = c,
                None => {
                    seen.insert(k, next.len() - base);
                    next.push(c);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quads(out: &[u8]) -> Vec<(u64, u64, u64, u64)> {
        out.chunks_exact(32)
            .map(|c| {
                let r = |k: usize| u64::from_le_bytes(c[k * 8..k * 8 + 8].try_into().unwrap());
                (r(0), r(1), r(2), r(3))
            })
            .collect()
    }

    fn texts(buf: &[u8], paths: Vec<Vec<PSeg>>, member: bool) -> Result<Vec<(u64, String)>, String> {
        let x = PathExtractor::new(paths, member);
        let mut out = Vec::new();
        x.extract(buf, &[(0, buf.len() as u64)], &mut out)?;
        Ok(quads(&out)
            .into_iter()
            .map(|(_, p, s, e)| (p, String::from_utf8_lossy(&buf[s as usize..e as usize]).into_owned()))
            .collect())
    }

    fn key(s: &str) -> PSeg {
        PSeg::key(s.as_bytes().to_vec())
    }

    #[test]
    fn dict_semantics() {
        let buf = br#" {"a": 1, "b": {"c": [10, 20, {"d": null}]}, "a": "last", "\u0061": 3.5} "#;
        assert_eq!(texts(buf, vec![vec![key("a")]], false).unwrap(), vec![(0, "3.5".to_string())]);
        assert_eq!(
            texts(buf, vec![vec![key("b"), key("c"), PSeg::Index(1)], vec![]], false).unwrap(),
            vec![(0, "20".to_string()), (1, buf[1..buf.len() - 1].iter().map(|&c| c as char).collect())]
        );
        assert_eq!(
            texts(buf, vec![vec![key("b"), key("c"), key("2"), key("d")]], false).unwrap(),
            vec![(0, "null".to_string())]
        );
        assert_eq!(
            texts(buf, vec![vec![PSeg::AnyKey]], false).unwrap(),
            vec![(0, "3.5".to_string()), (0, r#"{"c": [10, 20, {"d": null}]}"#.to_string())]
        );
        assert_eq!(
            texts(buf, vec![vec![key("b"), key("c"), PSeg::AnyIndex]], false).unwrap(),
            vec![(0, "10".to_string()), (0, "20".to_string()), (0, r#"{"d": null}"#.to_string())]
        );
        assert_eq!(texts(buf, vec![vec![key("b"), key("c"), key("02")]], false).unwrap(), vec![]);
        assert_eq!(texts(buf, vec![vec![PSeg::Index(0)]], false).unwrap(), vec![]);
    }

    #[test]
    fn member_spans() {
        let buf = br#""k" : {"x": [1, 2]}"#;
        assert_eq!(texts(buf, vec![vec![key("x"), PSeg::Index(0)]], true).unwrap(), vec![(0, "1".to_string())]);
        assert_eq!(texts(buf, vec![vec![]], true).unwrap(), vec![(0, r#"{"x": [1, 2]}"#.to_string())]);
    }

    #[test]
    fn invalid() {
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
            b"[",
            b"{\"a\" 1}",
        ] {
            assert!(texts(bad, vec![vec![]], false).is_err(), "{bad:?}");
        }
        let x = PathExtractor::new(vec![vec![]], false);
        let mut out = Vec::new();
        assert!(x.extract(b"[1]", &[(0, 9)], &mut out).is_err());
    }
}
