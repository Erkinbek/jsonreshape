//! `jsonreshape.core._rscore`: the fast byte-level core (docs/ARCHITECTURE.md §1).
//!
//! The logic lives in plain Rust modules (no PyO3 types, unit-testable with
//! `cargo test --no-default-features`); this file only converts types and validates
//! arguments exactly like the pure-Python reference (`jsonreshape/core/_pycore.py`).
//!
//! Work on inputs of at least [`DETACH_MIN`] bytes runs with the GIL released
//! (`py.detach`); smaller calls keep it, because re-acquiring a contended GIL can cost a
//! whole switch interval, which would dwarf the work itself (e.g. `numbers_plain` on a
//! 300-byte record).
//!
//! Every `#[pyclass]` keeps its state in a `Box`: Python allocates object memory with
//! only 16-byte alignment, while e.g. memchr's AVX2 searcher holds `__m256i` fields
//! (32-byte alignment). Stored inline, aligned SIMD loads from such fields fault.

pub mod extract;
pub mod finder;
pub mod locate;
pub mod numbers;
pub mod reformat;
pub mod scanner;
pub mod util;

#[cfg(test)]
mod scanner_tests;

/// Inputs smaller than this are processed without releasing the GIL.
pub const DETACH_MIN: usize = 256 * 1024;

#[cfg(feature = "python")]
mod bindings {
    use pyo3::exceptions::{PyTypeError, PyValueError};
    use pyo3::prelude::*;
    use pyo3::types::{PyByteArray, PyBytes, PyInt, PyList, PyString, PyTuple};

    use crate::extract::{PSeg, PathExtractor};
    use crate::finder::MultiFinder;
    use crate::locate::{Located, Seg as LSeg};
    use crate::reformat::Reformatter;
    use crate::scanner::{self, Scanner, Seg};
    use crate::util::{decode_json_str, read_pairs};
    use crate::DETACH_MIN;

    fn value_error(msg: impl Into<String>) -> PyErr {
        PyValueError::new_err(msg.into())
    }

    fn repr(o: &Bound<'_, PyAny>) -> String {
        o.repr().map(|r| r.to_string()).unwrap_or_else(|_| "?".into())
    }

    /// Byte arguments, converted exactly like the reference: `bytes` is used as is
    /// (zero-copy), `str` is a TypeError, anything else goes through `bytes(obj)`
    /// (bytearray, memoryview, mmap, array('Q'), ...; TypeError for unsupported types).
    fn bytes_arg<'py>(obj: &Bound<'py, PyAny>, what: &str) -> PyResult<Bound<'py, PyBytes>> {
        if let Ok(b) = obj.cast::<PyBytes>() {
            return Ok(b.clone());
        }
        if obj.is_instance_of::<PyString>() {
            return Err(PyTypeError::new_err(format!("{what} must be bytes, not str")));
        }
        let b = obj.py().get_type::<PyBytes>().call1((obj,))?;
        b.cast_into::<PyBytes>().map_err(PyErr::from)
    }

    /// Run `f` with the GIL released when the input is large.
    fn maybe_detach<T: Send, F: FnOnce() -> T + Send>(py: Python<'_>, size: usize, f: F) -> T {
        if size >= DETACH_MIN {
            py.detach(f)
        } else {
            f()
        }
    }

    /// `type(o) is int` (bool and int subclasses are rejected, like the reference).
    fn is_int(o: &Bound<'_, PyAny>) -> bool {
        o.is_exact_instance_of::<PyInt>()
    }

    /// Value of an exact `int`, saturated to the i64 range.
    fn int_value(o: &Bound<'_, PyAny>) -> PyResult<i64> {
        match o.extract::<i64>() {
            Ok(v) => Ok(v),
            Err(_) => Ok(if o.lt(0)? { i64::MIN } else { i64::MAX }),
        }
    }

    /// `str` → "surrogatepass" UTF-8 bytes (lone surrogates kept, like Python strings).
    fn str_bytes(s: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
        let enc = s.call_method1("encode", ("utf-8", "surrogatepass"))?;
        let b = enc.cast::<PyBytes>().map_err(PyErr::from)?;
        Ok(b.as_bytes().to_vec())
    }

    fn is_list_or_tuple(o: &Bound<'_, PyAny>) -> bool {
        o.is_instance_of::<PyList>() || o.is_instance_of::<PyTuple>()
    }

    // ---- Scanner ------------------------------------------------------------------------
    /// Validating, resumable record indexer (ARCHITECTURE.md §1.2).
    #[pyclass(name = "Scanner", module = "jsonreshape.core._rscore")]
    pub struct PyScanner {
        inner: Box<Scanner>,
    }

    #[pymethods]
    impl PyScanner {
        #[new]
        #[pyo3(signature = (mode, target_path = None, base_offset = None, max_depth = None),
               text_signature = "(mode, target_path=(), base_offset=0, max_depth=100000)")]
        fn new(
            mode: &Bound<'_, PyAny>,
            target_path: Option<&Bound<'_, PyAny>>,
            base_offset: Option<&Bound<'_, PyAny>>,
            max_depth: Option<&Bound<'_, PyAny>>,
        ) -> PyResult<Self> {
            let mode_v = if is_int(mode) { int_value(mode)? } else { 0 };
            if !(1..=5).contains(&mode_v) {
                return Err(value_error(format!("invalid scanner mode: {}", repr(mode))));
            }
            let mut segs = Vec::new();
            if let Some(tp) = target_path.filter(|t| !t.is_none()) {
                if !is_list_or_tuple(tp) {
                    return Err(value_error("target_path must be a list of str/int segments"));
                }
                for item in tp.try_iter()? {
                    let item = item?;
                    if is_int(&item) {
                        let v = int_value(&item)?;
                        if v < 0 {
                            return Err(value_error(format!("negative index in target_path: {v}")));
                        }
                        segs.push(Seg::Index(v as u64));
                    } else if item.is_exact_instance_of::<PyString>() {
                        segs.push(Seg::Key(str_bytes(&item)?));
                    } else {
                        return Err(value_error(format!("invalid target_path segment: {}", repr(&item))));
                    }
                }
            }
            if !segs.is_empty() && mode_v != scanner::MODE_CONTAINER as i64 {
                return Err(value_error("target_path is only supported in MODE_CONTAINER"));
            }
            let base = match base_offset {
                None => 0,
                Some(o) if is_int(o) && int_value(o)? >= 0 => int_value(o)?,
                Some(o) => return Err(value_error(format!("invalid base_offset: {}", repr(o)))),
            };
            let md = match max_depth {
                None => scanner::DEFAULT_MAX_DEPTH as i64,
                Some(o) if is_int(o) && int_value(o)? >= 1 => int_value(o)?,
                Some(o) => return Err(value_error(format!("invalid max_depth: {}", repr(o)))),
            };
            let md = usize::try_from(md).unwrap_or(usize::MAX);
            let inner = Scanner::new(mode_v as u8, segs, base as u64, md).map_err(value_error)?;
            Ok(PyScanner { inner: Box::new(inner) })
        }

        /// Feed the next chunk; returns newly completed record spans (u64 LE pairs).
        fn feed<'py>(&mut self, py: Python<'py>, chunk: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
            let data = bytes_arg(chunk, "chunk")?;
            if self.inner.finish_called() {
                return Err(value_error("Scanner.feed() called after finish()"));
            }
            let d: &[u8] = data.as_bytes();
            let inner = &mut self.inner;
            maybe_detach(py, d.len(), || {
                inner.feed(d);
            });
            Ok(PyBytes::new(py, inner.output()))
        }

        /// Signal EOF; returns the remaining spans (a second call returns b"").
        fn finish<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
            PyBytes::new(py, self.inner.finish())
        }

        /// MODE_LINES: broken lines since the last call.
        fn take_broken(&mut self) -> Vec<(u64, u64, u64, u64, &'static str, &'static str)> {
            self.inner
                .take_broken()
                .into_iter()
                .map(|b| (b.line_no, b.line_start, b.line_end, b.err_offset, b.code.as_str(), b.expected.as_str()))
                .collect()
        }

        #[getter]
        fn error(&self) -> Option<(u64, &'static str, &'static str, u64, u64)> {
            self.inner
                .error()
                .map(|e| (e.offset, e.code.as_str(), e.expected.as_str(), e.line, e.line_start))
        }

        #[getter]
        fn record_count(&self) -> u64 {
            self.inner.record_count()
        }

        #[getter]
        fn consumed(&self) -> u64 {
            self.inner.consumed()
        }

        #[getter]
        fn lines(&self) -> u64 {
            self.inner.lines()
        }

        #[getter]
        fn flags(&self) -> u8 {
            self.inner.flags()
        }

        #[getter]
        fn target_found(&self) -> bool {
            self.inner.target_found()
        }

        #[getter]
        fn container_span(&self) -> Option<(u64, u64)> {
            self.inner.container_span()
        }

        #[getter]
        fn finished(&self) -> bool {
            self.inner.finished()
        }
    }

    // ---- Reformatter --------------------------------------------------------------------
    /// Streaming whitespace normaliser (ARCHITECTURE.md §1.4).
    #[pyclass(name = "Reformatter", module = "jsonreshape.core._rscore")]
    pub struct PyReformatter {
        inner: Box<Reformatter>,
    }

    #[pymethods]
    impl PyReformatter {
        #[new]
        #[pyo3(signature = (indent = None, base_level = None),
               text_signature = "(indent=None, base_level=0)")]
        fn new(indent: Option<&Bound<'_, PyAny>>, base_level: Option<&Bound<'_, PyAny>>) -> PyResult<Self> {
            let unit: Option<Vec<u8>> = match indent.filter(|o| !o.is_none()) {
                None => None,
                Some(o) if is_int(o) => match int_value(o)? {
                    n @ 0..=16 => Some(vec![b' '; n as usize]),
                    n => return Err(value_error(format!("indent must be between 0 and 16, got {n}"))),
                },
                Some(o) => {
                    let is_tab = match o.cast::<PyString>() {
                        Ok(s) => s.to_str().map(|t| t == "\t").unwrap_or(false),
                        Err(_) => false,
                    };
                    if !is_tab {
                        return Err(value_error(format!("invalid indent: {} (None, 0..16 or '\\t')", repr(o))));
                    }
                    Some(b"\t".to_vec())
                }
            };
            let base = match base_level {
                None => 0,
                Some(o) if is_int(o) && int_value(o)? >= 0 => int_value(o)?,
                Some(o) => return Err(value_error(format!("invalid base_level: {}", repr(o)))),
            };
            Ok(PyReformatter { inner: Box::new(Reformatter::new(unit, base)) })
        }

        fn feed<'py>(&mut self, py: Python<'py>, chunk: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
            if self.inner.finished() {
                return Err(value_error("Reformatter.feed() called after finish()"));
            }
            let data = bytes_arg(chunk, "chunk")?;
            let d: &[u8] = data.as_bytes();
            let inner = &mut self.inner;
            maybe_detach(py, d.len(), || {
                inner.feed(d);
            });
            Ok(PyBytes::new(py, inner.output()))
        }

        fn finish<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
            if self.inner.finished() {
                return PyBytes::new(py, b"");
            }
            PyBytes::new(py, self.inner.finish())
        }
    }

    // ---- MultiFinder --------------------------------------------------------------------
    /// Multi-literal byte search (ARCHITECTURE.md §1.5).
    #[pyclass(name = "MultiFinder", module = "jsonreshape.core._rscore")]
    pub struct PyMultiFinder {
        inner: Box<MultiFinder>,
    }

    #[pymethods]
    impl PyMultiFinder {
        #[new]
        fn new(needles: &Bound<'_, PyAny>) -> PyResult<Self> {
            if needles.is_instance_of::<PyBytes>()
                || needles.is_instance_of::<PyByteArray>()
                || needles.is_instance_of::<PyString>()
            {
                return Err(value_error("needles must be a list of bytes"));
            }
            let it = needles
                .try_iter()
                .map_err(|_| value_error("needles must be a list of bytes"))?;
            let mut v = Vec::new();
            for item in it {
                let item = item?;
                let b = bytes_arg(&item, "needle")?;
                if b.as_bytes().is_empty() {
                    return Err(value_error("empty needle"));
                }
                v.push(b.as_bytes().to_vec());
            }
            let inner = MultiFinder::new(v).map_err(value_error)?;
            Ok(PyMultiFinder { inner: Box::new(inner) })
        }

        /// Indices (u64 LE) of the spans that contain a needle entirely inside them.
        fn find_in_spans<'py>(
            &self,
            py: Python<'py>,
            buf: &Bound<'py, PyAny>,
            spans: &Bound<'py, PyAny>,
        ) -> PyResult<Bound<'py, PyBytes>> {
            let data = bytes_arg(buf, "buf")?;
            let sp = bytes_arg(spans, "spans")?;
            let pairs = read_pairs(sp.as_bytes()).map_err(value_error)?;
            let d: &[u8] = data.as_bytes();
            let inner = &self.inner;
            let mut out = Vec::new();
            maybe_detach(py, d.len(), || inner.find_in_spans(d, &pairs, &mut out)).map_err(value_error)?;
            Ok(PyBytes::new(py, &out))
        }

        /// Located tokens `(span_idx, kind, token_start, token_end, path)` (ARCHITECTURE.md
        /// §1.5): every key/scalar of every span in which a needle occurrence lying inside
        /// the span starts, in document order, plus `(span_idx, 5, 0, 0, ())` after the
        /// tokens of a span with duplicate keys. ValueError for bad spans / invalid JSON.
        #[pyo3(signature = (buf, spans, member_spans = None),
               text_signature = "(buf, spans, member_spans=False)")]
        fn locate_tokens<'py>(
            &self,
            py: Python<'py>,
            buf: &Bound<'py, PyAny>,
            spans: &Bound<'py, PyAny>,
            member_spans: Option<&Bound<'py, PyAny>>,
        ) -> PyResult<Bound<'py, PyList>> {
            let data = bytes_arg(buf, "buf")?;
            let sp = bytes_arg(spans, "spans")?;
            let pairs = read_pairs(sp.as_bytes()).map_err(value_error)?;
            let member = match member_spans {
                None => false,
                Some(o) => o.is_truthy()?,
            };
            let d: &[u8] = data.as_bytes();
            let inner = &self.inner;
            let mut loc = Located::default();
            maybe_detach(py, d.len(), || inner.locate_tokens(d, &pairs, member, &mut loc)).map_err(value_error)?;
            located_to_list(py, d, &loc)
        }

        /// Smallest position in [start, end) where a needle occurs entirely, or -1.
        /// ValueError if start < 0, end < 0 or end > len(buf).
        #[pyo3(signature = (buf, start = None, end = None), text_signature = "(buf, start=0, end=None)")]
        fn find_first(
            &self,
            py: Python<'_>,
            buf: &Bound<'_, PyAny>,
            start: Option<&Bound<'_, PyAny>>,
            end: Option<&Bound<'_, PyAny>>,
        ) -> PyResult<i64> {
            let data = bytes_arg(buf, "buf")?;
            let n = data.as_bytes().len() as i64;
            let start = match start.filter(|o| !o.is_none()) {
                None => 0,
                Some(o) if is_int(o) => int_value(o)?,
                Some(_) => return Err(value_error("start/end must be integers")),
            };
            let end = match end.filter(|o| !o.is_none()) {
                None => n,
                Some(o) if is_int(o) => int_value(o)?,
                Some(_) => return Err(value_error("start/end must be integers")),
            };
            if start < 0 || end < 0 || end > n {
                return Err(value_error(format!("invalid range [{start}, {end}) for a buffer of {n} bytes")));
            }
            if start >= end {
                return Ok(-1);
            }
            let d: &[u8] = data.as_bytes();
            let inner = &self.inner;
            Ok(maybe_detach(py, d.len(), || inner.find_first(d, start as usize, end as usize)))
        }
    }

    /// Decoded object key as a Python `str` (Python `json` semantics: an escaped
    /// surrogate pair is one code point, a lone surrogate escape stays a lone surrogate).
    fn key_object<'py>(py: Python<'py>, raw: &[u8], esc: bool, tmp: &mut Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        let bytes: &[u8] = if esc {
            decode_json_str(raw, tmp);
            tmp
        } else {
            raw
        };
        match std::str::from_utf8(bytes) {
            Ok(s) => Ok(PyString::new(py, s).into_any()),
            // "surrogatepass" UTF-8 of lone surrogates
            Err(_) => PyBytes::new(py, bytes).call_method1("decode", ("utf-8", "surrogatepass")),
        }
    }

    #[inline(always)]
    fn mix(h: u64, w: u64) -> u64 {
        (h.rotate_left(5) ^ w).wrapping_mul(0x517c_c1b7_2722_0a95)
    }

    fn hash_bytes(mut h: u64, b: &[u8]) -> u64 {
        let mut chunks = b.chunks_exact(8);
        for c in &mut chunks {
            h = mix(h, u64::from_le_bytes(c.try_into().unwrap()));
        }
        let r = chunks.remainder();
        let mut w = [0u8; 8];
        w[..r.len()].copy_from_slice(r);
        mix(mix(h, u64::from_le_bytes(w)), b.len() as u64)
    }

    const CACHE_SLOTS: usize = 1024;
    /// Direct-mapped cache slot: a key string (its raw content `buf[start..end]`).
    type KeySlot<'py> = Option<(usize, usize, Bound<'py, PyAny>)>;
    /// Direct-mapped cache slot: a path tuple (the segments `Located::segs[a..b]`).
    type PathSlot<'py> = Option<((usize, usize), Bound<'py, PyTuple>)>;

    /// `Located` -> list of `(span_idx, kind, start, end, path)` tuples. Paths repeat from
    /// record to record and keys within a path, so both are memoized in small
    /// direct-mapped caches (equality verified, never more than [`CACHE_SLOTS`] entries);
    /// the empty path and a span's index object are shared.
    fn located_to_list<'py>(py: Python<'py>, d: &[u8], loc: &Located) -> PyResult<Bound<'py, PyList>> {
        let empty = PyTuple::empty(py);
        let segs = &loc.segs;
        let raw_of = |s: &LSeg| -> Option<&[u8]> {
            match *s {
                LSeg::Key { start, end, .. } => Some(&d[start..end]),
                LSeg::Index(_) => None,
            }
        };
        let same_path = |a: (usize, usize), b: (usize, usize)| -> bool {
            a.1 - a.0 == b.1 - b.0
                && segs[a.0..a.1].iter().zip(&segs[b.0..b.1]).all(|(x, y)| match (*x, *y) {
                    (LSeg::Index(i), LSeg::Index(j)) => i == j,
                    (LSeg::Key { .. }, LSeg::Key { .. }) => raw_of(x) == raw_of(y),
                    _ => false,
                })
        };
        let mut keys: Vec<KeySlot<'py>> = vec![None; CACHE_SLOTS];
        let mut paths: Vec<PathSlot<'py>> = vec![None; CACHE_SLOTS];
        let mut tmp = Vec::new();
        let mut items: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(loc.tokens.len());
        let mut elems: Vec<Bound<'py, PyAny>> = Vec::new();
        let mut last_span: Option<(usize, Bound<'py, PyAny>)> = None;
        for t in &loc.tokens {
            let path = if t.path.0 == t.path.1 {
                empty.clone()
            } else {
                let mut h = 0u64;
                for seg in &segs[t.path.0..t.path.1] {
                    h = match *seg {
                        LSeg::Index(ix) => mix(h ^ 0x9e37_79b9, ix),
                        LSeg::Key { start, end, .. } => hash_bytes(h, &d[start..end]),
                    };
                }
                let slot = (h as usize) % CACHE_SLOTS;
                match &paths[slot] {
                    Some((r, tup)) if same_path(*r, t.path) => tup.clone(),
                    _ => {
                        elems.clear();
                        for seg in &segs[t.path.0..t.path.1] {
                            match *seg {
                                LSeg::Index(ix) => elems.push(ix.into_pyobject(py)?.into_any()),
                                LSeg::Key { start, end, esc } => {
                                    let raw = &d[start..end];
                                    let ks = (hash_bytes(0, raw) as usize) % CACHE_SLOTS;
                                    let obj = match &keys[ks] {
                                        Some((s, e, o)) if &d[*s..*e] == raw => o.clone(),
                                        _ => {
                                            let o = key_object(py, raw, esc, &mut tmp)?;
                                            keys[ks] = Some((start, end, o.clone()));
                                            o
                                        }
                                    };
                                    elems.push(obj);
                                }
                            }
                        }
                        let tup = PyTuple::new(py, elems.drain(..))?;
                        paths[slot] = Some((t.path, tup.clone()));
                        tup
                    }
                }
            };
            let span = match &last_span {
                Some((s, o)) if *s == t.span => o.clone(),
                _ => {
                    let o = (t.span as u64).into_pyobject(py)?.into_any();
                    last_span = Some((t.span, o.clone()));
                    o
                }
            };
            let tup = PyTuple::new(
                py,
                [
                    span,
                    t.kind.into_pyobject(py)?.into_any(),
                    (t.start as u64).into_pyobject(py)?.into_any(),
                    (t.end as u64).into_pyobject(py)?.into_any(),
                    path.into_any(),
                ],
            )?;
            items.push(tup);
        }
        PyList::new(py, items)
    }

    // ---- PathExtractor ------------------------------------------------------------------
    /// Value spans at paths without parsing into Python objects (ARCHITECTURE.md §1.6).
    #[pyclass(name = "PathExtractor", module = "jsonreshape.core._rscore")]
    pub struct PyPathExtractor {
        inner: Box<PathExtractor>,
    }

    fn tag_is(t: &Bound<'_, PyAny>, name: &str) -> bool {
        match t.cast::<PyString>() {
            Ok(s) => s.to_str().map(|v| v == name).unwrap_or(false),
            Err(_) => false,
        }
    }

    fn parse_seg(seg: &Bound<'_, PyAny>) -> PyResult<PSeg> {
        let bad = || value_error(format!("invalid path segment: {}", repr(seg)));
        if !is_list_or_tuple(seg) {
            return Err(bad());
        }
        let items: Vec<Bound<'_, PyAny>> = seg.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        let tag = match items.first() {
            Some(t) => t,
            None => return Err(bad()),
        };
        if tag_is(tag, "key") && items.len() == 2 && items[1].is_exact_instance_of::<PyString>() {
            return Ok(PSeg::key(str_bytes(&items[1])?));
        }
        if tag_is(tag, "index") && items.len() == 2 && is_int(&items[1]) {
            let v = int_value(&items[1])?;
            if v >= 0 {
                return Ok(PSeg::Index(v as u64));
            }
        }
        if items.len() == 1 {
            if tag_is(tag, "any_index") {
                return Ok(PSeg::AnyIndex);
            }
            if tag_is(tag, "any_key") {
                return Ok(PSeg::AnyKey);
            }
        }
        Err(bad())
    }

    #[pymethods]
    impl PyPathExtractor {
        #[new]
        #[pyo3(signature = (paths, member_spans = None), text_signature = "(paths, member_spans=False)")]
        fn new(paths: &Bound<'_, PyAny>, member_spans: Option<&Bound<'_, PyAny>>) -> PyResult<Self> {
            if paths.is_instance_of::<PyString>() || paths.is_instance_of::<PyBytes>() {
                return Err(value_error("paths must be a list of paths"));
            }
            let it = paths
                .try_iter()
                .map_err(|_| value_error("paths must be a list of paths"))?;
            let mut v = Vec::new();
            for p in it {
                let p = p?;
                if !is_list_or_tuple(&p) {
                    return Err(value_error(format!("invalid path: {}", repr(&p))));
                }
                let mut path = Vec::new();
                for s in p.try_iter()? {
                    path.push(parse_seg(&s?)?);
                }
                v.push(path);
            }
            let member = match member_spans {
                None => false,
                Some(o) => o.is_truthy()?,
            };
            Ok(PyPathExtractor { inner: Box::new(PathExtractor::new(v, member)) })
        }

        /// Quads (span_idx, path_idx, start, end) as u64 LE values.
        fn extract<'py>(
            &self,
            py: Python<'py>,
            buf: &Bound<'py, PyAny>,
            spans: &Bound<'py, PyAny>,
        ) -> PyResult<Bound<'py, PyBytes>> {
            let data = bytes_arg(buf, "buf")?;
            let sp = bytes_arg(spans, "spans")?;
            let pairs = read_pairs(sp.as_bytes()).map_err(value_error)?;
            let d: &[u8] = data.as_bytes();
            let inner = &self.inner;
            let mut out = Vec::new();
            maybe_detach(py, d.len(), || inner.extract(d, &pairs, &mut out)).map_err(value_error)?;
            Ok(PyBytes::new(py, &out))
        }
    }

    // ---- numbers_plain ------------------------------------------------------------------
    /// True iff every number token outside strings is reproduced exactly by orjson.
    #[pyfunction]
    fn numbers_plain(py: Python<'_>, buf: &Bound<'_, PyAny>) -> PyResult<bool> {
        let data = bytes_arg(buf, "buf")?;
        let d: &[u8] = data.as_bytes();
        Ok(maybe_detach(py, d.len(), || crate::numbers::numbers_plain(d)))
    }

    #[pymodule]
    fn _rscore(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<PyScanner>()?;
        m.add_class::<PyReformatter>()?;
        m.add_class::<PyMultiFinder>()?;
        m.add_class::<PyPathExtractor>()?;
        m.add_function(wrap_pyfunction!(numbers_plain, m)?)?;
        m.add("MODE_CONTAINER", scanner::MODE_CONTAINER)?;
        m.add("MODE_BRACE_LIST", scanner::MODE_BRACE_LIST)?;
        m.add("MODE_LINES", scanner::MODE_LINES)?;
        m.add("MODE_STREAM", scanner::MODE_STREAM)?;
        m.add("MODE_SINGLE", scanner::MODE_SINGLE)?;
        m.add("FLAG_ESCAPES", scanner::FLAG_ESCAPES)?;
        m.add("FLAG_U_ESCAPES", scanner::FLAG_U_ESCAPES)?;
        m.add("FLAG_NON_ASCII", scanner::FLAG_NON_ASCII)?;
        m.add("DEFAULT_MAX_DEPTH", scanner::DEFAULT_MAX_DEPTH)?;
        for code in [
            scanner::ErrCode::UnexpectedChar,
            scanner::ErrCode::UnexpectedEof,
            scanner::ErrCode::InvalidNumber,
            scanner::ErrCode::InvalidLiteral,
            scanner::ErrCode::InvalidEscape,
            scanner::ErrCode::InvalidHex,
            scanner::ErrCode::ControlChar,
            scanner::ErrCode::InvalidUtf8,
            scanner::ErrCode::TrailingData,
            scanner::ErrCode::DepthLimit,
            scanner::ErrCode::TargetNotFound,
            scanner::ErrCode::TargetNotContainer,
        ] {
            let s = code.as_str();
            m.add(format!("ERR_{}", s.to_ascii_uppercase()), s)?;
        }
        m.add("__version__", env!("CARGO_PKG_VERSION"))?;
        Ok(())
    }
}
