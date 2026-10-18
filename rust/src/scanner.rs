//! Validating, resumable record indexer (ARCHITECTURE.md §1.2). Pure Rust, no PyO3.
//!
//! The scanner is a byte-level state machine that can stop at any byte boundary and
//! resume with the next chunk. Hot paths: SWAR scanning of string content (8 bytes per
//! step until a quote, backslash, control byte or non-ASCII byte), inline UTF-8 sequence
//! validation, tight digit loops for numbers, newline counting only while skipping
//! whitespace (raw newlines cannot occur anywhere else in valid JSON).
//!
//! Observable details follow the reference interpretations documented in
//! `jsonreshape/core/_pycore.py` (byte-identical results, any chunking):
//! * `consumed` / `lines` count every byte / `\n` passed to `feed()`, also after an error;
//! * flags are set by *complete* constructs only (a whole escape, a whole `\uXXXX`, a whole
//!   valid UTF-8 sequence); a construct cut by an error or EOF sets nothing;
//! * EOF (or a JSONL line end) inside a UTF-8 sequence is `unexpected_eof`/`string_end`;
//!   an invalid sequence is `invalid_utf8` at its lead byte, even across chunks;
//! * a scalar at the full target path is `target_not_container` at its first byte; a
//!   byte that cannot start any value is `unexpected_char` everywhere.

use crate::util::{decode_json_str, load8, str_special, utf8_lead, STR_SPECIAL, VALUE_START};

pub const MODE_CONTAINER: u8 = 1;
pub const MODE_BRACE_LIST: u8 = 2;
pub const MODE_LINES: u8 = 3;
pub const MODE_STREAM: u8 = 4;
pub const MODE_SINGLE: u8 = 5;

pub const FLAG_ESCAPES: u8 = 1;
pub const FLAG_U_ESCAPES: u8 = 2;
pub const FLAG_NON_ASCII: u8 = 4;

pub const DEFAULT_MAX_DEPTH: usize = 100_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrCode {
    UnexpectedChar,
    UnexpectedEof,
    InvalidNumber,
    InvalidLiteral,
    InvalidEscape,
    InvalidHex,
    ControlChar,
    InvalidUtf8,
    TrailingData,
    DepthLimit,
    TargetNotFound,
    TargetNotContainer,
}

impl ErrCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrCode::UnexpectedChar => "unexpected_char",
            ErrCode::UnexpectedEof => "unexpected_eof",
            ErrCode::InvalidNumber => "invalid_number",
            ErrCode::InvalidLiteral => "invalid_literal",
            ErrCode::InvalidEscape => "invalid_escape",
            ErrCode::InvalidHex => "invalid_hex",
            ErrCode::ControlChar => "control_char",
            ErrCode::InvalidUtf8 => "invalid_utf8",
            ErrCode::TrailingData => "trailing_data",
            ErrCode::DepthLimit => "depth_limit",
            ErrCode::TargetNotFound => "target_not_found",
            ErrCode::TargetNotContainer => "target_not_container",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Expected {
    Value,
    ValueOrEnd,
    KeyOrEnd,
    Key,
    Colon,
    CommaOrBracket,
    CommaOrBrace,
    OpenBrace,
    End,
    Digit,
    Hex,
    Escape,
    StringEnd,
    Literal,
    None,
}

impl Expected {
    pub fn as_str(self) -> &'static str {
        match self {
            Expected::Value => "value",
            Expected::ValueOrEnd => "value_or_end",
            Expected::KeyOrEnd => "key_or_end",
            Expected::Key => "key",
            Expected::Colon => "colon",
            Expected::CommaOrBracket => "comma_or_bracket",
            Expected::CommaOrBrace => "comma_or_brace",
            Expected::OpenBrace => "open_brace",
            Expected::End => "end",
            Expected::Digit => "digit",
            Expected::Hex => "hex",
            Expected::Escape => "escape",
            Expected::StringEnd => "string_end",
            Expected::Literal => "literal",
            Expected::None => "none",
        }
    }
}

/// `(offset, code, expected, line, line_start)` of the first error (non-LINES modes).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ScanError {
    pub offset: u64,
    pub code: ErrCode,
    pub expected: Expected,
    pub line: u64,
    pub line_start: u64,
}

/// MODE_LINES broken line: `(line_no, line_start, line_end, err_offset, code, expected)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BrokenLine {
    pub line_no: u64,
    pub line_start: u64,
    pub line_end: u64,
    pub err_offset: u64,
    pub code: ErrCode,
    pub expected: Expected,
}

/// Target path segment: decoded key ("surrogatepass" UTF-8 bytes) or array index.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Seg {
    Key(Vec<u8>),
    Index(u64),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum St {
    // between tokens
    Value,
    ValueOrEnd,
    KeyOrEnd,
    Key,
    Colon,
    AfterValue,
    End,
    StreamNext,
    OpenBrace,
    LineSkip,
    // inside tokens
    Str,
    StrEsc,
    StrHex,
    StrUtf8,
    NumMinus,
    NumZero,
    NumInt,
    NumDot,
    NumFrac,
    NumE,
    NumESign,
    NumExp,
    Lit,
    // stopped (error)
    Done,
}

const K_ARR: u8 = 0;
const K_OBJ: u8 = 1;
const K_BRACE: u8 = 2;

const NONE_DEPTH: usize = usize::MAX;

pub struct Scanner {
    mode: u8,
    max_depth: usize,
    st: St,
    stack: Vec<u8>,
    // token sub-state
    str_is_key: bool,
    hex_left: u8,
    u8_need: u8,
    u8_lo: u8,
    u8_hi: u8,
    u8_start: u64,
    lit: &'static [u8],
    lit_pos: usize,
    // positions
    base: u64,
    consumed: u64,
    /// `\n` bytes consumed by the state machine (error lines, JSONL line numbers)
    scan_lines: u64,
    scan_line_start: u64,
    /// `\n` bytes passed to `feed()` (the public `lines`; keeps counting after an error)
    fed_lines: u64,
    flags: u8,
    // records
    out: Vec<u8>,
    record_count: u64,
    emit_depth: usize,
    member_mode: bool,
    rec_start: u64,
    root_start: u64,
    // MODE_CONTAINER target tracking
    segs: Vec<Seg>,
    path_open: usize,
    watch_depth: usize,
    pending_on_path: bool,
    counters: Vec<u64>,
    target_found: bool,
    target_depth: usize,
    target_start: u64,
    container_span: Option<(u64, u64)>,
    key_capture: bool,
    key_buf: Vec<u8>,
    key_seg_start: usize,
    key_has_esc: bool,
    dec_buf: Vec<u8>,
    // results
    error: Option<ScanError>,
    finished: bool,
    finish_called: bool,
    // MODE_LINES
    broken: Vec<BrokenLine>,
    line_span: Option<(u64, u64)>,
    line_err: Option<(u64, ErrCode, Expected)>,
}

impl Scanner {
    /// `segs` is only used in MODE_CONTAINER. Returns `Err` for an unknown mode.
    pub fn new(mode: u8, segs: Vec<Seg>, base_offset: u64, max_depth: usize) -> Result<Scanner, String> {
        if !(MODE_CONTAINER..=MODE_SINGLE).contains(&mode) {
            return Err(format!("invalid scanner mode: {mode}"));
        }
        if !segs.is_empty() && mode != MODE_CONTAINER {
            return Err("target_path is only supported in MODE_CONTAINER".into());
        }
        if max_depth < 1 {
            return Err("max_depth must be >= 1".into());
        }
        let st = match mode {
            MODE_BRACE_LIST => St::OpenBrace,
            MODE_STREAM => St::StreamNext,
            _ => St::Value,
        };
        let emit_depth = match mode {
            MODE_SINGLE | MODE_STREAM => 0,
            MODE_BRACE_LIST => 1,
            _ => NONE_DEPTH,
        };
        let segs = if mode == MODE_CONTAINER { segs } else { Vec::new() };
        Ok(Scanner {
            mode,
            max_depth,
            st,
            stack: Vec::with_capacity(64),
            str_is_key: false,
            hex_left: 0,
            u8_need: 0,
            u8_lo: 0,
            u8_hi: 0,
            u8_start: 0,
            lit: b"",
            lit_pos: 0,
            base: base_offset,
            consumed: base_offset,
            scan_lines: 0,
            scan_line_start: base_offset,
            fed_lines: 0,
            flags: 0,
            out: Vec::new(),
            record_count: 0,
            emit_depth,
            member_mode: false,
            rec_start: 0,
            root_start: 0,
            segs,
            path_open: 0,
            watch_depth: NONE_DEPTH,
            pending_on_path: mode == MODE_CONTAINER,
            counters: Vec::new(),
            target_found: false,
            target_depth: NONE_DEPTH,
            target_start: 0,
            container_span: None,
            key_capture: false,
            key_buf: Vec::new(),
            key_seg_start: 0,
            key_has_esc: false,
            dec_buf: Vec::new(),
            error: None,
            finished: false,
            finish_called: false,
            broken: Vec::new(),
            line_span: None,
            line_err: None,
        })
    }

    // ---- public accessors -------------------------------------------------------------
    pub fn error(&self) -> Option<ScanError> {
        self.error
    }
    pub fn record_count(&self) -> u64 {
        self.record_count
    }
    pub fn consumed(&self) -> u64 {
        self.consumed
    }
    /// Number of `\n` bytes passed to `feed()` so far (also counted after an error).
    pub fn lines(&self) -> u64 {
        self.fed_lines
    }
    pub fn flags(&self) -> u8 {
        self.flags
    }
    pub fn target_found(&self) -> bool {
        self.target_found
    }
    pub fn container_span(&self) -> Option<(u64, u64)> {
        self.container_span
    }
    pub fn finished(&self) -> bool {
        self.finished
    }
    /// `finish()` has been called (the binding then rejects `feed()` with ValueError).
    pub fn finish_called(&self) -> bool {
        self.finish_called
    }
    pub fn take_broken(&mut self) -> Vec<BrokenLine> {
        std::mem::take(&mut self.broken)
    }
    /// Spans produced by the last `feed()`/`finish()` call.
    pub fn output(&self) -> &[u8] {
        &self.out
    }

    /// Feed the next chunk; returns the u64 LE span pairs completed in it.
    ///
    /// `consumed` always advances by `chunk.len()` (also after an error, when the bytes
    /// are ignored), so it is independent of how the input is split into chunks.
    pub fn feed(&mut self, chunk: &[u8]) -> &[u8] {
        self.out.clear();
        let base = self.consumed;
        self.consumed += chunk.len() as u64;
        self.fed_lines += memchr::memchr_iter(b'\n', chunk).count() as u64;
        if self.finished || chunk.is_empty() {
            return &self.out;
        }
        if self.mode == MODE_LINES {
            self.feed_lines(chunk, base);
        } else {
            self.run(chunk, base);
        }
        &self.out
    }

    /// Signal EOF; returns the remaining spans.
    pub fn finish(&mut self) -> &[u8] {
        self.out.clear();
        if self.finish_called {
            return &self.out;
        }
        self.finish_called = true;
        if self.finished {
            return &self.out;
        }
        let eof = self.consumed;
        if self.mode == MODE_LINES {
            self.end_line(eof);
        } else {
            self.eof_token(eof);
            if self.error.is_none() {
                self.eof_structural(eof);
            }
            if self.error.is_none() && self.mode == MODE_CONTAINER && !self.target_found {
                self.fail(eof, ErrCode::TargetNotFound, Expected::None);
            }
        }
        self.finished = true;
        &self.out
    }

    // ---- internals --------------------------------------------------------------------
    #[inline(always)]
    fn abs(&self, i: usize) -> u64 {
        self.base + i as u64
    }

    #[inline(always)]
    fn emit(&mut self, s: u64, e: u64) {
        self.out.extend_from_slice(&s.to_le_bytes());
        self.out.extend_from_slice(&e.to_le_bytes());
        self.record_count += 1;
    }

    #[cold]
    fn fail(&mut self, off: u64, code: ErrCode, exp: Expected) {
        if self.mode == MODE_LINES {
            if self.line_err.is_none() {
                self.line_err = Some((off, code, exp));
            }
            self.st = St::LineSkip;
        } else {
            self.error = Some(ScanError {
                offset: off,
                code,
                expected: exp,
                line: self.scan_lines,
                line_start: self.scan_line_start,
            });
            self.st = St::Done;
            self.finished = true;
        }
    }

    fn feed_lines(&mut self, buf: &[u8], base: u64) {
        let n = buf.len();
        let mut i = 0;
        while i < n {
            match memchr::memchr(b'\n', &buf[i..]) {
                Some(k) => {
                    let j = i + k;
                    if j > i {
                        self.run(&buf[i..j], base + i as u64);
                    }
                    self.end_line(base + j as u64);
                    self.scan_lines += 1;
                    self.scan_line_start = base + j as u64 + 1;
                    i = j + 1;
                }
                None => {
                    self.run(&buf[i..], base + i as u64);
                    break;
                }
            }
        }
    }

    /// MODE_LINES: the current line ends at `eol` (offset of `\n`, or EOF).
    fn end_line(&mut self, eol: u64) {
        self.eof_token(eol);
        match self.st {
            St::End => {
                if let Some((s, e)) = self.line_span {
                    self.emit(s, e);
                }
            }
            St::Value if self.stack.is_empty() => {} // whitespace-only line
            St::LineSkip => {}
            _ => self.eof_structural(eol),
        }
        if let Some((off, code, exp)) = self.line_err.take() {
            self.broken.push(BrokenLine {
                line_no: self.scan_lines,
                line_start: self.scan_line_start,
                line_end: eol,
                err_offset: off,
                code,
                expected: exp,
            });
        }
        self.st = St::Value;
        self.stack.clear();
        self.line_span = None;
    }

    /// EOF (or JSONL line end) while inside a token.
    fn eof_token(&mut self, eof: u64) {
        match self.st {
            St::NumZero | St::NumInt | St::NumFrac | St::NumExp => self.value_done(eof),
            St::NumMinus | St::NumDot | St::NumE | St::NumESign => {
                self.fail(eof, ErrCode::UnexpectedEof, Expected::Digit)
            }
            St::Lit => self.fail(eof, ErrCode::UnexpectedEof, Expected::Literal),
            St::Str => self.fail(eof, ErrCode::UnexpectedEof, Expected::StringEnd),
            St::StrEsc => self.fail(eof, ErrCode::UnexpectedEof, Expected::Escape),
            St::StrHex => self.fail(eof, ErrCode::UnexpectedEof, Expected::Hex),
            // a sequence cut by EOF (or a JSONL line end) is truncated, not invalid
            St::StrUtf8 => self.fail(eof, ErrCode::UnexpectedEof, Expected::StringEnd),
            _ => {}
        }
    }

    /// EOF (or JSONL line end) between tokens.
    fn eof_structural(&mut self, eof: u64) {
        let exp = match self.st {
            St::Value => Expected::Value,
            St::ValueOrEnd => Expected::ValueOrEnd,
            St::KeyOrEnd => Expected::KeyOrEnd,
            St::Key => Expected::Key,
            St::Colon => Expected::Colon,
            St::AfterValue => self.after_value_expected(),
            St::OpenBrace => Expected::OpenBrace,
            _ => return,
        };
        self.fail(eof, ErrCode::UnexpectedEof, exp);
    }

    #[inline]
    fn after_value_expected(&self) -> Expected {
        match self.stack.last() {
            Some(&K_ARR) => Expected::CommaOrBracket,
            _ => Expected::CommaOrBrace,
        }
    }

    /// A value ended at absolute offset `end` (exclusive).
    #[inline(always)]
    fn value_done(&mut self, end: u64) {
        let d = self.stack.len();
        if d == self.emit_depth {
            let s = self.rec_start;
            self.emit(s, end);
        }
        if d == 0 {
            self.st = match self.mode {
                MODE_STREAM => St::StreamNext,
                MODE_LINES => {
                    self.line_span = Some((self.root_start, end));
                    St::End
                }
                _ => St::End,
            };
        } else {
            self.st = St::AfterValue;
        }
    }

    /// Process the chunk `buf` whose first byte is at absolute offset `base`.
    fn run(&mut self, buf: &[u8], base: u64) {
        self.base = base;
        let n = buf.len();
        let mut i = 0usize;
        while i < n {
            i = match self.st {
                St::Str => self.scan_str(buf, i),
                St::NumInt => self.num_int(buf, i),
                St::NumFrac => self.num_frac(buf, i),
                St::Done => return,
                St::LineSkip => n,
                St::Value
                | St::ValueOrEnd
                | St::KeyOrEnd
                | St::Key
                | St::Colon
                | St::AfterValue
                | St::End
                | St::StreamNext
                | St::OpenBrace => self.structural(buf, i),
                St::StrEsc => self.str_escape(buf, i),
                St::StrHex => self.str_hex(buf, i),
                St::StrUtf8 => self.str_utf8(buf, i),
                St::NumMinus => self.num_minus(buf, i),
                St::NumZero => self.num_after_int(buf, i),
                St::NumDot => self.num_dot(buf, i),
                St::NumE => self.num_e(buf, i),
                St::NumESign => self.num_esign(buf, i),
                St::NumExp => self.num_exp(buf, i),
                St::Lit => self.literal(buf, i),
            };
        }
        if self.key_capture {
            // key string continues in the next chunk: keep its bytes
            let from = self.key_seg_start.min(n);
            self.key_buf.extend_from_slice(&buf[from..n]);
            self.key_seg_start = 0;
        }
    }

    #[inline(always)]
    fn structural(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        let mut b;
        loop {
            b = buf[i];
            if b > b' ' {
                break;
            }
            match b {
                b' ' | b'\t' | b'\r' => {}
                b'\n' => {
                    self.scan_lines += 1;
                    self.scan_line_start = self.base + i as u64 + 1;
                }
                _ => break,
            }
            i += 1;
            if i >= n {
                return n;
            }
        }
        match self.st {
            St::AfterValue => {
                let top = *self.stack.last().unwrap_or(&K_ARR);
                if b == b',' {
                    self.st = if top == K_OBJ { St::Key } else { St::Value };
                    return i + 1;
                }
                if top == K_ARR {
                    if b == b']' {
                        return self.close(i);
                    }
                    self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::CommaOrBracket);
                } else {
                    if b == b'}' {
                        return self.close(i);
                    }
                    self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::CommaOrBrace);
                }
                i
            }
            St::Value => {
                if VALUE_START[b as usize] {
                    return self.begin_value(buf, i);
                }
                self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::Value);
                i
            }
            St::Key | St::KeyOrEnd => {
                if b == b'"' {
                    return self.begin_key(buf, i);
                }
                if b == b'}' && self.st == St::KeyOrEnd {
                    return self.close(i);
                }
                let exp = if self.st == St::Key { Expected::Key } else { Expected::KeyOrEnd };
                self.fail(self.abs(i), ErrCode::UnexpectedChar, exp);
                i
            }
            St::Colon => {
                if b == b':' {
                    self.st = St::Value;
                    return i + 1;
                }
                self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::Colon);
                i
            }
            St::ValueOrEnd => {
                let top = *self.stack.last().unwrap_or(&K_ARR);
                if (b == b']' && top == K_ARR) || (b == b'}' && top == K_BRACE) {
                    return self.close(i);
                }
                if VALUE_START[b as usize] {
                    return self.begin_value(buf, i);
                }
                self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::ValueOrEnd);
                i
            }
            St::End => {
                self.fail(self.abs(i), ErrCode::TrailingData, Expected::End);
                i
            }
            St::StreamNext => {
                if VALUE_START[b as usize] {
                    return self.begin_value(buf, i);
                }
                self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::Value);
                i
            }
            St::OpenBrace => {
                if b == b'{' {
                    if self.stack.len() >= self.max_depth {
                        self.fail(self.abs(i), ErrCode::DepthLimit, Expected::None);
                        return i;
                    }
                    self.stack.push(K_BRACE);
                    self.st = St::ValueOrEnd;
                    return i + 1;
                }
                self.fail(self.abs(i), ErrCode::UnexpectedChar, Expected::OpenBrace);
                i
            }
            _ => unreachable!("structural() called in token state"),
        }
    }

    /// `buf[i]` starts a value (caller checked VALUE_START).
    #[inline(always)]
    fn begin_value(&mut self, buf: &[u8], i: usize) -> usize {
        let b = buf[i];
        let pos = self.abs(i);
        let d = self.stack.len();
        if d == self.emit_depth && !self.member_mode {
            self.rec_start = pos;
        }
        if d == 0 {
            self.root_start = pos;
        }
        let is_container = b == b'{' || b == b'[';
        if is_container && d >= self.max_depth {
            self.fail(pos, ErrCode::DepthLimit, Expected::None);
            return i;
        }
        if (self.pending_on_path || d == self.watch_depth) && !self.path_value_start(b, pos, d) {
            return i;
        }
        match b {
            b'"' => {
                self.str_is_key = false;
                self.st = St::Str;
                self.scan_str(buf, i + 1)
            }
            b'{' => {
                self.stack.push(K_OBJ);
                self.st = St::KeyOrEnd;
                i + 1
            }
            b'[' => {
                self.stack.push(K_ARR);
                self.st = St::ValueOrEnd;
                i + 1
            }
            b'-' => {
                self.st = St::NumMinus;
                i + 1
            }
            b'0' => {
                self.st = St::NumZero;
                i + 1
            }
            b'1'..=b'9' => {
                self.st = St::NumInt;
                self.num_int(buf, i + 1)
            }
            b't' => self.begin_literal(buf, i, b"true"),
            b'f' => self.begin_literal(buf, i, b"false"),
            _ => self.begin_literal(buf, i, b"null"),
        }
    }

    /// MODE_CONTAINER path bookkeeping at the start of a value at depth `d`.
    /// Returns false when an error was raised.
    #[cold]
    #[inline(never)]
    fn path_value_start(&mut self, b: u8, pos: u64, d: usize) -> bool {
        let is_container = b == b'{' || b == b'[';
        if d > 0 && d == self.watch_depth && self.stack[d - 1] == K_ARR {
            let c = self.counters[d - 1];
            self.counters[d - 1] = c + 1;
            if let Seg::Index(ix) = self.segs[d - 1] {
                if c == ix {
                    self.pending_on_path = true;
                }
            }
        }
        if !self.pending_on_path {
            return true;
        }
        self.pending_on_path = false;
        if self.path_open == self.segs.len() {
            // this value is the target
            if !is_container {
                self.fail(pos, ErrCode::TargetNotContainer, Expected::None);
                return false;
            }
            self.target_found = true;
            self.target_start = pos;
            self.target_depth = d + 1;
            self.emit_depth = d + 1;
            // object target: records are members ("k": v), not values
            self.member_mode = b == b'{';
            self.watch_depth = NONE_DEPTH;
            self.path_open = d + 1;
        } else if is_container {
            self.path_open = d + 1;
            self.watch_depth = d + 1;
            if self.counters.len() < d + 1 {
                self.counters.resize(d + 1, 0);
            }
            self.counters[d] = 0;
        }
        true
    }

    #[inline(always)]
    fn begin_key(&mut self, buf: &[u8], i: usize) -> usize {
        let d = self.stack.len();
        if d == self.emit_depth && self.member_mode {
            self.rec_start = self.abs(i);
        }
        self.str_is_key = true;
        if d == self.watch_depth && d > 0 {
            if let Seg::Key(_) = self.segs[d - 1] {
                self.key_capture = true;
                self.key_buf.clear();
                self.key_seg_start = i + 1;
                self.key_has_esc = false;
            }
        }
        self.st = St::Str;
        self.scan_str(buf, i + 1)
    }

    #[inline(always)]
    fn close(&mut self, i: usize) -> usize {
        let d = self.stack.len();
        let end = self.abs(i) + 1;
        if d == self.path_open && d > 0 {
            self.path_open = d - 1;
            if self.target_found && d == self.target_depth && self.container_span.is_none() {
                self.container_span = Some((self.target_start, end));
                self.emit_depth = NONE_DEPTH;
            }
            self.watch_depth = if self.target_found { NONE_DEPTH } else { self.path_open };
        }
        self.stack.pop();
        self.value_done(end);
        i + 1
    }

    // ---- strings ------------------------------------------------------------------------
    /// Inside a string at `buf[i]`.
    #[inline(always)]
    fn scan_str(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        loop {
            while i + 8 <= n {
                let m = str_special(load8(buf, i));
                if m != 0 {
                    i += (m.trailing_zeros() >> 3) as usize;
                    break;
                }
                i += 8;
            }
            while i < n && !STR_SPECIAL[buf[i] as usize] {
                i += 1;
            }
            if i >= n {
                return n;
            }
            let b = buf[i];
            if b == b'"' {
                return self.end_string(buf, i);
            }
            if b == b'\\' {
                if self.str_is_key {
                    self.key_has_esc = true;
                }
                // fast path: complete escape inside this buffer
                if i + 1 < n {
                    let e = buf[i + 1];
                    match e {
                        b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => {
                            self.flags |= FLAG_ESCAPES;
                            i += 2;
                            continue;
                        }
                        b'u' => {
                            if i + 6 <= n
                                && buf[i + 2].is_ascii_hexdigit()
                                && buf[i + 3].is_ascii_hexdigit()
                                && buf[i + 4].is_ascii_hexdigit()
                                && buf[i + 5].is_ascii_hexdigit()
                            {
                                self.flags |= FLAG_ESCAPES | FLAG_U_ESCAPES;
                                i += 6;
                                continue;
                            }
                            self.hex_left = 4;
                            self.st = St::StrHex;
                            return i + 2;
                        }
                        _ => {
                            self.fail(self.abs(i + 1), ErrCode::InvalidEscape, Expected::Escape);
                            return i + 1;
                        }
                    }
                }
                self.st = St::StrEsc;
                return i + 1;
            }
            if b < 0x20 {
                self.fail(self.abs(i), ErrCode::ControlChar, Expected::StringEnd);
                return i;
            }
            // non-ASCII: validate the UTF-8 sequence (flag set once it is complete)
            let (need, lo, hi) = match utf8_lead(b) {
                Some(t) => t,
                None => {
                    self.fail(self.abs(i), ErrCode::InvalidUtf8, Expected::StringEnd);
                    return i;
                }
            };
            let need = need as usize;
            if i + need < n {
                let c1 = buf[i + 1];
                let ok = c1 >= lo
                    && c1 <= hi
                    && (need < 2 || (buf[i + 2] & 0xC0) == 0x80)
                    && (need < 3 || (buf[i + 3] & 0xC0) == 0x80);
                if !ok {
                    self.fail(self.abs(i), ErrCode::InvalidUtf8, Expected::StringEnd);
                    return i;
                }
                self.flags |= FLAG_NON_ASCII;
                i += need + 1;
                continue;
            }
            // sequence split across chunks: validate what we have, keep the rest pending
            self.u8_start = self.abs(i);
            self.u8_need = need as u8;
            self.u8_lo = lo;
            self.u8_hi = hi;
            self.st = St::StrUtf8;
            return self.str_utf8(buf, i + 1);
        }
    }

    #[inline(always)]
    fn end_string(&mut self, buf: &[u8], i: usize) -> usize {
        if self.str_is_key {
            if self.key_capture {
                self.key_capture = false;
                let from = self.key_seg_start.min(i);
                self.compare_key(&buf[from..i]);
            }
            self.st = St::Colon;
        } else {
            let end = self.abs(i) + 1;
            self.value_done(end);
        }
        i + 1
    }

    /// Compare the key just completed (bytes kept in `key_buf` + `tail`) with the target
    /// segment of the deepest on-path object.
    #[cold]
    fn compare_key(&mut self, tail: &[u8]) {
        let d = self.stack.len();
        let mut kb = std::mem::take(&mut self.key_buf);
        let raw: &[u8] = if kb.is_empty() {
            tail
        } else {
            kb.extend_from_slice(tail);
            &kb
        };
        let eq = match self.segs.get(d.wrapping_sub(1)) {
            Some(Seg::Key(k)) => {
                if self.key_has_esc {
                    decode_json_str(raw, &mut self.dec_buf);
                    self.dec_buf == *k
                } else {
                    raw == &k[..]
                }
            }
            _ => false,
        };
        kb.clear();
        self.key_buf = kb;
        if eq {
            self.pending_on_path = true;
        }
    }

    fn str_escape(&mut self, buf: &[u8], i: usize) -> usize {
        match buf[i] {
            b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => {
                self.flags |= FLAG_ESCAPES;
                self.st = St::Str;
                i + 1
            }
            b'u' => {
                self.hex_left = 4;
                self.st = St::StrHex;
                i + 1
            }
            _ => {
                self.fail(self.abs(i), ErrCode::InvalidEscape, Expected::Escape);
                i
            }
        }
    }

    fn str_hex(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        while i < n && self.hex_left > 0 {
            if !buf[i].is_ascii_hexdigit() {
                self.fail(self.abs(i), ErrCode::InvalidHex, Expected::Hex);
                return i;
            }
            i += 1;
            self.hex_left -= 1;
        }
        if self.hex_left == 0 {
            self.flags |= FLAG_ESCAPES | FLAG_U_ESCAPES;
            self.st = St::Str;
        }
        i
    }

    fn str_utf8(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        while i < n && self.u8_need > 0 {
            let c = buf[i];
            if c < self.u8_lo || c > self.u8_hi {
                let at = self.u8_start;
                self.fail(at, ErrCode::InvalidUtf8, Expected::StringEnd);
                return i;
            }
            self.u8_lo = 0x80;
            self.u8_hi = 0xBF;
            self.u8_need -= 1;
            i += 1;
        }
        if self.u8_need == 0 {
            self.flags |= FLAG_NON_ASCII;
            self.st = St::Str;
        }
        i
    }

    // ---- numbers ------------------------------------------------------------------------
    fn num_minus(&mut self, buf: &[u8], i: usize) -> usize {
        let b = buf[i];
        if b == b'0' {
            self.st = St::NumZero;
            i + 1
        } else if b.is_ascii_digit() {
            self.st = St::NumInt;
            i + 1
        } else {
            self.fail(self.abs(i), ErrCode::InvalidNumber, Expected::Digit);
            i
        }
    }

    #[inline(always)]
    fn num_int(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        while i < n && buf[i].is_ascii_digit() {
            i += 1;
        }
        if i >= n {
            return n;
        }
        self.num_after_int(buf, i)
    }

    /// After the integer part (`0` or `[1-9][0-9]*`) at `buf[i]`.
    #[inline(always)]
    fn num_after_int(&mut self, buf: &[u8], i: usize) -> usize {
        match buf[i] {
            b'.' => {
                self.st = St::NumDot;
                i + 1
            }
            b'e' | b'E' => {
                self.st = St::NumE;
                i + 1
            }
            _ => {
                let end = self.abs(i);
                self.value_done(end);
                i
            }
        }
    }

    fn num_dot(&mut self, buf: &[u8], i: usize) -> usize {
        if buf[i].is_ascii_digit() {
            self.st = St::NumFrac;
            self.num_frac(buf, i + 1)
        } else {
            self.fail(self.abs(i), ErrCode::InvalidNumber, Expected::Digit);
            i
        }
    }

    #[inline(always)]
    fn num_frac(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        while i < n && buf[i].is_ascii_digit() {
            i += 1;
        }
        if i >= n {
            return n;
        }
        match buf[i] {
            b'e' | b'E' => {
                self.st = St::NumE;
                i + 1
            }
            _ => {
                let end = self.abs(i);
                self.value_done(end);
                i
            }
        }
    }

    fn num_e(&mut self, buf: &[u8], i: usize) -> usize {
        let b = buf[i];
        if b == b'+' || b == b'-' {
            self.st = St::NumESign;
            i + 1
        } else if b.is_ascii_digit() {
            self.st = St::NumExp;
            i + 1
        } else {
            self.fail(self.abs(i), ErrCode::InvalidNumber, Expected::Digit);
            i
        }
    }

    fn num_esign(&mut self, buf: &[u8], i: usize) -> usize {
        if buf[i].is_ascii_digit() {
            self.st = St::NumExp;
            i + 1
        } else {
            self.fail(self.abs(i), ErrCode::InvalidNumber, Expected::Digit);
            i
        }
    }

    fn num_exp(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        while i < n && buf[i].is_ascii_digit() {
            i += 1;
        }
        if i >= n {
            return n;
        }
        let end = self.abs(i);
        self.value_done(end);
        i
    }

    // ---- literals -----------------------------------------------------------------------
    #[inline(always)]
    fn begin_literal(&mut self, buf: &[u8], i: usize, lit: &'static [u8]) -> usize {
        let l = lit.len();
        if i + l <= buf.len() && &buf[i..i + l] == lit {
            let end = self.abs(i + l);
            self.value_done(end);
            return i + l;
        }
        self.lit = lit;
        self.lit_pos = 1;
        self.st = St::Lit;
        self.literal(buf, i + 1)
    }

    fn literal(&mut self, buf: &[u8], mut i: usize) -> usize {
        let n = buf.len();
        let l = self.lit.len();
        while i < n && self.lit_pos < l {
            if buf[i] != self.lit[self.lit_pos] {
                self.fail(self.abs(i), ErrCode::InvalidLiteral, Expected::Literal);
                return i;
            }
            i += 1;
            self.lit_pos += 1;
        }
        if self.lit_pos == l {
            let end = self.abs(i);
            self.value_done(end);
        }
        i
    }
}
