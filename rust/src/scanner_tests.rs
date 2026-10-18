//! Unit tests for `scanner` (pure Rust). Every case is also checked for chunk-split
//! invariance: whole input, byte-by-byte feeding and a 2-way split at every position
//! must give identical spans, errors, broken lines, flags and line counts.

use crate::scanner::*;

type Err5 = (u64, &'static str, &'static str, u64, u64);
type Case = (&'static [u8], (u64, &'static str, &'static str));
type Res = (Vec<(u64, u64)>, Option<Err5>, Vec<BrokenLine>, u8, u64, u64);

fn scan_chunks(mode: u8, segs: &[Seg], base: u64, chunks: &[&[u8]]) -> Res {
    let mut s = Scanner::new(mode, segs.to_vec(), base, DEFAULT_MAX_DEPTH).unwrap();
    let mut out = Vec::new();
    let mut broken = Vec::new();
    for c in chunks {
        out.extend_from_slice(s.feed(c));
        broken.extend(s.take_broken());
    }
    out.extend_from_slice(s.finish());
    broken.extend(s.take_broken());
    let spans = out
        .chunks_exact(16)
        .map(|c| {
            (
                u64::from_le_bytes(c[..8].try_into().unwrap()),
                u64::from_le_bytes(c[8..].try_into().unwrap()),
            )
        })
        .collect();
    let err = s
        .error()
        .map(|e| (e.offset, e.code.as_str(), e.expected.as_str(), e.line, e.line_start));
    (spans, err, broken, s.flags(), s.lines(), s.consumed())
}

/// Scan whole, byte by byte and split at every position: all must agree.
fn scan(mode: u8, segs: &[Seg], data: &[u8]) -> Res {
    let whole = scan_chunks(mode, segs, 0, &[data]);
    let bytes: Vec<&[u8]> = data.chunks(1).collect();
    assert_eq!(
        scan_chunks(mode, segs, 0, &bytes),
        whole,
        "byte-by-byte {:?}",
        String::from_utf8_lossy(data)
    );
    for k in 0..=data.len() {
        let (a, b) = data.split_at(k);
        assert_eq!(scan_chunks(mode, segs, 0, &[a, b]), whole, "split at {k}");
    }
    whole
}

fn key(s: &str) -> Seg {
    Seg::Key(s.as_bytes().to_vec())
}

#[test]
fn container_root_array_and_object() {
    let (spans, err, ..) = scan(MODE_CONTAINER, &[], br#"[1, "a", {"b": [true]}]"#);
    assert_eq!(spans, vec![(1, 2), (4, 7), (9, 22)]);
    assert_eq!(err, None);
    let (spans, ..) = scan(MODE_CONTAINER, &[], br#"{"a": 1, "b": [2]}"#);
    assert_eq!(spans, vec![(1, 7), (9, 17)]);
    let mut s = Scanner::new(MODE_CONTAINER, vec![], 100, DEFAULT_MAX_DEPTH).unwrap();
    let mut out = s.feed(b" [] ").to_vec();
    out.extend_from_slice(s.finish());
    assert!(out.is_empty());
    assert_eq!(s.container_span(), Some((101, 103)));
    assert!(s.target_found() && s.finished() && s.error().is_none());
    assert_eq!(s.consumed(), 104);
}

#[test]
fn target_paths() {
    let doc = br#"{"meta": {"data": [0]}, "data": [10, 20], "data": [30]}"#;
    let (spans, err, ..) = scan(MODE_CONTAINER, &[key("data")], doc);
    assert_eq!(err, None);
    let p10 = doc.windows(2).position(|w| w == b"10").unwrap() as u64;
    assert_eq!(spans, vec![(p10, p10 + 2), (p10 + 4, p10 + 6)]);
    let doc = br#"{"data": [{"x": [1]}, {"x": [2, 3]}]}"#;
    let (spans, ..) = scan(MODE_CONTAINER, &[key("data"), Seg::Index(1), key("x")], doc);
    assert_eq!(spans, vec![(29, 30), (32, 33)]);
    let (_, err, ..) = scan(MODE_CONTAINER, &[key("a")], br#"{"a": 5}"#);
    assert_eq!(err, Some((6, "target_not_container", "none", 0, 0)));
    let (_, err, ..) = scan(MODE_CONTAINER, &[key("a")], b"{\"b\": [1]}\n");
    assert_eq!(err, Some((11, "target_not_found", "none", 1, 11)));
    let (_, err, ..) = scan(MODE_CONTAINER, &[Seg::Index(0)], br#"{"0": [1]}"#);
    assert_eq!(err.unwrap().1, "target_not_found");
    let (_, err, ..) = scan(MODE_CONTAINER, &[key("0")], b"[[1]]");
    assert_eq!(err.unwrap().1, "target_not_found");
    // escaped key matches its decoded form (\x5c = backslash)
    let (spans, err, ..) = scan(MODE_CONTAINER, &[key("data")], b"{\"d\x5cu0061ta\": [7]}");
    assert_eq!((spans, err), (vec![(15, 16)], None));
    // object target: member spans
    let (spans, ..) = scan(MODE_CONTAINER, &[key("o")], br#"{"o": {"k": 1, "l": [2]}}"#);
    assert_eq!(spans, vec![(7, 13), (15, 23)]);
}

#[test]
fn errors() {
    let cases: &[Case] = &[
        (b"[01]", (2, "unexpected_char", "comma_or_bracket")),
        (b"[1x]", (2, "unexpected_char", "comma_or_bracket")),
        (b"[-]", (2, "invalid_number", "digit")),
        (b"[tru]", (4, "invalid_literal", "literal")),
        (b"[1.]", (3, "invalid_number", "digit")),
        (b"[1e+]", (4, "invalid_number", "digit")),
        (b"[\"\\x\"]", (3, "invalid_escape", "escape")),
        (b"[\"\x5cu12G4\"]", (6, "invalid_hex", "hex")),
        (b"[\"a\tb\"]", (3, "control_char", "string_end")),
        (b"[\"\xff\"]", (2, "invalid_utf8", "string_end")),
        (b"[\"\xc3(\"]", (2, "invalid_utf8", "string_end")),
        (b"[\"\xed\xa0\x80\"]", (2, "invalid_utf8", "string_end")),
        (b"[\"\xc3", (3, "unexpected_eof", "string_end")),
        (b"[1] x", (4, "trailing_data", "end")),
        (b"[1,]", (3, "unexpected_char", "value")),
        (br#"{"a":1,}"#, (7, "unexpected_char", "key")),
        (br#"{"a" 1}"#, (5, "unexpected_char", "colon")),
        (b"{1:2}", (1, "unexpected_char", "key_or_end")),
        (br#"{"a":1 "b":2}"#, (7, "unexpected_char", "comma_or_brace")),
        (b"[", (1, "unexpected_eof", "value_or_end")),
        (b"[1,", (3, "unexpected_eof", "value")),
        (br#"{"a""#, (4, "unexpected_eof", "colon")),
        (b"[1", (2, "unexpected_eof", "comma_or_bracket")),
        (b"[\"ab", (4, "unexpected_eof", "string_end")),
        (b"[\"a\\", (4, "unexpected_eof", "escape")),
        (b"[\"\x5cu12", (6, "unexpected_eof", "hex")),
        (b"[-", (2, "unexpected_eof", "digit")),
        (b"[tr", (3, "unexpected_eof", "literal")),
        (b"   ", (3, "unexpected_eof", "value")),
        (b"[\xc3\xa9]", (1, "unexpected_char", "value_or_end")),
    ];
    for (doc, (off, code, exp)) in cases {
        let (_, err, ..) = scan(MODE_CONTAINER, &[], doc);
        let e = err.unwrap_or_else(|| panic!("no error for {doc:?}"));
        assert_eq!((e.0, e.1, e.2), (*off, *code, *exp), "{:?}", String::from_utf8_lossy(doc));
    }
    let (_, err, ..) = scan(MODE_CONTAINER, &[], b"[\n1,\n  x]");
    assert_eq!(err, Some((7, "unexpected_char", "value", 2, 5)));
}

#[test]
fn other_modes() {
    let (spans, err, ..) = scan(MODE_SINGLE, &[], b" 123 ");
    assert_eq!((spans, err), (vec![(1, 4)], None));
    let (spans, err, ..) = scan(MODE_STREAM, &[], br#"1 2 [3]{"a":4}"s"truefalse1-2"#);
    assert_eq!(err, None);
    assert_eq!(
        spans,
        vec![(0, 1), (2, 3), (4, 7), (7, 14), (14, 17), (17, 21), (21, 26), (26, 27), (27, 29)]
    );
    let (spans, err, ..) = scan(MODE_BRACE_LIST, &[], br#"{ {"a":1}, [2], 3 }"#);
    assert_eq!((spans, err), (vec![(2, 9), (11, 14), (16, 17)], None));
    let (_, err, ..) = scan(MODE_BRACE_LIST, &[], b"");
    assert_eq!(err, Some((0, "unexpected_eof", "open_brace", 0, 0)));
    let (spans, err, ..) = scan(MODE_BRACE_LIST, &[], br#"{"a":1}"#);
    assert_eq!(
        (spans, err),
        (vec![(1, 4)], Some((4, "unexpected_char", "comma_or_brace", 0, 0)))
    );
}

#[test]
fn lines_mode() {
    let doc = b"{\"a\":1}\n{\"b\":\n[1] x\n\"ok\"";
    let (spans, err, broken, _, lines, _) = scan(MODE_LINES, &[], doc);
    assert_eq!(err, None);
    assert_eq!(spans, vec![(0, 7), (20, 24)]);
    assert_eq!(lines, 3);
    let b: Vec<_> = broken
        .iter()
        .map(|b| (b.line_no, b.line_start, b.line_end, b.err_offset, b.code.as_str(), b.expected.as_str()))
        .collect();
    assert_eq!(
        b,
        vec![(1, 8, 13, 13, "unexpected_eof", "value"), (2, 14, 19, 18, "trailing_data", "end")]
    );
    let (spans, _, broken, ..) = scan(MODE_LINES, &[], b"[2]\r\n\n  3  \n\"ab\r\n");
    assert_eq!(spans, vec![(0, 3), (8, 9)]);
    assert_eq!(
        (broken[0].err_offset, broken[0].code, broken[0].line_end),
        (15, ErrCode::ControlChar, 16)
    );
}

#[test]
fn flags() {
    assert_eq!(scan(MODE_SINGLE, &[], br#"["a\nb"]"#).3, FLAG_ESCAPES);
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"\x5cu00e9\"]").3, FLAG_ESCAPES | FLAG_U_ESCAPES);
    assert_eq!(scan(MODE_SINGLE, &[], "[\"\u{e9}\"]".as_bytes()).3, FLAG_NON_ASCII);
    assert_eq!(scan(MODE_SINGLE, &[], br#"{"\t":1}"#).3, FLAG_ESCAPES);
    // only complete constructs set flags
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"a\\x\"]").3, 0);
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"a\\").3, 0);
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"\x5cu12").3, 0);
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"\xc3").3, 0);
    assert_eq!(scan(MODE_SINGLE, &[], b"[\"\xc3(\"]").3, 0);
}

#[test]
fn lines_count_every_fed_newline() {
    let mut s = Scanner::new(MODE_SINGLE, vec![], 0, DEFAULT_MAX_DEPTH).unwrap();
    s.feed(b"[1, x]\n");
    s.feed(b"\n\n");
    s.finish();
    let e = s.error().unwrap();
    assert_eq!((e.offset, e.line), (4, 0));
    assert_eq!((s.lines(), s.consumed()), (3, 9));
    assert!(s.finish_called() && s.finished());
}

#[test]
fn constructor_errors() {
    assert!(Scanner::new(0, vec![], 0, 10).is_err());
    assert!(Scanner::new(MODE_SINGLE, vec![Seg::Index(0)], 0, 10).is_err());
    assert!(Scanner::new(MODE_SINGLE, vec![], 0, 0).is_err());
    assert!(Scanner::new(MODE_CONTAINER, vec![Seg::Index(0)], 0, 1).is_ok());
}

#[test]
fn depth_limit() {
    let mut s = Scanner::new(MODE_SINGLE, vec![], 0, 2).unwrap();
    s.feed(b"[[[]]]");
    let e = s.error().unwrap();
    assert_eq!((e.offset, e.code, e.expected), (2, ErrCode::DepthLimit, Expected::None));
    let mut s = Scanner::new(MODE_SINGLE, vec![], 0, 2).unwrap();
    s.feed(b"[[]]");
    s.finish();
    assert!(s.error().is_none());
}
