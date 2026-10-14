#!/usr/bin/env python3
"""
cockpit_mcp_server_ng.py — Cockpit bundle extractor + MCP server.

WHAT THIS DOES
============

Reads all the packages of cocpit server and parses them statically with
tree-sitter (cockpit_js_parser.py), not by execution. The ouput is modeleled to
records, renders them, dedupes them, bridges them to cockpit_bridge.Call
objects, and exposes the lot as a CLI and an MCP server.

WHAT THE MCP LAYER ADDS
=======================
Over a CLI for one-shot use, an MCP surface (when the `mcp` SDK is
importable):

    cockpit_server(target)               connect: user/password via the
                                         COCKPIT_USER / COCKPIT_PASSWORD
                                         environment (or a tty prompt), then
                                         fetch + parse that host's bundles
    cockpit_overview()                   host, bundles, record counts
    cockpit_parse(bundles…)              parse now, in process
    cockpit_records(kind, substring)     search the records
    cockpit_describe_record(index|site)  one record + its Call list
    cockpit_invoke(index|site, …)        run it; only plans that may change
                                         the host need confirm=true

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
import shlex
import socket
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
try:
    import cockpit_js_parser as cjp
except (ImportError, OSError) as _exc:       # pragma: no cover - reported on use
    cjp = None                               # type: ignore
    _CJP_ERROR = str(_exc)

DEFAULT_TARGET = "localhost:9090"
DEFAULT_FETCH_DIR = "/tmp/opencode/live"


# ==========================================================================
# Record model (Python mirror of the cockpit_js_parser records)
# ==========================================================================

class Node:
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
    """The options object of a cockpit.* call """

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
    """Parse each bundle in process and lift the records."""
    if not bundles:
        raise RuntimeError("run_parser: no bundles given")
    if cjp is None:
        raise RuntimeError("bundle parser unavailable (%s); install "
                           "tree-sitter and tree-sitter-javascript" % _CJP_ERROR)
    payload: list[dict[str, Any]] = []
    for b in bundles:
        if isinstance(b, dict):
            payload.append(dict(b))
        else:
            p = os.path.abspath(b)
            payload.append({"file": os.path.basename(p), "path": p})
    data = cjp.parse_bundles(payload)
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
# Dedupe: as cockpit has differnt buttons leading to the same actions we 
# have to de-duplicate
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

    def fill(argv: list[str], pool: dict[str, Any],
             extras: list[Any], target: str | None,
             whole: bool = False) -> list[str]:
        # Named fill first (a caller may know the real name), then a
        # positional fill of the remaining holes: target → first hole,
        # the extra values in the order given → the following holes. This
        # is exactly the spec §8 model ("argv with holes to be filled by
        # the live target / extra values"); the hole names in the bundle
        # are minified, so positional order is the stable contract.
        # A list value is spliced in as several argv elements (spread holes,
        # or a variable that holds the whole argv). With `whole` the record's
        # argv *is* one variable, so a plain string is shell-split as well.
        positional = list(extras)
        if target is not None:
            positional = [target] + positional
        pos_index = 0
        def one(a: str) -> list[str]:
            nonlocal pos_index
            m = re.fullmatch(r"\{(.+)\}", a)
            if not m:
                return [a]
            key = m.group(1).strip()
            if key in pool:
                val = pool[key]
            elif pos_index < len(positional):
                val = positional[pos_index]
                pos_index += 1
            else:
                return [a]
            if isinstance(val, (list, tuple)):
                return [str(v) for v in val]
            if whole:
                return shlex.split(str(val))
            return [str(val)]
        return [x for a in argv for x in one(a)]

    def _arg_str_or_hole(x: Any) -> str:
        """Render a spec node as an argv element: leaves as the raw value
        (so `"chpasswd"` becomes `chpasswd`, not `'"chpasswd"'`); non-leaves
        keep their placeholder form `{...}`. The `fill()` pass below matches
        placeholders of the form `{...}` - this is what lets a caller pass an
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
        is_arr = isinstance(first, Node) and first.k == "arr"
        # spawn(P): P is the whole argv; script(text) is one program text
        whole = r.kind == "spawn" and not is_arr
        if is_arr:
            for x in (first.v or []):
                if not isinstance(x, Node) and not isinstance(x, dict):
                    x = Node("other", x)
                argv.append(_arg_str_or_hole(x))
        else:
            argv.append(_arg_str_or_hole(first))
        pool: dict[str, Any] = {}
        extra_list: list[Any] = []
        target = None
        if req is not None:
            target = str(req.at) if getattr(req, "at", None) else None
            pool.update(req.extra or {})
            pool.update(req.arguments or {})
            extra_list = list((req.extra or {}).values())
            extra_list += list((req.arguments or {}).values())
        argv = fill(argv, pool, extra_list, target, whole=whole)
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
        # cockpit.script(text, args): the text is one shell program, the
        # args become $1... — never join them into the program text
        return [cbg.Call("spawn", argv=["sh", "-c", argv[0], "sh"] + argv[1:],
                         stdin=stdin, detail=detail)]
    if r.kind == "dbus":
        # svc, path, iface, method are the first four slots (they are
        # literal in the bundle); everything after is payload. A slot that
        # came back as an unknown ("?" or a bare var) is filled by the caller,
        # so a proxy record whose interface the page resolves at runtime can
        # still be performed.
        want = 4
        while len(r.args) < want:
            r.args.append(Node("other"))
        svc, path, iface, meth = r.args[:4]

        def pick(n: Node, *names: str, at: bool = False) -> str:
            v = _scalar(n)
            if (v in ("", "?", "<service>") or n.k == "var") and req is not None:
                for nm in names:
                    for src in (req.extra or {}, req.arguments or {}):
                        if src.get(nm) not in (None, ""):
                            return str(src[nm])
                if at and getattr(req, "at", None):
                    return str(req.at)
            return v

        return [cbg.Call("dbus",
                         service=pick(svc, "service", "svc"),
                         path=pick(path, "path", at=True),
                         interface=pick(iface, "interface", "iface"),
                         method=_scalar(meth),
                         args=[(a.raw if a.raw is not None else {"k": a.k,
                                                                 "v": a.v})
                               for a in r.args[4:]],
                         detail=detail)]
    if r.kind == "file":
        # a literal path is used as is; a variable path is a hole that the
        # caller fills with `at` (or extra/arguments "path")
        if isinstance(first, Node) and first.k == "str":
            fpath = str(first.v)
        else:
            fpath = _arg_str_or_hole(first)
            if req is not None:
                src = dict(req.extra or {}, **(req.arguments or {}))
                if src.get("path") not in (None, ""):
                    fpath = str(src["path"])
                elif getattr(req, "at", None):
                    fpath = str(req.at)
        return [cbg.Call("spawn", argv=["cat", fpath], detail=detail)]
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


# ==========================================================================
# Danger: which planned calls need confirm=true
# ==========================================================================
#
# Only calls that can change the host (or leak secrets) are gated. A call is
# safe when its command is on the read-only list below (optionally narrowed to
# read-only subcommands/flags), or when it is a D-Bus getter. Anything unknown
# counts as dangerous: the list errs on the side of asking.

# command → None (any arguments are read-only) or a predicate over argv[1:]
_SAFE_COMMANDS: dict[str, Any] = {
    "cat": None, "date": lambda a: not any(x in ("-s", "--set") or
                                           x.startswith("--set=") for x in a),
    "findmnt": None, "test": None, "[": None, "stat": None, "ls": None,
    "head": None, "tail": None, "grep": None, "md5sum": None, "wc": None,
    "uname": None, "id": None, "getent": None, "lsblk": None, "df": None,
    "free": None, "uptime": None, "true": None, "echo": None,
    "udevadm": lambda a: a[:1] == ["info"],
    "showmount": None, "lsinitrd": None,
    "vdoformat": lambda a: a == ["--version"],
    "kpatch": lambda a: a[:1] == ["list"],
    "wg": lambda a: a[:1] in (["pubkey"], ["show"], ["genkey"]),
    "systemctl": lambda a: _first_word(a) in (
        "show", "status", "is-enabled", "is-active", "is-failed", "cat",
        "list-units", "list-unit-files", "list-timers", "list-sockets"),
    "timedatectl": lambda a: _first_word(a) in ("", "status", "show",
                                                "list-timezones",
                                                "timesync-status"),
    "hostnamectl": lambda a: _first_word(a) in ("", "status"),
    "journalctl": lambda a: not any(x.startswith(("--vacuum", "--rotate",
                                                  "--flush", "--sync"))
                                    for x in a),
    "zypper": lambda a: _first_word(a) in (
        "repos", "lr", "search", "se", "info", "if", "list-up dates", "lu",
        "list-patches", "lp", "patches", "packages", "pa", "products", "pd",
        "services", "ls", "patch-check", "pchk", "what-provides", "wp",
        "ps", "needs-rebooting"),
    "rpm": lambda a: bool(a) and a[0].startswith("-q"),
    "suseconnect": lambda a: bool(a) and all(
        x in ("-s", "--status", "--status-text", "-l", "--list-extensions",
              "--json") for x in a),
    "cryptsetup": lambda a: _first_word(a) in ("luksDump", "status",
                                               "isLuks"),
    "update-crypto-policies": lambda a: a == ["--show"],
    "btrfs": lambda a: a[:2] in (["subvolume", "list"], ["subvolume", "show"],
                                 ["filesystem", "show"], ["filesystem", "df"],
                                 ["filesystem", "usage"]),
    "command": lambda a: a[:1] == ["-v"],
    "last": None, "pwscore": None,
    "lastlog": lambda a: not any(x in ("-C", "--clear", "-S", "--set")
                                 for x in a),
    "chage": lambda a: bool(a) and a[0] in ("-l", "--list"),
    "passwd": lambda a: bool(a) and a[0] in ("-S", "--status"),
    "useradd": lambda a: a == ["-D"],
    "loginctl": lambda a: _first_word(a).startswith(("list-", "show-")) or
    _first_word(a) in ("user-status", "session-status", "seat-status"),
    "dnf": lambda a: a == ["--version"] or _first_word(a) in (
        "needs-restarting", "list", "info", "repolist", "repoquery",
        "search", "check-update", "history"),
}

# files whose content is a secret even though reading them changes nothing
_SECRET_FILES = ("/etc/shadow", "/etc/gshadow", "/etc/ssh/ssh_host_",
                 "/root/.ssh/", "/etc/NetworkManager/system-connections/")

# D-Bus methods that only read
_SAFE_DBUS = re.compile(r"(Get|List|Introspect|Ping|Query|Can|Is|Has|Poll)"
                        r"[A-Za-z]*$")


def _first_word(args: list[str]) -> str:
    """First non-option argument (the subcommand), '' if there is none."""
    return next((a for a in args if not a.startswith("-")), "")


def _argv_danger(argv: list[str]) -> str | None:
    """Why this argv may change the host, or None if it is read-only."""
    if not argv:
        return "empty command"
    cmd = os.path.basename(argv[0])
    if cmd in ("sh", "bash") and len(argv) >= 3 and argv[1] == "-c":
        return _shell_danger(argv[2])
    if cmd not in _SAFE_COMMANDS:
        return "%s is not a known read-only command" % cmd
    check = _SAFE_COMMANDS[cmd]
    if check is not None and not check(argv[1:]):
        return "%s %s may change the host" % (cmd, " ".join(argv[1:]))
    if cmd == "cat" and any(a.startswith(_SECRET_FILES) for a in argv[1:]):
        return "reads secret file(s)"
    return None


def _shell_danger(script: str) -> str | None:
    """A shell snippet is safe only if it is a pipeline / &&/|| chain of
    read-only commands without redirects, substitutions or ';'."""
    # a single '|' is a pipe and fine; only redirects/subshells/';' are not
    if re.search(r"[;<>`]|\$\(", script):
        return "shell script with redirects or substitutions"
    try:
        tokens = shlex.split(script)
    except ValueError:
        return "unparsable shell script"
    segment: list[str] = []
    for tok in tokens + ["|"]:
        if tok in ("|", "||", "&&"):
            if segment:
                why = _argv_danger(segment)
                if why:
                    return why
            segment = []
        elif tok == "&":
            return "shell script with background jobs"
        elif tok == "!" and not segment:
            continue                    # negating the exit status only
        else:
            segment.append(tok)
    return None


def classify_calls(calls: list[Any]) -> list[str]:
    """Reasons the plan needs confirm=true; empty means it only reads."""
    reasons: list[str] = []
    for c in calls:
        if c.op == "spawn":
            why = _argv_danger(list(c.argv))
        elif c.op == "dbus":
            why = None if _SAFE_DBUS.match(c.method or "") else \
                "D-Bus %s.%s may change the host" % (c.interface or "?",
                                                     c.method or "?")
        else:
            why = "unknown call type %s" % c.op
        if why:
            reasons.append(why)
    return reasons


def unfilled_holes(calls: list[Any]) -> list[str]:
    """`{…}` placeholders still left in a plan (the caller must fill them)."""
    holes: list[str] = []
    for c in calls:
        vals = list(c.argv) if c.op == "spawn" else [c.service, c.path,
                                                     c.interface]
        holes += [v for v in vals
                  if re.fullmatch(r"\{.+\}|<[a-z ]+>|\?", v or "")]
    return holes


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
    p.add_argument("--writable", action="store_true",
                   help="--mcp: let cockpit_invoke perform plans that may "
                        "change the host (still needs confirm=true)")
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


def _resolve_port(target: str) -> int:
    """The port of a [schema://]host[:port] target (Cockpit's 9090 by default)."""
    rest = target.split("://", 1)[-1].rsplit("/", 1)[0]
    if rest.startswith("["):                       # IPv6 literal
        rest = rest[1:]
    host, _, port = rest.rpartition(":")
    if port.isdigit():
        return int(port)
    return 9090


def _target_display(target: str) -> str:
    """Render the target with its resolved IP address, e.g.
    ``myhost.example.org (9090, 198.51.100.7)`` or, when the target already
    is an address, ``10.0.0.4 (9090)`` — so it is clear which host the MCP
    server is connected to."""
    rest = target.split("://", 1)[-1].rsplit("/", 1)[0]
    if rest.startswith("["):                       # IPv6 literal
        host = rest[1:].split("]", 1)[0]
    elif ":" in rest:
        host = rest.rpartition(":")[0]
    else:
        host = rest
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addr = next((i[4][0] for i in infos), None)
    except OSError:
        addr = None
    port = _resolve_port(target)
    if addr and addr != host:                      # name with a different IP
        return "%s (%d, %s)" % (host, port, addr)
    if addr:                                       # the target is the IP
        return "%s (%d)" % (host, port)
    return "%s (%d, could not resolve %s)" % (host, port, host)


def _mcp_instructions() -> str:
    """The MCP server description: it names the Cockpit host the server is
    connected to, with that host's resolved IP address, so the client can
    tell which machine is being addressed."""
    return ("Cockpit host %s exposed over MCP.  Work in order: "
            "cockpit_overview → cockpit_parse → cockpit_records → "
            "cockpit_describe_record → cockpit_invoke."
            % _target_display(NG_CONFIG.target))


if MCP_AVAILABLE and FastMCP is not None:
    mcp = FastMCP("cockpit", )

    @mcp.tool(title="Connect to a Cockpit host",
              description="Point the server at a Cockpit host: credentials are "
                          "read from COCKPIT_USER and COCKPIT_PASSWORD (a "
                          "tty prompt otherwise) — pass them as arguments in "
                          "that order. Then fetches every page bundle and "
                          "parses it, so cockpit_records/… work on that host. "
                          "The server also starts fine without a host: call "
                          "this first, or pass local bundles to "
                          "cockpit_parse.")
    async def cockpit_server(target: str | None = None,
                             user: str | None = None,
                             password: str | None = None,
                             fetch: bool = True) -> dict[str, Any]:
        if clt is None:
            return {"error": "cannot connect: cockpit_list_tools missing"}
        if fetch and (not user or not password):
            return {"error": "pass user and password (or fetch=false after "
                              "COCKPIT_USER / COCKPIT_PASSWORD are set)"}
        previous = (NG_CONFIG.target, NG_CONFIG.user)
        NG_CONFIG.target = target or NG_CONFIG.target
        if user:
            NG_CONFIG.user = user
        if password:
            NG_CONFIG.password = password
        resolved_user = user or os.environ.get("COCKPIT_USER") \
            or NG_CONFIG.user or getpass.getuser()
        if not (password or os.environ.get("COCKPIT_PASSWORD")
                or NG_CONFIG.password) and sys.stdin.isatty():
            password = getpass.getpass(
                "Cockpit password for %s: " % resolved_user)
            NG_CONFIG.password = password
        NG_CONFIG.user = resolved_user
        if (NG_CONFIG.target, resolved_user) != previous:
            # A new host or user: the cached root session and the parsed
            # bundles both belong to the old one, so drop them.
            if cbg is not None:
                cbg.BRIDGE.close()
            NG_CONFIG.bundles = []
            _reset_records()
        out: dict[str, Any] = {"connected": True, "target": NG_CONFIG.target,
                              "user": resolved_user}
        if fetch:
            try:
                fetched = fetch_bundles(
                    NG_CONFIG.target, NG_CONFIG.package or None,
                    user=resolved_user,
                    password=NG_CONFIG.password,
                    insecure=NG_CONFIG.insecure, into_dir=NG_CONFIG.into_dir)
                NG_CONFIG.bundles += [b for b in fetched
                                      if b not in NG_CONFIG.bundles]
                _reset_records()
                recs = _ensure_records()
                out.update({"fetched": len(fetched),
                            "bundles_total": len(NG_CONFIG.bundles),
                            "records": len(recs),
                            "distinct": len(dedupe(recs))})
            except (OSError, RuntimeError) as exc:
                return {"connected": False, "error": str(exc),
                        "target": NG_CONFIG.target}
        if mcp is not None:
            mcp._mcp_server.instructions = _mcp_instructions()
        return out

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

    def _resolve(index: int | None, site: str | None
                 ) -> tuple[int, Record] | dict[str, Any]:
        """Pick a record by global index or by its `file:pos` site."""
        recs = _RECS_DEDUPED or _ensure_records()
        if site:
            fname, _, pos = site.rpartition(":")
            for i, r in enumerate(recs):
                if (pos.isdigit() and r.pos == int(pos)
                        and fname and r.file.endswith(fname)):
                    return i, r
            return {"error": "no record at site %r (want <file>:<pos> as "
                             "shown in cockpit_records)" % site}
        index = 0 if index is None else index
        if not 0 <= index < len(recs):
            return {"error": "index %d out of range (0..%d)"
                             % (index, len(recs) - 1)}
        return index, recs[index]

    def _plan_info(calls: list[Any]) -> dict[str, Any]:
        reasons = classify_calls(calls)
        return {"plan": [c.as_json() for c in calls],
                "dangerous": bool(reasons),
                **({"danger": reasons} if reasons else {}),
                **({"holes": holes} if (holes := unfilled_holes(calls))
                   else {})}

    @mcp.tool(title="List Cockpit records",
              description="De-duplicated bridge-mutating call sites. Each "
                          "record carries its global `index` (and `site`) "
                          "for cockpit_describe_record / cockpit_invoke.")
    async def cockpit_records(kind: str = "any",
                              substring: str = "",
                              limit: int = 20) -> dict[str, Any]:
        recs = [(i, r) for i, r in enumerate(_RECS_DEDUPED or _ensure_records())
                if kind in ("any", "all", r.kind)
                and (not substring or substring in r.render())]
        return {"count": len(recs), "returned": len(recs[:limit]),
                "records": [{"index": i, "site": "%s:%d" % (r.file, r.pos),
                             **r.as_dict()} for i, r in recs[:limit]]}

    @mcp.tool(title="Describe one Cockpit record",
              description="One record in full, plus its Call list and whether "
                          "running it needs confirm=true. Select by global "
                          "`index` or by `site` (<file>:<pos>).")
    async def cockpit_describe_record(index: int | None = None,
                                      site: str | None = None
                                      ) -> dict[str, Any]:
        found = _resolve(index, site)
        if isinstance(found, dict):
            return found
        index, rec = found
        out = {"index": index, "site": "%s:%d" % (rec.file, rec.pos),
               **rec.as_dict()}
        if cbg is not None:
            try:
                info = _plan_info(record_to_calls(rec, None))
                out["would_call"] = info.pop("plan")
                out.update(info)
            except Exception as exc:   # noqa: BLE001
                out["would_call_error"] = str(exc)
        return out

    @mcp.tool(title="Perform one Cockpit record",
              description="Run the recipe for a record (select by global "
                          "`index` or `site` <file>:<pos>). Read-only plans "
                          "run directly; plans that may change the host are "
                          "only shown unless confirm=true (and the server is "
                          "--writable). dry_run=true always just shows the "
                          "plan. `at` fills the first {…} hole of the argv, a "
                          "variable file path, or an unknown D-Bus object "
                          "path; `extra`/`arguments` fill holes by name, then "
                          "in order. A list value fills one hole with several "
                          "argv elements; when the whole argv is one hole "
                          "(argv=[ {P} ]) a string is shell-split.")
    async def cockpit_invoke(index: int | None = None,
                             site: str | None = None,
                             arguments: dict[str, Any] | None = None,
                             extra: dict[str, Any] | None = None,
                             at: str | None = None,
                             dry_run: bool = False,
                             confirm: bool = False) -> dict[str, Any]:
        found = _resolve(index, site)
        if isinstance(found, dict):
            return found
        index, rec = found
        head = {"index": index, "site": "%s:%d" % (rec.file, rec.pos),
                "record": rec.render()}
        if cbg is None:
            return {"error": "cockpit_bridge not importable", **head}
        try:
            req = cbg.Request(dict(arguments or {}), dict(extra or {}), at)
            calls = record_to_calls(rec, req)
            if at:
                plain = cbg.Request(dict(arguments or {}), dict(extra or {}))
                if ([c.render() for c in record_to_calls(rec, plain)]
                        == [c.render() for c in calls]):
                    head["warning"] = ("`at` was not used: this record has "
                                       "no hole for it (to select a record "
                                       "by position use `site`)")
        except Exception as exc:   # noqa: BLE001
            return {"error": str(exc), **head}
        info = _plan_info(calls)
        if dry_run:
            return {"performed": False, "dry_run": True, **head, **info}
        if "holes" in info:
            return {"performed": False, **head, **info,
                    "error": "the plan still has unfilled holes: pass `at`, "
                             "`extra` or `arguments` for them"}
        if info["dangerous"]:
            if not NG_CONFIG.writable or NG_CONFIG.read_only:
                return {"performed": False, **head, **info,
                        "error": "this plan may change the host and the "
                                 "server is read-only (start it with "
                                 "--writable)"}
            if not confirm:
                return {"performed": False, **head, **info,
                        "error": "this plan may change the host: review it "
                                 "and call again with confirm=true"}
        try:
            result = perform_bridge(calls, NG_CONFIG.password or None)
            return {**head, "dangerous": info["dangerous"], **result}
        except (OSError, RuntimeError, ConnectionError, struct.error) as exc:
            if cbg.BRIDGE is not None:
                cbg.BRIDGE.close()
            return {"error": str(exc), "performed": False,
                    "backend": "bridge", **head}

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
    if args.writable:
        NG_CONFIG.read_only = False
        NG_CONFIG.writable = True
    if os.environ.get("COCKPIT_PASSWORD"):
        NG_CONFIG.password = os.environ["COCKPIT_PASSWORD"]
    # no getpass prompt here: the MCP stdio session owns stdin/stdout and a
    # host is optional — cockpit_server connects (and asks, if on a tty)
    if NG_CONFIG.bundles:
        with contextlib.suppress(RuntimeError, OSError):
            _ensure_records()
    if mcp is not None:
        mcp._mcp_server.instructions = _mcp_instructions()
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
