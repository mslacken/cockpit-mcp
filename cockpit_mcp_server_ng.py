#!/usr/bin/env python3
"""
cockpit_mcp_server_ng.py — Cockpit bundle extractor + MCP server.

WHAT THIS IS
============
The Python half of a two-program pipeline implementing the parser and
recipe contract from COCKPIT_JSDIS_SPEC.md:

    bundle  →  parse.js (Node + @babel/parser + @babel/traverse)
    →  [ {kind, argv, opts, stdin, file, pos, func, …} ]
    →  dedupe, render per spec §5 / §10.1
    →  record_to_calls()  →  list[cockpit_bridge.Call]  →  cockpit-bridge

The Node shim does the static parsing (the spec, §7/§9, mandates a JS AST
parser and forbids executing the bundle); this script drives it, models the
records, renders them, dedupes them, bridges them to cockpit_bridge.Call
objects, and exposes the lot as a CLI and an MCP server.

WHAT THE RECORD CONTRACT SAYS
============================
Per spec §5 / §10.1, every bridge-mutating call site becomes a record:

    {
      "kind":      "spawn" | "script" | "dbus" | "file" | "proxy",
      "argv":      [ <node>… ]            // argv array (or primary str)
      "opts":      { "superuser": "require"|"try"|null,
                     "err":       "message"|"out"|null,
                     "bus":       "internal"|"system"|null,
                     "env":       ["LC_ALL=C"],
                     "pty":       false }
      "stdin":     <node> | null,          // the .input() payload
      "chain":     [ "then", "catch" ],    // promise tail, context only
      "file":      "…/users.js",
      "pos":       12345,
      "func":      "changeLocked" | null,  // §6.2 enclosing helper
      "passthrough": true                  // only if argv is a bare variable
    }

A "node" in argv is one of (spec §5.1):
    str, num, bool, arr, var, cond, call, binop, member, obj, tpl.

The parser classifies; the recipe-filler (this file) decides which
{…} holes the caller fills with `at` / `extra` when the action runs.

WHAT THE MCP LAYER ADDS
=======================
Over a CLI for one-shot use, an MCP surface (when the `mcp` SDK is
importable):

    cockpit_overview()                   host, bundles, record counts
    cockpit_parse(bundles…)              parse now, in process
    cockpit_records(kind, substring)     search the records
    cockpit_describe_record(index)       one record + its Call list
    cockpit_invoke(index, …)             dry-run or perform (confirm gated)

USAGE
=====
    ./cockpit_mcp_server_ng.py --records \
        --bundle /path/users.js --bundle /path/storaged.js
    ./cockpit_mcp_server_ng.py --recipe spawn --argv-contains=chpasswd \
        --stdin='bob:hunter2' --bundle /path/users.js
    ./cockpit_mcp_server_ng.py --fetch --insecure https://myhost:9090 \
        --into /tmp/opencode/live
    ./cockpit_mcp_server_ng.py --mcp --bundle /path/users.js
"""

from __future__ import annotations

import argparse
import contextlib
import struct
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

try:
    import cockpit_bridge as cbg
except ImportError:                          # pragma: no cover - optional
    cbg = None                               # type: ignore
try:
    import cockpit_list_tools as clt
except ImportError:                          # pragma: no cover - optional
    clt = None                               # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TARGET = "localhost:9090"
NODE = os.environ.get("COCKPIT_MCP_NODE", "node")
PARSER_JS = os.path.join(HERE, "parser", "parse.js")
FALLBACK_JS = "/tmp/opencode/parser/parse.js"
DEFAULT_FETCH_DIR = "/tmp/opencode/live"


# ==========================================================================
# Node shim availability
# ==========================================================================

def ensure_parser() -> str:
    """Path to a working parse.js shim (or a clear failure)."""
    env_p = os.environ.get("COCKPIT_JSDIS_PARSE_JS")
    if env_p and os.path.exists(env_p):
        return env_p
    for cand in (PARSER_JS, FALLBACK_JS):
        if os.path.exists(cand):
            return cand
    raise RuntimeError(
        "parser shim not found: expect " + PARSER_JS + " (or set "
        "COCKPIT_JSDIS_PARSE_JS=/path/to/parse.js)")


def _find_node() -> str:
    p = shutil.which(NODE)
    if not p:
        raise RuntimeError("Node required to run parse.js; install it or "
                           "point COCKPIT_MCP_NODE at a node binary")
    return p


# ==========================================================================
# Record model (Python mirror of the Node shim output)
# ==========================================================================

class Node:
    """A spec §5.1 argv element: a leaf value or a composite."""

    __slots__ = ("k", "v", "raw")

    def __init__(self, k: str, v: Any = None, raw: Any = None) -> None:
        self.k = k
        self.v = v
        self.raw = raw

    @staticmethod
    def from_dict(d: Any) -> "Node":
        if not isinstance(d, dict):
            return Node("other", d, d)
        return Node(d.get("k", "other"), d.get("v"), d)

    def render(self) -> str:
        k, v = self.k, self.v
        if k == "str":    return '"%s"' % v
        if k == "num":    return str(v)
        if k == "bool":   return str(bool(v)).lower()
        if k == "null":   return "null"
        if k == "var":    return "{%s}" % v
        if k == "arr":    return " ".join(_node_render(x) for x in (v or []))
        if k == "cond":
            return "{ %s | %s }" % (_node_render(v.get("a")),
                                    _node_render(v.get("b")))
        if k == "binop":
            return "{%s %s %s}" % (_node_render(v.get("a")),
                                   v.get("op", "?"),
                                   _node_render(v.get("b")))
        if k == "call":
            vv = v if isinstance(v, dict) else {"id": v}
            return "{ call %s }" % vv.get("id", "?")
        if k == "member": return "{ %s }" % v
        if k == "obj":    return "{ object }"
        if k == "tpl":    return "{ template }"
        if k == "fn":     return "{ fn %s }" % (v or "?")
        if k == "expr":   return "{ " + _node_render(v) + " }"
        if k == "imp":    return "{ import }"
        return "{ ?%s }" % k


def _node_render(x: Any) -> str:
    if isinstance(x, Node):
        return x.render()
    if isinstance(x, dict):
        return Node.from_dict(x).render()
    return str(x)


class Opts:
    """The options object of a cockpit.* call (spec §3)."""

    __slots__ = ("superuser", "err", "bus", "env", "pty", "extras")

    def __init__(self,
                 superuser: str | None = None,
                 err: str | None = None,
                 bus: str | None = None,
                 env: list[str] | None = None,
                 pty: Any = None,
                 extras: dict[str, Any] | None = None) -> None:
        def _s(v):
            """Coerce any node/value to a string (or leave None alone) so the
            dedupe key stays hashable and opts render stays uniform."""
            if v is None:
                return None
            if isinstance(v, str):
                return v
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, list):
                return " ".join(_s(x) or "" for x in v) or None
            if isinstance(v, dict):
                return json.dumps(v, default=str)
            return str(v)
        self.superuser = _s(superuser)
        self.err = _s(err)
        self.bus = _s(bus)
        self.env = _s(env)
        self.pty = _s(pty)
        self.extras = extras or {}

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"superuser": self.superuser, "err": self.err,
                               "bus": self.bus, "env": self.env, "pty": self.pty}
        out.update(self.extras)
        return out


class Record:
    """One bridge-mutating call site, in spec §5 form."""

    def __init__(self,
                 kind: str,
                 method: str,
                 object_id: str,
                 args: list[Node],
                 file: str = "",
                 pos: int = 0,
                 func: str | None = None,
                 func_kind: str | None = None,
                 passthrough: bool = False,
                 primary: Node | None = None,
                 interface: str | None = None,
                 path: str | None = None,
                 options: Opts | None = None,
                 stdin: Node | None = None,
                 chain: list[str] | None = None) -> None:
        self.kind = kind
        self.method = method
        self.object = object_id
        self.args = args
        self.file = file
        self.pos = pos
        self.func = func
        self.func_kind = func_kind
        self.passthrough = passthrough
        self.primary = primary
        self.interface = interface
        self.path = path
        self.options = options
        self.stdin = stdin
        self.chain = chain or []

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Record":
        args = [Node.from_dict(a) for a in (d.get("args") or [])]
        opts_raw = d.get("options")
        options = None
        if isinstance(opts_raw, dict) and opts_raw:
            options = Opts(superuser=opts_raw.get("superuser"),
                           err=opts_raw.get("err"),
                           bus=opts_raw.get("bus"),
                           env=opts_raw.get("env"),
                           pty=opts_raw.get("pty"))
        sd, pd = d.get("stdin"), d.get("primary")
        argv = d.get("argv")
        # argv/primary/stdin come back as raw dicts; lift them to Nodes
        kind = d.get("kind", "?")
        primary: Node | None = None
        if kind in ("dbus", "file") and isinstance(d.get("primary"), dict):
            primary = Node.from_dict(d["primary"])
        return Record(
            kind=kind,
            method=d.get("method") or d.get("kind") or "?",
            object_id=d.get("object") or "?",
            args=args,
            file=d.get("file") or "",
            pos=int(d.get("pos") or 0),
            func=d.get("func"),
            func_kind=d.get("func_kind"),
            passthrough=bool(d.get("passthrough")),
            primary=primary,
            interface=d.get("interface"),
            path=d.get("path"),
            options=options,
            stdin=Node.from_dict(sd) if isinstance(sd, dict) else None,
            chain=list(d.get("chain") or []),
        )

    # ------------------------------------------------------------------
    def argv_nodes(self) -> list[Node]:
        return [self.args[0]] if self.args else []

    def render_argv(self) -> str:
        nodes = self.argv_nodes()
        return nodes[0].render() if nodes else "<no argv>"

    def render_opts(self) -> str:
        if not self.options:
            return ""
        parts: list[str] = []
        for k, v in self.options.as_dict().items():
            if v in (None, "", [], {}):
                continue
            if isinstance(v, list):
                v = " ".join(repr(s) for s in v)
            parts.append("%s:%s" % (k, v))
        return "{ " + ", ".join(parts) + " }" if parts else ""

    def render(self) -> str:
        return (f"kind={self.kind:<8} argv=[ {self.render_argv()} ]  "
                f"opts={self.render_opts() or '—'}  "
                f"stdin={'yes' if self.stdin is not None else '—'}  "
                f"func={self.func or '—'}")

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "method": self.method,
            "object": self.object,
            "args": [n.raw if n.raw is not None else {"k": n.k, "v": n.v}
                     for n in self.args],
            "file": self.file,
            "pos": self.pos,
        }
        if self.func:         d["func"] = self.func
        if self.func_kind:    d["func_kind"] = self.func_kind
        if self.passthrough:  d["passthrough"] = True
        if self.options:      d["options"] = self.options.as_dict()
        if self.stdin is not None:
            d["stdin"] = self.stdin.raw or {"k": self.stdin.k, "v": self.stdin.v}
        if self.primary is not None:
            d["primary"] = self.primary.raw or {"k": self.primary.k,
                                                "v": self.primary.v}
        if self.interface:    d["interface"] = self.interface
        if self.path:         d["path"] = self.path
        if self.chain:        d["chain"] = list(self.chain)
        d["render"] = self.render()
        return d


# ==========================================================================
# Parse: bundle(s) → [Record]
# ==========================================================================

class ParseResult:
    def __init__(self) -> None:
        self.records: list[Record] = []
        self.parse_errors: list[str] = []

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out


def run_parser(bundles: list[str | dict[str, Any]]) -> ParseResult:
    """Parse each bundle via the Node shim and lift the records."""
    if not bundles:
        raise RuntimeError("run_parser: no bundles given")
    parse_js = ensure_parser()
    node = _find_node()
    payload: list[dict[str, Any]] = []
    for b in bundles:
        if isinstance(b, dict):
            payload.append(dict(b))
        else:
            p = os.path.abspath(b)
            payload.append({"file": os.path.basename(p), "path": p})
    workdir = os.path.dirname(parse_js)
    in_path = os.path.join(workdir or ".", "in.json")
    with open(in_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    timeout = int(os.environ.get("COCKPIT_MCP_PARSE_TIMEOUT", "600"))
    try:
        proc = subprocess.run([node, parse_js, in_path],
                              check=False, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        r = ParseResult()
        r.parse_errors.append("parse.js: timed out at %ds" % timeout)
        return r
    if proc.returncode != 0:
        r = ParseResult()
        r.parse_errors.append(
            "parse.js exit %d: %s" % (proc.returncode, proc.stderr[:800]))
        return r
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        r = ParseResult()
        r.parse_errors.append("parse.js output: %s" % exc)
        return r
    out = ParseResult()
    for d in data:
        if isinstance(d, dict) and d.get("kind") == "error":
            out.parse_errors.append(
                "%s: %s" % (d.get("file", "?"), d.get("error", "error")))
            continue
        try:
            out.records.append(Record.from_dict(d))
        except (TypeError, ValueError, KeyError) as exc:
            out.parse_errors.append(
                "record shape: %s: %s" % (type(exc).__name__, exc))
    return out


# ==========================================================================
# Dedupe: spec §2's "distinct, de-duplicated"
# ==========================================================================

def dedupe(records: list[Record]) -> list[Record]:
    """Key by (kind, method, argv-shape, superuser, err, has-stdin)."""
    seen: dict[tuple, Record] = {}
    order: list[tuple] = []
    for r in records:
        key = (r.kind, r.method,
               tuple(n.render() for n in r.argv_nodes()),
               r.options.superuser if r.options else None,
               r.options.err if r.options else None,
               bool(r.stdin))
        if key not in seen:
            seen[key] = r
            order.append(key)
    return [seen[k] for k in order]


# ==========================================================================
# Recipe: Record + Request → list[Call]   (spec §8)
# ==========================================================================

def record_to_calls(r: Record, req: Any | None,
                    stdin_override: str | None = None) -> list[Any]:
    """Fill a record's argv holes from the caller and build Call objects."""
    if cbg is None:
        raise RuntimeError("cockpit_bridge could not be imported")
    nodes = r.argv_nodes()
    if not nodes:
        return []
    first = nodes[0]
    detail = "from parsed record @ %s:%d (%s)" % (r.file, r.pos, r.method)

    def fill(argv: list[str], pool: dict[str, str],
             extras: list[str], target: str | None) -> list[str]:
        # Named fill first (a caller may know the real name), then a
        # positional fill of the remaining holes: target → first hole,
        # the extra values in the order given → the following holes. This
        # is exactly the spec §8 model ("argv with holes to be filled by
        # the live target / extra values"); the hole names in the bundle
        # are minified, so positional order is the stable contract.
        positional = list(extras)
        if target is not None:
            positional = [target] + positional
        pos_index = 0
        def one(a: str) -> str:
            nonlocal pos_index
            m = re.fullmatch(r"\{(.+)\}", a)
            if not m:
                return a
            key = m.group(1).strip()
            if key in pool:
                return pool[key]
            if pos_index < len(positional):
                val = positional[pos_index]
                pos_index += 1
                return val
            return a
        return [one(a) for a in argv]

    def _arg_str_or_hole(x: Any) -> str:
        """Render a spec §5.1 node as an argv element: leaves as the raw value
        (so `"chpasswd"` becomes `chpasswd`, not `'"chpasswd"'`); non-leaves
        keep their placeholder form `{…}`. The `fill()` pass below matches
        placeholders of the form `{…}` — this is what lets a caller pass an
        `at` / `extra` value straight into one of those slots."""
        # x may be a bare value (e.g. from a record that used Node.raw), a
        # dict node, or a Node instance.
        n = x
        if not isinstance(n, Node) and not isinstance(n, dict):
            return str(n)
        if not isinstance(n, Node):
            n = Node.from_dict(n)
        if n.k in ("str", "num", "bool", "null"):
            v = n.v
            if v is None:
                return ""
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)
        # For a placeholder, use the render form so fill() can identify it.
        rendered = _node_render(n)
        if rendered.startswith("{") and rendered.endswith("}"):
            return rendered
        return rendered

    if r.kind in ("spawn", "script"):
        argv: list[str] = []
        if isinstance(first, Node) and first.k == "arr":
            for x in (first.v or []):
                if not isinstance(x, Node) and not isinstance(x, dict):
                    x = Node("other", x)
                argv.append(_arg_str_or_hole(x))
        else:
            argv.append(_arg_str_or_hole(first))
        pool: dict[str, str] = {}
        extra_list: list[str] = []
        target = None
        if req is not None:
            target = str(req.at) if getattr(req, "at", None) else None
            pool.update({k: str(v) for k, v in (req.extra or {}).items()})
            pool.update({k: str(v) for k, v in (req.arguments or {}).items()})
            extra_list = [str(v) for v in (req.extra or {}).values()]
            extra_list += [str(v) for v in (req.arguments or {}).values()]
        argv = fill(argv, pool, extra_list, target)
        # resolve a stdin payload: CLI --stdin wins, then request extras
        stdin: str | None = stdin_override
        if stdin is None and req is not None:
            for k in ("stdin", "password", "passphrase", "key", "body"):
                v = dict(req.extra or {}).get(k)
                if v not in (None, ""):
                    stdin = str(v)
                    break
        if r.kind == "spawn":
            return [cbg.Call("spawn", argv=argv, stdin=stdin, detail=detail)]
        return [cbg.Call("spawn",
                         argv=["sh", "-c", " ".join(argv[1:])]
                         if len(argv) > 1 else argv,
                         stdin=stdin, detail=detail)]
    if r.kind == "dbus":
        # svc, path, iface, method are the first four slots (spec §7: they are
        # literal in the bundle); everything after is payload. A slot that
        # came back as an unknown ("?" or a bare var) is filled by the caller,
        # so a proxy record whose interface the page resolves at runtime can
        # still be performed.
        want = 4
        while len(r.args) < want:
            r.args.append(Node("other"))
        svc, path, iface, meth = r.args[:4]

        def pick(n: Node, *names: str) -> str:
            v = _scalar(n)
            if v in ("", "?", "<service>") and req is not None:
                for nm in names:
                    for src in (req.extra or {}, req.arguments or {}):
                        if src.get(nm) not in (None, ""):
                            return str(src[nm])
            return v

        return [cbg.Call("dbus",
                         service=pick(svc, "service", "svc"),
                         path=pick(path, "path"),
                         interface=pick(iface, "interface", "iface"),
                         method=_scalar(meth),
                         args=[(a.raw if a.raw is not None else {"k": a.k,
                                                                 "v": a.v})
                               for a in r.args[4:]],
                         detail=detail)]
    if r.kind == "file":
        svc = first.v if isinstance(first, Node) and first.k == "str" \
            else (first.v if first.v else "<path>")
        return [cbg.Call("spawn", argv=["cat", str(svc)], detail=detail)]
    if r.kind == "proxy":
        return [cbg.Call("spawn",
                         argv=["<proxy>", str(r.interface or "?"),
                               str(r.path or "?")],
                         detail=detail)]
    return [cbg.Call("spawn", argv=["<record: %s>" % r.kind],
                     detail="no recipe rule for %s at %s:%d"
                            % (r.kind, r.file, r.pos))]


def _scalar(n: Node) -> str:
    return str(n.v) if n.k in ("str", "var") else str(n.k)


def perform_bridge(calls: list[Any],
                   password: str | None = None) -> dict[str, Any]:
    """Run a plan over the wire.

    Points the bridge at NG_CONFIG's target, TLS and user settings (so the call
    lands on the same host the records were parsed from), then hands the calls
    to ``Bridge.perform`` with the password that starts the superuser peer.
    Kept as a module-level function so the confirm-gated, real path of
    ``cockpit_invoke`` can be exercised without the MCP SDK.
    """
    if cbg is None:
        raise RuntimeError("cockpit_bridge not importable")
    if clt is not None:
        clt.set_target(NG_CONFIG.target)
        clt.INSECURE = NG_CONFIG.insecure
        os.environ["COCKPIT_USER"] = NG_CONFIG.user or getpass.getuser()
        if password:
            os.environ["COCKPIT_PASSWORD"] = password
    return cbg.BRIDGE.perform(calls, dry_run=False, password=password)


# ==========================================================================
# Fetch: log in to a live host, dump every page bundle to disk
# ==========================================================================

def fetch_bundles(target: str,
                  packages: list[str] | None = None,
                  user: str = "", password: str = "",
                  insecure: bool = False,
                  into_dir: str = DEFAULT_FETCH_DIR) -> list[str]:
    if clt is None:
        raise RuntimeError("cockpit_list_tools not importable; either run this "
                           "file next to the scanner or pass --bundle ….")
    clt.set_target(target)
    clt.INSECURE = insecure
    os.environ["COCKPIT_USER"] = user or getpass.getuser()
    os.environ["COCKPIT_PASSWORD"] = password
    cookie, manifests = clt.connect()
    pkgs = clt.build_packages(manifests, packages or [])
    # page.scripts is only filled once each page's HTML has been read and its
    # <script src> tags resolved; that is exactly what introspect() does.
    clt.introspect(pkgs, cookie)
    af = clt.AssetFetcher(cookie)
    os.makedirs(into_dir, exist_ok=True)
    host = clt.MANIFEST_HOST
    # De-duplicate the asset list up front (deterministic, in the main thread);
    # the fetch+write below can then run on a pool sharing the keep-alive conn.
    jobs: list[tuple[str, str]] = []       # (url, out_path)
    seen: set[str] = set()
    for pkg in pkgs:
        for page in pkg.pages:
            for spath in (page.scripts or []):
                p = clt.posixpath.normpath(
                    "/cockpit/@%s/%s/%s" % (host, pkg.name, spath))
                if p in seen or not p.endswith(".js"):
                    continue
                seen.add(p)
                jobs.append((p, os.path.join(into_dir, "LIVE_" + p.replace("/", "__"))))

    def _fetch_write(job: tuple[str, str]) -> str | None:
        url, fn = job
        body = af.text(url)
        if not body or body.lstrip().startswith("<!DOCTYPE"):
            return None
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(body)
        return fn

    out: list[str] = []
    if len(jobs) > 1:
        workers = int(os.environ.get("COCKPIT_FETCH_WORKERS", "8"))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for result in ex.map(_fetch_write, jobs):
                if result is not None:
                    out.append(result)
    else:
        for job in jobs:
            fn = _fetch_write(job)
            if fn is not None:
                out.append(fn)
    return out


# ==========================================================================
# CLI
# ==========================================================================

def _count_kinds(recs: list[Record]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in recs:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def _count_by_file(recs: list[Record]) -> list[tuple[str, int]]:
    out: dict[str, int] = {}
    for r in recs:
        out[r.file] = out.get(r.file, 0) + 1
    return sorted(out.items())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Parse Cockpit bundles, recover the bridge-mutating "
                    "call sites and emit the spec §5 records; serve over "
                    "MCP with --mcp.")
    p.add_argument("--target", default=None, metavar="[SCHEME://]HOST[:PORT]",
                   help="Cockpit server (default: %s)" % DEFAULT_TARGET)
    p.add_argument("--user", default=None, help="Cockpit user")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS certificate verification")
    p.add_argument("--bundle", action="append", default=[], metavar="PATH",
                   help="a .js bundle to parse (repeatable)")
    p.add_argument("--fetch", action="store_true",
                   help="fetch every page bundle from --target into --into")
    p.add_argument("--into", default=DEFAULT_FETCH_DIR,
                   help="where to write fetched bundles")
    p.add_argument("--package", action="append", default=[], metavar="NAME",
                   help="restrict --fetch to this package (repeatable)")
    p.add_argument("--parse", action="store_true",
                   help="emit the spec §5 records as JSON")
    p.add_argument("--records", action="store_true",
                   help="emit the de-duplicated record listing")
    p.add_argument("--list", action="store_true", help="same as --records")
    p.add_argument("--kind", default=None,
                   help="filter by kind: spawn|script|dbus|file|proxy|any")
    p.add_argument("--substring", default="",
                   help="only records whose render() contains this")
    p.add_argument("--recipe", dest="recipe_name", default=None, metavar="KIND",
                   help="show the Call list a record of this kind produces")
    p.add_argument("--argv-contains", dest="argv_contains", default="",
                   help="--recipe: narrow to records whose render() contains "
                        "this (e.g. 'chpasswd')")
    p.add_argument("--stdin", dest="stdin", default="",
                   help="--recipe: value to supply for the record's stdin")
    p.add_argument("--at", default=None, help="target object for the recipe")
    p.add_argument("--extra", action="append", default=[], metavar="K=V",
                   help="--recipe: an extra input (K=V), repeatable")
    p.add_argument("--cache", default=None,
                   help="read pre-parsed records JSON instead of re-parsing")
    p.add_argument("--json", action="store_true", help="JSON on stdout")
    p.add_argument("--quiet", action="store_true", help="less log output")
    p.add_argument("--mcp", action="store_true",
                   help="serve over stdio as an MCP server")
    p.add_argument("--version", action="store_true")
    args = p.parse_args(argv)

    if args.version:
        print("cockpit_mcp_server_ng.py (1.0) — Cockpit bundle extractor + MCP")
        return 0
    if args.mcp:
        if not MCP_AVAILABLE:
            print("mcp SDK not installed: pip install mcp", file=sys.stderr)
            return 1
        return _run_mcp(args)

    user = args.user or os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD") or ""
    if (not args.fetch and not args.bundle and args.cache is None
            and not password and sys.stdin.isatty() and not args.quiet):
        password = getpass.getpass("Cockpit password for %s: " % user)
    target = args.target or os.environ.get("COCKPIT_HOST") or DEFAULT_TARGET

    bundles: list[str] = list(args.bundle)
    if args.fetch:
        try:
            fetched = fetch_bundles(target, args.package or None,
                                    user=user, password=password,
                                    insecure=args.insecure,
                                    into_dir=args.into)
            bundles += fetched
            if not args.quiet:
                print("fetched %d bundles into %s" % (len(fetched), args.into),
                      file=sys.stderr)
        except (OSError, RuntimeError) as exc:
            print("fetch failed: %s" % exc, file=sys.stderr)
            return 1
    if not bundles and args.cache is None:
        fallback = args.into
        if os.path.isdir(fallback):
            for fn in sorted(os.listdir(fallback)):
                if fn.endswith(".js"):
                    bundles.append(os.path.join(fallback, fn))
        if not bundles:
            print("no bundles to parse: pass --bundle, --fetch, or --cache",
                  file=sys.stderr)
            return 1

    if args.cache:
        with open(args.cache, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        recs = [Record.from_dict(d) for d in data
                if isinstance(d, dict) and d.get("kind") != "error"]
    else:
        result = run_parser(bundles)
        recs = result.records
        if result.parse_errors and not args.quiet:
            for e in result.parse_errors:
                print("parse error: %s" % e, file=sys.stderr)

    ded = dedupe(recs)

    if args.json or args.parse:
        payload = [r.as_dict() for r in ded
                   if args.kind in (None, "any", r.kind)]
        print(json.dumps(payload, indent=2))
        return 0

    if args.records or args.list:
        out = [r for r in ded
               if args.kind in (None, "any", r.kind)
               and (not args.substring or args.substring in r.render())]
        print("%d distinct records (from %d sites, %d bundles):"
              % (len(out), len(recs), len({r.file for r in recs})))
        for r in out:
            print("  " + r.render())
        print("\nkinds: " + "  ".join("%s=%d" % kv
                                     for kv in _count_kinds(recs).items()))
        return 0

    if args.recipe_name:
        recs2 = [r for r in ded if r.kind == args.recipe_name]
        if args.argv_contains:
            cands = [r for r in recs2 if args.argv_contains in r.render()]
            if cands:
                recs2 = cands
        if not recs2:
            print("no records of kind %s%s"
                  % (args.recipe_name, "" if not args.argv_contains
                     else " containing %r" % args.argv_contains),
                  file=sys.stderr)
            return 1
        print("%d candidate record(s) of kind %s%s:"
              % (len(recs2), args.recipe_name,
                 "" if not args.argv_contains
                 else " containing %r" % args.argv_contains))
        for r in recs2[:10]:
            print("  " + r.render())
        if cbg is not None:
            extra = dict(kv.split("=", 1) for kv in args.extra) if args.extra \
                else {}
            req = cbg.Request({}, extra, args.at) if (args.at or extra) else None
            try:
                calls = record_to_calls(recs2[0], req,
                                        stdin_override=args.stdin
                                        or None)
                print("\nwould produce %d call(s):" % len(calls))
                for c in calls:
                    print("  " + c.render())
            except Exception as exc:   # noqa: BLE001 - demo path
                print("  (recipe failed: %s)" % exc, file=sys.stderr)
        return 0

    # default: one-shot JSON with stats
    print(json.dumps({
        "bundles": len(bundles),
        "records": len(recs),
        "distinct": len(ded),
        "kinds": _count_kinds(recs),
        "files": _count_by_file(recs),
        "records": [r.as_dict() for r in ded],
    }, indent=2))
    return 0


# ==========================================================================
# MCP layer (if the SDK is importable)
# ==========================================================================

MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except Exception:                              # pragma: no cover
    FastMCP = None                             # type: ignore


@dataclass
class NgConfig:
    target: str = DEFAULT_TARGET
    user: str = ""
    password: str = ""
    bundles: list[str] = field(default_factory=list)
    insecure: bool = False
    read_only: bool = True
    writable: bool = False
    package: list[str] = field(default_factory=list)
    into_dir: str = DEFAULT_FETCH_DIR


NG_CONFIG = NgConfig()
_RECS: list[Record] = []
_RECS_DEDUPED: list[Record] = []


def _ensure_records() -> list[Record]:
    global _RECS, _RECS_DEDUPED
    if _RECS:
        return _RECS
    if not NG_CONFIG.bundles:
        raise RuntimeError("no bundles: call cockpit_parse or pass --bundle")
    _RECS = run_parser(NG_CONFIG.bundles).records
    _RECS_DEDUPED = dedupe(_RECS)
    return _RECS


def _reset_records() -> None:
    global _RECS, _RECS_DEDUPED
    _RECS = []
    _RECS_DEDUPED = []


def _state_brief(records: list[Record] | None = None,
                 limit: int = 6) -> list[str]:
    pool = records if records is not None else (_RECS_DEDUPED or [])
    return ["%s %s %s" % (r.kind, r.render_argv(), r.render_opts())
            for r in pool[:limit]]


if MCP_AVAILABLE and FastMCP is not None:
    mcp = FastMCP(
        "cockpit",
        instructions=("Cockpit host exposed over MCP.  Work in order: "
                      "cockpit_overview → cockpit_parse → cockpit_records → "
                      "cockpit_describe_record → cockpit_invoke."))

    @mcp.tool(title="Cockpit host overview",
              description="Target, user, bundles, parsed record counts.")
    async def cockpit_overview() -> dict[str, Any]:
        state: dict[str, Any] = {
            "target": NG_CONFIG.target,
            "user": NG_CONFIG.user or getpass.getuser(),
            "bundles": [os.path.basename(b) for b in NG_CONFIG.bundles],
            "read_only": NG_CONFIG.read_only,
        }
        try:
            recs = _ensure_records()
            state["records"] = len(recs)
            state["distinct"] = len(dedupe(recs))
            state["kinds"] = _count_kinds(recs)
            state["brief"] = _state_brief()[:6]
        except RuntimeError as exc:
            state["note"] = "records not parsed yet: %s" % exc
            state["next"] = "cockpit_parse()"
        return state

    @mcp.tool(title="Parse Cockpit bundles",
              description="Parse NG_CONFIG.bundles into spec §5 records.")
    async def cockpit_parse(bundles: list[str] | None = None,
                            target: str | None = None,
                            fetch: bool = False) -> dict[str, Any]:
        if fetch:
            fetched = fetch_bundles(
                target or NG_CONFIG.target, NG_CONFIG.package or None,
                user=NG_CONFIG.user or getpass.getuser(),
                password=NG_CONFIG.password, insecure=NG_CONFIG.insecure,
                into_dir=NG_CONFIG.into_dir)
            NG_CONFIG.bundles += [b for b in fetched
                                  if b not in NG_CONFIG.bundles]
        if bundles:
            NG_CONFIG.bundles += [b for b in bundles
                                  if b not in NG_CONFIG.bundles]
        _reset_records()
        recs = _ensure_records()
        ded = dedupe(recs)
        return {"bundles": len(NG_CONFIG.bundles),
                "records": len(recs), "distinct": len(ded),
                "kinds": _count_kinds(recs),
                "brief": _state_brief(ded)[:8]}

    @mcp.tool(title="List Cockpit records",
              description="De-duplicated bridge-mutating call sites.")
    async def cockpit_records(kind: str = "any",
                              substring: str = "",
                              limit: int = 20) -> dict[str, Any]:
        recs = [r for r in (_RECS_DEDUPED or _ensure_records())
                if kind in ("any", "all", r.kind)
                and (not substring or substring in r.render())]
        return {"count": len(recs), "returned": len(recs[:limit]),
                "records": [r.as_dict() for r in recs[:limit]]}

    @mcp.tool(title="Describe one Cockpit record",
              description="One record in full, plus its Call list.")
    async def cockpit_describe_record(index: int = 0) -> dict[str, Any]:
        recs = _RECS_DEDUPED or _ensure_records()
        if index >= len(recs):
            return {"error": "index %d out of range (0..%d)"
                        % (index, len(recs) - 1)}
        rec = recs[index]
        out = rec.as_dict()
        if cbg is not None:
            try:
                out["would_call"] = [c.as_json()
                                     for c in record_to_calls(rec, None)]
            except Exception as exc:   # noqa: BLE001
                out["would_call_error"] = str(exc)
        return out

    @mcp.tool(title="Perform one Cockpit record",
              description="Dry-run or execute the recipe for a record. "
                          "confirm=true required for a real change.")
    async def cockpit_invoke(index: int = 0,
                             arguments: dict[str, Any] | None = None,
                             extra: dict[str, Any] | None = None,
                             at: str | None = None,
                             dry_run: bool = True,
                             confirm: bool = False) -> dict[str, Any]:
        can_mutate = (not NG_CONFIG.read_only) and NG_CONFIG.writable
        if not can_mutate and not dry_run:
            # Default is read-only: without an explicit writable flag this tool
            # will only ever list the plan, and says so rather than pretending.
            dry_run = True
        recs = _RECS_DEDUPED or _ensure_records()
        if index >= len(recs):
            return {"error": "index %d out of range (0..%d)"
                        % (index, len(recs) - 1)}
        rec = recs[index]
        if cbg is None:
            return {"error": "cockpit_bridge not importable",
                    "record": rec.as_dict()}
        try:
            req = cbg.Request(dict(arguments or {}), dict(extra or {}), at)
            calls = record_to_calls(rec, req)
        except Exception as exc:   # noqa: BLE001
            return {"error": str(exc), "dry_run": dry_run,
                    "record": rec.as_dict()}
        if dry_run:
            return {"performed": False, "dry_run": True,
                    "backend": "bridge", "record": rec.as_dict(),
                    "plan": [c.as_json() for c in calls],
                    "note": "a dry run lists what cockpit_bridge would do"}
        if not confirm:
            return {"error": "a destructive change needs confirm=true",
                    "record": rec.as_dict(),
                    "plan": [c.as_json() for c in calls]}
        try:
            return perform_bridge(calls, NG_CONFIG.password or None)
        except (OSError, RuntimeError, ConnectionError, struct.error) as exc:
            if cbg.BRIDGE is not None:
                cbg.BRIDGE.close()
            return {"error": str(exc), "performed": False,
                    "backend": "bridge"}

    @mcp.tool(title="Re-fetch and re-parse",
              description="Log in, fetch every page bundle, re-parse.")
    async def cockpit_refresh() -> dict[str, Any]:
        if clt is None:
            return {"error": "cannot fetch: cockpit_list_tools missing"}
        try:
            fetched = fetch_bundles(
                NG_CONFIG.target, NG_CONFIG.package or None,
                user=NG_CONFIG.user or getpass.getuser(),
                password=NG_CONFIG.password, insecure=NG_CONFIG.insecure,
                into_dir=NG_CONFIG.into_dir)
            NG_CONFIG.bundles += [b for b in fetched
                                  if b not in NG_CONFIG.bundles]
            _reset_records()
            recs = _ensure_records()
            return {"fetched": len(fetched),
                    "bundles_total": len(NG_CONFIG.bundles),
                    "records": len(recs), "distinct": len(dedupe(recs))}
        except (OSError, RuntimeError) as exc:
            return {"error": str(exc)}


def _run_mcp(args: Any) -> int:
    NG_CONFIG.target = args.target or NG_CONFIG.target
    NG_CONFIG.insecure = args.insecure
    if args.user:
        NG_CONFIG.user = args.user
    if args.bundle:
        NG_CONFIG.bundles = list(args.bundle)
    if args.package:
        NG_CONFIG.package = list(args.package)
    if args.into:
        NG_CONFIG.into_dir = args.into
    if os.environ.get("COCKPIT_PASSWORD"):
        NG_CONFIG.password = os.environ["COCKPIT_PASSWORD"]
    elif not args.quiet and sys.stdin.isatty():
        NG_CONFIG.password = getpass.getpass(
            "Cockpit password for %s: "
            % (NG_CONFIG.user or getpass.getuser()))
    if NG_CONFIG.bundles:
        with contextlib.suppress(RuntimeError, OSError):
            _ensure_records()
    try:
        mcp.run("stdio")            # type: ignore[union-attr]
    finally:
        if cbg is not None:
            cbg.BRIDGE.close()
    return 0


# ==========================================================================
# Entry point
# ==========================================================================

if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError) as exc:
        print("cockpit_mcp_server_ng: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
