#!/usr/bin/env python3
"""
cockpit_js_parser.py — static recovery of Cockpit bridge-mutating call sites.

Parses served .js bundles with tree-sitter (NO execution) and emits one
record per bridge-mutating call site:

  { "kind":    "spawn"|"script"|"dbus"|"file"|"proxy",
    "method":  "spawn"|"script"|"Format"|…,
    "object":  "Wa.default"  (the minified re-exported caller),
    "args":    [ <node>… ],
    "options": { "superuser":"require"|"try"|null,
                 "err":"message"|"out"|null,
                 "bus":"internal"|"system"|null,
                 "env":[…], "pty":… , …extras },
    "stdin":   <node>|null,          // the chained .input() payload
    "chain":   [ "then","catch",… ],
    "func":    <enclosing function name>|null,
    "passthrough": bool,             // argv is a bare variable
    "interface","path": …,          // dbus proxy records
    "primary": <node>,              // file records
    "file": "…/users.js", "pos": 12345 }

Node vocabulary (node() output):
  {k:"str"|"num"|"bool"|"null"|"var"|… , v: …}  (… see §5.1)

The record format was defined by the former Babel-based parse.js; the
helpers below paper over the tree-sitter / Babel AST differences so the
output (including `pos`) stays identical:
  * tree-sitter keeps parentheses (parenthesized_expression) and an
    `arguments` wrapper node, Babel does not -> `_val` / `_parent`.
  * Babel types optional chains as Optional{Member,Call}Expression,
    which parse.js never matches -> `_optional`.
  * Babel offsets are UTF-16 code units, tree-sitter's are bytes -> `_Pos`.
  * tagged templates are call_expression in tree-sitter.

Library use: parse_bundles([{ "file", "path" }, …]) -> [record | error].
Standalone: cockpit_js_parser.py <in.json> prints the records as JSON;
input is a JSON array of { "file" , "path" } (or { "bundles": […] }).
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import warnings
from typing import Any

from tree_sitter import Language, Node, Parser, Query, QueryCursor


_LIB_NAMES = ("libtree-sitter-javascript.so",
              "libtree-sitter-javascript.so.0")


def _js_language() -> Language:
    """Load the JavaScript grammar.

    Prefer the pip `tree_sitter_javascript` wheel; otherwise fall back to
    the system shared library (e.g. the openSUSE `tree-sitter-javascript`
    package providing `libtree-sitter-javascript.so`), calling its
    `tree_sitter_javascript` entry point through ctypes."""
    try:
        import tree_sitter_javascript
        return Language(tree_sitter_javascript.language())
    except ModuleNotFoundError:
        import ctypes
        for root in ("/usr/lib64", "/usr/lib", "/usr/local/lib"):
            for name in _LIB_NAMES:
                path = os.path.join(root, name)
                if not os.path.exists(path):
                    continue
                lib = ctypes.CDLL(path)
                fn = lib.tree_sitter_javascript
                fn.restype = ctypes.c_void_p
                # tree-sitter >= 0.26 takes the language pointer as an int
                # (a raw c_void_p object is rejected); silence the deprecation
                # warning for the int form.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return Language(int(fn()))
        raise


JS = _js_language()

METHODS = {"spawn": "spawn", "spawnp": "spawn", "script": "script",
           "dbus": "dbus", "file": "file"}

# Curated list of the used expression in the cockpit src tree.
PLUMBING = {"then", "catch", "finally", "done", "on", "off", "once",
            "emit", "addListener", "removeListener", "close", "send", "write",
            "read", "clear", "toString", "prototype", "call", "apply", "bind",
            "get", "set", "has", "hasOwnProperty", "proxy", "dbus"}

Q_SITES = Query(JS, """
((member_expression property: (property_identifier) @p) @m
 (#any-of? @p "spawn" "spawnp" "script" "dbus" "file"))
""")
Q_ASSIGN = Query(JS, """
(variable_declarator name: (identifier) @name value: (_) @init) @site
(assignment_expression left: (identifier) @name right: (_) @init) @site
(augmented_assignment_expression left: (identifier) @name right: (_) @init) @site
""")
Q_METHODS = Query(JS, """
((member_expression object: (identifier) property: (property_identifier) @p) @m
 (#match? @p "^[A-Z]"))
""")

NULL = {"k": "null", "v": None}
FUNCS = {"function_expression", "generator_function", "arrow_function"}


# --------------------------------------------------------------------------
# tree shape helpers (Babel equivalence)
# --------------------------------------------------------------------------
def _text(n: Node) -> str:
    return n.text.decode("utf-8", "surrogatepass")


def _val(n: Node | None) -> Node | None:
    """Strip parentheses, which Babel does not represent."""
    while n is not None and n.type == "parenthesized_expression":
        n = next((c for c in n.named_children if c.type != "comment"), None)
    return n


def _parent(n: Node) -> Node | None:
    """Babel's parentPath.node: skip parens and the `arguments` wrapper."""
    p = n.parent
    while p is not None and p.type in ("parenthesized_expression", "arguments"):
        p = p.parent
    return p


def _kids(n: Node) -> list[Node]:
    return [c for c in n.named_children if c.type != "comment"]


def _optional(n: Node) -> bool:
    """True when Babel would type `n` as an Optional{Member,Call}Expression:
    an optional_chain token anywhere down the object/function spine."""
    while n is not None and n.type in ("member_expression", "call_expression",
                                       "subscript_expression"):
        if any(c.type == "optional_chain" for c in n.children):
            return True
        n = n.child_by_field_name("function" if n.type == "call_expression"
                                  else "object")
    return False


def _is_call(n: Node | None) -> bool:
    """Babel CallExpression (not optional, not a tagged template)."""
    if n is None or n.type != "call_expression" or _optional(n):
        return False
    a = n.child_by_field_name("arguments")
    return a is not None and a.type == "arguments"


def _is_member(n: Node | None) -> bool:
    """Babel (non-optional) MemberExpression, computed or not."""
    return (n is not None and n.type in ("member_expression", "subscript_expression")
            and not _optional(n))


def _args(call: Node) -> list[Node]:
    a = call.child_by_field_name("arguments")
    if a is None or a.type != "arguments":
        return []
    return [_val(c) for c in _kids(a)]


def _prop_name(m: Node) -> str | None:
    """`.name` of a member's property (Identifier only, like Babel)."""
    if m.type == "member_expression":
        p = m.child_by_field_name("property")
        return _text(p) if p is not None and p.type == "property_identifier" else None
    i = _val(m.child_by_field_name("index"))    # x[ident] — computed Identifier
    return _text(i) if i is not None and i.type == "identifier" else None


# --------------------------------------------------------------------------
# literals
# --------------------------------------------------------------------------
_ESC = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|"
                  r"[0-7]{1,3}|\r\n|[\s\S])")
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
           "\n": "", "\r\n": "", "\r": "", "\u2028": "", "\u2029": ""}


def _unescape(raw: str, template: bool = False) -> str | None:
    bad = False

    def rep(m: re.Match) -> str:
        nonlocal bad
        e = m.group(1)
        if e in _SIMPLE:
            return _SIMPLE[e]
        if e[0] == "u":
            return chr(int(e[2:-1] if e[1] == "{" else e[1:], 16))
        if e[0] == "x":
            return chr(int(e[1:], 16))
        if e[0] in "01234567":
            if e == "0":
                return "\0"
            if template:            # octal escapes are invalid in templates
                bad = True
                return ""
            return chr(int(e, 8))
        if e in ("u", "x"):
            bad = True
        return e

    if template:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    s = _ESC.sub(rep, raw)
    if bad:
        return None
    # merge surrogate pairs from \uD83D\uDE00 style escapes
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _str_value(n: Node) -> str:
    return _unescape(_text(n)[1:-1]) or ""


def _num_value(n: Node) -> int | float | None:
    t = _text(n).replace("_", "").lower()
    try:
        if t.startswith(("0x", "0o", "0b")):
            v: float = int(t, 0)
        elif len(t) > 1 and t[0] == "0" and t.isdigit():
            v = int(t, 8) if all(c in "01234567" for c in t) else int(t, 10)
        else:
            v = float(t)
    except ValueError:
        return None
    if isinstance(v, float) and math.isinf(v):
        return None                 # JSON.stringify(Infinity) -> null
    if float(v).is_integer() and abs(v) < 2 ** 53:
        return int(v)
    return float(v)


def _num_str(v: int | float | None) -> str:
    """String(number) for the common cases."""
    if v is None:
        return "Infinity"
    if isinstance(v, int):
        return str(v)
    r = repr(v)
    return r[:-2] if r.endswith(".0") else r


def _is_bigint(n: Node) -> bool:
    return n.type == "number" and _text(n).endswith("n")


def _literal_value(n: Node | None) -> Any:
    """Babel literal `.value` (or a sentinel when the node has none)."""
    if n is None:
        return _NOVAL
    if n.type == "string":
        return _str_value(n)
    if n.type == "number":
        return _text(n)[:-1].replace("_", "") if _is_bigint(n) else _num_value(n)
    if n.type == "true":
        return True
    if n.type == "false":
        return False
    return _NOVAL


_NOVAL = object()


def _key_of(k: Node | None) -> str | None:
    """`p.key.name ?? p.key.value` as a string, or None."""
    if k is None:
        return None
    if k.type == "computed_property_name":
        k = _val(next(iter(_kids(k)), None))
        if k is None:
            return None
    if k.type in ("property_identifier", "identifier", "private_property_identifier",
                  "shorthand_property_identifier"):
        return _text(k)
    v = _literal_value(k)
    if v is _NOVAL:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if k.type == "number" and not _is_bigint(k):
        return _num_str(v)
    return str(v)


# --------------------------------------------------------------------------
# §5.1 node vocabulary
# --------------------------------------------------------------------------
_EXPR_TYPE = {
    "unary_expression": "UnaryExpression", "update_expression": "UpdateExpression",
    "assignment_expression": "AssignmentExpression",
    "augmented_assignment_expression": "AssignmentExpression",
    "await_expression": "AwaitExpression", "yield_expression": "YieldExpression",
    "class": "ClassExpression", "regex": "RegExpLiteral", "null": "NullLiteral",
    "super": "Super", "import": "Import", "object_pattern": "ObjectPattern",
    "array_pattern": "ArrayPattern", "string": "StringLiteral",
}


def dot_path(n: Node | None) -> str | None:
    n = _val(n)
    if n is None:
        return None
    if n.type == "identifier":
        return _text(n)
    if n.type == "this":
        return "this"
    if not _is_member(n):
        return None
    obj = n.child_by_field_name("object")
    if n.type == "subscript_expression":
        return dot_path(obj)
    prop = n.child_by_field_name("property")
    if prop is None or prop.type != "property_identifier":
        return dot_path(obj)
    base = dot_path(obj)
    return (base + "." + _text(prop))[:80] if base else None


def _array(n: Node) -> list[dict]:
    out: list[dict] = []
    pending = False
    for c in n.children:
        if c.type == ",":
            if not pending:
                out.append(NULL)
            pending = False
        elif c.is_named and c.type != "comment":
            out.append(node(c))
            pending = True
    return out


def _quasis(n: Node) -> tuple[list[Node], list[str]]:
    exprs, quasis = [], []
    src = n.text
    base = n.start_byte
    seg_start = base + 1
    for c in n.children:
        if c.type == "template_substitution":
            quasis.append(src[seg_start - base:c.start_byte - base])
            inner = _kids(c)
            exprs.append(_val(inner[0]) if inner else None)
            seg_start = c.end_byte
    quasis.append(src[seg_start - base:n.end_byte - base - 1])
    cooked = [_unescape(q.decode("utf-8", "surrogatepass"), template=True) for q in quasis]
    return exprs, [q for q in cooked if q is not None]


def _obj_props(n: Node) -> list[Node]:
    return [c for c in _kids(n)
            if c.type in ("pair", "shorthand_property_identifier",
                          "method_definition", "spread_element")]


def _prop_key_value(p: Node) -> tuple[Node | None, Node | None]:
    """(key, value) of an object member, Babel-style (methods/spreads: no value)."""
    if p.type == "pair":
        return p.child_by_field_name("key"), _val(p.child_by_field_name("value"))
    if p.type == "shorthand_property_identifier":
        return p, p
    if p.type == "method_definition":
        return p.child_by_field_name("name"), None
    return None, None


def node(n: Node | None) -> dict:
    n = _val(n)
    if n is None:
        return NULL
    t = n.type
    if t == "string":
        return {"k": "str", "v": _str_value(n)}
    if t == "number":
        if _is_bigint(n):
            return {"k": "expr", "v": "BigIntLiteral"}
        return {"k": "num", "v": _num_value(n)}
    if t in ("true", "false"):
        return {"k": "bool", "v": t == "true"}
    if t in ("identifier", "undefined", "shorthand_property_identifier"):
        return {"k": "var", "v": _text(n)}
    if t == "this":
        return {"k": "var", "v": "this"}
    if t == "meta_property":
        return {"k": "var", "v": re.sub(r"\s+", "", _text(n))}
    if t in ("member_expression", "subscript_expression"):
        if _optional(n):
            return {"k": "expr", "v": "OptionalMemberExpression"}
        obj = _val(n.child_by_field_name("object"))
        dp = dot_path(n)
        if not dp:
            dp = (_text(obj) + ".…") if obj is not None and obj.type == "identifier" else "<member>"
        return {"k": "member", "v": dp}
    if t == "array":
        return {"k": "arr", "v": _array(n)}
    if t == "ternary_expression":
        test = _val(n.child_by_field_name("condition"))
        out: dict = {"k": "cond", "v": {"a": node(n.child_by_field_name("consequence")),
                                        "b": node(n.child_by_field_name("alternative"))}}
        if test is not None and (test.type == "identifier" or _is_member(test)):
            out["sel"] = True
        return out
    if t in ("call_expression", "new_expression"):
        a = n.child_by_field_name("arguments")
        if t == "call_expression" and (a is None or a.type != "arguments"):
            return {"k": "expr", "v": "TaggedTemplateExpression"}
        if t == "call_expression" and _optional(n):
            return {"k": "expr", "v": "OptionalCallExpression"}
        c = _val(n.child_by_field_name("function" if t == "call_expression" else "constructor"))
        ident = _text(c) if c is not None and c.type == "identifier" else (dot_path(c) or "?")
        args = _kids(a) if a is not None else []
        return {"k": "call", "v": {"id": ident, "args": [node(x) for x in args[:4]]}}
    if t == "binary_expression":
        return {"k": "binop", "v": {"op": _text(n.child_by_field_name("operator")),
                                    "a": node(n.child_by_field_name("left")),
                                    "b": node(n.child_by_field_name("right"))}}
    if t == "template_string":
        exprs, quasis = _quasis(n)
        return {"k": "tpl", "v": {"exprs": [node(x) for x in exprs], "quasis": quasis}}
    if t == "object":
        o: dict[str, dict] = {}
        for i, p in enumerate(_obj_props(n)[:20]):
            k, v = _prop_key_value(p)
            name = _key_of(k)
            name = name if name is not None else str(i)
            o[name] = {"k": "fn", "v": name} if v is not None and v.type in FUNCS else node(v)
        return {"k": "obj", "v": o}
    if t in ("function_expression", "generator_function"):
        nm = n.child_by_field_name("name")
        return {"k": "fn", "v": _text(nm) if nm is not None else "<fn>"}
    if t == "arrow_function":
        return {"k": "fn", "v": "<fn>"}
    if t == "sequence_expression":
        kids = _kids(n)
        return node(kids[-1]) if kids else {"k": "expr"}
    if t == "spread_element":
        inner = _kids(n)
        return {"k": "binop", "v": {"op": "...", "a": {"k": "var", "v": "<spread>"},
                                    "b": node(inner[0] if inner else None)}}
    if t in ("jsx_element", "jsx_self_closing_element"):
        return {"k": "obj", "v": {"jsx": True}}
    return {"k": "expr", "v": _EXPR_TYPE.get(t, t)}


# --------------------------------------------------------------------------
# surrounding context
# --------------------------------------------------------------------------
def enclosing_fn(call: Node) -> str | None:
    c = call.parent
    while c is not None:
        t = c.type
        if t in ("function_declaration", "generator_function_declaration",
                 "function_expression", "generator_function"):
            nm = c.child_by_field_name("name")
            if nm is not None:
                return _text(nm)
        elif t == "method_definition":
            k = c.child_by_field_name("name")
            if k is not None and k.type != "private_property_identifier":
                if k.type == "computed_property_name":
                    inner = _val(next(iter(_kids(k)), None))
                    v = _key_of(inner) if inner is not None else None
                    return v if v is not None else "undefined"
                return _key_of(k)
        elif t == "variable_declarator":
            nm = c.child_by_field_name("name")
            if nm is not None and nm.type == "identifier":
                return _text(nm)
        elif t == "program":
            return None
        c = c.parent
    return None


def caller_label(call: Node) -> str:
    c = _val(call.child_by_field_name("function"))
    if _is_member(c):
        obj = _val(c.child_by_field_name("object"))
        if obj is not None:
            return dot_path(obj) or (_text(obj) if obj.type == "identifier" else "(caller)")
    return "(caller)"


def tail(call: Node) -> list[str]:
    """Spawn(…).input(…).done().then().catch() — the chained call steps."""
    t: list[str] = []
    cur = call
    while len(t) < 10:
        m = _parent(cur)
        if m is not None and m.type == "member_expression" and not _optional(m):
            prop = m.child_by_field_name("property")
            if (prop is not None and prop.type == "property_identifier"
                    and _val(m.child_by_field_name("object")) == cur):
                c = _parent(m)
                if _is_call(c) and _val(c.child_by_field_name("function")) == m:
                    t.append(_text(prop))
                    cur = c
                    continue
        break
    return list(dict.fromkeys(t))


def stdin_of(call: Node) -> dict | None:
    """The payload of a chained `X.input(payload)`."""
    m = _parent(call)
    if (m is not None and m.type == "member_expression" and not _optional(m)
            and _prop_name(m) == "input"
            and _val(m.child_by_field_name("object")) == call):
        c = _parent(m)
        if _is_call(c):
            a = _args(c)
            if a:
                return node(a[0])
    return None


def opts(n: Node | None) -> dict | None:
    """The options object (the 2nd arg, when present)."""
    u = _val(n)
    if u is None or u.type != "object":
        return None
    o: dict[str, Any] = {}
    for p in _obj_props(u):
        k, v = _prop_key_value(p)
        ks = _key_of(k)
        if ks is None:
            continue
        lv = _literal_value(v) if v is not None and v.type in ("string", "true", "false") else _NOVAL
        if ks in ("superuser", "err", "bus", "pty") and lv is not _NOVAL:
            o[ks] = lv
        elif ks in ("env", "environ"):
            o["env"] = _array(v) if v is not None and v.type == "array" else node(v)
        else:
            o[ks] = node(v)
    return o or None


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
class _Pos:
    """Byte offset -> UTF-16 offset (what Babel's node.start reports)."""

    def __init__(self, src: bytes) -> None:
        self.src = src
        self.ascii = src.isascii()
        self.last_b = 0
        self.last_u = 0

    def __call__(self, b: int) -> int:
        if self.ascii:
            return b
        if b < self.last_b:
            self.last_b = self.last_u = 0
        chunk = self.src[self.last_b:b].decode("utf-8", "surrogatepass")
        self.last_u += len(chunk.encode("utf-16-le", "surrogatepass")) // 2
        self.last_b = b
        return self.last_u


def _base(kind: str, method: str, call: Node, args: list[Node], pos: _Pos) -> dict:
    return {"kind": kind, "method": method, "object": caller_label(call),
            "args": [], "options": opts(args[1] if len(args) > 1 else None),
            "stdin": None, "chain": tail(call), "func": enclosing_fn(call),
            "passthrough": False, "pos": pos(call.start_byte)}


def spawn_script(kind: str, call: Node, pos: _Pos) -> dict:
    args = _args(call)
    out = _base(kind, kind, call, args, pos)
    argv = args[0] if args else None
    if argv is not None:
        if argv.type == "array":
            out["args"].append({"k": "arr", "v": _array(argv)})
        elif argv.type == "identifier":
            out["args"].append({"k": "var", "v": _text(argv)})
            out["passthrough"] = True
        else:
            out["args"].append(node(argv))
    out["stdin"] = stdin_of(call)
    return out


def file_record(call: Node, pos: _Pos) -> dict:
    args = _args(call)
    out = _base("file", "file", call, args, pos)
    if args and args[0] is not None:
        prim = node(args[0])
        out["args"].append(prim)
        out["primary"] = prim
    out["stdin"] = stdin_of(call)
    return out


def dbus_root(call: Node, pos: _Pos) -> dict:
    args = _args(call)
    out = _base("proxy", "dbus", call, args, pos)
    svc = args[0] if args else None
    out["args"] = [{"k": "str", "v": _str_value(svc)} if svc is not None and svc.type == "string"
                   else {"k": "var", "v": "<service>"}]
    return out


# --------------------------------------------------------------------------
# proxy follower — best-effort
#   X = Y.dbus(SVC, opts) ; X = Y.proxy(IFACE, path) ; X.Method(args…)
# --------------------------------------------------------------------------
def _root_call(n: Node | None, name: str) -> bool:
    if not _is_call(n):
        return False
    c = _val(n.child_by_field_name("function"))
    return _is_member(c) and _prop_name(c) == name


def _calls(q: Query, root: Node) -> list[Node]:
    """Calls whose (paren-stripped) callee is a captured member, in pre-order."""
    out = []
    for _, cap in QueryCursor(q).matches(root):
        m = cap["m"][0]
        c = _parent(m)
        if _is_call(c) and _val(c.child_by_field_name("function")) == m:
            out.append(c)
    return sorted(out, key=lambda x: (x.start_byte, -x.end_byte))


def proxy_map(root: Node) -> dict[str, dict]:
    m: dict[str, dict] = {}
    sites = []
    for _, cap in QueryCursor(Q_ASSIGN).matches(root):
        sites.append((cap["site"][0], _text(cap["name"][0]), _val(cap["init"][0])))
    sites.sort(key=lambda s: (s[0].start_byte, -s[0].end_byte))
    for _, name, init in sites:
        if _root_call(init, "dbus"):
            a = _args(init)
            s = a[0] if a else None
            bus = None
            o = a[1] if len(a) > 1 else None
            if o is not None and o.type == "object":
                for p in _obj_props(o):
                    k, v = _prop_key_value(p)
                    if _key_of(k) == "bus" and v is not None and v.type == "string":
                        bus = _str_value(v)
            m[name] = {"service": _str_value(s) if s is not None and s.type == "string" else None,
                       "iface": None, "path": None,
                       "bus": bus or (None if s is not None else "internal")}
        elif _root_call(init, "proxy"):
            a = _args(init)
            base = _val(_val(init.child_by_field_name("function")).child_by_field_name("object"))
            bi = m.get(_text(base), {}) if base is not None and base.type == "identifier" else {}
            iface = None
            if a:
                lv = _literal_value(a[0])
                if isinstance(lv, str):
                    iface = lv
                elif isinstance(lv, bool):
                    iface = "true" if lv else "false"
                elif lv is not _NOVAL and lv is not None:
                    iface = _num_str(lv)
            path = a[1] if len(a) > 1 else None
            m[name] = {"service": bi.get("service"), "iface": iface,
                       "path": _str_value(path) if path is not None and path.type == "string" else None,
                       "bus": bi.get("bus")}
    return m


def proxy_methods(root: Node, pmap: dict[str, dict], pos: _Pos) -> list[dict]:
    recs: list[dict] = []
    seen: set[str] = set()
    calls = _calls(Q_METHODS, root)
    for call in calls:
        if not _is_call(call):
            continue
        c = _val(call.child_by_field_name("function"))
        if not _is_member(c):
            continue
        name = _text(c.child_by_field_name("property"))
        v = _text(c.child_by_field_name("object"))
        if v not in pmap or name in PLUMBING:
            continue
        info = pmap[v]
        sig = "%s|%s|%s|%s" % (name, info["iface"] or "", info["path"] or "", v)
        if sig in seen:
            continue
        seen.add(sig)
        service = (info["service"] if info["service"] is not None
                   else info["iface"] if info["iface"] is not None
                   else "<cockpit-internal>" if info["bus"] == "internal" else "<service>")
        recs.append({
            "kind": "dbus", "method": name, "object": "<proxy " + v + ">",
            "args": [{"k": "str", "v": service},
                     {"k": "str", "v": info["path"] if info["path"] is not None else "/"},
                     {"k": "str", "v": info["iface"] or "?"},
                     {"k": "str", "v": name},
                     *[node(a) for a in _args(call) if a is not None]],
            "options": {"bus": info["bus"]} if info["bus"] else None,
            "stdin": None, "chain": tail(call), "func": enclosing_fn(call),
            "interface": info["iface"] or None, "path": info["path"] or None,
            "passthrough": False, "pos": pos(call.start_byte),
        })
    return recs


# --------------------------------------------------------------------------
# parse one bundle → [records]
# --------------------------------------------------------------------------
def parse_one(file: str, abs_path: str) -> list[dict] | dict:
    try:
        with open(abs_path, "rb") as fh:
            src = fh.read()
    except OSError as e:
        return {"kind": "error", "file": file, "error": "read: " + str(e)}
    tree = Parser(JS).parse(src)
    root = tree.root_node
    pos = _Pos(src)
    records: list[dict] = []
    calls = _calls(Q_SITES, root)
    for call in calls:
        if not _is_call(call):
            continue
        c = _val(call.child_by_field_name("function"))
        if not _is_member(c):
            continue
        kind = METHODS[_text(c.child_by_field_name("property"))]
        obj = _val(c.child_by_field_name("object"))
        if not (obj is not None and (obj.type == "identifier" or _is_member(obj))):
            continue
        if kind == "dbus":
            records.append(dbus_root(call, pos))
        elif kind == "file":
            records.append(file_record(call, pos))
        else:
            records.append(spawn_script(kind, call, pos))
    try:
        pmap = proxy_map(root)
        if pmap:
            records.extend(proxy_methods(root, pmap, pos))
    except Exception:                    # best-effort: keep the core records
        pass
    for r in records:
        r["file"] = file
    return records


def parse_bundles(bundles: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in bundles:
        file = b.get("file") or (os.path.basename(b["path"]) if b.get("path") else "?")
        abs_path = b.get("path") or file
        base = os.path.basename(abs_path).lower()
        if base == "cockpit.js" or base.endswith(".po") or "po.manifest" in base:
            out.append({"kind": "error", "file": file, "error": "skipped: runtime plumbing bundle"})
            continue
        try:
            rs = parse_one(file, abs_path)
        except Exception as e:           # noqa: BLE001
            out.append({"kind": "error", "file": file, "error": str(e)})
            continue
        out.extend(rs if isinstance(rs, list) else [rs])
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: cockpit_js_parser.py <in.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    bundles = payload if isinstance(payload, list) else payload.get("bundles", [])
    json.dump(parse_bundles(bundles), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
