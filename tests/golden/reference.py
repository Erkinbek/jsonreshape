"""Line-by-line Python transliteration of the json-tuzilma.html engine (test oracle).

A copy of the stage-4 analysis' verified transliteration (``engine_sketch.py`` +
``engine_sketch_ops.py``: 0 mismatches against the real engine on 82 targeted and 4 000 random
cases): inference (``newAcc``/``inferInto``/``fromAcc``/``setOpen``), compiled paths and bindings,
the evaluation (``resolve``/``ev``/``evArray``/``evElem``/``buildObj``/``preorder``/``transform``) and
every edit operation, on trees in the engine's snapshot format (dicts with camelCase flags,
``WILD = "\\u0002"``, ``ctx`` as the ``SEP``-joined compiled path). It follows the JavaScript
semantics literally (``js_keys`` order in inference, 100 undo steps, no redo) and is used by the
tests as an independent oracle for ``jsonreshape.mapping`` (differential tests) and to evaluate
mapping trees with the reference semantics (the golden outputs).
"""

from __future__ import annotations

import json
import re
from typing import Any

SEP, WILD = "\u0001", "\u0002"
MISSING = object()  # JS Symbol('missing'); never None (= JSON null)
TYPE_ORDER = ["object", "array", "string", "number", "boolean", "null"]
_INDEX_RE = re.compile(r"0|[1-9][0-9]*")
JS_WS = "".join(map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                         0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF)))


def is_array_index(k: str) -> bool:
    """JS 'array index' property key: canonical ASCII decimal integer 0 .. 2**32-2."""
    return _INDEX_RE.fullmatch(k) is not None and int(k) <= 4294967294


def js_keys(d: dict) -> list:
    """Object.keys order: array-index keys ascending, then the other keys in insertion order."""
    idx = sorted((k for k in d if is_array_index(k)), key=int)
    return idx + [k for k in d if not is_array_index(k)]


def js_order(v: Any) -> Any:
    """Re-order every dict the way a JS object holds it (iterative)."""
    if not isinstance(v, (dict, list)):
        return v
    root: list[Any] = [None]
    stack: list[tuple[Any, Any, Any]] = [(v, root, 0)]
    while stack:
        x, parent, slot = stack.pop()
        if isinstance(x, dict):
            new: Any = {}
            parent[slot] = new
            for k in js_keys(x):
                new[k] = None
                stack.append((x[k], new, k))
        elif isinstance(x, list):
            new = [None] * len(x)
            parent[slot] = new
            for i, el in enumerate(x):
                stack.append((el, new, i))
        else:
            parent[slot] = x
    return root[0]


def canon(v: Any) -> Any:
    """Order-preserving, JS-number-aware comparison form (objects in JS key order)."""
    if isinstance(v, dict):
        return ("{}", [(k, canon(v[k])) for k in js_keys(v)])
    if isinstance(v, list):
        return ("[]", [canon(x) for x in v])
    if v is None or isinstance(v, (bool, str)):
        return v
    return ("num", float(v))


def type_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    raise TypeError(type(v))


# ------------------------------------------------------------------ inference
class Acc:
    __slots__ = ("count", "obj_count", "types", "props", "elem", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.obj_count = 0
        self.types: set[str] = set()
        self.props: dict[str, Acc] = {}
        self.elem: Acc | None = None
        self.samples: list[str] = []


def js_sample(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    return json.dumps(v, ensure_ascii=False)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def infer_into(acc: Acc, v: Any) -> None:
    stack = [(acc, v)]
    while stack:
        a, x = stack.pop()
        a.count += 1
        t = type_of(x)
        a.types.add(t)
        if t == "object":
            a.obj_count += 1
            todo = []
            for k in js_keys(x):
                c = a.props.get(k)
                if c is None:
                    c = a.props[k] = Acc()
                todo.append((c, x[k]))
            stack.extend(reversed(todo))
        elif t == "array":
            if a.elem is None:
                a.elem = Acc()
            stack.extend((a.elem, el) for el in reversed(x))
        elif x is not None and len(a.samples) < 3:
            s = js_sample(x)
            if utf16_len(s) > 28:
                s = s.encode("utf-16-le", "surrogatepass")[:54].decode("utf-16-le", "surrogatepass") + "…"
            if s not in a.samples:
                a.samples.append(s)


class Ids:
    def __init__(self, start: int = 0) -> None:
        self.uid = start

    def next(self) -> int:
        self.uid += 1
        return self.uid


def mk(ids: Ids, **o: Any) -> dict:
    n = {"id": ids.next(), "key": "", "kind": "value", "src": [], "children": [], "types": [], "count": 0,
         "pcount": None, "samples": [], "synthetic": False, "recKey": None, "computed": None, "ctx": None,
         "open": False, "isElem": False, "isRoot": False}
    n.update(o)
    return n


def from_acc(ids: Ids, acc: Acc, key: str, src: list, pcount: Any, **flags: Any) -> dict:
    """Iterative fromAcc (ids in pre-order like the recursion)."""
    root: dict | None = None
    stack: list[tuple[Any, ...]] = [(acc, key, src, pcount, flags, None)]
    while stack:
        a, k, s, pc, fl, out = stack.pop()
        kind = "array" if "array" in a.types else "object" if "object" in a.types else "value"
        n = mk(ids, key=k, kind=kind, src=s, pcount=pc, count=a.count,
               types=[t for t in TYPE_ORDER if t in a.types], samples=list(a.samples), **fl)
        if out is None:
            root = n
        else:
            out.append(n)
        if kind == "object":
            for ck, ca in reversed(list(a.props.items())):
                stack.append((ca, ck, s + [ck], a.obj_count, {}, n["children"]))
        if kind == "array":
            stack.append((a.elem or Acc(), "", s + [WILD], None, {"isElem": True}, n["children"]))
    assert root is not None
    return root


def walk(n: dict) -> list[tuple[dict, dict | None, int]]:
    out = []
    stack: list[tuple[dict, dict | None, int]] = [(n, None, 0)]
    while stack:
        x, p, d = stack.pop()
        out.append((x, p, d))
        stack.extend((c, x, d + 1) for c in reversed(x["children"]))
    return out


def set_open(tree: dict) -> None:
    small = len(walk(tree)) < 160
    for n, _p, depth in walk(tree):
        n["open"] = depth <= 1 or (n["isElem"] and depth <= 3) or (small and depth < 6)


def build_schema(data: Any, ids: Ids | None = None) -> dict:
    acc = Acc()
    infer_into(acc, data)
    tree = from_acc(ids or Ids(), acc, "", [], None, isRoot=True)
    set_open(tree)
    return tree


# ------------------------------------------------------------------ paths & bindings & evaluation
def compile_path(path: list) -> tuple[list, str]:
    keys, acc = [], ""
    for i, seg in enumerate(path):
        acc = acc + SEP + seg if i else seg
        keys.append(acc if seg == WILD else None)
    return keys, acc


def look(b: Any, k: str) -> Any:
    while b is not None:
        if b[0] == k:
            return b
        b = b[2]
    return None


def preorder(arr: Any, k: str):
    stack = [(iter(arr), 1)]
    while stack:
        it, d = stack[-1]
        for el in it:
            yield el, d
            sub = el.get(k) if isinstance(el, dict) else None
            if isinstance(sub, list):
                stack.append((iter(sub), d + 1))
                break
        else:
            stack.pop()


class Engine:
    def __init__(self, tree: dict, root: Any, limit: Any = None) -> None:
        self.tree, self.root = tree, root
        self.limit = float("inf") if not limit else limit
        self.cp: dict[int, tuple] = {}
        self.pcp: dict[int, tuple] = {}
        for n, _p, _d in walk(tree):
            if not n.get("synthetic") and not n.get("computed"):
                segs = n["src"]
                k, full = compile_path(segs)
                self.cp[id(n)] = (segs, k, full)
                if n.get("isElem"):
                    p = segs[:-1]
                    k2, full2 = compile_path(p)
                    self.pcp[id(n)] = (p, k2, full2)

    def resolve(self, cp: tuple, b: Any) -> tuple[list, bool]:
        segs, keys, _ = cp
        start, val = 0, self.root
        for i in range(len(segs) - 1, -1, -1):
            if keys[i] is not None:
                h = look(b, keys[i])
                if h is not None:
                    start, val = i + 1, h[1]
                    break
        br, gathered = [(val, b)], False
        i = start
        while i < len(segs) and br:
            s, nx = segs[i], []
            if s == WILD:
                gathered = True
                for v, xb in br:
                    if isinstance(v, list):
                        nx.extend((el, (keys[i], el, xb)) for el in v)
            else:
                for v, xb in br:
                    if isinstance(v, dict) and s in v:
                        nx.append((v[s], xb))
            br = nx
            i += 1
        return br, gathered

    def build_obj(self, n: dict, b: Any) -> dict:
        o = {}
        for c in n["children"]:
            r = self.ev(c, b)
            if r is not MISSING:
                o[c["key"]] = r
        return o

    def ev_elem(self, E: dict, b: Any, el: Any) -> Any:
        if E["kind"] == "object":
            return self.build_obj(E, b) if isinstance(el, dict) else el
        if E["kind"] == "array":
            return self.ev_array(E, b) if isinstance(el, list) else el
        return el

    def ev_array(self, n: dict, b: Any) -> Any:
        out, saw, fb = [], False, MISSING
        for E in n["children"]:
            if not E.get("isElem"):
                continue
            br, _ = self.resolve(self.pcp[id(E)], b)
            ek = self.cp[id(E)][2]
            for v, xb in br:
                if not isinstance(v, list):
                    if fb is MISSING:
                        fb = v
                    continue
                saw = True
                items = preorder(v, E["recKey"]) if E.get("recKey") else ((el, 1) for el in v)
                for el, d in items:
                    if len(out) >= self.limit:
                        return out
                    r = self.ev_elem(E, (ek + "#d", d, (ek, el, xb)), el)
                    if r is not MISSING:
                        out.append(r)
        return out if saw else fb

    def ev(self, n: dict, b: Any) -> Any:
        if n.get("computed") == "depth":
            h = look(b, n["ctx"] + "#d")
            return h[1] if h is not None else MISSING
        if n.get("synthetic"):
            return self.build_obj(n, b)
        if n["kind"] == "array":
            return self.ev_array(n, b)
        br, gathered = self.resolve(self.cp[id(n)], b)

        def one(v: Any, xb: Any) -> Any:
            return self.build_obj(n, xb) if n["kind"] == "object" and isinstance(v, dict) else v

        if gathered:
            out = []
            for v, xb in br:
                if len(out) >= self.limit:
                    break
                out.append(one(v, xb))
            return out
        return one(*br[0]) if br else MISSING


def transform(tree: dict, root: Any, limit: Any = None) -> Any:
    r = Engine(tree, root, limit).ev(tree, None)
    return None if r is MISSING else js_order(r)


def preview_values(tree: dict, data: Any) -> list:
    """The prototype's live preview of every record (limit 21, 1-element unwrap)."""
    vals = []
    is_arr = isinstance(data, list)
    for i in range(len(data) if is_arr else 1):
        val = transform(tree, data[i:i + 1] if is_arr else data, 21)
        if is_arr and isinstance(val, list) and len(val) == 1:
            val = val[0]
        vals.append(val)
    return vals


# ------------------------------------------------------------------ edit operations
def js_trim(s: str) -> str:
    return s.strip(JS_WS)


class Designer:
    """The engine globals TREE / ORIG / UNDO / uid / DATA and every edit operation."""

    def __init__(self, data: Any, undo_cap: int = 100) -> None:
        self.data = data
        self.ids = Ids(0)
        self.tree = build_schema(data, self.ids)
        self.orig = json.dumps(self.tree, ensure_ascii=False)
        self.undo_stack: list[str] = []
        self.undo_cap = undo_cap
        self.msgs: list[str] = []
        self.parent: dict[int, dict] = {}
        self.compile_all()

    def snapshot(self) -> str:
        return json.dumps(self.tree, ensure_ascii=False)

    def compile_all(self) -> None:
        self.parent = {}
        for n, p, _d in walk(self.tree):
            if p is not None:
                self.parent[id(n)] = p

    def on_change(self, msg: str) -> None:
        self.compile_all()
        self.msgs.append(msg)

    def push_undo(self) -> None:
        self.undo_stack.append(self.snapshot())
        if len(self.undo_stack) > self.undo_cap:
            self.undo_stack.pop(0)

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.tree = json.loads(self.undo_stack.pop())
        self.compile_all()
        self.on_change("Oxirgi o'zgarish bekor qilindi")

    def reset(self) -> None:
        self.push_undo()
        self.tree = json.loads(self.orig)
        self.compile_all()
        self.on_change("Asl tuzilma tiklandi")

    def is_within(self, t: dict, n: dict) -> bool:
        x: dict | None = t
        while x is not None:
            if x is n:
                return True
            x = self.parent.get(id(x))
        return False

    @staticmethod
    def unique_key(p: dict, key: str, self_node: dict | None) -> str:
        used = {c["key"] for c in p["children"] if c is not self_node and not c["isElem"]}
        if key not in used:
            return key
        i = 2
        while f"{key}_{i}" in used:
            i += 1
        return f"{key}_{i}"

    def detach(self, n: dict) -> dict:
        p = self.parent[id(n)]
        for i, c in enumerate(p["children"]):
            if c is n:
                del p["children"][i]
                break
        return p

    @staticmethod
    def can_inside(t: dict, n: dict) -> bool:
        return n["kind"] == "array" if t["kind"] == "array" else t["kind"] == "object"

    @staticmethod
    def can_sibling(t: dict) -> bool:
        return not t["isRoot"] and not t["isElem"]

    def zone_for(self, t: dict, n: dict, frac: float) -> str | None:
        if n is None or t is None or self.is_within(t, n):
            return None
        ins, sib = self.can_inside(t, n), self.can_sibling(t)
        if ins and sib:
            return "before" if frac < 0.28 else "after" if frac > 0.72 else "inside"
        if ins:
            return "inside"
        if sib:
            return "before" if frac < 0.5 else "after"
        return None

    def move_node(self, n: dict, t: dict, zone: str) -> None:
        self.compile_all()
        if n is None or t is None or self.is_within(t, n) or n["isRoot"] or n["isElem"]:
            return
        msg = ""
        if zone == "inside" and t["kind"] == "array":
            if n["kind"] != "array":
                return
            self.push_undo()
            self.detach(n)
            t["children"].extend(n["children"])
            t["open"] = True
            msg = f"«{n['key']}» elementlari «{'ildiz' if t['isRoot'] else t['key']}» massiviga qo'shildi"
        elif zone == "inside":
            if t["kind"] != "object":
                return
            self.push_undo()
            self.detach(n)
            k = self.unique_key(t, n["key"], n)
            if k != n["key"]:
                msg = f"«{n['key']}» nomi band edi, «{k}» deb qo'yildi"
            n["key"] = k
            t["children"].append(n)
            t["open"] = True
        else:
            if not self.can_sibling(t):
                return
            self.push_undo()
            p = self.parent[id(t)]
            self.detach(n)
            i = next(j for j, c in enumerate(p["children"]) if c is t) + (1 if zone == "after" else 0)
            k = self.unique_key(p, n["key"], n)
            if k != n["key"]:
                msg = f"«{n['key']}» nomi band edi, «{k}» deb qo'yildi"
            n["key"] = k
            p["children"].insert(i, n)
        self.on_change(msg)

    def delete_node(self, n: dict) -> None:
        self.compile_all()
        if n is None or n["isRoot"]:
            return
        p = self.parent[id(n)]
        if n["isElem"] and len(p["children"]) <= 1:
            return
        self.push_undo()
        self.detach(n)
        self.on_change(f"«{'element' if n['isElem'] else n['key']}» natijadan olib tashlandi")

    def unwrap_node(self, o: dict) -> None:
        self.compile_all()
        if o is None or o["kind"] != "object" or o["isRoot"] or o["isElem"]:
            return
        self.push_undo()
        p = self.parent[id(o)]
        i = next(j for j, c in enumerate(p["children"]) if c is o)
        self.detach(o)
        renamed = 0
        for c in o["children"]:
            k = self.unique_key(p, c["key"], c)
            if k != c["key"]:
                renamed += 1
            c["key"] = k
            p["children"].insert(i, c)
            i += 1
        self.on_change(f"«{o['key']}» ichidagi {len(o['children'])} ta maydon bir pog'ona yuqoriga chiqdi"
                       + (f", {renamed} tasining nomiga raqam qo'shildi" if renamed else ""))

    def add_group(self, t: dict) -> dict | None:
        self.compile_all()
        if t is None or t["kind"] != "object":
            return None
        self.push_undo()
        g = mk(self.ids, key=self.unique_key(t, "yangi_guruh", None), kind="object", synthetic=True, src=None,
               types=["object"], open=True)
        t["children"].append(g)
        t["open"] = True
        self.on_change("")
        return g

    def rename_node(self, n: dict, name: Any) -> None:
        self.compile_all()
        name = js_trim(str(name))
        if n is None or n["isRoot"] or n["isElem"] or not name or name == n["key"]:
            return
        self.push_undo()
        k = self.unique_key(self.parent[id(n)], name, n)
        n["key"] = k
        self.on_change(f"«{name}» band edi, «{k}» deb nomlandi" if k != name else "")

    @staticmethod
    def is_under(src: Any, base: list) -> bool:
        return src is not None and len(src) > len(base) and all(src[i] == s for i, s in enumerate(base))

    @staticmethod
    def find_rec(A: dict, E: dict) -> dict | None:
        if not E["isElem"] or E["kind"] != "object" or E["recKey"]:
            return None
        cands = [C for C in E["children"]
                 if C["kind"] == "array" and not C["synthetic"] and len(C["src"]) == len(E["src"]) + 1
                 and C["src"][:-1] == E["src"] and C["children"] and C["children"][0]["kind"] == "object"]
        if not cands:
            return None
        want = A["src"][-1] if A["src"] else object()
        return next((C for C in cands if C["src"][-1] == want), cands[0])

    def can_flatten(self, A: dict) -> bool:
        return A["kind"] == "array" and any(self.find_rec(A, E) for E in A["children"])

    def flatten_rec(self, A: dict) -> None:
        self.compile_all()
        if not self.can_flatten(A):
            return
        self.push_undo()
        total, key_name = 0, ""
        for E in A["children"]:
            C = self.find_rec(A, E)
            if not C:
                continue
            k = C["src"][-1]
            key_name = k
            eng = Engine(self.tree, self.data)
            segs = E["src"]
            keys, full = compile_path(segs)
            br, _ = eng.resolve((segs, keys, full), None)
            items = list(preorder([v for v, _ in br], k))
            acc = Acc()
            for el, _ in items:
                infer_into(acc, el)
            foreign = [c for c in E["children"]
                       if c["synthetic"] or c["computed"] or not self.is_under(c["src"], E["src"])]
            kids = [mk(self.ids, key="_depth", kind="value", computed="depth", ctx=full, types=["number"], src=None)]
            for key, c in acc.props.items():
                if key != k:
                    kids.append(from_acc(self.ids, c, key, E["src"] + [key], acc.obj_count))
            E["children"] = kids + foreign
            for c in E["children"]:
                if c["computed"]:
                    c["key"] = self.unique_key(E, c["key"], c)
            E["recKey"] = k
            E["count"] = acc.count
            E["open"] = True
            total += len(items)
        A["open"] = True
        self.on_change(f"«{key_name}» ichidagi barcha darajalar bitta massivga yig'ildi: {total} ta element")

    # -- harness REF and ops
    def resolve_ref(self, ref: Any) -> dict:
        n = self.tree
        steps = parse_ref(ref) if isinstance(ref, str) else ref
        for s in steps:
            n = n["children"][s] if isinstance(s, int) else next(c for c in n["children"]
                                                                   if not c["isElem"] and c["key"] == s)
        return n

    def ref_of(self, n: dict) -> list:
        steps: list[Any] = []
        x = n
        p = self.parent.get(id(x))
        while p is not None:
            i = next(j for j, c in enumerate(p["children"]) if c is x)
            first = next((c for c in p["children"] if not c["isElem"] and c["key"] == x["key"]), None)
            steps.insert(0, x["key"] if (not x["isElem"] and first is x) else i)
            x = p
            p = self.parent.get(id(x))
        return steps

    def apply(self, op: dict) -> dict:
        """Apply one harness op; returns the log entry (applied / changed / undo / msg / result)."""
        before_snap, before_msgs = self.snapshot(), len(self.msgs)
        entry: dict[str, Any] = {"op": op["op"]}
        o = op["op"]
        result: Any = MISSING

        def N() -> dict:
            return self.resolve_ref(op["node"])

        def T() -> dict:
            return self.resolve_ref(op["target"])

        if o == "drop":
            n, t = N(), T()
            self.compile_all()
            z = self.zone_for(t, n, op["frac"])
            result = {"zone": z}
            if not n["isRoot"] and not n["isElem"] and z:
                self.move_node(n, t, z)
        elif o == "zone":
            n, t = N(), T()
            self.compile_all()
            result = self.zone_for(t, n, op["frac"])
        elif o == "move":
            self.move_node(N(), T(), op["zone"])
        elif o == "merge":
            self.move_node(N(), T(), "inside")
        elif o == "rename":
            self.rename_node(N(), op["name"])
        elif o == "delete":
            self.delete_node(N())
        elif o == "group":
            g = self.add_group(N())
            if g is not None and "name" in op:
                self.rename_node(g, op["name"])
            if g is not None:
                self.compile_all()
                result = {"ref": self.ref_of(g), "key": g["key"]}
        elif o == "unwrap":
            self.unwrap_node(N())
        elif o == "flatten":
            self.flatten_rec(N())
        elif o == "undo":
            self.undo()
        elif o == "reset":
            self.reset()
        elif o == "canFlatten":
            self.compile_all()
            result = self.can_flatten(N())
        elif o == "output":
            result = transform(self.tree, self.data)
        else:
            raise ValueError(o)
        applied = len(self.msgs) > before_msgs
        entry.update(applied=applied, changed=self.snapshot() != before_snap, undo=len(self.undo_stack),
                     msg=self.msgs[-1] if applied else None)
        if result is not MISSING:
            entry["result"] = result
        return entry


_REF_PART = re.compile(r"([^\[\]]*)((?:\[[0-9]*\])*)")


def parse_ref(s: str) -> list:
    steps: list[Any] = []
    if s == "":
        return steps
    for part in s.split("."):
        m = _REF_PART.fullmatch(part)
        if m is None:
            raise ValueError(f"bad REF segment {part!r}")
        if m.group(1):
            steps.append(m.group(1))
        idx = re.findall(r"\[([0-9]*)\]", m.group(2))
        steps.extend(int(d) if d else 0 for d in idx)
    return steps


def badges_of(tree: dict) -> list[dict]:
    """The harness ``allBadges`` (rowHTML badge logic) of a snapshot tree."""
    out = []
    stack: list[tuple[dict, dict | None, list]] = [(tree, None, [])]
    refs = _refs(tree)
    while stack:
        n, p, ctx = stack.pop()
        b = []
        if n["synthetic"]:
            b.append("yangi guruh")
        if n["computed"]:
            b.append("daraja raqami")
        if n["isElem"] and n["recKey"]:
            b.append("rekursiv · " + n["recKey"])
        if n["isElem"] and p is not None and not p["isRoot"] and SEP.join(n["src"][:-1]) != SEP.join(p["src"] or []):
            b.append("manba: " + path_label(n["src"][:-1]))
        if not n["synthetic"] and not n["computed"] and not n["isElem"] and not n["isRoot"]:
            if _is_moved(n, p):
                b.append("ko'chirildi")
            orig = n["src"][-1]
            if n["key"] != orig:
                b.append("asl nomi: " + orig)
            gathered, broadcast = _relation(n, ctx)
            if gathered:
                b.append("ro'yxatga yig'iladi")
            elif broadcast:
                b.append("har elementga takrorlanadi")
        if b:
            out.append({"ref": refs[id(n)], "badges": b})
        ctx2 = ctx + [compile_path(n["src"])[1]] if n["isElem"] else ctx
        stack.extend((c, n, ctx2) for c in reversed(n["children"]))
    return out


def _refs(tree: dict) -> dict[int, list]:
    refs: dict[int, list] = {id(tree): []}
    for n, _p, _d in walk(tree):
        for i, c in enumerate(n["children"]):
            first = next((x for x in n["children"] if not x["isElem"] and x["key"] == c["key"]), None)
            refs[id(c)] = refs[id(n)] + [c["key"] if (not c["isElem"] and first is c) else i]
    return refs


def path_label(src: list) -> str:
    s = ""
    for seg in src:
        s += "[]" if seg == WILD else ("." if s else "") + seg
    return s or "(ildiz)"


def _is_moved(n: dict, p: dict | None) -> bool:
    if n["synthetic"] or n["computed"] or n["isElem"] or n["isRoot"] or p is None:
        return False
    if p["synthetic"]:
        return True
    return SEP.join(n["src"][:-1]) != SEP.join(p["src"] or [])


def _relation(n: dict, ctx: list) -> tuple[bool, bool]:
    keys, _ = compile_path(n["src"])
    wk = [k for k in keys if k is not None]
    last = -1
    for i, k in enumerate(wk):
        if k in ctx:
            last = i
    near = ctx[-1] if ctx else None
    return len(wk) - 1 > last, bool(near) and near not in wk


# ------------------------------------------------------------------ conversions
def from_mapping_tree(tree: Any) -> dict:
    """A ``jsonreshape.mapping`` tree (MappingTree or MapNode) in the engine snapshot format."""
    root = getattr(tree, "root", tree)
    out: dict | None = None
    stack: list[tuple[Any, list | None]] = [(root, None)]
    while stack:
        n, dest = stack.pop()
        d = {"id": n.id, "key": n.key, "kind": n.kind,
             "src": None if n.src is None else [WILD if s is None else s for s in n.src], "children": [],
             "types": list(n.types), "count": n.count, "pcount": n.pcount, "samples": list(n.samples),
             "synthetic": n.synthetic, "recKey": n.rec_key, "computed": n.computed,
             "ctx": None if n.ctx is None else compile_path([WILD if s is None else s for s in n.ctx])[1],
             "open": n.open, "isElem": n.is_elem, "isRoot": n.is_root}
        if dest is None:
            out = d
        else:
            dest.append(d)
        stack.extend((c, d["children"]) for c in reversed(n.children))
    assert out is not None
    return out
