//! Multi-literal byte search (ARCHITECTURE.md §1.5). Pure Rust, no PyO3.
//!
//! One needle: `memchr::memmem` (SIMD); several: Aho-Corasick with leftmost-first
//! semantics (its packed/Teddy SIMD prefilter does most of the work). `find_in_spans`
//! searches the whole region once and maps leftmost matches to spans, so the cost is close
//! to one linear scan of the buffer regardless of how many (small) spans there are.
//!
//! Case folding: the needles of a case-insensitive search (`textmatch.py`) are every ASCII
//! case variant of a few patterns (`TOSHKENT`, `TOSHKENt`, ... 256 of them) plus a few
//! needles without ASCII letters (`ß`, `ſ`, ...). Hundreds of patterns defeat the SIMD
//! prefilters. When the needle set is exactly the ASCII case closure of its lowercase
//! forms, the finder searches the ASCII-lowercased haystack for those few lowercase forms
//! instead -- the same matches (positions and lengths), pure byte semantics kept.

use aho_corasick::{AhoCorasick, AhoCorasickKind, Input, MatchKind};
use memchr::memmem;

pub struct MultiFinder {
    single: Option<memmem::Finder<'static>>,
    ac: Option<AhoCorasick>,
    /// the needles are the ASCII case closure of the (lowercase) patterns above: they are
    /// searched in the ASCII-lowercased haystack
    fold: bool,
}

/// `Some(lowercase forms)` when the distinct `needles` are exactly all ASCII case variants
/// of their lowercase forms (a needle without ASCII letters is its own only variant).
fn case_closure(needles: &[Vec<u8>]) -> Option<Vec<Vec<u8>>> {
    let mut groups: std::collections::HashMap<Vec<u8>, usize> = std::collections::HashMap::new();
    for n in needles {
        *groups.entry(n.to_ascii_lowercase()).or_insert(0) += 1;
    }
    let mut forms = Vec::with_capacity(groups.len());
    for (low, count) in groups {
        let letters = low.iter().filter(|b| b.is_ascii_lowercase()).count();
        // distinct needles with the same lowercase form are distinct case variants
        if letters >= 32 || count != 1usize << letters {
            return None;
        }
        forms.push(low);
    }
    forms.sort();
    Some(forms)
}

/// `dst` = ASCII-lowercased `src`.
#[inline]
pub(crate) fn lower_into(src: &[u8], dst: &mut Vec<u8>) {
    dst.clear();
    dst.extend(src.iter().map(|&b| b | (((b.wrapping_sub(b'A') < 26) as u8) << 5)));
}

impl MultiFinder {
    /// `Err` when `needles` is empty or contains an empty needle.
    pub fn new(needles: Vec<Vec<u8>>) -> Result<MultiFinder, String> {
        if needles.is_empty() {
            return Err("MultiFinder needs at least one needle".into());
        }
        if needles.iter().any(|n| n.is_empty()) {
            return Err("MultiFinder needles must not be empty".into());
        }
        let mut uniq: Vec<Vec<u8>> = Vec::with_capacity(needles.len());
        for n in needles {
            if !uniq.contains(&n) {
                uniq.push(n);
            }
        }
        let (patterns, fold) = match case_closure(&uniq) {
            Some(forms) if forms.len() < uniq.len() => (forms, true),
            _ => (uniq, false),
        };
        if patterns.len() == 1 {
            let f = memmem::Finder::new(&patterns[0]).into_owned();
            return Ok(MultiFinder { single: Some(f), ac: None, fold });
        }
        let ac = AhoCorasick::builder()
            .match_kind(MatchKind::LeftmostFirst)
            .kind(Some(AhoCorasickKind::DFA))
            .build(&patterns)
            .or_else(|_| {
                AhoCorasick::builder()
                    .match_kind(MatchKind::LeftmostFirst)
                    .build(&patterns)
            })
            .map_err(|e| format!("cannot build needle automaton: {e}"))?;
        Ok(MultiFinder { single: None, ac: Some(ac), fold })
    }

    /// Whether the haystack must be ASCII-lowercased (`lower_into`) before
    /// [`Self::search`].
    #[inline]
    pub(crate) fn folds(&self) -> bool {
        self.fold
    }

    /// Start of the leftmost match lying entirely inside `hay[s..e]`, where `hay` is the
    /// haystack, ASCII-lowercased when [`Self::folds`] (`None` when `s >= e`).
    #[inline]
    pub(crate) fn search(&self, hay: &[u8], s: usize, e: usize) -> Option<usize> {
        if s >= e {
            return None;
        }
        if let Some(f) = &self.single {
            return f.find(&hay[s..e]).map(|p| s + p);
        }
        let ac = self.ac.as_ref()?;
        ac.find(Input::new(hay).range(s..e)).map(|m| m.start())
    }

    /// Smallest `p` in `[start, end)` where a needle occurs and ends `<= end`, or -1.
    /// The caller validates the range (`start, end <= buf.len()`); `start >= end` gives -1.
    pub fn find_first(&self, buf: &[u8], start: usize, end: usize) -> i64 {
        let end = end.min(buf.len());
        if start >= end {
            return -1;
        }
        let found = if self.fold {
            let mut low = Vec::new();
            lower_into(&buf[start..end], &mut low);
            self.search(&low, 0, low.len()).map(|p| p + start)
        } else {
            self.search(buf, start, end)
        };
        match found {
            Some(p) => p as i64,
            None => -1,
        }
    }

    /// Indices of the spans (u64 LE pairs relative to `buf`) that contain a needle
    /// occurrence entirely inside them, as u64 LE values in increasing order.
    ///
    /// Spans must be sorted and non-overlapping (caller contract). The algorithm is the
    /// reference one, step for step, so even spans violating the contract give the same
    /// result as the Python backend: search the leftmost occurrence `p` from the current
    /// span start to the end of `buf`, skip the spans ending at or before `p` (binary
    /// search over the span ends), then test the span holding `p` from `p` to its end.
    pub fn find_in_spans(&self, buf: &[u8], spans: &[(u64, u64)], out: &mut Vec<u8>) -> Result<(), String> {
        let len = buf.len() as u64;
        for (k, &(s, e)) in spans.iter().enumerate() {
            if s > e || e > len {
                return Err(format!("span {k} ({s}, {e}) is out of bounds for a buffer of {len} bytes"));
            }
        }
        out.clear();
        let n = buf.len();
        let count = spans.len();
        if count == 0 {
            return Ok(());
        }
        // case folding: search a lowercased copy of the region the searches can reach
        let mut low = Vec::new();
        let (hay, base) = if self.fold {
            let from = spans.iter().map(|s| s.0 as usize).min().unwrap_or(0);
            lower_into(&buf[from..], &mut low);
            (&low[..], from)
        } else {
            (buf, 0)
        };
        let find = |s: usize, e: usize| self.search(hay, s - base, e - base).map(|p| p + base);
        let mut i = 0usize;
        while i < count {
            let p = match find(spans[i].0 as usize, n) {
                Some(p) => p,
                None => break,
            };
            let j = bisect_right_ends(spans, p as u64, i);
            if j >= count {
                break;
            }
            if spans[j].0 as usize > p {
                i = j; // p lies in a gap between spans: continue from span j
                continue;
            }
            if find(p, spans[j].1 as usize).is_some() {
                out.extend_from_slice(&(j as u64).to_le_bytes());
            }
            i = j + 1;
        }
        Ok(())
    }
}

/// Python's `bisect.bisect_right(ends, x, lo)` over the span ends.
fn bisect_right_ends(spans: &[(u64, u64)], x: u64, lo: usize) -> usize {
    let mut lo = lo;
    let mut hi = spans.len();
    while lo < hi {
        let mid = (lo + hi) / 2;
        if x < spans[mid].1 {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    lo
}

#[cfg(test)]
mod tests {
    use super::*;

    fn idx(out: &[u8]) -> Vec<u64> {
        out.chunks_exact(8).map(|c| u64::from_le_bytes(c.try_into().unwrap())).collect()
    }

    fn brute(needles: &[&[u8]], buf: &[u8], spans: &[(u64, u64)]) -> Vec<u64> {
        let mut r = Vec::new();
        for (k, &(s, e)) in spans.iter().enumerate() {
            let hay = &buf[s as usize..e as usize];
            if needles.iter().any(|n| hay.windows(n.len()).any(|w| w == *n)) {
                r.push(k as u64);
            }
        }
        r
    }

    #[test]
    fn spans_and_first() {
        let buf = b"abc|xabcx|ab|cabc|bc";
        let spans = [(0u64, 3u64), (4, 9), (10, 12), (13, 17), (18, 20)];
        for needles in [vec![&b"abc"[..]], vec![&b"abc"[..], &b"bc"[..]], vec![&b"ca"[..], &b"zz"[..]]] {
            let f = MultiFinder::new(needles.iter().map(|n| n.to_vec()).collect()).unwrap();
            let mut out = Vec::new();
            f.find_in_spans(buf, &spans, &mut out).unwrap();
            assert_eq!(idx(&out), brute(&needles, buf, &spans), "{needles:?}");
        }
        let f = MultiFinder::new(vec![b"abc".to_vec(), b"x".to_vec()]).unwrap();
        let n = buf.len();
        assert_eq!(f.find_first(buf, 0, n), 0);
        assert_eq!(f.find_first(buf, 1, n), 4);
        assert_eq!(f.find_first(buf, 6, 8), -1);
        assert_eq!(f.find_first(buf, 6, 9), 8);
        assert_eq!(f.find_first(buf, n - 3, n), -1);
        assert_eq!(f.find_first(buf, 9, 6), -1);
        assert!(MultiFinder::new(vec![]).is_err());
        assert!(MultiFinder::new(vec![b"".to_vec()]).is_err());
    }

    /// Every ASCII case variant of `p`.
    fn variants(p: &[u8]) -> Vec<Vec<u8>> {
        let mut out = vec![Vec::new()];
        for &c in p {
            let mut next = Vec::new();
            for v in &out {
                let mut a = v.clone();
                a.push(c.to_ascii_lowercase());
                next.push(a);
                if c.is_ascii_alphabetic() {
                    let mut b = v.clone();
                    b.push(c.to_ascii_uppercase());
                    next.push(b);
                }
            }
            out = next;
        }
        out
    }

    #[test]
    fn case_closures_fold() {
        assert_eq!(case_closure(&variants(b"aB1")), Some(vec![b"ab1".to_vec()]));
        let mut partial = variants(b"ab");
        partial.pop();
        assert_eq!(case_closure(&partial), None);
        let mut mixed = variants(b"xy");
        mixed.push(b"\xc3\x9f".to_vec());
        mixed.push(b"--".to_vec());
        assert_eq!(case_closure(&mixed), Some(vec![b"--".to_vec(), b"xy".to_vec(), b"\xc3\x9f".to_vec()]));
        assert!(MultiFinder::new(mixed.clone()).unwrap().folds());
        assert!(!MultiFinder::new(partial.clone()).unwrap().folds());
        assert!(!MultiFinder::new(vec![b"ab".to_vec()]).unwrap().folds());
        assert!(!MultiFinder::new(vec![b"12".to_vec(), b"-".to_vec()]).unwrap().folds());
        // folding finders agree with brute force (spans, first occurrence, lengths)
        let mut seed = 777u64;
        let mut rnd = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let alphabet = b"aAbBzZ-\xc3\x9f[@`{";
        for _ in 0..400 {
            let mut needles: Vec<Vec<u8>> = Vec::new();
            for _ in 0..1 + rnd() % 3 {
                let p: Vec<u8> = (0..1 + rnd() % 3).map(|_| alphabet[(rnd() % 8) as usize]).collect();
                needles.extend(variants(&p));
            }
            if rnd() % 2 == 0 {
                needles.push(b"\xc3\x9f".to_vec());
            }
            if rnd() % 4 == 0 {
                needles.pop(); // usually breaks the closure: exact search
            }
            if needles.is_empty() {
                continue;
            }
            let len = (rnd() % 50) as usize;
            let buf: Vec<u8> = (0..len).map(|_| alphabet[(rnd() % alphabet.len() as u64) as usize]).collect();
            let mut spans = Vec::new();
            let mut p = 0u64;
            while (p as usize) < len {
                let s = p + rnd() % 3;
                let e = (s + rnd() % 9).min(len as u64);
                if s > e {
                    break;
                }
                spans.push((s, e));
                p = e;
            }
            let f = MultiFinder::new(needles.clone()).unwrap();
            let mut out = Vec::new();
            f.find_in_spans(&buf, &spans, &mut out).unwrap();
            let nrefs: Vec<&[u8]> = needles.iter().map(|v| &v[..]).collect();
            assert_eq!(idx(&out), brute(&nrefs, &buf, &spans), "{buf:?} {spans:?} {needles:?}");
            for s in 0..=len {
                for e in s..=len {
                    let want = (s..e)
                        .find(|&q| nrefs.iter().any(|n| q + n.len() <= e && &buf[q..q + n.len()] == *n))
                        .map_or(-1, |q| q as i64);
                    assert_eq!(f.find_first(&buf, s, e), want, "{buf:?} {s} {e} {needles:?}");
                }
            }
        }
    }

    #[test]
    fn crossing_matches() {
        // the leftmost match crosses the span end but a shorter one fits
        let buf = b"xxabcdyy";
        let f = MultiFinder::new(vec![b"abcdyy".to_vec(), b"abc".to_vec()]).unwrap();
        let mut out = Vec::new();
        f.find_in_spans(buf, &[(0, 5), (5, 8)], &mut out).unwrap();
        assert_eq!(idx(&out), vec![0]);
        // pseudo-random exhaustive comparison with brute force
        let mut seed = 12345u64;
        let mut rnd = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..300 {
            let len = (rnd() % 60) as usize;
            let buf: Vec<u8> = (0..len).map(|_| b"ab"[(rnd() % 2) as usize]).collect();
            let mut spans = Vec::new();
            let mut p = 0u64;
            while (p as usize) < len {
                let s = p + rnd() % 3;
                let e = (s + rnd() % 8).min(len as u64);
                if s > e {
                    break;
                }
                spans.push((s, e));
                p = e;
            }
            let nd: Vec<Vec<u8>> = (0..1 + rnd() % 3)
                .map(|_| (0..1 + rnd() % 3).map(|_| b"ab"[(rnd() % 2) as usize]).collect())
                .collect();
            let f = MultiFinder::new(nd.clone()).unwrap();
            let mut out = Vec::new();
            f.find_in_spans(&buf, &spans, &mut out).unwrap();
            let nrefs: Vec<&[u8]> = nd.iter().map(|v| &v[..]).collect();
            assert_eq!(idx(&out), brute(&nrefs, &buf, &spans), "{buf:?} {spans:?} {nd:?}");
        }
    }
}
