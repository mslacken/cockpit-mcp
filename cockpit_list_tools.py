#!/usr/bin/env python3
"""
cockpit_list_tools.py — list the capabilities of a *running* Cockpit server.

WHAT IT DOES
============
Cockpit's UI is a set of *packages*; each package ships a manifest.json that
declares its pages (menu/tools/dashboard entries, see doc/packages.md), and
may expose shell commands. That manifest is the coarse-grained "capabilities"
listing — but it says nothing about what a page actually lets you *do*: the
buttons you can press and the fields you can fill in.

So this script does two passes:

  1. the manifest pass — package → pages / commands,
  2. the page pass — for every page, fetch its HTML and the JavaScript bundle
     it loads, and recover the interactive controls (buttons, text inputs,
     checkboxes, selects, dialogs, forms) from them.

Both run by default; --no-controls stops after the manifest pass.

It talks to the server the way the browser does:

    1. GET /cockpit/login with Authorization: Basic base64(user:password\\0)
       (pkg/static/login.js) → session cookie.
    2. WebSocket /cockpit/socket, subprotocol cockpit1, + the "init" handshake
       (doc/protocol.md, "Command: init"). This is the channel any MCP wrapper
       needs before it can open dbus/stream/fsread channels — we verify it
       works, but don't fail the listing if the server rejects the handshake
       (e.g. an Origin allow-list) since the listing only needs the cookie.
    3. GET /cockpit/@<host>/manifests.json (src/cockpit/packages.py) → the
       pages and commands actually *installed* on that host.
    4. GET /cockpit/@<host>/<package>/<page>.html and the scripts it pulls in
       → the controls on that page.

HOW THE CONTROL INTROSPECTION WORKS
===================================
A Cockpit page is an empty <div> plus a bundle; the markup is built at runtime
by React, so there is nothing to see in the HTML (static markup is still read,
for the few pages — e.g. static/login.html — that have it). The bundle is
esbuild output: JSX has been compiled to createElement(Component, {props},
children) and, in a release build, every identifier is minified — but the
*string literals* and the *prop names* survive verbatim.

That is enough to recognise a control by its props: onClick makes a button,
onChange + value a text input, isChecked a checkbox, onSelect a select, and so
on. So we mask out strings/comments/regexes (so braces can be matched safely),
find each props object containing such an anchor prop, read its top-level
string props, and take the label from label/aria-label/title or from the first
string literal among the element's children — which is where a gettext call
like _("Change profile") puts it.

This is a heuristic, not a parser: it can miss a control whose handler is
spread in from a variable, and it can report one that is only rendered
conditionally. It is accurate enough to answer "what can I do on this page".

DEPENDENCIES
============
Python 3 stdlib only. No pip installs, and no local checkout of the source —
every piece of data comes from the server.

USAGE
=====
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py                 # localhost:9090, everything
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py myhost          # another host, port 9090
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py myhost:9091
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py https://myhost  # TLS
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --package systemd
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --json          # machine readable (for MCP)
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --no-controls   # manifest only, fast

    COCKPIT_CA=/path/ca.pem ./cockpit_list_tools.py https://myhost   # pin a CA
    ./cockpit_list_tools.py --insecure https://myhost                # skip verification

If COCKPIT_PASSWORD is unset you are prompted for it on the (tty) command line.
The default user is the current local user; override with --user/COCKPIT_USER.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import contextlib
import dataclasses
import getpass
import gzip
import html.parser
import http.client
import json
import os
import posixpath
import re
import socket
import ssl
import string
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator


def _envbool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


DEFAULT_TARGET = "localhost:9090"
DEFAULT_PORT = int(os.environ.get("COCKPIT_PORT", "9090"))
DEFAULT_TLS = _envbool("COCKPIT_TLS")

HOST = os.environ.get("COCKPIT_HOST", "localhost")
PORT = DEFAULT_PORT
USE_TLS = DEFAULT_TLS
INSECURE = _envbool("COCKPIT_TLS_INSECURE")
CA_CERT = os.environ.get("COCKPIT_CA") or os.environ.get("SSL_CERT_FILE")
TIMEOUT = int(os.environ.get("COCKPIT_TIMEOUT", "10"))
SCHEME = "wss" if USE_TLS else "ws"
# The name the server uses for *itself* in /cockpit/@<host>/ URLs. Defaults to
# the connection target; the WS "init" message (doc/protocol.md) reports the
# authoritative value. A server serving its own packages only recognises that
# name (e.g. "localhost") and 403's a foreign hostname (AllowMultiHost gate,
# src/ws/cockpitchannelresponse.c).
MANIFEST_HOST = HOST


def set_target(spec: str) -> None:
    """Point the module-level connection settings at ``spec``.

    Accepted: ``host``, ``host:port``, ``[::1]:9090``, ``https://host:port``.
    A scheme selects TLS; otherwise the COCKPIT_TLS default stays in effect.
    """
    global HOST, PORT, USE_TLS, SCHEME, MANIFEST_HOST

    rest = spec.strip()
    tls = DEFAULT_TLS
    if "://" in rest:
        scheme, _, rest = rest.partition("://")
        tls = scheme.lower() in ("https", "wss")
    rest = rest.split("/", 1)[0]

    port = DEFAULT_PORT
    if rest.startswith("["):                       # [::1] / [::1]:9090
        host, _, tail = rest[1:].partition("]")
        if tail.startswith(":"):
            port = int(tail[1:])
    elif rest.count(":") == 1:
        host, _, text = rest.partition(":")
        port = int(text)
    else:
        host = rest

    HOST, PORT, USE_TLS = host or "localhost", port, tls
    SCHEME = "wss" if USE_TLS else "ws"
    MANIFEST_HOST = HOST    # overwritten by the WS init's host (connect().)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ==========================================================================
# Cockpit client (login, WebSocket init, manifests.json, page assets)
# ==========================================================================

def _tls_context() -> ssl.SSLContext | None:
    """Return an ssl.SSLContext if TLS is wanted, else None."""
    if not USE_TLS:
        return None
    if not INSECURE:
        return ssl.create_default_context(cafile=CA_CERT)  # cafile=None → system store
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(path: str, headers: dict[str, str]) -> tuple[http.client.HTTPResponse, bytes]:
    """Small blocking HTTP client. Returns the response (for status/headers) and its body.

    Cockpit serves pre-compressed assets (packages.py keeps ``foo.js.gz`` on
    disk and answers a request for ``foo.js`` with Content-Encoding: gzip), and
    http.client does not decode that for us.
    """
    conn: http.client.HTTPConnection = (
        http.client.HTTPSConnection(HOST, PORT, timeout=TIMEOUT, context=_tls_context())
        if USE_TLS else http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT))
    try:
        conn.request("GET", path, headers={**headers, "Accept-Encoding": "gzip",
                                           "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read()
        if (resp.getheader("Content-Encoding") or "").lower() == "gzip" and body:
            with contextlib.suppress(OSError, EOFError):
                body = gzip.decompress(body)
        return resp, body
    finally:
        conn.close()


def do_login(user: str, password: str) -> str:
    """
    Log in and return the session cookie as "name=value", ready for a "Cookie:" header.

    The browser (pkg/static/login.js) does:
        Authorization: "Basic " + btoa(user + ":" + password + '\0' + known_hosts)
    known_hosts is empty for localhost; we mirror that exactly.
    """
    basic = base64.b64encode(f"{user}:{password}\0".encode()).decode("ascii")
    resp, _ = http_get("/cockpit/login", {
        "Authorization": f"Basic {basic}",
        # browser sends X-Superuser: none|require|try (login.js).
        "X-Superuser": os.environ.get("COCKPIT_SUPERUSER", "none"),
    })
    cookie = (resp.getheader("Set-Cookie") or "").split(";", 1)[0].strip()
    if resp.status != 200 or "=" not in cookie:
        raise RuntimeError(f"login failed: HTTP {resp.status}, headers: {resp.getheaders()}")
    return cookie


def fetch_manifests(cookie: str) -> dict[str, Any]:
    path = f"/cockpit/@{MANIFEST_HOST}/manifests.json"
    resp, body = http_get(path, {"Cookie": cookie})
    if resp.status != 200:
        raise RuntimeError(f"GET {path} failed: HTTP {resp.status}")
    return json.loads(body)


class AssetFetcher:
    """Fetches package files over HTTP, caching by path (bundles are shared)."""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self._cache: dict[str, str | None] = {}
        self.fetched = 0
        self.bytes = 0

    def text(self, path: str) -> str | None:
        """GET a package file as text. None if it is missing or unreadable."""
        if path in self._cache:
            return self._cache[path]
        body: str | None = None
        try:
            resp, raw = http_get(path, {"Cookie": self.cookie})
            if resp.status == 200:
                body = raw.decode("utf-8", "replace")
                self.fetched += 1
                self.bytes += len(raw)
            else:
                log(f"   (skip) GET {path}: HTTP {resp.status}")
        except (OSError, RuntimeError) as exc:
            log(f"   (skip) GET {path}: {exc}")
        self._cache[path] = body
        return body


# --------------------------------------------------------------------------
# WebSocket client (RFC 6455 subset — text frames only, no extensions)
# --------------------------------------------------------------------------

OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x8, 0x9, 0xA


def _mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


class WebSocket:
    """Minimal WebSocket connection: unfragmented frames, no extensions.

    Reads go through the socket's buffered reader. That matters for the very
    first message: the server usually sends its ``init`` frame in the same TCP
    segment as the 101 response, so those bytes are already in the buffer when
    the handshake head has been parsed. Reading from the raw socket instead
    would drop them, and the client would block until a server keepalive
    triggered a protocol-error close.
    """

    def __init__(self, sock: socket.socket, reader: BinaryIO) -> None:
        self.sock = sock
        self.reader = reader

    def _read(self, n: int) -> bytes:
        """Read exactly n bytes."""
        data = self.reader.read(n)
        if len(data) != n:
            raise ConnectionError("peer closed socket")
        return data

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """Send one unfragmented frame. Client→server frames MUST be masked (RFC 6455)."""
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        key = os.urandom(4)
        self.sock.sendall(head + key + _mask(payload, key))

    def recv_frame(self) -> tuple[int, bytes]:
        head = self._read(2)
        opcode = head[0] & 0x0F
        length: int = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        key = self._read(4) if head[1] & 0x80 else b""
        payload = self._read(length) if length else b""
        return opcode, _mask(payload, key) if key else payload

    def send_control(self, msg: dict[str, Any]) -> None:
        """Control channel = empty channel id prefix ("\\n" + json)."""
        self.send_frame(OP_TEXT, ("\n" + json.dumps(msg)).encode())

    def recv_control(self) -> dict[str, Any] | None:
        """Read the next *substantive* control message. None on clean close.

        Keepalives are answered and skipped: WebSocket pings (RFC 6455) and
        Cockpit "ping" control messages, which per doc/protocol.md "Command:
        ping" must be echoed back as an identical "pong" (cockpitwebservice.c:
        process_ping). The server sends one every ~5s (cockpit_ws_ping_interval)
        and it can arrive *before* init on a slow client.
        """
        while True:
            opcode, payload = self.recv_frame()
            if opcode == OP_CLOSE:
                return None
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode != OP_TEXT:  # pong, binary, ... — ignore
                continue
            text = payload.decode()
            if not text.startswith("\n"):
                raise RuntimeError(
                    f"unexpected payload frame before a control message: {text[:120]!r}…")
            msg: dict[str, Any] = json.loads(text[1:])
            if msg.get("command") == "ping":
                self.send_control({**msg, "command": "pong"})
            elif msg.get("command") != "pong":
                return msg

    def init_session(self) -> dict[str, Any]:
        """Run the init handshake. Returns the server's init message."""
        first = self.recv_control()
        if first is None or first.get("command") != "init":
            raise RuntimeError(f"expected 'init' as first message, got: {first}")
        self.send_control({"command": "init", "version": 1})
        return first

    def close(self) -> None:
        """Send a clean WS close frame if possible, then close the socket."""
        with contextlib.suppress(OSError):
            self.send_frame(OP_CLOSE)
        self.reader.close()
        self.sock.close()


def ws_connect(cookie: str) -> WebSocket:
    """Open a TCP(/TLS) socket, perform the WS handshake, return a WebSocket."""
    origin = f"{'https' if USE_TLS else 'http'}://{HOST}:{PORT}"
    request = (
        "GET /cockpit/socket HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Origin: {origin}\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: cockpit1\r\n"
        f"Cookie: {cookie}\r\n"
        "\r\n"
    ).encode("ascii")

    sock: socket.socket = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    ctx = _tls_context()
    if ctx is not None:
        sock = ctx.wrap_socket(sock, server_hostname=HOST)
    sock.sendall(request)

    # Read the response head line by line; whatever the server sent after it
    # stays in the reader's buffer (see WebSocket docstring).
    reader = sock.makefile("rb")
    head = [reader.readline().decode("latin-1").strip()]
    while line := reader.readline().strip():
        head.append(line.decode("latin-1"))
    if not head[0].startswith("HTTP/1.1 101"):
        hint = ""
        if "403" in head[0]:
            hint = ("\n   403 = Origin check failed (src/ws/websocketserver.c). The server "
                    "requires an 'Origin' header matching the connection, e.g. "
                    f"{origin!r}. A [WebService] Origins= entry can also extend the allowlist.")
        raise RuntimeError("WS handshake failed: {!r}{}\n---\n{}\n---".format(
            head[0], hint, "\n".join(head)))
    return WebSocket(sock, reader)


def connect() -> tuple[str, dict[str, Any]]:
    """Log in to the server; return the session cookie and its manifests.json."""
    global MANIFEST_HOST
    user = os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD") or getpass.getpass(
        f"Cockpit password for {user}@{HOST}:{PORT}: ")

    log(f"logging in as {user}@{HOST}:{PORT}  ({SCHEME}://…)")
    cookie = do_login(user, password)
    cookie_name, _, cookie_val = cookie.partition("=")
    log(f"   -> session cookie: {cookie_name}={cookie_val[:20]}…")

    # Verify the WebSocket endpoint + init handshake work (what an MCP wrapper
    # needs), but don't fail the listing over it — the listing only needs the
    # cookie, and some servers gate the socket on an Origin allow-list.
    log(f"verifying WebSocket {SCHEME}://{HOST}:{PORT}/cockpit/socket (subprotocol: cockpit1)")
    init_msg: dict[str, Any] = {}
    try:
        with contextlib.closing(ws_connect(cookie)) as ws:
            init_msg = ws.init_session()
        log(f"   session inited: host={init_msg.get('host')!r}  seed={init_msg.get('channel-seed')!r}")
    except (OSError, RuntimeError) as exc:
        log(f"   (warning) websocket init failed: {exc}")
        init_msg = {}

    # The init message carries the name *this* server uses for itself. Serve
    # our own package assets under that name — a foreign hostname (e.g. the
    # name we dialed by) would 403 under the AllowMultiHost gate, since the
    # server treats it as a peer we are not logged into.
    server_host = init_msg.get("host") or "localhost"
    MANIFEST_HOST = server_host
    log(f"fetching {SCHEME}://{HOST}:{PORT}/cockpit/@{MANIFEST_HOST}/manifests.json")
    return cookie, fetch_manifests(cookie)


# ==========================================================================
# The model: package → pages / commands → controls
# ==========================================================================

@dataclass
class Command:
    """A shell-exposed command a package declares in its manifest."""
    name: str
    description: str | None
    args: list[str] = field(default_factory=list)
    command: str | None = None

    def render(self) -> str:
        out = self.name
        if self.args:
            out += " " + " ".join(self.args)
        if self.description:
            out += f'  — {self.description}'
        if self.command:
            out += f"  → {self.command}"
        return out


@dataclass
class Control:
    """An interactive element of a page: a button, an input, a select, …"""
    kind: str                       # button / input / checkbox / select / dialog / …
    label: str | None = None
    id: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    source: str = ""                # the file it was recovered from

    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.label or "", self.id or "")

    def render(self, show_dialog: bool = True) -> str:
        out = f"{self.kind:<12} {'“' + self.label + '”' if self.label else '(no label)'}"
        if self.id:
            out += f"  #{self.id}"
        attrs = self.attrs if show_dialog else {k: v for k, v in self.attrs.items() if k != "dialog"}
        if attrs:
            out += "  " + " ".join(f"{k}={v}" for k, v in sorted(attrs.items()))
        return out


@dataclass
class Subpage:
    """A sub-view a page can switch to: a Cockpit dialog (e.g. a create-form
    or confirm box) or a route (e.g. an account's details).

    Cockpit pages are a single HTML file that hosts many such sub-views, so the
    buttons a sub-view offers do not all live on the "primary" page — creating
    a user and deleting one are separate sub-views. We recover them from the
    per-dialog ``id`` / ``title`` / ``actions`` markers and the field ids they
    own.
    """
    id: str                          # dialog id (e.g. "accounts-create-dialog")
    title: str | None = None         # the dialog's title
    actions: list[tuple[str, str]] = field(default_factory=list)  # (caption, style)
    controls: list[Control] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)              # owned field ids

    def render(self) -> str:
        out = f'subpage "{self.id}"'
        if self.title:
            out += f'  — {self.title}'
        if self.actions:
            out += "  actions: " + ", ".join(c for c, _ in self.actions)
        if self.fields:
            ids = ", ".join(self.fields)
            out += f"  fields: {ids[:60]}{'…' if len(ids) > 60 else ''}"
        return out


@dataclass
class Page:
    """A manifest entry (menu/tools/dashboard, or a package-private section)."""
    section: str
    key: str
    label: str
    path: str                      # the HTML file, relative to the package
    keywords: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)   # bundles that were scanned
    controls: list[Control] = field(default_factory=list)
    subpages: list[Subpage] = field(default_factory=list)

    def render(self) -> str:
        return f"[{self.section}] {self.label}  →  {self.path}"


@dataclass
class Package:
    name: str
    requires: dict[str, Any] = field(default_factory=dict)
    meta: str | None = None
    commands: list[Command] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)


# ==========================================================================
# Reading the manifests
# ==========================================================================

MANIFEST_SECTIONS = ("menu", "tools", "dashboard")


def _page_sections(manifest: dict[str, Any]) -> Iterator[str]:
    """menu/tools/dashboard, plus package-private sections of the same shape.

    pkg/playground/manifest.json declares its demo pages under a "playground"
    key that only its own index page reads; those are still pages.
    """
    yield from MANIFEST_SECTIONS
    for key, value in manifest.items():
        if key in MANIFEST_SECTIONS or not isinstance(value, dict) or not value:
            continue
        if all(isinstance(v, dict) and "label" in v for v in value.values()):
            yield key


def _manifest_pages(pkgname: str, manifest: dict[str, Any]) -> Iterator[Page]:
    """Yield a Page per manifest entry. doc/packages.md: menu/tools/dashboard."""
    for section in _page_sections(manifest):
        entries = manifest.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            entry = value if isinstance(value, dict) else {}
            keywords: list[str] = []
            for group in entry.get("keywords") or []:
                keywords += (group or {}).get("matches") or []
            yield Page(
                section=section,
                key=key,
                label=entry.get("label") or key,
                path=entry.get("path") or f"{key}.html",
                keywords=keywords,
                docs=[d.get("url", "") for d in (entry.get("docs") or []) if isinstance(d, dict)],
            )


def _manifest_commands(manifest: dict[str, Any]) -> list[Command]:
    """Yield the shell commands the package declares, if any."""
    out: list[Command] = []
    for item in manifest.get("commands") or []:
        if not isinstance(item, dict):
            continue
        out.append(Command(
            name=item.get("name") or "?",
            description=item.get("description"),
            args=[a for a in (item.get("args") or []) if a],
            command=item.get("command"),
        ))
    return out


def build_packages(manifests: dict[str, Any], only: list[str]) -> list[Package]:
    packages = []
    for name in sorted(manifests):
        if only and name not in only:
            continue
        manifest = manifests[name] or {}
        packages.append(Package(
            name=name,
            requires=manifest.get("requires") or {},
            meta=manifest.get("meta"),
            commands=_manifest_commands(manifest),
            pages=list(_manifest_pages(name, manifest)),
        ))
    return packages


# ==========================================================================
# Introspection, part 1: the page HTML
# ==========================================================================

HTML_CONTROL_TAGS = {"button", "input", "select", "textarea", "form"}


class PageParser(html.parser.HTMLParser):
    """Collects static controls and the scripts a page pulls in."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.controls: list[Control] = []
        self.scripts: list[str] = []
        self._pending: Control | None = None   # <button>…</button>: label is the text

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "script" and attr.get("src"):
            self.scripts.append(attr["src"])
            return
        if tag not in HTML_CONTROL_TAGS:
            return

        kind = tag
        if tag == "input":
            kind = f"input[{attr.get('type', 'text')}]"
        control = Control(
            kind=kind,
            label=attr.get("aria-label") or attr.get("title") or attr.get("value") or None,
            id=attr.get("id") or None,
            attrs={k: attr[k] for k in ("type", "name", "placeholder") if attr.get(k)},
            source=self.source,
        )
        self.controls.append(control)
        self._pending = control if tag == "button" else None

    def handle_endtag(self, tag: str) -> None:
        self._pending = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if self._pending is not None and text and not self._pending.label:
            self._pending.label = text


# ==========================================================================
# Introspection, part 2: the JavaScript bundle
# ==========================================================================
#
# esbuild output, minified or not: JSX has become createElement(C, {props},
# …children). Identifiers are renamed but prop names and string literals are
# not, so a control is recognised by the props it carries.

# Strings, comments and regex literals. Matching them lets us (a) blank them
# out so braces/parens can be counted, and (b) collect the literals that carry
# the user-visible labels.
_JS_TOKEN_RE = re.compile(r"""
      (?P<line>  //[^\n]* )
    | (?P<block> /\*.*?\*/ )
    | (?P<dq>    "(?:[^"\\\n]|\\.)*" )
    | (?P<sq>    '(?:[^'\\\n]|\\.)*' )
    | (?P<tpl>   `(?:[^`\\]|\\.)*` )
    | (?P<rx>    /(?![/*])(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\]){1,200}/[a-z]* )
""", re.X | re.S)

# A '/' right after one of these is a division operator, not a regex…
_DIV_AFTER = set(")]}\"'`") | set(string.ascii_letters + string.digits + "_$")
# …unless the identifier before it is actually a keyword ("return /x/").
_KEYWORD_BEFORE_RE = re.compile(
    r"\b(return|typeof|instanceof|in|of|new|delete|void|throw|case|do|else|yield|await)$")

_PROP_KEY_RE = re.compile(r'(?:([A-Za-z_$][\w$-]*)|"([^"]*)")\s*:')
_IDENT_RE = re.compile(r"[\w$]+")
_CALL_RE = re.compile(r"\s*(?:[\w$.]+\()?")


def _before(text: str, pos: int) -> tuple[str, int]:
    """Last non-space character before ``pos``, and its index (-1 if none)."""
    i = pos - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return (text[i] if i >= 0 else ""), i


def _after(text: str, pos: int) -> str:
    """First non-space character at or after ``pos``, '' at end of input."""
    i = pos
    while i < len(text) and text[i].isspace():
        i += 1
    return text[i] if i < len(text) else ""


def _unquote(literal: str) -> str:
    """Best-effort decode of a JS string literal."""
    body = literal[1:-1]
    try:
        return json.loads('"' + body.replace('"', '\\"').replace("\\'", "'") + '"')
    except ValueError:
        return body


class JSSource:
    """A JS file with its strings/comments/regexes masked out.

    ``masked`` has exactly the same length as the original text, which keeps
    every offset usable in both; brace and paren counting over ``masked`` can
    therefore not be thrown off by a '{' inside a string.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.strings: list[tuple[int, int, str]] = []   # (start, end, value), in order

        pieces: list[str] = []
        pos = cursor = 0
        while (match := _JS_TOKEN_RE.search(text, cursor)) is not None:
            start, end = match.span()
            kind = match.lastgroup
            if kind == "rx":
                char, at = _before(text, start)
                if char in _DIV_AFTER and not _KEYWORD_BEFORE_RE.search(text, max(0, at - 11), at + 1):
                    # a division, not a regex: resume *inside* the false match,
                    # or the strings it spans would never get masked
                    cursor = start + 1
                    continue
            token = match.group()
            if kind in ("dq", "sq"):
                self.strings.append((start, end, _unquote(token)))
                filler = token[0] + "\0" * (len(token) - 2) + token[-1]
            else:
                filler = " " * len(token)
            pieces.append(text[pos:start])
            pieces.append(filler)
            pos = cursor = end
        pieces.append(text[pos:])
        self.masked = "".join(pieces)
        self._starts = [start for start, _, _ in self.strings]   # for bisect

    def enclosing_object(self, pos: int, window: int = 4000) -> tuple[int, int] | None:
        """Span of the innermost {...} containing ``pos``, or None if unbalanced."""
        masked, depth = self.masked, 0
        start = -1
        for i in range(pos, max(0, pos - window) - 1, -1):
            char = masked[i]
            if char == "}":
                depth += 1
            elif char == "{":
                if depth == 0:
                    start = i
                    break
                depth -= 1
        if start < 0:
            return None
        depth = 0
        for i in range(start, min(len(masked), start + 4 * window)):
            char = masked[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        return None

    def props(self, start: int, end: int) -> dict[str, str | None]:
        """Top-level keys of the object literal at [start, end).

        The value is the decoded string for string literals, else None — we
        only care *that* e.g. onClick is present, not what it computes.
        """
        masked = self.masked
        out: dict[str, str | None] = {}
        i, depth = start + 1, 0
        while i < end - 1:
            char = masked[i]
            if char in "{[(":
                depth += 1
            elif char in "}])":
                depth -= 1
            elif depth == 0 and (char.isalpha() or char in "_$\""):
                match = _PROP_KEY_RE.match(masked, i, end)
                if match:
                    name = match.group(1) or self.text[match.start(2):match.end(2)]
                    out[name] = self._string_at(match.end())
                    i = match.end()
                    continue
                # not a key — skip the whole identifier so we don't rescan it
                ident = _IDENT_RE.match(masked, i, end)
                if ident:
                    i = ident.end()
                    continue
            i += 1
        return out

    def _string_at(self, pos: int) -> str | None:
        """The string literal at ``pos``, if any.

        A translated prop is a call — ``label:_("User name")`` — so a single
        leading ``ident(`` is stepped over; an arrow function (``(a,b)=>…``)
        has no identifier in front of its '(' and is left alone.
        """
        skip = _CALL_RE.match(self.masked, pos)
        pos = skip.end() if skip else pos
        index = bisect.bisect_left(self._starts, pos)
        if index < len(self.strings) and self._starts[index] == pos:
            return self.strings[index][2]
        return None

    def child_string(self, start: int, finish: int) -> str | None:
        """First plausible label literal in [start, finish) — the children.

        What surrounds the literal says what it is: a prop value follows a ':',
        and the tag of ``createElement("div", {…})`` is a call's first argument,
        so it is both preceded by '(' and followed by ','. A gettext call —
        ``_("Change profile")`` — is preceded by '(' and followed by ')'.
        """
        for pos, end, value in self.strings[bisect.bisect_left(self._starts, start):]:
            if pos >= finish:
                break
            if not _is_label(value):
                continue
            head, _ = _before(self.masked, pos)
            if head == ":":
                continue
            if head == "(" and _after(self.masked, end) == ",":
                continue
            return value
        return None


def _is_label(value: str) -> bool:
    """Does this string literal look like something a user would read?

    Bundles are full of strings that are not labels: tag names, CSS classes,
    prop names, PatternFly component names. Cockpit's own labels are
    translated sentence-case text, which is what this keeps.
    """
    text = value.strip()
    if not 1 < len(text) <= 80 or not text[0].isalnum():
        return False
    if re.match(r"^(pf-|ct-|fa-|https?:|/|\./|\.\./)", text):
        return False
    if " " in text:
        return True
    # a single word: sentence case ("Reboot"), not an identifier ("className",
    # "MenuToggle"), a class/file name ("foo-bar", "x.js") or a tag ("div")
    return text[0].isupper() and not re.search(r"[a-z][A-Z]|[-_./]", text)


# Unminified builds keep the component name; it beats guessing from props.
PF_COMPONENTS = {
    "Button": "button", "TextInput": "input", "TextArea": "textarea",
    "Checkbox": "checkbox", "Switch": "switch", "Radio": "radio",
    "Select": "select", "FormSelect": "select", "SimpleSelect": "select",
    "TypeaheadSelect": "select", "NumberInput": "input", "SearchInput": "input",
    "DatePicker": "input", "TimePicker": "input", "Slider": "slider",
    "FileUpload": "input[file]", "ClipboardCopy": "input",
    "Modal": "dialog", "Dropdown": "menu", "DropdownItem": "menu-item",
    "MenuItem": "menu-item", "SelectOption": "option", "Form": "form",
    "ToggleGroupItem": "toggle", "Tab": "tab",
}

# Props that mark an object literal as (probably) describing a control.
ANCHOR_PROPS = ("onClick", "onChange", "onSelect", "onToggle", "onSubmit",
                "onConfirm", "isChecked", "placeholder")
_ANCHOR_RE = re.compile(r"\b(" + "|".join(ANCHOR_PROPS) + r")\s*:")

_CREATE_ELEMENT_RE = re.compile(r"(?:createElement|jsxs?|h)\s*\(\s*([\w$.]+)\s*,\s*$")
INPUT_TYPES = ("text", "password", "email", "number", "search", "tel", "url",
               "date", "time", "file", "range")

LABEL_PROPS = ("label", "aria-label", "title", "text", "content", "placeholder", "name")
# Kinds whose label is their child text; an <input> has none, so a literal
# found after it belongs to whatever is rendered next.
LABEL_FROM_CHILDREN = ("button", "menu-item", "option", "tab", "toggle", "link")


def _classify(props: dict[str, str | None], component: str | None) -> str | None:
    """Name the kind of control an element with these props is, or None."""
    if component in PF_COMPONENTS:
        return PF_COMPONENTS[component]

    kind_of_type = props.get("type")
    if "isChecked" in props or kind_of_type == "checkbox":
        return "checkbox"
    if kind_of_type == "radio":
        return "radio"
    if "onSubmit" in props:
        return "form"
    if "onSelect" in props or "selections" in props or "onSelectionChange" in props:
        return "select"
    if "onConfirm" in props or ("isOpen" in props and "onClose" in props):
        return "dialog"
    if "onClick" in props:
        return "button"
    if "onChange" in props or "placeholder" in props:
        if kind_of_type in INPUT_TYPES:
            return f"input[{kind_of_type}]"
        if {"value", "placeholder", "isRequired", "validated"} & props.keys():
            return "input"
        return "input" if "onChange" in props else None
    if "onToggle" in props:
        return "toggle"
    return None


_FIELD_ID_RE = re.compile(r"\b(?:fieldId|htmlFor)\s*:")

# ===========================================================================
# Sub-page discovery: Cockpit dialogs and their owned fields
# ===========================================================================
#
# A Cockpit page hosts many sub-views. "Dialog" is the canonical sub-view:
# ``cockpitDialog({id:"…-dialog", title:"…", body:…}, {actions:[{caption,
# style, clicked}], …})``. In a minified bundle the dialog's id/title still
# survive (they are string literals), as do the ``caption`` / ``style`` of
# each action. Cockpit pages also follow a strong naming convention that the
# dialog's fields share the dialog's id minus the trailing "-dialog" (e.g.
# dialog ``accounts-create-dialog`` owns ``accounts-create-real-name`` and
# ``accounts-create-force-password-change``). That lets us attribute the flat
# controls we already recover to a specific sub-page instead of lumping them
# all under "the page".

_DIALOG_ID_RE = re.compile(r'\bid\s*:\s*"([A-Za-z0-9_\-./]*dialog[A-Za-z0-9_\-./]*)",')
_FIELD_ID_RE_2 = re.compile(r'\bid\s*:\s*"([A-Za-z0-9_\-./]+)"')
_TITLE_LIT_RE = re.compile(r'\btitle\s*:\s*(?:[\w.]+\(\s*)?"([^"]+)"')
_ACTION_CAPTION_RE = re.compile(r'\bcaption\s*:\s*(?:[\w.]+\()?\s*"([^"]+)"')
_ACTION_STYLE_RE = re.compile(r'\bstyle\s*:\s*"?([A-Za-z]+)"?')


@dataclass
class _DialogDecl:
    id: str
    title: str | None = None
    actions: list[tuple[str, str]] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        """The name a field id shares with this dialog: the id minus ``-dialog``."""
        return (self.id[:-7] if self.id.endswith("-dialog")
                else (self.id[:-6] if self.id.endswith("dialog") else self.id)) or self.id


def _actions_arrays(js: JSSource) -> list[tuple[int, int]]:
    """Balanced spans of every ``actions: [ … ]`` array in the bundle."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(r"\bactions\s*:\s*\[", js.masked):
        open_i = m.end() - 1
        depth = 0
        i = open_i
        while i < len(js.masked):
            c = js.masked[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    out.append((open_i, i))
                    break
            i += 1
    return out


def _parse_actions(js: JSSource, span: tuple[int, int] | None) -> list[tuple[str, str]]:
    """(caption, style) pairs from inside one ``actions:[…]`` array."""
    if span is None:
        return []
    lo, hi = span
    text = js.text[lo:hi]
    masked = js.masked[lo:hi]
    actions: list[tuple[str, str]] = []
    seen: set[str] = set()
    # split the array into its top-level {…} objects (depth over masked text)
    objs: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, c in enumerate(masked):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append((start, i))
                start = -1
    for os_, oe in objs:
        otext = text[os_:oe + 1]
        capm = _ACTION_CAPTION_RE.search(otext)
        if not capm:
            continue
        cap = capm.group(1)
        if not cap or cap in seen:
            continue
        seen.add(cap)
        stm = _ACTION_STYLE_RE.search(otext)
        actions.append((cap, stm.group(1) if stm else ""))
    return actions


def _discover_dialogs(js: JSSource) -> dict[str, _DialogDecl]:
    """Recover the dialogs a bundle declares: id, title, actions.

    Each dialog's footer (``{actions:[…], …}``) is a separate literal that sits
    just after its declaration and before the next dialog's, so in source order
    a dialog *owns* the ``actions:[…]`` arrays that fall between its id and the
    next id.
    """
    arrays = _actions_arrays(js)
    positions: list[tuple[int, str, str | None]] = []
    for m in _DIALOG_ID_RE.finditer(js.text):
        span = js.enclosing_object(m.start())
        lo = span[0] if span else max(0, m.start() - 200)
        hi = span[1] if span else min(len(js.text), m.end() + 200)
        title = None
        tm = _TITLE_LIT_RE.search(js.text[lo:hi])
        if tm:
            title = tm.group(1)
        positions.append((m.start(), m.group(1), title))
    positions.sort()

    decls: dict[str, _DialogDecl] = {}
    for i, (pos, did, title) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(js.text)
        # arrays owned by this declaration: their opening bracket starts within
        # (pos, end); take the first such array as this dialog's footer.
        owned = [span for span in arrays if pos < span[0] < end]
        actions = _parse_actions(js, owned[0] if owned else None)
        decl = _DialogDecl(id=did, title=title, actions=actions)
        old = decls.get(did)
        if old is None or len(decl.actions) > len(old.actions):
            decls[did] = decl
    return decls


def _owning_dialog(field_id: str, decls: dict[str, _DialogDecl]) -> str | None:
    """Longest dialog whose prefix (id minus ``-dialog``) is a prefix of field_id."""
    if not field_id or field_id.endswith("dialog"):
        return None
    best: tuple[int, str] = (-1, "")
    for did, decl in decls.items():
        p = decl.prefix
        if p and (field_id == p or field_id.startswith(p + "-") or field_id.startswith(p + "_")):
            if len(p) > best[0]:
                best = (len(p), did)
    return best[1] or None


def _attribute(page_controls: list[Control],
              field_ids: list[str],
              decls: dict[str, _DialogDecl]
              ) -> tuple[list[Control], list[Subpage]]:
    """Split the flat control/field set into page controls and per-dialog sub-pages."""
    subs: dict[str, Subpage] = {did: Subpage(id=did, title=d.title, actions=list(d.actions))
                                for did, d in decls.items()}
    for cid in field_ids:
        owner = _owning_dialog(cid, decls)
        if owner and owner in subs:
            if cid not in subs[owner].fields:
                subs[owner].fields.append(cid)
    attributed: set[tuple[str, str, str]] = set()
    for control in page_controls:
        owner = _owning_dialog(control.id or "", decls) if control.id else None
        if owner and owner in subs:
            control.attrs["dialog"] = owner
            subs[owner].controls.append(control)
            attributed.add(control.key())
    # synthesise action buttons that were not picked up as plain controls
    for sub in subs.values():
        existing = {(c.label or "", c.id or "") for c in sub.controls}
        for caption, style in sub.actions:
            if (caption, "") in existing:
                continue
            sub.controls.append(Control(
                kind="button",
                label=caption,
                attrs={k: v for k, v in {"style": style, "dialog": sub.id}.items() if v},
            ))
    # page controls = those not attributed to any dialog
    left = [ctrl for ctrl in page_controls if ctrl.key() not in attributed]
    return left, sorted(subs.values(), key=lambda s: s.id)


def _field_labels(js: JSSource) -> dict[str, str]:
    """id → label, from the <FormGroup label=… fieldId=…> wrappers.

    An <input> carries no text of its own; in a Cockpit form its caption lives
    on the group around it, keyed by the id of the field it labels.
    """
    labels: dict[str, str] = {}
    for match in _FIELD_ID_RE.finditer(js.masked):
        span = js.enclosing_object(match.start())
        if span is None:
            continue
        props = js.props(*span)
        field_id = props.get("fieldId") or props.get("htmlFor")
        if field_id and props.get("label"):
            labels.setdefault(field_id, props["label"] or "")
    return labels


def scan_js(text: str, source: str) -> tuple[list[Control], list[Subpage]]:
    """Recover the controls a bundle renders, and the subpages it embeds.

    See the module docstring for the flat-control pass; the sub-page pass walks
    every ``{id:"…-dialog", …}`` object in the bundle and attributes the flat
    controls to the dialog whose prefix matches (e.g. ``accounts-create-*``
    fields belong to ``accounts-create-dialog``).
    """
    js = JSSource(text)
    field_labels = _field_labels(js)
    found: dict[tuple[str, str, str], Control] = {}
    field_ids: list[str] = []

    for anchor in _ANCHOR_RE.finditer(js.masked):
        span = js.enclosing_object(anchor.start())
        if span is None:
            continue
        start, end = span
        props = js.props(start, end)
        if anchor.group(1) not in props:        # the anchor sat in a nested object
            continue

        element = _CREATE_ELEMENT_RE.search(js.masked, max(0, start - 60), start)
        component = element.group(1).rpartition(".")[2] if element else None
        kind = _classify(props, component if component and len(component) > 2 else None)
        if kind is None:
            continue

        label = next((props[p] for p in LABEL_PROPS
                      if props.get(p) and not re.match(r"^(pf-|ct-|fa-)", props[p] or "")), None)
        if label is None and props.get("id"):
            label = field_labels.get(props["id"] or "")
        if label is None and kind in LABEL_FROM_CHILDREN:
            # children of createElement(C, {…}, HERE) — where a label literal lives
            label = js.child_string(end, min(len(js.text), end + 400))

        control = Control(
            kind=kind,
            label=label,
            id=props.get("id"),
            attrs={k: props[k] for k in ("variant", "type", "name") if props.get(k)},  # type: ignore[misc]
            source=source,
        )
        found.setdefault(control.key(), control)
        if control.id and control.id not in field_ids:
            field_ids.append(control.id)

    # pick up fieldIds / htmlFor / id attributes that never triggered an anchor
    # (form-group captions etc.) so the ownership pass can see them.
    for field_id in field_labels:
        if field_id not in field_ids:
            field_ids.append(field_id)
    for m in _FIELD_ID_RE_2.finditer(js.text):
        iid = m.group(1)
        if iid not in field_ids:
            field_ids.append(iid)

    page_controls = sorted(found.values(), key=Control.key)
    decls = _discover_dialogs(js)
    return _attribute(page_controls, field_ids, decls)


# ==========================================================================
# Introspection: tying it together
# ==========================================================================

# Runtime/i18n plumbing every page loads; no UI of its own.
SKIP_SCRIPT_RE = re.compile(r"(^|/)(po(\.manifest)?(\.[\w-]+)?\.js|manifests\.js|cockpit\.js)$")


def introspect_page(pkg: str, page: Page, assets: AssetFetcher) -> None:
    """Fill in page.controls / page.scripts / page.subpages from the HTML + bundles."""
    relative = page.path.split("#", 1)[0].split("?", 1)[0]
    if not relative or relative.startswith(("http:", "https:", "//")):
        return
    base = posixpath.normpath(posixpath.join(f"/cockpit/@{MANIFEST_HOST}/{pkg}/", relative))

    body = assets.text(base)
    if body is None:
        return
    parser = PageParser(source=relative)
    parser.feed(body)
    controls: list[Control] = list(parser.controls)
    subpages: list[Subpage] = []

    for src in parser.scripts:
        if src.startswith(("http:", "https:", "//")) or not src.endswith(".js"):
            continue
        url = posixpath.normpath(posixpath.join(posixpath.dirname(base) + "/", src))
        if SKIP_SCRIPT_RE.search(url):
            continue
        script = assets.text(url)
        if script is None:
            continue
        page.scripts.append(posixpath.basename(url))
        page_controls, script_subpages = scan_js(script, posixpath.basename(url))
        controls += page_controls
        subpages += script_subpages

    seen: set[tuple[str, str, str]] = set()
    for control in controls:
        if control.key() not in seen:
            seen.add(control.key())
            page.controls.append(control)
    page.controls.sort(key=Control.key)

    # Merge subpages by id (same dialog may be declared across several bundles).
    by_id: dict[str, Subpage] = {}
    for sub in subpages:
        existing = by_id.get(sub.id)
        if existing is None:
            by_id[sub.id] = sub
            continue
        if sub.title and not existing.title:
            existing.title = sub.title
        for pair in sub.actions:
            if pair not in existing.actions:
                existing.actions.append(pair)
        have = {c.key() for c in existing.controls}
        for c in sub.controls:
            if c.key() not in have:
                existing.controls.append(c)
        for f in sub.fields:
            if f not in existing.fields:
                existing.fields.append(f)
    page.subpages = sorted(by_id.values(), key=lambda s: s.id)


def introspect(packages: list[Package], cookie: str) -> None:
    assets = AssetFetcher(cookie)
    total = sum(len(p.pages) for p in packages)
    log(f"introspecting {total} page(s) for buttons and inputs")
    done = 0
    for pkg in packages:
        for page in pkg.pages:
            done += 1
            introspect_page(pkg.name, page, assets)
            log(f"   [{done}/{total}] {pkg.name}/{page.path}: {len(page.controls)} control(s)")
    log(f"   fetched {assets.fetched} file(s), {assets.bytes / 1024:.0f} KiB")


# ==========================================================================
# Output
# ==========================================================================

@dataclass
class Node:
    text: str
    children: list["Node"] = field(default_factory=list)

    def add(self, text: str, children: list["Node"] | None = None) -> "Node":
        node = Node(text, children or [])
        self.children.append(node)
        return node


def render_tree(node: Node, out=sys.stdout) -> None:
    print(node.text, file=out)
    _render_children(node, "", out)


def _render_children(node: Node, prefix: str, out) -> None:
    last = len(node.children) - 1
    for i, child in enumerate(node.children):
        stem = "└── " if i == last else "├── "
        cont = "    " if i == last else "│   "
        print(prefix + stem + child.text, file=out)
        _render_children(child, prefix + cont, out)


def build_tree(packages: list[Package], title: str) -> Node:
    root = Node(title)
    for pkg in packages:
        head = pkg.name
        if pkg.requires:
            head += f"   requires {pkg.requires}"
        if pkg.meta:
            head += f"   ({pkg.meta})"
        pnode = root.add(head)
        if pkg.commands:
            cnode = pnode.add(f"commands ({len(pkg.commands)})")
            for cmd in pkg.commands:
                cnode.add(cmd.render())
        if pkg.pages:
            gnode = pnode.add(f"pages ({len(pkg.pages)})")
            for page in pkg.pages:
                node = gnode.add(page.render())
                if page.keywords:
                    node.add("keywords: " + ", ".join(sorted(set(page.keywords))))
                if page.docs:
                    node.add("docs: " + ", ".join(page.docs))
                if page.controls:
                    label = f"controls ({len(page.controls)})"
                    if page.scripts:
                        label += "  from " + ", ".join(page.scripts)
                    knode = node.add(label)
                    for control in page.controls:
                        knode.add(control.render())
                if page.subpages:
                    snode = node.add(f"subpages ({len(page.subpages)})")
                    for sub in page.subpages:
                        subnode = snode.add(sub.render())
                        if sub.fields:
                            subnode.add("fields: " + ", ".join(sub.fields))
                        for control in sub.controls:
                            subnode.add(control.render(show_dialog=False))
        if not pkg.pages and not pkg.commands:
            pnode.add("(no menu/tools/dashboard/commands entries)")
    return root


def to_json(packages: list[Package]) -> str:
    """Same tree as the ASCII output, as JSON — the shape an MCP server would serve."""
    return json.dumps([dataclasses.asdict(p) for p in packages], indent=2)


def summarise(packages: list[Package]) -> str:
    pages = sum(len(p.pages) for p in packages)
    commands = sum(len(p.commands) for p in packages)
    controls = sum(len(page.controls) for p in packages for page in p.pages)
    return f"{len(packages)} package(s), {pages} page(s), {commands} command(s), {controls} control(s)"


# ==========================================================================
# Main
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    global INSECURE

    parser = argparse.ArgumentParser(
        description="List the capabilities (pages, commands, buttons and inputs) "
                    "of a running Cockpit server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment: COCKPIT_HOST/PORT/USER/PASSWORD/TLS/TLS_INSECURE/CA/SUPERUSER/TIMEOUT")
    parser.add_argument("target", nargs="?", default=None,
                        metavar="[SCHEME://]HOST[:PORT]",
                        help=f"the Cockpit server to query (default: {DEFAULT_TARGET})")
    parser.add_argument("--package", metavar="NAME", action="append", default=[],
                        help="restrict to this package (repeatable)")
    parser.add_argument("--user", metavar="NAME", help="log in as this user")
    parser.add_argument("--no-controls", dest="controls", action="store_false",
                        help="list manifests only; skip the per-page button/input scan")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the server's TLS certificate")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a tree")
    args = parser.parse_args(argv)

    set_target(args.target or os.environ.get("COCKPIT_HOST") or DEFAULT_TARGET)
    if args.insecure:
        INSECURE = True
    if args.user:
        os.environ["COCKPIT_USER"] = args.user

    cookie, manifests = connect()
    packages = build_packages(manifests, args.package)
    if args.controls:
        introspect(packages, cookie)

    title = f"{SCHEME}://{HOST}:{PORT}"
    if args.json:
        print(to_json(packages))
    else:
        render_tree(build_tree(packages, title))
        print()
    log(summarise(packages))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("\ninterrupted")
        raise SystemExit(130) from None
    except (OSError, RuntimeError) as exc:
        log(f"error: {exc}")
        raise SystemExit(2) from None
