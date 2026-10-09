#!/usr/bin/env node
/*
 * parse.js — static recovery of Cockpit bridge-mutating call sites.
 *
 * Implements COCKPIT_JSDIS_SPEC.md §5. Reads one or more served .js bundles,
 * parses them with @babel/parser (NO execution, vm, or eval), walks the AST
 * with @babel/traverse, and emits — on stdout, as a JSON array — one record per
 * bridge-mutating call site:
 *
 *   { "kind":    "spawn"|"script"|"dbus"|"file"|"proxy",
 *     "method":  "spawn"|"script"|"Format"|…,
 *     "object":  "Wa.default"  (the minified re-exported caller),
 *     "args":    [ <node>… ],
 *     "options": { "superuser":"require"|"try"|null,
 *                  "err":"message"|"out"|null,
 *                  "bus":"internal"|"system"|null,
 *                  "env":[…], "pty":… , …extras },
 *     "stdin":   <node>|null,          // the chained .input() payload
 *     "chain":   [ "then","catch",… ],
 *     "func":    <enclosing function name>|null,
 *     "passthrough": bool,             // argv is a bare variable
 *     "interface","path": …,          // dbus proxy records
 *     "primary": <node>,              // file records
 *     "file": "…/users.js", "pos": 12345 }
 *
 * Node vocabulary (toNode output):
 *   {k:"str"|"num"|"bool"|"null"|"var"|… , v: …}  (… see §5.1)
 *
 * input (argv[1]) : JSON array of { "file" , "path" } (or { "bundles": […] })
 * output (stdout) : JSON array of records; { "kind":"error", … } on failure.
 */
"use strict";

const fs = require("fs");
const pathMod = require("path");
const babel = require("@babel/parser");
const _traverse = require("@babel/traverse");
const traverse = _traverse.default || _traverse;

// The four bridge verbs (spawn is a near-alias of spawnp; we accept both).
// A Set, not a plain object — so we do not inherit hasOwnProperty / toString /
// valueOf from Object.prototype the way METHODS[c.property.name] used to.
const METHODS = new Set(["spawn", "spawnp", "script", "dbus", "file"]);
const KIND_OF = { spawn: "spawn", spawnp: "spawn", script: "script",
                  dbus: "dbus", file: "file" };

// --------------------------------------------------------------------------
// Bundle parsing (prefer script; fall back to module; never execute)
// --------------------------------------------------------------------------
function parseBundle(code) {
  const plugins = ["jsx", "classProperties", "dynamicImport", "optionalChaining",
    "nullishCoalescingOperator", "objectRestSpread", "classPrivateProperties",
    "classPrivateMethods", "topLevelAwait", "bigInt"];
  const cfgs = [
    { sourceType: "script", plugins, errorRecovery: true, tokens: false },
    { sourceType: "module", plugins, errorRecovery: true, tokens: false },
  ];
  let err = null;
  for (const cfg of cfgs) {
    try { return babel.parse(code, cfg); } catch (e) { err = e; }
  }
  throw err || new Error("parse failed");
}
// --------------------------------------------------------------------------
// small AST helpers
// --------------------------------------------------------------------------
function unwrap(n) {
  while (n && (n.type === "ChainExpression" || n.type === "TSNonNullExpression"
      || n.type === "TSAsExpression")) n = n.expression;
  return n;
}
function dotPath(node) {
  if (!node) return null;
  if (node.type === "Identifier") return node.name;
  if (node.type === "ThisExpression") return "this";
  if (node.type !== "MemberExpression") return null;
  if (node.computed || !node.property || node.property.type !== "Identifier")
    return dotPath(node.object);
  const base = dotPath(node.object);
  return base ? (base + "." + node.property.name).slice(0, 80) : null;
}
// --------------------------------------------------------------------------
// §5.1 node vocabulary
// --------------------------------------------------------------------------
function node(n) {
  if (n === null || n === undefined) return { k: "null", v: null };
  if (typeof n !== "object") return { k: "other", v: String(n) };
  switch (n.type) {
    case "StringLiteral":   return { k: "str", v: n.value };
    case "NumberLiteral":
    case "NumericLiteral":  return { k: "num", v: n.value };
    case "BooleanLiteral":  return { k: "bool", v: n.value };
    case "Identifier":      return { k: "var", v: n.name };
    case "ThisExpression":  return { k: "var", v: "this" };
    case "MetaProperty":    return { k: "var", v: n.meta.name + "." + n.property.name };
    case "MemberExpression":
      return { k: "member", v: dotPath(n) || (n.object.name ? n.object.name + ".…" : "<member>") };
    case "ArrayExpression":
      return { k: "arr", v: (n.elements || []).map(x => x ? node(x) : { k: "null", v: null }) };
    case "ConditionalExpression": {
      const sel = n.test && (n.test.type === "Identifier" || n.test.type === "MemberExpression");
      const out = { k: "cond", v: { a: node(n.consequent), b: node(n.alternate) } };
      if (sel) out.sel = true;
      return out;
    }
    case "CallExpression":
    case "NewExpression": {
      const c = n.callee;
      const id = c.type === "Identifier" ? c.name : (dotPath(c) || (c.name || "?"));
      return { k: "call", v: { id, args: (n.arguments || []).slice(0, 4).map(node) } };
    }
    case "BinaryExpression":
    case "LogicalExpression":
      return { k: "binop", v: { op: n.operator, a: node(n.left), b: node(n.right) } };
    case "TemplateLiteral": {
      const q = (n.quasis || []).map(x => (x.value && x.value.cooked != null) ? x.value.cooked : null)
                              .filter(x => x !== null);
      return { k: "tpl", v: { exprs: (n.expressions || []).map(node), quasis: q } };
    }
    case "ObjectExpression": {
      const o = {};
      (n.properties || []).slice(0, 20).forEach((p, i) => {
        if (!p) return;
        const key = p.key ? (p.key.name != null ? p.key.name : p.key.value) : i;
        const name = key != null ? String(key) : String(i);
        o[name] = (p.value && (p.value.type === "FunctionExpression" ||
            p.value.type === "ArrowFunctionExpression" || p.value.type === "ObjectMethod"))
          ? { k: "fn", v: (p.key && p.key.name) || name }
          : node(p.value);
      });
      return { k: "obj", v: o };
    }
    case "FunctionExpression":
    case "ArrowFunctionExpression":
      return { k: "fn", v: (n.id && n.id.name) || "<fn>" };
    case "ChainExpression":   return node(n.expression);
    case "SequenceExpression":return node(n.expressions[n.expressions.length - 1] || {});
    case "SpreadElement":
      return { k: "binop", v: { op: "...", a: { k: "var", v: "<spread>" }, b: node(n.argument) } };
    case "JSXElement":
    case "JSXFragment":       return { k: "obj", v: { jsx: true } };
    default: return { k: "expr", v: n.type };
  }
}
// --------------------------------------------------------------------------
// surrounding context
// --------------------------------------------------------------------------
function enclosingFn(p) {
  for (let c = p.parentPath; c; c = c.parentPath) {
    const n = c.node;
    if (!n) continue;
    if (n.type === "FunctionDeclaration" && n.id) return n.id.name;
    if (n.type === "FunctionExpression" && n.id) return n.id.name;
    if (n.type === "ObjectMethod" || n.type === "ClassMethod") {
      if (n.key) return n.key.name != null ? n.key.name : String(n.key.value);
    }
    if (n.type === "VariableDeclarator" && n.id && n.id.name) return n.id.name;
    if (n.type === "Program") return null;
  }
  return null;
}
function callerLabel(p) {
  const c = p.node.callee;
  if (c && c.type === "MemberExpression" && c.object)
    return dotPath(c.object) || (c.object.name || "(caller)");
  return "(caller)";
}
function tail(p) {
  // Spawn(…).input(…).done().then().catch() — each step is a MemberExpression
  // with the previous call as .object. Walk up while each parent matches.
  const t = [];
  let cur = p;
  while (t.length < 10) {
    const member = cur.parentPath && unwrap(cur.parentPath.node);
    if (member && member.type === "MemberExpression" && member.property
        && member.property.type === "Identifier" && member.object === cur.node) {
      const name = member.property.name;
      // Only record the call steps: a MemberExpression with no wrapping
      // CallExpression would be a property access, not a call step.
      const call = cur.parentPath.parentPath && cur.parentPath.parentPath.node;
      if (call && call.type === "CallExpression" && call.callee === member) {
        t.push(name);
        cur = cur.parentPath.parentPath;
        continue;
      }
    }
    break;
  }
  // Deduplicate consecutive duplicates (a .then chain may collapse one)
  return [...new Set(t)];
}
function input(p) {
  // the spawn/script call is the `.object` of  `X.input(payload)` :
  //   parent      = MemberExpression (property "input", object == p.node)
  //   parent.parent = CallExpression (the .input(…) call)
  const member = unwrap(p.parentPath && p.parentPath.node);
  if (member && member.type === "MemberExpression" && member.property
      && member.property.name === "input" && member.object === p.node) {
    const call = p.parentPath.parentPath && p.parentPath.parentPath.node;
    if (call && call.type === "CallExpression" && call.arguments && call.arguments.length)
      return node(call.arguments[0]);
  }
  return null;
}
// --------------------------------------------------------------------------
// options object (the 2nd arg, when present, is always the options object)
// --------------------------------------------------------------------------
function opts(n) {
  const u = unwrap(n);
  if (!u || u.type !== "ObjectExpression") return null;
  const o = {};
  for (const p of (u.properties || [])) {
    if (!p) continue;
    const key = p.key ? (p.key.name != null ? p.key.name : p.key.value) : null;
    if (key == null) continue;
    const ks = String(key);
    if ((ks === "superuser" || ks === "err" || ks === "bus" || ks === "pty")
        && (p.value && (typeof p.value.value === "string" || typeof p.value.value === "boolean"))) {
      o[ks] = p.value.value;
    } else if (ks === "env" || ks === "environ") {
      o.env = (p.value && p.value.type === "ArrayExpression")
        ? (p.value.elements || []).map(x => x ? node(x) : { k: "null", v: null })
        : node(p.value);
    } else {
      o[ks] = node(p.value);
    }
  }
  return Object.keys(o).length ? o : null;
}
// --------------------------------------------------------------------------
// one record for a spawn / script site
// --------------------------------------------------------------------------
function spawnScript(kind, p) {
  const args = p.node.arguments || [];
  const argv = args[0] && unwrap(args[0]);
  const out = {
    kind: "spawnp" === kind ? "spawn" : kind,
    method: "spawnp" === kind ? "spawn" : kind,
    object: callerLabel(p),
    args: [],
    options: opts(args[1]),
    stdin: null,
    chain: tail(p),
    func: enclosingFn(p),
    passthrough: false,
    pos: p.node.start || 0,
  };
  if (argv) {
    if (argv.type === "ArrayExpression") {
      out.args.push({ k: "arr", v: (argv.elements || []).map(x => x ? node(x) : { k: "null", v: null }) });
    } else if (argv.type === "Identifier") {
      out.args.push({ k: "var", v: argv.name });
      out.passthrough = true;
    } else {
      out.args.push(node(argv));
    }
  }
  out.stdin = input(p);
  return out;
}
// one record for a file site
function fileRecord(p) {
  const args = p.node.arguments || [];
  const primary = args[0] && unwrap(args[0]);
  const out = {
    kind: "file", method: "file", object: callerLabel(p),
    args: [], options: opts(args[1]), stdin: null,
    chain: tail(p), func: enclosingFn(p), passthrough: false,
    pos: p.node.start || 0,
  };
  if (primary) {
    const prim = (primary.type === "StringLiteral") ? { k: "str", v: primary.value }
        : (primary.type === "Identifier" ? { k: "var", v: primary.name } : node(primary));
    out.args.push(prim);
    out.primary = prim;
  }
  out.stdin = input(p);
  return out;
}
// one record for a dbus(.SVC, opts) root (its mutations come from the proxy pass)
function dbusRoot(p) {
  const args = p.node.arguments || [];
  const svc = args[0] && unwrap(args[0]);
  return {
    kind: "proxy", method: "dbus", object: callerLabel(p),
    args: [ (svc && svc.type === "StringLiteral") ? { k: "str", v: svc.value }
            : { k: "var", v: "<service>" } ],
    options: opts(args[1]), stdin: null,
    chain: tail(p), func: enclosingFn(p), passthrough: false,
    pos: p.node.start || 0,
  };
}
// --------------------------------------------------------------------------
// §7 proxy follower — best-effort: any failure here is ignored, it only ever
// adds D-Bus method records, never disturbs the core spawn / script / file set.
//   X = Y.dbus(SVC, opts) ; X = Y.proxy(IFACE, path) ; X.Method(args…)
// --------------------------------------------------------------------------
const PLUMBING = new Set(["then", "catch", "finally", "done", "on", "off", "once",
  "emit", "addListener", "removeListener", "close", "send", "write", "read",
  "clear", "toString", "prototype", "call", "apply", "bind", "get", "set",
  "has", "hasOwnProperty", "proxy", "dbus"]);
function isDbusRoot(n) {
  return n && n.type === "CallExpression" && n.callee && n.callee.type === "MemberExpression"
    && n.callee.property && n.callee.property.name === "dbus";
}
function isProxyRoot(n) {
  return n && n.type === "CallExpression" && n.callee && n.callee.type === "MemberExpression"
    && n.callee.property && n.callee.property.name === "proxy";
}
function proxyMap(ast) {
  const m = new Map();
  function consider(name, init) {
    if (!name || !init) return;
    if (isDbusRoot(init)) {
      const a = init.arguments || [];
      const s = a[0] && unwrap(a[0]);
      let bus = null;
      const optsArg = a[1] && unwrap(a[1]);
      if (optsArg && optsArg.type === "ObjectExpression")
        for (const pr of (optsArg.properties || []))
          if (pr && pr.key && (pr.key.name === "bus" || pr.key.value === "bus")
              && pr.value && pr.value.type === "StringLiteral") bus = pr.value.value;
      m.set(name, { service: (s && s.type === "StringLiteral") ? s.value : null,
                    iface: null, path: null, bus: bus || ((s ? null : "internal")) });
    } else if (isProxyRoot(init)) {
      const a = init.arguments || [];
      const base = init.callee.object;
      const bi = (base && base.name) ? (m.get(base.name) || {}) : {};
      m.set(name, {
        service: bi.service || null,
        iface: (a[0] && unwrap(a[0]) && (a[0].type || unwrap(a[0]).type) === "StringLiteral")
          ? unwrap(a[0]).value : (a[0] && unwrap(a[0]).value != null ? String(unwrap(a[0]).value) : null),
        path: (a[1] && unwrap(a[1]) && unwrap(a[1]).type === "StringLiteral")
          ? unwrap(a[1]).value : null,
        bus: bi.bus || null,
      });
    }
  }
  const guard = (fn) => { try { fn(); } catch (e) { /* best-effort */ } };
  guard(() => traverse(ast, {
    VariableDeclarator(p) { consider(p.node.id && p.node.id.name, unwrap(p.node.init)); },
    AssignmentExpression(p) { consider(p.node.left && p.node.left.name, unwrap(p.node.right)); },
  }));
  return m;
}
function proxyMethods(ast, map) {
  // D-Bus method names are always Capitalised by convention (UDisks2.Format,
  // systemd.Manager.ListUnits, PackageKit.GetUpdates, …). Single-letter
  // minified locals are far more likely to be a `next` / `map` / `find` on an
  // array than a real proxy method, so we only emit records whose method
  // starts with an upper-case letter. This is the only filter between "40
  // closed D-Bus verbs" and "every Array method in the bundle."
  const isMethod = (n) => n && n.length
    && (n[0] >= "A" && n[0] <= "Z") && !PLUMBING.has(n);
  const recs = [];
  const seen = new Set();
  const guard = (fn) => { try { fn(); } catch (e) { /* best-effort */ } };
  guard(() => traverse(ast, {
    CallExpression(p) {
      const c = p.node.callee;
      if (!c || c.type !== "MemberExpression" || !c.property || c.property.type !== "Identifier") return;
      const name = c.property.name;
      const v = c.object && c.object.name;
      if (!v || !map.has(v) || !isMethod(name)) return;
      const info = map.get(v);
      const sig = name + "|" + (info.iface || "") + "|" + (info.path || "") + "|" + v;
      if (seen.has(sig)) return;
      seen.add(sig);
      const service = info.service != null ? info.service
          : (info.iface != null ? info.iface : (info.bus === "internal" ? "<cockpit-internal>" : "<service>"));
      recs.push({
        kind: "dbus", method: name, object: "<proxy " + v + ">",
        args: [
          { k: "str", v: service },
          { k: "str", v: info.path != null ? info.path : "/" },
          { k: "str", v: info.iface || "?" },
          { k: "str", v: name },
          ...(p.node.arguments || []).filter(Boolean).map(a => node(unwrap(a))),
        ],
        options: info.bus ? { bus: info.bus } : null,
        stdin: null, chain: tail(p), func: enclosingFn(p),
        interface: info.iface || null, path: info.path || null,
        passthrough: false, pos: p.node.start || 0,
      });
    },
  }));
  return recs;
}
// --------------------------------------------------------------------------
// parse one bundle → [records]
// --------------------------------------------------------------------------
function parseOne(file, absPath) {
  let code;
  try { code = fs.readFileSync(absPath, "utf8"); }
  catch (e) { return { kind: "error", file, error: "read: " + e.message }; }
  let ast;
  try { ast = parseBundle(code); }
  catch (e) { return { kind: "error", file, error: "parse: " + (e && e.message) }; }

  const records = [];
  traverse(ast, {
    CallExpression(p) {
      const c = p.node.callee;
      if (!c || c.type !== "MemberExpression" || !c.property || c.property.type !== "Identifier") return;
      const name = c.property.name;
      if (!METHODS.has(name) || !KIND_OF[name]) return;   // reject Object.prototype leaks
      const kind = KIND_OF[name];
      const obj = c.object;
      if (!(obj && (obj.type === "Identifier" || obj.type === "MemberExpression"))) return;
      if (kind === "dbus") records.push(dbusRoot(p));
      else if (kind === "file") records.push(fileRecord(p));
      else records.push(spawnScript(kind, p));
    },
  });

  // Best-effort D-Bus proxy methods (a failure here is ignored entirely).
  try {
    const m = proxyMap(ast);
    if (m.size) for (const r of proxyMethods(ast, m)) { r.file = file; records.push(r); }
  } catch (e) { /* keep the core records */ }

  for (const r of records) { if (r.file == null) r.file = file; if (r.pos == null) r.pos = 0; }
  return records;
}
// --------------------------------------------------------------------------
// entry point
// --------------------------------------------------------------------------
function main() {
  const [inPath] = process.argv.slice(2);
  if (!inPath) { console.error("usage: parse.js <in.json>"); process.exit(2); }
  let payload;
  try { payload = JSON.parse(fs.readFileSync(inPath, "utf8")); }
  catch (e) { console.error("bad input: " + e.message); process.exit(2); }
  const bundles = Array.isArray(payload) ? payload
    : (payload && Array.isArray(payload.bundles)) ? payload.bundles : [];

  const out = [];
  for (const b of bundles) {
    const file = b.file || (b.path ? pathMod.basename(b.path) : "?");
    const absPath = b.path || file;
    const base = pathMod.basename(absPath).toLowerCase();
    if (/^cockpit\.js$/.test(base) || base.endsWith(".po") || /po\.manifest/.test(base)) {
      out.push({ kind: "error", file, error: "skipped: runtime plumbing bundle" });
      continue;
    }
    let rs;
    try { rs = parseOne(file, absPath); }
    catch (e) { out.push({ kind: "error", file, error: (e && e.message) || String(e) }); continue; }
    if (Array.isArray(rs)) out.push(...rs); else out.push(rs);
  }
  process.stdout.write(JSON.stringify(out, null, 2) + "\n");
}
if (require.main === module) main();
module.exports = { parseOne, node, parseBundle };
