"""EditSession: every command on every document kind (§12.5, acceptance test 19 core part).

All 7 document kinds (+ a wrapped document with an object container) x compact/pretty styles x both core backends
run one scripted scenario of every command through ``edits_helpers.Harness``: after every step the parsed effective
document equals the independent model, the ChangeSet is consistent with the documents before/after, undo restores
the exact previous effective bytes and redo re-creates them; at the end undo-all restores the original document byte
for byte (records and the root text) and redo-all the final one.
"""

from __future__ import annotations

from typing import Any

import pytest

from edits_helpers import KINDS, Harness, N, open_doc, others, parse, pos, ref
from jsonreshape import editops, reader
from jsonreshape.edits import EditSession, Ref
from jsonreshape.recordset import RecordSet


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------------
# every command on every kind
# ----------------------------------------------------------------------------------
def scenario(h: Harness) -> None:
    m = lambda: h.model   # noqa: E731
    P = lambda key: pos(h.model, key)   # noqa: E731
    single = h.model.kind == "single"
    member = h.model.member
    wrapped = h.model.kind == "wrapped"
    anchors = ("x", "bo'sh", "deep", "ism ‘o’")
    # -- values (anchors: record 2 gets "x", 1 gets "bo'sh", 0 gets "deep", 3 gets "ism ‘o’") ------------
    r = ref(m(), 1, "tags", 0)
    h.run(lambda s: s.set_value(r, '{"new": [1, 2.50, "Ўзбек"], "n": 12345678901234567890}'),
          lambda md: md.set_value(r, parse('{"new": [1, 2.50, "Ўзбек"], "n": 12345678901234567890}')), targets=(r,))
    r2 = ref(m(), 2)
    text = '{"id": 2, "x": [1e400, -0.0, 0.1, 1.10, "O\'zbekiston"]}'
    h.run(lambda s: s.set_value(r2, text), lambda md: md.set_value(r2, parse(text)), targets=(r2,))
    r0 = ref(m(), 0)
    h.run(lambda s: s.set_path(Ref(r0.record, r0.path + ("deep", "er", "est")), '"yangi"'),
          lambda md: md.set_path(r0.record, r0.path + ("deep", "er", "est"), "yangi"))
    rr = ref(m(), 0, "deep", "er", "est")
    h.run(lambda s: s.set_path(Ref(rr.record, rr.path), "true"), lambda md: md.set_path(rr.record, rr.path, True),
          targets=(rr,))
    h.fails("edits.error.not_object", lambda s: s.set_path(Ref(rr.record, rr.path + ("z",)), "1"))
    rn = ref(m(), 3, "name")
    h.run(lambda s: s.rename_key(rn, "ism ‘o’"), lambda md: md.rename(rn, "ism ‘o’"), targets=(rn,))
    h.fails("edits.error.key_exists", lambda s: s.rename_key(ref(m(), 3, "tags"), "id"))
    h.run(lambda s: s.rename_key(ref(m(), 3, "tags"), "tags"), lambda md: None)       # same key: no step
    h.run(lambda s: s.set_value(ref(m(), 3, "id"), "3"), lambda md: None)               # same value: no step
    if member:
        rk = ref(m(), 4)
        h.run(lambda s: s.rename_key(rk, "yangi kalit"), lambda md: md.rename(rk, "yangi kalit"), targets=(rk,))
        h.fails("edits.error.key_exists", lambda s: s.rename_key(ref(m(), 4), m().records[0][0]))
    elif not single:
        h.fails("edits.error.not_member", lambda s: s.rename_key(ref(m(), 4), "x"))
    h.fails("edits.error.not_member", lambda s: s.rename_key(ref(m(), 1, "tags", 0), "x"))
    # -- inserting ---------------------------------------------------------------------------------
    rt = ref(m(), 1, "tags")
    h.run(lambda s: s.insert(rt, 0, '"birinchi"'), lambda md: md.insert(rt, 0, "birinchi", None))
    h.run(lambda s: s.insert(rt, None, '{"o": null}'), lambda md: md.insert(rt, None, parse('{"o": null}'), None))
    ro = ref(m(), 1)
    h.run(lambda s: s.insert(ro, 1, "[]", key="bo'sh"), lambda md: md.insert(ro, 1, [], "bo'sh"))
    h.fails("edits.error.key_exists", lambda s: s.insert(ref(m(), 1), 0, "1", key="id"))
    h.fails("edits.error.key_required", lambda s: s.insert(ref(m(), 1), 0, "1"))
    h.fails("edits.error.not_container", lambda s: s.insert(ref(m(), 1, "id"), 0, "1"))
    h.fails("edits.error.bad_index", lambda s: s.insert(ref(m(), 1, "tags"), 99, "1"))
    h.fails("edits.error.invalid_json", lambda s: s.insert(ref(m(), 1, "tags"), 0, "[1,"))
    rem = ref(m(), 1, "bo'sh")
    h.run(lambda s: s.insert(rem, 0, '{"a": 1}'), lambda md: md.insert(rem, 0, parse('{"a": 1}'), None))
    h.run(lambda s: s.paste(ro, '{"p": [true]}'), lambda md: md.paste(ro, None, parse('{"p": [true]}'), None))
    h.run(lambda s: s.paste(ro, "2", index=0), lambda md: md.paste(ro, 0, N("2"), None))
    h.fails("edits.error.key_exists", lambda s: s.paste(ref(m(), 1), "2", key="pasted"))
    if single:
        h.fails("edits.error.single_root", lambda s: s.insert(None, 0, "1"))
        h.fails("edits.error.single_root", lambda s: s.paste(None, "[1, 2]", as_records=True))
    elif member:
        h.fails("edits.error.key_required", lambda s: s.insert(None, 0, "{}"))
        h.fails("edits.error.key_exists", lambda s: s.insert(None, 0, "{}", key=m().records[2][0]))
        h.run(lambda s: s.insert(None, 1, '{"y": 1}', key="q"),
              lambda md: md.insert_records(1, [(parse('{"y": 1}'), "q")]))
        h.run(lambda s: s.paste(None, '{"z": 2}'), lambda md: md.insert_records(None, [(parse('{"z": 2}'), None)]))
        h.run(lambda s: s.paste(None, '{"m1": 1, "m2": [2]}', index=0, as_records=True),
              lambda md: md.insert_records(0, [(N("1"), "m1"), ([N("2")], "m2")]))
        h.run(lambda s: s.paste(None, '[1, 2]', index=2, as_records=True),
              lambda md: md.insert_records(2, [(N("1"), None), (N("2"), None)]))
    else:
        h.run(lambda s: s.insert(None, 0, '{"y": 1}'), lambda md: md.insert_records(0, [(parse('{"y": 1}'), None)]))
        h.run(lambda s: s.paste(None, '[1, {"q": 2}]', index=3, as_records=True),
              lambda md: md.insert_records(3, [(N("1"), None), (parse('{"q": 2}'), None)]))
        h.run(lambda s: s.paste(None, '"x"'), lambda md: md.insert_records(None, [("x", None)]))
    # -- deleting / duplicating (records are found by their anchors from here on) ----------------------
    d1 = [ref(m(), P("bo'sh"), "tags", 0), ref(m(), P("x"), "x", 1), ref(m(), P("x"), "x", 3)]
    h.run(lambda s: s.delete(d1), lambda md: md.delete(d1))
    d2 = [ref(m(), P("deep"), "num"), ref(m(), P("ism ‘o’"), "num"), ref(m(), P("ism ‘o’"))]
    h.run(lambda s: s.delete(d2), lambda md: md.delete(d2))            # the whole D covers its "num"
    if single:
        h.fails("edits.error.single_root", lambda s: s.delete([Ref(0, ())]))
        h.fails("edits.error.single_root", lambda s: s.duplicate([Ref(0, ())]))
    dup = [ref(m(), P("bo'sh")), ref(m(), P("bo'sh"), "tags"), ref(m(), P("x"), "x", 0), ref(m(), P("x"), "x", 1),
           ref(m(), P("x"), "id")]
    h.run(lambda s: s.duplicate(dup), lambda md: md.duplicate(dup))
    # -- moves ------------------------------------------------------------------------------------
    rx = ref(m(), P("x"), "x")
    mv = [ref(m(), P("x"), "x", 3), ref(m(), P("x"), "x", 0)]
    h.run(lambda s: s.move(mv, rx, 2), lambda md: md.move(mv, rx, 2))                       # reorder inside
    one = [ref(m(), P("x"), "x", 1)]
    h.run(lambda s: s.move(one, rx, None), lambda md: md.move(one, rx, None))               # one source: move_child
    first = ref(m(), P("x"), "x", 0)
    h.run(lambda s: s.move_by([first], 1), lambda md: md.move([first], ref(md, pos(md, "x"), "x"), 2))
    h.run(lambda s: s.move_by([ref(m(), P("x"), "x", 0)], -1), lambda md: None)             # top: nothing moves
    tgt = ref(m(), P("bo'sh"))
    srcs = [ref(m(), P("x"), "x", 0), ref(m(), P("bo'sh"), "tags"), ref(m(), P("x"), "id")]
    h.run(lambda s: s.move(srcs, tgt, 0), lambda md: md.move(srcs, tgt, 0))                 # own + other records
    h.fails("edits.error.move_into_self", lambda s: s.move([ref(m(), P("bo'sh"))], ref(m(), P("bo'sh"), "name"), 0))
    h.fails("edits.error.not_container", lambda s: s.move([ref(m(), P("x"), "x", 0)], ref(m(), P("bo'sh"), "name"), 0))
    arr = ref(m(), P("x"), "x")
    elems = [ref(m(), P("bo'sh"), "name")]
    h.run(lambda s: s.move(elems, arr, 0), lambda md: md.move(elems, arr, 0))               # member -> array
    obj = ref(m(), P("bo'sh"))
    els = [ref(m(), P("x"), "x", 0)]
    h.run(lambda s: s.move(els, obj, None), lambda md: md.move(els, obj, None))            # element -> object
    if not single:
        whole = [ref(m(), others(m(), *anchors)[-1])]
        into = ref(m(), P("bo'sh"))
        h.run(lambda s: s.move(whole, into, 1), lambda md: md.move(whole, into, 1))          # record -> node
        recs = [ref(m(), others(m(), *anchors)[0]), ref(m(), P("deep"))]
        n = len(m().records)
        h.run(lambda s: s.move(recs, None, n), lambda md: md.move(recs, None, n))
        h.run(lambda s: s.move_by([ref(m(), 1)], -1), lambda md: md.move([ref(md, 1)], None, 0))
        nd = [ref(m(), P("bo'sh"), "nested"), ref(m(), others(m(), *anchors)[0])]
        h.run(lambda s: s.move(nd, None, 1), lambda md: md.move(nd, None, 1))                # node -> record level
    else:
        h.fails("edits.error.single_root", lambda s: s.move([ref(m(), 1, "id")], None, 0))
        nd = [ref(m(), P("bo'sh"), "nested"), ref(m(), others(m(), *anchors)[0])]
        root = Ref(0, (), ())
        h.run(lambda s: s.move(nd, root, 1), lambda md: md.move(nd, root, 1))               # into the root array
    h.fails("edits.error.not_siblings", lambda s: s.move_by([ref(m(), P("x"), "x"), ref(m(), P("bo'sh"), "id")], 1))
    # -- types ------------------------------------------------------------------------------------
    ct = [ref(m(), P("bo'sh"), "id"), ref(m(), P("x"), "x", 0)]
    h.run(lambda s: s.change_type(ct, "string"), lambda md: md.change_type(ct, "string"), targets=tuple(ct))
    ct2 = [ref(m(), P("bo'sh"), "id")]
    h.run(lambda s: s.change_type(ct2, "array"), lambda md: md.change_type(ct2, "array"), targets=tuple(ct2))
    h.run(lambda s: s.change_type(ct2, "array"), lambda md: None)                         # already an array
    h.fails("edits.error.convert", lambda s: s.change_type([ref(m(), P("bo'sh"), "id")], "number"))
    # -- record level, any size -----------------------------------------------------------------------
    if not single:
        n = len(m().records)
        h.run(lambda s: s.duplicate_records(RecordSet.from_indices([n - 1, 0], gen=s.state.gen)),
              lambda md: md.duplicate([ref(md, 0), ref(md, n - 1)]))
        h.run(lambda s: s.delete_records([2, 0, 2]), lambda md: md.delete([ref(md, 0), ref(md, 2)]))
        h.fails("edits.error.stale_scope",
                lambda s: s.delete_records(RecordSet.from_indices([0], gen=s.state.gen + 999)))
        h.fails("edits.error.bad_index", lambda s: s.delete_records([10 ** 6]))
    else:
        h.fails("edits.error.single_root", lambda s: s.delete_records([0]))
        h.fails("edits.error.single_root", lambda s: s.duplicate_records([0]))
    # -- wrapper -------------------------------------------------------------------------------------
    if wrapped:
        meta = ref(m(), None, "meta")
        h.run(lambda s: s.set_value(Ref(None, ("meta",)), '{"v2": [1, 2]}'),
              lambda md: md.set_value(ref(md, None, "meta"), parse('{"v2": [1, 2]}')), targets=(meta,))
        h.run(lambda s: s.insert(Ref(None, ()), 0, '"oldin"', key="boshi"),
              lambda md: md.insert(Ref(None, (), ()), 0, "oldin", "boshi"))
        tail = ref(m(), None, "tail")
        h.run(lambda s: s.rename_key(Ref(None, ("tail",)), "dum"), lambda md: md.rename(ref(md, None, "tail"), "dum"),
              targets=(tail,))
        wsrc = [ref(m(), None, "meta")]
        wtgt = ref(m(), P("x"))
        h.run(lambda s: s.move(wsrc, wtgt, 0), lambda md: md.move(wsrc, wtgt, 0))            # wrapper -> record
        back = [ref(m(), P("x"), "meta")]
        h.run(lambda s: s.move(back, Ref(None, (), ()), None), lambda md: md.move(back, Ref(None, (), ()), None))
        h.run(lambda s: s.duplicate([ref(m(), None, "dum")]), lambda md: md.duplicate([ref(md, None, "dum")]))
        h.run(lambda s: s.delete([ref(m(), None, "boshi")]), lambda md: md.delete([ref(md, None, "boshi")]))
        dum = ref(m(), None, "dum")
        h.run(lambda s: s.change_type([dum], "string"), lambda md: md.change_type([dum], "string"), targets=(dum,))
    h.finish()


@pytest.mark.parametrize("style", ["compact", "pretty2"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_command_on_every_kind(tmp_path, closing, backend, kind, style) -> None:
    doc = open_doc(tmp_path / "src.json", kind, style=style, seed=KINDS.index(kind) + (7 if style == "compact" else 13))
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    assert s.style == editops.detect_style(doc)
    scenario(Harness(doc, s))


