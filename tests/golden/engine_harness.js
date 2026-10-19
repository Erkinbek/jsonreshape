#!/usr/bin/env node
/*
 * engine_harness.js - golden-output generator for the jsonreshape mapping engine (stage 4).
 *
 * Loads the REFERENCE engine of json-tuzilma.html (the JavaScript between the markers
 * "ENGINE-START" and "ENGINE-END") byte-for-byte, runs it inside a node:vm sandbox, replays an
 * edit script on the inferred tree (or loads a given tree) and prints what the prototype would
 * export. Nothing of the engine is re-implemented here; the harness only calls its functions
 * the way the page's UI code does.
 *
 * Usage:
 *   node engine_harness.js CASE.json [options]
 *   node engine_harness.js - [options]               (case JSON read from stdin)
 *
 * Options:
 *   --html PATH    json-tuzilma.html to load. Default: $JSON_TUZILMA_HTML, else the first
 *                  json-tuzilma.html found walking up from the current directory, then from this
 *                  script's directory, else "E:/Projects/json flatting/json-tuzilma.html".
 *   --export       print ONLY the export text, byte-identical to the page's "Eksport .json" button:
 *                  JSON.stringify(out, null, 2); with --compact: JSON.stringify(out, null, 0).
 *                  (Batch input: one export text per case, separated by a line "\u001e".)
 *   --compact      compact JSON for whatever is printed (default: 2-space indent)
 *   --no-tree      omit "tree" (the final tree snapshot) from the result object
 *   --per-record   add "perRecord": [transform([r]) for every record]  (array data only)
 *                  and "perRecordConcatEqualsOutput": whether concatenating them gives "output"
 *   --preview      add "preview": the live-preview value of every record, computed exactly like
 *                  renderPreview(): transform(DATA.slice(i, i+1), 21), 1-element array unwrapped
 *   --badges       add "badges": UI badges of every node that has one (rowHTML logic, Uzbek labels)
 *   --shape        add "shape": a readable one-line-per-node outline of the final tree
 *   --out FILE     write UTF-8 (no BOM) to FILE instead of stdout
 *
 * Case JSON (one object; or an array of them / {"cases":[...]} for batch mode):
 *   {
 *     "name":     "optional label, echoed back",
 *     "records":  [ ... ],          the file as an array of records (JSON array file or JSONL)
 *     "data":     <any JSON>,       alternative to "records": any root value (object root, keyed tree)
 *     "text":     "<raw file text>",alternative: parsed with the engine's parseLoose()
 *                                   (BOM strip, "{ {..},{..} }" fix, JSON Lines) -> result "fixed"
 *     "sample":   <any JSON>,       optional: the data the tree is inferred from and that "flatten"
 *                                   reads (default: the data itself, as in the prototype); the final
 *                                   transform always runs on records/data/text
 *     "tree":     { ... },          optional: start from this tree (engine snapshot format, i.e. the
 *                                   "tree" this harness prints) instead of the inferred one
 *     "wildcard": "[]",             optional: inside "tree", src/ctx segments equal to this value
 *                                   (a string, or null) mean the array step (the engine's WILD
 *                                   "\u0002"); "ctx" may also be given as a segment array
 *     "ops":      [ ... ],          optional edit script, applied in order (see below)
 *     "limit":    21                optional LIMIT for the final transform (default: none = export)
 *   }
 *
 * Ops (REF = node reference, see below):
 *   {"op":"move",    "node":REF, "target":REF, "zone":"before"|"inside"|"after"}  moveNode()
 *   {"op":"drop",    "node":REF, "target":REF, "frac":0..1}   zoneFor() then moveNode() = a mouse drop
 *                                                            at that height fraction of the target row
 *   {"op":"zone",    "node":REF, "target":REF, "frac":0..1}   query: zoneFor() result only
 *   {"op":"merge",   "node":REF, "target":REF}               = move with zone "inside" (array onto array)
 *   {"op":"rename",  "node":REF, "name":"new name"}          renameNode()
 *   {"op":"delete",  "node":REF}                             deleteNode()
 *   {"op":"group",   "node":REF, "name":"holat"}             addGroup(); then renameNode(group, name)
 *                                                            when "name" is given (two undo steps, as in the UI)
 *   {"op":"unwrap",  "node":REF}                             unwrapNode()
 *   {"op":"flatten", "node":REF}                             flattenRec()
 *   {"op":"undo"}                                            undo()
 *   {"op":"reset"}                                           resetTree()
 *   {"op":"canFlatten", "node":REF}                          query: canFlatten()
 *   {"op":"output"}                                          query: transform(DATA) at this point
 *
 * REF:
 *   - array of steps from the root: a string = the first non-element child with that key,
 *     an integer = the child at that index (for an array node 0 = its first element node);
 *   - string shorthand: ""  = root, "[]" = root element, "[].contact.address.city",
 *     "[].children[].name", "orders[1]" (second element node of orders); a key containing
 *     "." "[" or "]" needs the array form;
 *   - {"id": 17}  = node id (ids are deterministic: uid restarts at 0 for every case).
 *
 * Result object (default mode):
 *   {"name", "output", "log":[{"i","op","applied","changed","undo","msg","result"}], "tree", ...}
 *   applied = the engine reached onChange() (the op was accepted); changed = the tree snapshot
 *   differs from before the op; undo = UNDO.length afterwards; msg = the onChange toast text.
 *
 * Exit code: 0 ok, 1 a case failed (its result has "error"), 2 usage / engine load error.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* ------------------------------------------------------------------ arguments */
function parseArgs(argv) {
  const o = { input: null, html: null, export: false, compact: false, tree: true, perRecord: false,
    preview: false, badges: false, shape: false, out: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--html') o.html = argv[++i];
    else if (a === '--out') o.out = argv[++i];
    else if (a === '--export') o.export = true;
    else if (a === '--compact') o.compact = true;
    else if (a === '--no-tree') o.tree = false;
    else if (a === '--per-record') o.perRecord = true;
    else if (a === '--preview') o.preview = true;
    else if (a === '--badges') o.badges = true;
    else if (a === '--shape') o.shape = true;
    else if (a === '-h' || a === '--help') { o.help = true; }
    else if (a.startsWith('--')) throw new Error('unknown option ' + a);
    else if (o.input === null) o.input = a;
    else throw new Error('unexpected argument ' + a);
  }
  return o;
}

function findHtml(explicit) {
  if (explicit) return explicit;
  if (process.env.JSON_TUZILMA_HTML) return process.env.JSON_TUZILMA_HTML;
  for (const start of [process.cwd(), __dirname]) {
    let d = path.resolve(start);
    for (;;) {
      const f = path.join(d, 'json-tuzilma.html');
      if (fs.existsSync(f)) return f;
      const up = path.dirname(d);
      if (up === d) break;
      d = up;
    }
  }
  return 'E:/Projects/json flatting/json-tuzilma.html';
}

/* ------------------------------------------------------------------ engine loading */
const EXPORTS = ['SEP', 'WILD', 'MISSING', 'TYPE_ORDER', 'typeOf', 'isObj', 'hasOwn', 'newAcc', 'inferInto',
  'mk', 'fromAcc', 'countNodes', 'setOpen', 'buildSchema', 'compile', 'compileAll', 'pathLabel', 'bind',
  'look', 'resolve', 'setKey', 'buildObj', 'preorder', 'evElem', 'evArray', 'ev', 'transform', 'relation',
  'isMoved', 'snapshot', 'pushUndo', 'undo', 'resetTree', 'isWithin', 'uniqueKey', 'detach', 'canInside',
  'canSibling', 'zoneFor', 'moveNode', 'deleteNode', 'unwrapNode', 'addGroup', 'renameNode', 'isUnder',
  'findRec', 'canFlatten', 'flattenRec', 'parseLoose', 'errorLocation'];
const VARS = ['DATA', 'TREE', 'ORIG', 'ROOT', 'LIMIT', 'BYID', 'PARENT', 'UNDO', 'uid'];
const REQUIRED = ['buildSchema', 'transform', 'compileAll', 'snapshot', 'moveNode', 'zoneFor', 'flattenRec'];

function loadEngine(htmlPath, hooks) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const START = '/*ENGINE-START*/', END = '/*ENGINE-END*/';
  const a = html.indexOf(START), b = html.indexOf(END);
  if (a < 0 || b < 0 || b < a) throw new Error(htmlPath + ': ENGINE-START / ENGINE-END markers not found');
  // pad with newlines so that stack traces show the line numbers of the HTML file
  const firstLine = html.slice(0, a).split('\n').length;
  const code = '\n'.repeat(firstLine - 1) + html.slice(a, b + END.length);
  const footer = '\n;globalThis.__ENGINE__ = {\n' +
    EXPORTS.map(n => `  ${JSON.stringify(n)}: (typeof ${n} === 'undefined' ? undefined : ${n}),`).join('\n') + '\n' +
    VARS.map(v => `  get ${v}(){ return ${v}; }, set ${v}(x){ ${v} = x; },`).join('\n') + '\n' +
    '  JSONparse: s => JSON.parse(s), JSONstringify: (v, r, s) => JSON.stringify(v, r, s)\n};\n';
  const sandbox = { onChange: msg => hooks.onChange(msg), console };
  vm.createContext(sandbox);
  vm.runInContext(code + footer, sandbox, { filename: htmlPath });
  const E = sandbox.__ENGINE__;
  for (const n of REQUIRED) if (typeof E[n] !== 'function') throw new Error(htmlPath + ': engine function ' + n + ' missing');
  return E;
}

/* ------------------------------------------------------------------ helpers */
let E = null;
const hook = { called: false, msg: null };
// The page's onChange(msg) = render() (whose first statement is compileAll()) + toast(msg).
function onChange(msg) { hook.called = true; hook.msg = msg; E.compileAll(); }

function parseRefString(s) {
  const steps = [];
  if (s === '') return steps;
  for (const part of s.split('.')) {
    const m = /^([^[\]]*)((?:\[\d*\])*)$/.exec(part);
    if (!m) throw new Error('bad REF segment ' + JSON.stringify(part) + ' in ' + JSON.stringify(s));
    if (m[1] !== '') steps.push(m[1]);
    const idx = m[2].match(/\[(\d*)\]/g) || [];
    for (const t of idx) steps.push(t.length > 2 ? parseInt(t.slice(1, -1), 10) : 0);
    if (m[1] === '' && !idx.length) throw new Error('empty REF segment in ' + JSON.stringify(s));
  }
  return steps;
}

function resolveRef(ref) {
  E.compileAll();
  if (ref === undefined || ref === null) return E.TREE;
  if (typeof ref === 'object' && !Array.isArray(ref)) {
    const n = E.BYID.get(ref.id);
    if (!n) throw new Error('no node with id ' + JSON.stringify(ref.id));
    return n;
  }
  const steps = Array.isArray(ref) ? ref : parseRefString(String(ref));
  let n = E.TREE;
  for (const s of steps) {
    const next = typeof s === 'number' ? n.children[s] : n.children.find(c => !c.isElem && c.key === s);
    if (!next) throw new Error('REF ' + JSON.stringify(ref) + ': no child ' + JSON.stringify(s) +
      ' under ' + JSON.stringify(refOf(n)));
    n = next;
  }
  return n;
}

// array-form REF of a node: key when it is unique among non-element siblings, else index
function refOf(n) {
  const steps = [];
  for (let x = n, p = E.PARENT.get(x.id); p; x = p, p = E.PARENT.get(x.id)) {
    const i = p.children.indexOf(x);
    const first = p.children.find(c => !c.isElem && c.key === x.key);
    steps.unshift(!x.isElem && first === x ? x.key : i);
  }
  return steps;
}

function label(n) {
  if (n.isRoot) return '(root)';
  if (n.isElem) return '[]';
  return JSON.stringify(n.key);
}

function shapeOf(tree) {
  const lines = [];
  (function walk(n, d) {
    const f = [];
    if (n.synthetic) f.push('synthetic');
    if (n.computed) f.push('computed=' + n.computed + ' ctx=' + E.pathLabel(n.ctx === '' ? [] : String(n.ctx).split(E.SEP)));
    if (n.recKey) f.push('recKey=' + n.recKey);
    const src = n.synthetic || n.computed ? '' : ' <- ' + E.pathLabel(n.src);
    lines.push('  '.repeat(d) + label(n) + ' ' + n.kind + (f.length ? ' [' + f.join(', ') + ']' : '') + src);
    for (const c of n.children) walk(c, d + 1);
  })(tree, 0);
  return lines;
}

function badgesOf(n, p, ctx) {
  const SEP = E.SEP, b = [];
  if (n.synthetic) b.push('yangi guruh');
  if (n.computed) b.push('daraja raqami');
  if (n.isElem && n.recKey) b.push('rekursiv \u00b7 ' + n.recKey);
  if (n.isElem && p && !p.isRoot && n.src.slice(0, -1).join(SEP) !== (p.src || []).join(SEP))
    b.push('manba: ' + E.pathLabel(n.src.slice(0, -1)));
  if (!n.synthetic && !n.computed && !n.isElem && !n.isRoot) {
    if (E.isMoved(n, p)) b.push("ko'chirildi");
    const orig = n.src[n.src.length - 1];
    if (n.key !== orig) b.push('asl nomi: ' + orig);
    const r = E.relation(n, ctx);
    if (r.gathered) b.push("ro'yxatga yig'iladi");
    else if (r.broadcast) b.push('har elementga takrorlanadi');
  }
  return b;
}

function allBadges() {
  E.compileAll();
  const out = [];
  (function walk(n, p, ctx) {          // same ctx rule as walkRender()
    const b = badgesOf(n, p, ctx);
    if (b.length) out.push({ ref: refOf(n), badges: b });
    const ctx2 = n.isElem ? ctx.concat([n._cp.full]) : ctx;
    for (const c of n.children) walk(c, n, ctx2);
  })(E.TREE, null, []);
  return out;
}

// fill in mk() defaults, keep given ids (fresh ids for missing/duplicate ones), map wildcard
function normalizeTree(t, hasWild, wildcard) {
  let maxId = 0;
  (function scan(n) {
    if (Number.isInteger(n.id) && n.id > maxId) maxId = n.id;
    for (const c of n.children || []) scan(c);
  })(t);
  if (E.uid < maxId) E.uid = maxId;
  const seen = new Set();
  const mapSeg = s => (hasWild && s === wildcard ? E.WILD : s);
  function norm(n) {
    const o = Object.assign({ key: '', kind: 'value', src: [], children: [], types: [], count: 0, pcount: null,
      samples: [], synthetic: false, recKey: null, computed: null, ctx: null, open: false, isElem: false,
      isRoot: false }, n);
    if (!Number.isInteger(o.id) || seen.has(o.id)) o.id = ++E.uid;
    seen.add(o.id);
    if (Array.isArray(o.src)) o.src = o.src.map(mapSeg);
    if (Array.isArray(o.ctx)) o.ctx = E.compile(o.ctx.map(mapSeg)).full;
    o.children = (n.children || []).map(norm);
    return o;
  }
  const root = norm(t);
  if (t.isRoot === undefined) root.isRoot = true;
  return root;
}

const plain = v => E.JSONparse(E.JSONstringify(v));   // detached deep copy (sandbox realm)

/* ------------------------------------------------------------------ one case */
function runCase(c, opt) {
  const res = {};
  if (c.name !== undefined) res.name = c.name;
  let data;
  if (Object.prototype.hasOwnProperty.call(c, 'text')) {
    const r = E.parseLoose(String(c.text));
    data = r.data; res.fixed = r.fixed;
  } else if (Object.prototype.hasOwnProperty.call(c, 'records')) data = c.records;
  else if (Object.prototype.hasOwnProperty.call(c, 'data')) data = c.data;
  else throw new Error('case needs "records", "data" or "text"');
  const design = Object.prototype.hasOwnProperty.call(c, 'sample') ? c.sample : data;

  // fresh engine state, exactly like the page's loadData(): DATA = ROOT = data; buildSchema(DATA)
  E.uid = 0; E.LIMIT = Infinity;
  E.DATA = design; E.ROOT = design;
  E.buildSchema(design);                         // TREE, ORIG (inferred), UNDO = [], compileAll()
  if (c.tree) {
    const hasWild = Object.prototype.hasOwnProperty.call(c, 'wildcard');
    E.TREE = normalizeTree(c.tree, hasWild, c.wildcard); E.UNDO = []; E.compileAll();
  }

  const log = [];
  (c.ops || []).forEach((op, i) => {
    hook.called = false; hook.msg = null;
    const before = E.snapshot();
    const entry = { i, op: op.op };
    let result;
    const N = () => resolveRef(op.node), T = () => resolveRef(op.target);
    switch (op.op) {
      case 'move': E.moveNode(N(), T(), op.zone); break;
      case 'merge': E.moveNode(N(), T(), 'inside'); break;
      case 'drop': {
        const n = N(), t = T();
        E.compileAll();
        const z = E.zoneFor(t, n, op.frac);
        result = { zone: z };
        if (!n.isRoot && !n.isElem && z) E.moveNode(n, t, z);   // dragstart refuses root / element rows
        break;
      }
      case 'zone': { const n = N(), t = T(); E.compileAll(); result = E.zoneFor(t, n, op.frac); break; }
      case 'rename': E.renameNode(N(), op.name); break;
      case 'delete': E.deleteNode(N()); break;
      case 'group': {                            // addGroup() reports msg ''; msg = the rename's, if any
        const g = E.addGroup(N());
        if (g && op.name !== undefined) E.renameNode(g, op.name);
        if (g) { E.compileAll(); result = { ref: refOf(g), key: g.key }; }
        break;
      }
      case 'unwrap': E.unwrapNode(N()); break;
      case 'flatten': E.flattenRec(N()); break;
      case 'undo': E.undo(); break;
      case 'reset': E.resetTree(); break;
      case 'canFlatten': E.compileAll(); result = E.canFlatten(N()); break;
      case 'output': result = plain(E.transform(E.DATA)); break;
      default: throw new Error('unknown op ' + JSON.stringify(op.op) + ' at index ' + i);
    }
    entry.applied = hook.called;
    entry.changed = E.snapshot() !== before;
    entry.undo = E.UNDO.length;
    entry.msg = hook.called ? hook.msg : null;
    if (result !== undefined) entry.result = result;
    log.push(entry);
  });

  // final transform over the real data, exactly like doExport(): transform(DATA)
  E.DATA = data; E.ROOT = data;
  const out = E.transform(data, c.limit);
  res.output = out;
  res.log = log;
  if (opt.tree) res.tree = E.JSONparse(E.snapshot());
  if (opt.shape) res.shape = shapeOf(E.TREE);
  if (opt.badges) res.badges = allBadges();
  if (opt.perRecord && Array.isArray(data)) {
    res.perRecord = data.map((_, i) => E.transform(data.slice(i, i + 1)));
    const flat = [].concat(...res.perRecord.map(r => (Array.isArray(r) ? r : [r])));
    res.perRecordConcatEqualsOutput = E.JSONstringify(flat) === E.JSONstringify(out);
  }
  if (opt.preview) {
    const isArr = Array.isArray(data);
    const vals = [];
    for (let i = 0; i < (isArr ? data.length : 1); i++) {
      let val = E.transform(isArr ? data.slice(i, i + 1) : data, 21);
      if (isArr && Array.isArray(val) && val.length === 1) val = val[0];
      vals.push(val);
    }
    res.preview = vals;
  }
  return res;
}

/* ------------------------------------------------------------------ main */
function main() {
  let opt;
  try { opt = parseArgs(process.argv.slice(2)); }
  catch (e) { process.stderr.write(e.message + '\n'); process.exitCode = 2; return; }
  if (opt.help || opt.input === null) {
    const head = fs.readFileSync(__filename, 'utf8').replace(/^#!.*\r?\n/, '').split('*/')[0];
    process.stdout.write(head.replace(/^\/\*\s?|^ \* ?/gm, '') + '\n');
    if (opt.input === null && !opt.help) process.exitCode = 2;
    return;
  }
  const htmlPath = findHtml(opt.html);
  try { E = loadEngine(htmlPath, { onChange }); }
  catch (e) { process.stderr.write('cannot load engine: ' + e.message + '\n'); process.exitCode = 2; return; }

  let text;
  try { text = opt.input === '-' ? fs.readFileSync(0, 'utf8') : fs.readFileSync(opt.input, 'utf8'); }
  catch (e) { process.stderr.write('cannot read input: ' + e.message + '\n'); process.exitCode = 2; return; }
  text = text.replace(/^\uFEFF/, '');
  let input;
  try { input = E.JSONparse(text); }                 // parsed in the engine's realm, like the page
  catch (e) { process.stderr.write('input is not JSON: ' + e.message + '\n'); process.exitCode = 2; return; }
  const batch = Array.isArray(input) || (input && Array.isArray(input.cases));
  const cases = Array.isArray(input) ? input : batch ? input.cases : [input];

  const results = [];
  for (const c of cases) {
    try { results.push(runCase(c, opt)); }
    catch (e) {
      results.push({ name: c && c.name, error: String(e && e.stack || e) });
      process.exitCode = 1;
    }
  }
  const indent = opt.compact ? 0 : 2;
  let outText;
  if (opt.export) {
    outText = results.map(r => (r.error ? 'ERROR: ' + r.error : E.JSONstringify(r.output === undefined ? null : r.output, null, indent)))
      .join('\n\u001e\n');
  } else {
    outText = E.JSONstringify(batch ? results : results[0], null, indent);
  }
  if (opt.out) fs.writeFileSync(opt.out, outText, 'utf8');
  else process.stdout.write(outText + '\n');
}

/* ------------------------------------------------------------------ module use */
// const H = require('./engine_harness.js'); const E = H.init();  then H.runCase(case, opts), H.refOf(node) ...
function init(htmlPath) { E = loadEngine(findHtml(htmlPath), { onChange }); return E; }
module.exports = { init, runCase, resolveRef, refOf, shapeOf, allBadges, normalizeTree, findHtml, loadEngine,
  hook, get engine() { return E; } };

if (require.main === module) main();
