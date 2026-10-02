#!/usr/bin/env python3
"""
cockpit_list_tools.py — list the capabilities of a *running* Cockpit server.

WHAT IT DOES
============
Cockpit's UI is a set of *packages*; each package ships a manifest.json that
declares its pages (menu/tools/dashboard entries, see
doc/modules/guide/pages/packages.adoc), and
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

Beyond the label, each control gets a best-effort *purpose hint*: the action
verb its id encodes ("network-interface-delete" → delete), combined with the
first plausible gettext string in the few hundred characters after the element
(confirmation captions, aria-labels, dialog titles). It is "the neighbourhood
says it does X", not the handler's actual semantics — the handler itself is
minified away.

Dialogs are recovered three times over, because Cockpit has three generations
of dialog in tree and each leaves a different trace in the bundle:

  * markup — a modal whose props carry an id ({id:"accounts-create-dialog",
    …}); its fields are the control ids sharing that id's prefix. Its DOM id
    is the one it declares.
  * spec — a *description* of the dialog passed to dialog_open({Title,
    Fields:[TextInput("name", _("Name"), {…}), …], Action}), as
    pkg/storaged/dialog.jsx does. The fields are listed in the spec itself,
    tag and label included; the field constructors are renamed by minification
    but each one renders exactly one data-field-type, so reading those literals
    back recovers what kind of input a tag really is. Every such dialog lives
    at #dialog and its fields at [data-field="<tag>"].
  * state — pkg/lib/cockpit/dialog.tsx, the current idiom: one state object
    with a handle per input (field={dlg.field("ssid")}), and framework-stamped
    buttons marked "dialog-apply" / "dialog-cancel" / "dialog-error-message".
    The handles name the fields, the markers name the buttons. Older builds
    stamp the markers as a plain id, current ones as an OUIA component id;
    both are recognised and the selectors follow whichever is in use.

Where the field lists a fixed set of values — choices:[{value, title}] in a
spec, options:[{value, label}] in the state framework — those are recovered
too, so a select becomes an enum rather than an open string.

A dialog opened by Dialogs.show(<SomeDialog/>) also names its own opener: the
component is the only thing linking the button to the dialog once the handler
is minified, so that button is reported as opening it rather than as an action
of its own.

This is a heuristic, not a parser: it can miss a control whose handler is
spread in from a variable, and it can report one that is only rendered
conditionally. It is accurate enough to answer "what can I do on this page".

THE --tools VIEW
================
The tree above is a *map* of the UI: everything that is there, in the shape it
is in. --tools answers the other question — "which actions can I invoke, and
what do I have to pass them?" — by re-reading the same findings as a flat list
of tools, each with a name, a description and its input values (an MCP tool
definition: name / description / inputSchema, which --tools --json emits
verbatim, with the Cockpit coordinates of the dialog and controls alongside).

The mapping is:

  * a dialog is an action with inputs — its action captions ("Create",
    "Delete") say what performing it means, its fields are the inputs, its
    title describes it. A dialog with two captions is two tools.
  * a page-level button, menu item or switch is an action without inputs.
  * a manifest command is a tool whose inputs are its declared args.

Controls that only move around the UI are not actions and are left out, as is
a dialog's "Cancel". Since the scan attaches to a button whatever plausible
caption is near it in the bundle, a page-level control additionally has to
*name* an action — an id like "network-bond-delete", or a caption beginning
with a verb — or it is dropped as scenery. Input types come from the control
(checkbox → boolean, NumberInput → integer, everything else a string), and a
select whose options were recovered carries them as a JSON Schema enum.

--tools prints that list as a *tree* — package → page → tool — with each tool
on one line as its call signature, ``name(arg, required_arg*)``, and its title,
dialog, inputs and selectors folded underneath. That is the shape to review a
whole catalog in; --tools --flat prints each tool in full instead, and
--tools --json emits the MCP definitions. They are *mock* tools throughout:
definitions recovered from the UI, with the coordinates a driver would need.
Nothing in this script invokes anything.

Each tool also carries the selectors a caller needs to drive it — the dialog,
the button that opens it, the one that confirms it, the one that abandons it,
and one per input. They are the same selectors Cockpit's own browser tests use
(test/common/dialoglib.py, test/common/storagelib.py), which is what makes them
worth trusting: a dialog with two confirm buttons ("Create snapshot and mount"
/ "Create snapshot only") is told apart by position the way the tests do it.

The same caveat applies, one level up: a tool here is an action the UI offers,
not a verified API call.

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
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --tools         # tree of mock tools
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --tools --flat  # one tool at a time, in full
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --tools --json  # MCP tool definitions

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
import textwrap
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
    hint: str | None = None         # best-effort "what this roughly does" (see module docstring)
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, caption) of a select
    conditional: bool = False       # only shown for some values of the other fields
    selector: str | None = None     # CSS selector that reaches it, when the idiom pins one down
    opens: str | None = None        # for a button: the dialog component it shows, if resolvable

    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.label or "", self.id or "")

    def render(self, show_dialog: bool = True) -> str:
        out = f"{self.kind:<12} {'“' + self.label + '”' if self.label else '(no label)'}"
        if self.id:
            out += f"  #{self.id}"
        attrs = self.attrs if show_dialog else {k: v for k, v in self.attrs.items() if k != "dialog"}
        if attrs:
            out += "  " + " ".join(f"{k}={v}" for k, v in sorted(attrs.items()))
        if self.choices:
            vals = ", ".join(v for v, _ in self.choices)
            out += f"  one of: {vals[:60]}{'…' if len(vals) > 60 else ''}"
        if self.conditional:
            out += "  (conditional)"
        if self.opens:
            out += "  opens a dialog"
        if self.hint:
            out += f"  → {self.hint}"
        return out


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_")


def _humanise(text: str) -> str:
    """"network-bond-delete" → "Network bond delete" — a title for an unlabelled control."""
    words = [w for w in re.split(r"[-_./\s]+", text) if w]
    return " ".join(words).capitalize() if words else text


def _dialog_prefix(dialog_id: str) -> str:
    """The name a dialog's field ids share with it: its id minus ``-dialog``."""
    return (dialog_id[:-7] if dialog_id.endswith("-dialog")
            else (dialog_id[:-6] if dialog_id.endswith("dialog") else dialog_id)) or dialog_id


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
    field_labels: dict[str, str] = field(default_factory=dict)   # field id → form-group caption
    framework: str = "markup"        # markup / spec / state — which idiom built it
    dom_id: str | None = None        # the id the dialog really carries in the DOM
    selectors: dict[str, str] = field(default_factory=dict)      # how a driver reaches it
    component: str | None = None     # the (minified) component a button shows to open it

    @property
    def prefix(self) -> str:
        """The name this dialog's own field ids start with (its id minus ``-dialog``)."""
        return _dialog_prefix(self.id)

    def render(self) -> str:
        out = f'subpage "{self.id}" [{self.framework}]'
        if self.title:
            out += f'  — {self.title}'
        if self.actions:
            out += "  actions: " + ", ".join(c for c, _ in self.actions)
        if self.fields:
            ids = ", ".join(self.fields)
            out += f"  fields: {ids[:60]}{'…' if len(ids) > 60 else ''}"
        if self.selectors.get("dialog"):
            out += f"  at {self.selectors['dialog']}"
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
        extra = {k: attr[k] for k in ("type", "name", "placeholder") if attr.get(k)}
        if "required" in attr:
            extra["required"] = "true"
        control = Control(
            kind=kind,
            label=attr.get("aria-label") or attr.get("title") or attr.get("value") or None,
            id=attr.get("id") or None,
            attrs=extra,
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
# "isRequired: true", or its minified spelling "isRequired:!0".
_IS_REQUIRED_RE = re.compile(r"\bisRequired\s*:\s*(?:!0\b|true\b)")
INPUT_TYPES = ("text", "password", "email", "number", "search", "tel", "url",
               "date", "time", "file", "range")

LABEL_PROPS = ("label", "aria-label", "title", "text", "content", "placeholder", "name")
# Kinds whose label is their child text; an <input> has none, so a literal
# found after it belongs to whatever is rendered next.
LABEL_FROM_CHILDREN = ("button", "menu-item", "option", "tab", "toggle", "link")

# Action verbs Cockpit ids are named after ("…-delete", "…new-…-toggle"). A
# small whitelist rather than an arbitrary word list, so an id mentioning an
# action verb is a signal, but one that happens to mention a data noun isn't.
ACTION_VERBS = (
    "delete", "remove", "create", "new", "add", "save", "apply", "submit",
    "enable", "disable", "start", "stop", "restart", "reload", "reconfigure",
    "edit", "change", "modify", "update", "open", "close", "show", "hide",
    "clear", "cancel", "confirm", "revert", "reset", "toggle", "on", "off",
    "connect", "disconnect", "import", "export", "attach", "detach",
    "upgrade", "install", "refresh", "retry", "reboot", "shutdown",
)

# Kinds a purpose hint is meaningful for.
HINT_KINDS = frozenset(("button", "select", "checkbox", "switch", "menu-item",
                        "option", "toggle", "radio"))

# Prop-style words that pass _is_label() but are not user-visible captions,
# so a weak hint never becomes "variant=primary".
_JUNK_WORDS = frozenset(w.lower() for w in (
    "primary", "secondary", "danger", "success", "warning", "link", "plain",
    "true", "false", "null", "auto", "center", "left", "right", "top", "bottom",
    "default", "static", "dynamic", "fixed", "inline", "block"))

# How close a string has to be to the control to be considered its caption:
# near enough that a dialog title or action caption is usually reached, but a
# sentence on the next page almost never is.
HINT_WINDOW = 600


def _is_candidate(value: str) -> bool:
    return _is_label(value) and value.strip().lower() not in _JUNK_WORDS


def _id_verb(control_id: str | None) -> str | None:
    """The last token of a control id, if it is one of the action verbs above."""
    if not control_id:
        return None
    verb = re.split(r"[-_\s]+", control_id.lower())[-1]
    return verb if verb in ACTION_VERBS else None


def _purpose_hint(js: JSSource, end: int, label: str | None, control_id: str | None) -> str | None:
    """Best-effort "what this roughly does", from id + the closest nearby strings.

    Two independent signals, either alone is enough:
      * the control's id naming an action verb (…-delete, …-restart) — strong;
      * the closest plausible gettext string after the element (a caption, an
        aria-label, a nearby dialog title) — weak on its own.

    A "strong" phrase is one the verb itself appears in ("Delete bond0" for a
    delete button) and outranks "weak" phrases like a bare "Cancel". Without a
    verb there is only the weakest evidence, and a bare technical word
    ("primary") is dropped rather than guessed. A string that *is* the label
    adds no information and is skipped.
    """
    verb = _id_verb(control_id)
    lab = (label or "").strip().lower()
    strong = weak = None
    for pos, _end, value in js.strings[bisect.bisect_left(js._starts, end):]:
        if pos >= end + HINT_WINDOW:
            break
        candidate = value.strip()
        if not _is_candidate(candidate) or candidate.lower() == lab:
            continue
        if verb and verb in candidate.lower():
            strong = candidate
            break
        weak = weak or candidate
    if strong:
        return strong
    if verb:
        return f"a {verb} action"
    return weak


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
# ids the dialog state framework stamps on its own buttons — they contain
# "dialog" and would otherwise read as three further dialogs
STATE_MARKER_IDS = frozenset(("dialog-apply", "dialog-cancel", "dialog-error-message"))
# …and the ids it gives their inputs ("dialog-" + "field" + "-" + path). None of
# them belong to whichever markup dialog happens to share the "dialog" prefix.
_STATE_OWNED_ID_RE = re.compile(r"^dialog-(apply|cancel|error-message|field-)")
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
        return _dialog_prefix(self.id)


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
        if m.group(1) in STATE_MARKER_IDS:
            continue        # the state framework's own buttons, not dialogs
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
    if not field_id or field_id.endswith("dialog") or _STATE_OWNED_ID_RE.match(field_id):
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
              decls: dict[str, _DialogDecl],
              field_labels: dict[str, str]
              ) -> tuple[list[Control], list[Subpage]]:
    """Split the flat control/field set into page controls and per-dialog sub-pages."""
    # a markup dialog's declared id *is* its DOM id, so it is its own selector
    subs: dict[str, Subpage] = {
        did: Subpage(id=did, title=d.title, actions=list(d.actions),
                     framework="markup", dom_id=did, selectors={"dialog": f"#{did}"})
        for did, d in decls.items()}
    for cid in field_ids:
        owner = _owning_dialog(cid, decls)
        if owner and owner in subs:
            if cid not in subs[owner].fields:
                subs[owner].fields.append(cid)
            if cid in field_labels:
                subs[owner].field_labels.setdefault(cid, field_labels[cid])
    attributed: set[tuple[str, str, str]] = set()
    for control in page_controls:
        owner = _owning_dialog(control.id or "", decls) if control.id else None
        if owner and owner in subs:
            control.attrs["dialog"] = owner
            control.selector = f"#{control.id}"
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


# ===========================================================================
# Sub-page discovery, second idiom: declarative dialog specs
# ===========================================================================
#
# pkg/storaged/dialog.jsx builds a dialog from a *description* instead of from
# markup, and most of the destructive actions on a host live there:
#
#   dialog_open({ Title: _("Create RAID device"),
#                 Fields: [ TextInput("name", _("Name"), {…}),
#                           SelectOne("level", _("RAID level"), {…}),
#                           CheckBoxes("encrypt", _("Encryption"),
#                                      { fields: [{tag: "on", title: _("…")}] }) ],
#                 Action: { Title: _("Create"), Danger: _("…") } })
#
# Nothing there is an HTML id, so the prefix attribution above cannot see it —
# but the spec is better than anything markup gives us: each field arrives as
# (tag, label) and the Action says what confirming the dialog does. Minifying
# renames the field constructors but keeps that shape, ident("tag", _("Label"),
# {…}), so the tag and the label survive; the constructor's *name* does not,
# which is why the kind falls back to a guess from the label.

_SPEC_FIELDS_RE = re.compile(r"\bFields\s*:\s*\[")
_SPEC_FIELD_RE = re.compile(
    r'(?P<ctor>[\w$.]+)\(\s*"(?P<tag>[a-z0-9_.]+)"\s*,\s*(?:[\w$.]+\(\s*)?"(?P<title>[^"]+)"')
# the sub-fields of a CheckBoxes group: {tag: "nomount", title: _("…")}
_SPEC_SUBFIELD_RE = re.compile(
    r'\{\s*tag\s*:\s*"(?P<tag>[\w.-]+)"\s*,\s*[Tt]itle\s*:\s*(?:[\w$.]+\(\s*)?"(?P<title>[^"]+)"')
_SPEC_ACTION_RE = re.compile(r"\bAction\s*:\s*\{")
_PASSWORD_RE = re.compile(r"pass(word|phrase)", re.I)

# dialog.jsx's exported field constructors, for the builds that keep their names.
SPEC_FIELD_KINDS = {
    "TextInput": "input", "PassInput": "input[password]",
    "ComboBox": "select", "SelectOne": "select", "SelectOneRadio": "select",
    "SelectRow": "select", "SelectSpace": "select", "SelectSpaces": "select",
    "CheckBoxes": "checkbox", "SizeSlider": "input[number]",
}

# Every field constructor in pkg/storaged/dialog.jsx renders a wrapper carrying
# ``data-field=<tag>`` and ``data-field-type=<kind>`` — the attributes the test
# suite drives the dialog through (test/common/storagelib.py: dialog_field,
# dialog_set_val). Both are plain string literals, so they survive minification
# even though the constructor's *name* does not: reading the kind out of the
# constructor body beats guessing it from the field's caption.
_FIELD_TYPE_RE = re.compile(r'"data-field-type"\s*:\s*"(?P<kind>[ \w-]+)"')
# "Pe=(e,o,t)=>…" — the shape every field constructor compiles to.
_CTOR_DEF_RE = re.compile(r"([\w$]+)\s*=\s*\(\s*[\w$]+\s*,\s*[\w$]+\s*,\s*[\w$]+\s*\)\s*=>")
# a constructor that delegates its markup to a helper component (PassInput does)
_DELEGATE_RE = re.compile(r"\bcreateElement\s*\(\s*([A-Za-z_$][\w$]*)\s*,")
# any definition of an identifier, for following that delegation one hop
# "$" is not a word character, so \b cannot delimit a minified name that
# starts with one; a negative look-behind over [\w$] can.
_ANY_DEF_RE = (r"(?:function\s+{0}\s*\(|(?<![\w$]){0}\s*=\s*"
               r"(?:function\b|\(|[\w$]+\s*=>))")

FIELD_TYPE_KINDS = {
    "text-input": "input", "text-input-checked": "input",
    "combobox": "select", "select": "select", "select-radio": "select",
    "select-row": "select", "select-spaces": "select",
    "checkbox": "checkbox", "size-slider": "input[number]",
}
# markers that tell a text input apart from a password one inside a constructor
_PASSWORD_MARKUP_RE = re.compile(r'new-password|"password"|:"password"')

# Selectors the test suite uses to drive a storaged dialog. Every spec dialog is
# rendered by dialog_open() with a literal id of "dialog" (pkg/storaged/
# dialog.jsx), so the synthesised per-dialog name below is *our* label for it,
# never a DOM id.
SPEC_DIALOG_SELECTOR = "#dialog"
SPEC_CANCEL_SELECTOR = "#dialog button.cancel"


def _balanced(masked: str, start: int, opener: str, closer: str) -> tuple[int, int] | None:
    """Span of the bracket pair that opens at ``start``."""
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == opener:
            depth += 1
        elif masked[i] == closer:
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _assignment_body(masked: str, start: int) -> tuple[int, int]:
    """Span of the value assigned at ``start``, up to the ',' that ends it.

    Minified bundles are long ``var A=…,B=…,C=…`` chains, so an assignment runs
    until the first comma (or semicolon) that is not inside a bracket.
    """
    depth = 0
    for i in range(start, len(masked)):
        char = masked[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return start, i
            depth -= 1
        elif depth == 0 and char in ",;":
            return start, i
    return start, len(masked)


def _definition_span(js: JSSource, ident: str) -> tuple[int, int] | None:
    """Where ``ident``'s definition starts and ends.

    The span has to stop where the definition does: minified bundles pack one
    component against the next, so a fixed-size window would read a password
    input's markup as belonging to the plain text input in front of it.
    """
    match = re.search(_ANY_DEF_RE.format(re.escape(ident)), js.masked)
    if match is None:
        return None
    head = js.masked[match.start():match.end()].rstrip()
    after = match.end()
    if head.endswith("("):                       # function f( … / f=( …
        after -= 1
    elif head.endswith("function"):              # f=function( …
        after = js.masked.find("(", after)
    if not head.endswith("=>"):                  # step over the parameter list
        paren = _balanced(js.masked, after, "(", ")") if after >= 0 else None
        if paren is None:
            return None
        after = paren[1] + 1
    while after < len(js.masked) and js.masked[after].isspace():
        after += 1
    if js.masked[after:after + 1] == "{":        # a block body
        block = _balanced(js.masked, after, "{", "}")
        if block:
            return match.start(), block[1] + 1
    return _assignment_body(js.masked, after)    # a concise arrow body


def _definition_body(js: JSSource, ident: str, limit: int = 4000) -> str:
    """``ident``'s definition, for sniffing what it renders."""
    span = _definition_span(js, ident)
    return js.text[span[0]:min(span[1], span[0] + limit)] if span else ""


def _spec_kind_map(js: JSSource) -> dict[str, str]:
    """Minified field-constructor name → control kind, read from its markup.

    Each constructor is a three-argument arrow that renders exactly one
    ``data-field-type``; PassInput delegates to a helper component, so one hop
    of indirection is followed. Builds that kept their names fall through to
    SPEC_FIELD_KINDS instead and never reach here.
    """
    kinds: dict[str, str] = {}
    for match in _CTOR_DEF_RE.finditer(js.masked):
        name = match.group(1)
        if name in kinds:
            continue
        lo, hi = _assignment_body(js.masked, match.end())
        body = js.text[lo:hi]
        type_match = _FIELD_TYPE_RE.search(body)
        if type_match is None:                   # delegates its markup elsewhere
            delegate = _DELEGATE_RE.search(body)
            if delegate is None:
                continue
            body = _definition_body(js, delegate.group(1))
            type_match = _FIELD_TYPE_RE.search(body)
            if type_match is None:
                continue
        kind = FIELD_TYPE_KINDS.get(type_match.group("kind").strip())
        if not kind:
            continue
        if kind == "input" and _PASSWORD_MARKUP_RE.search(body):
            kind = "input[password]"
        kinds[name] = kind
    return kinds


def _spec_kind(ctor: str, tag: str, title: str, kind_map: dict[str, str]) -> str:
    base = ctor.rpartition(".")[2]
    kind = SPEC_FIELD_KINDS.get(base) or kind_map.get(base)
    if kind:
        # the kind map cannot tell a password apart when the constructor shares
        # its markup with the plain text input, so keep the name-based check
        if kind == "input" and _PASSWORD_RE.search(f"{tag} {title}"):
            return "input[password]"
        return kind
    if _PASSWORD_RE.search(f"{tag} {title}"):
        return "input[password]"
    return "input"


def _top_level_value(js: JSSource, lo: int, hi: int, key: str) -> tuple[int, int] | None:
    """Span of the value of top-level ``key`` in the object literal [lo, hi).

    Only depth zero counts: a ``choices:`` nested inside this field's own
    ``nested_fields`` belongs to that inner field, not to this one.
    """
    masked = js.masked
    pattern = re.compile(r"\b" + re.escape(key) + r"\s*:")
    depth, i = 0, lo + 1
    while i < hi - 1:
        char = masked[i]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif depth == 0:
            match = pattern.match(masked, i, hi)
            if match:
                value = _after_index(masked, match.end())
                if value < hi and masked[value] in "[{":
                    closer = "]" if masked[value] == "[" else "}"
                    span = _balanced(masked, value, masked[value], closer)
                    return (span[0], span[1] + 1) if span else None
                return None
        i += 1
    return None


def _after_index(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


# the entries of a storaged ``choices: [{value: "raid0", title: _("RAID 0")}]``
_CHOICE_RE = re.compile(
    r'\bvalue\s*:\s*"(?P<value>[^"]*)"\s*,\s*(?:title|label|content)\s*:'
    r'\s*(?:[\w$.]+\(\s*)?"(?P<caption>[^"]*)"')
# …and the reverse spelling, {label: "…", value: "…"}
_CHOICE_REV_RE = re.compile(
    r'\b(?:title|label|content)\s*:\s*(?:[\w$.]+\(\s*)?"(?P<caption>[^"]*)"\s*,'
    r'\s*value\s*:\s*"(?P<value>[^"]*)"')


def _choices(js: JSSource, span: tuple[int, int] | None) -> list[tuple[str, str]]:
    """The (value, caption) pairs of a static choice list.

    Many choice lists are computed at runtime (``choices: compute_layout_
    choices(pvs)``) and yield nothing — the field then stays an open string
    rather than becoming an enum.
    """
    if span is None:
        return []
    body = js.text[span[0]:span[1]]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in list(_CHOICE_RE.finditer(body)) + list(_CHOICE_REV_RE.finditer(body)):
        value, caption = match.group("value"), match.group("caption")
        if value in seen:
            continue
        seen.add(value)
        out.append((value, caption))
    return out


def _top_level_objects(masked: str, lo: int, hi: int) -> list[tuple[int, int]]:
    """Spans of the ``{…}`` objects directly inside [lo, hi)."""
    out: list[tuple[int, int]] = []
    depth, start = 0, -1
    for i in range(lo, hi):
        if masked[i] == "{":
            if depth == 0:
                start = i
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append((start, i + 1))
                start = -1
    return out


def _spec_actions(js: JSSource, span: tuple[int, int]) -> list[tuple[str, str]]:
    """What confirming the dialog is called: Action.Title, or its Variants.

    Only the Action's *own* keys count: its ``action`` callback routinely opens
    further dialogs, and their titles are not captions of this one.
    """
    match = _SPEC_ACTION_RE.search(js.masked, *span)
    if match is None:
        return []
    action = _balanced(js.masked, match.end() - 1, "{", "}")
    if action is None:
        return []
    props = js.props(action[0], action[1] + 1)
    style = "danger" if {"Danger", "DangerButton"} & props.keys() else ""

    captions = [props["Title"]] if props.get("Title") else []
    if "Variants" in props:            # several ways to confirm the same dialog
        variants = re.compile(r"\bVariants\s*:\s*\[").search(js.masked, *action)
        array = _balanced(js.masked, variants.end() - 1, "[", "]") if variants else None
        if array:
            for obj in _top_level_objects(js.masked, *array):
                title = js.props(*obj).get("Title")
                if title:
                    captions.append(title)

    out: list[tuple[str, str]] = []
    for caption in captions:
        if caption and (caption, style) not in out:
            out.append((caption, style))
    return out


def _spec_field_options(js: JSSource, after: int, limit: int) -> tuple[int, int] | None:
    """Span of the options object of a field constructor call.

    ``TextInput("name", _("Name"), {…})`` — from the end of the title literal
    the only things between us and the options are the gettext call's ')' and
    the argument comma. Anything else means the call had no options object.
    """
    i = after
    while i < limit:
        char = js.masked[i]
        if char.isspace() or char in "),":
            i += 1
            continue
        if char == "{":
            span = _balanced(js.masked, i, "{", "}")
            return (span[0], span[1] + 1) if span else None
        return None
    return None


def _discover_spec_dialogs(js: JSSource, source: str,
                           kind_map: dict[str, str]) -> list[Subpage]:
    """Recover the dialogs declared as ``{Title, Fields:[…], Action}`` specs."""
    out: list[Subpage] = []
    seen: set[str] = set()
    for match in _SPEC_FIELDS_RE.finditer(js.masked):
        array = _balanced(js.masked, match.end() - 1, "[", "]")
        spec = js.enclosing_object(match.start())
        if array is None or spec is None:
            continue
        title = js.props(*spec).get("Title")
        actions = _spec_actions(js, spec)

        controls: list[Control] = []
        labels: dict[str, str] = {}
        lo, hi = array[0], array[1]
        for found in _SPEC_FIELD_RE.finditer(js.text, lo, hi):
            ctor, tag, label = found.group("ctor", "tag", "title")
            if tag in labels:
                continue
            labels[tag] = label
            options = _spec_field_options(js, found.end(), hi)
            props = js.props(*options) if options else {}
            choices = _choices(js, _top_level_value(js, *options, "choices")) if options else []
            kind = _spec_kind(ctor, tag, label, kind_map)
            if choices and not kind.startswith("select"):
                kind = "select"
            controls.append(Control(
                kind=kind, label=label, id=tag, source=source,
                choices=choices,
                conditional="visible" in props,
                selector=f'{SPEC_DIALOG_SELECTOR} [data-field="{tag}"]'))
        for sub in _SPEC_SUBFIELD_RE.finditer(js.text, lo, hi):
            tag, label = sub.group("tag", "title")
            if tag in labels:
                continue
            labels[tag] = label
            controls.append(Control(      # a CheckBoxes group's own checkboxes
                kind="checkbox", label=label, id=tag, source=source,
                selector=f'{SPEC_DIALOG_SELECTOR} [data-field="{tag}"]'))
        if not controls and not actions:
            continue

        # a spec dialog has no id of its own; name it after its title, in the
        # same shape as the ids the markup-declared dialogs carry
        base = _slug(title or (actions[0][0] if actions else "")).replace("_", "-") or "dialog"
        dialog_id = f"{base}-dialog"
        n = 2
        while dialog_id in seen:                 # two dialogs can share a title
            dialog_id, n = f"{base}-{n}-dialog", n + 1
        seen.add(dialog_id)
        # every storaged dialog is rendered as "#dialog"; the apply buttons are
        # in footer order, which is the order the captions were recovered in.
        selectors = {"dialog": SPEC_DIALOG_SELECTOR, "cancel": SPEC_CANCEL_SELECTOR}
        for i, (caption, _style) in enumerate(actions, start=1):
            selectors[f"action:{caption}"] = f"#dialog button.apply:nth-of-type({i})"
        out.append(Subpage(id=dialog_id, title=title, actions=actions,
                           controls=controls, fields=list(labels),
                           field_labels=labels, framework="spec",
                           dom_id="dialog", selectors=selectors))
    return out


# ===========================================================================
# Sub-page discovery, third idiom: the dialog state framework
# ===========================================================================
#
# pkg/lib/cockpit/dialog.tsx ("Dialog Implementation Convenience Kit") is the
# idiom new Cockpit dialogs are written in. It has neither an id-prefixed field
# convention nor a declarative spec; instead all of the dialog's state lives in
# one object and each input is bound to a *handle* into it:
#
#   const dlg = useDialogState({ ssid: "", security: "none", password: "" });
#   …
#   <Modal isOpen onClose={Dialogs.close}>
#     <DialogTextInput      label={_("Network name")} field={dlg.field("ssid")} />
#     <DialogDropdownSelect label={_("Security")}     field={dlg.field("security")}
#                           options={[{value: "none", label: _("None")}, …]} />
#     <DialogActionButton dialog={dlg} action={apply}>{_("Connect")}</DialogActionButton>
#     <DialogCancelButton dialog={dlg} />
#   </Modal>
#
# Two things survive minification intact: the ``field: X.field("ssid")`` prop,
# whose literal is the field's path through the state object, and the marker
# the framework stamps on its own buttons — "dialog-apply", "dialog-cancel" and
# "dialog-error-message". The markers identify the porcelain components by
# name, which in turn identifies the confirm button and its caption without
# having to guess from a list of English words; the handles give the inputs,
# the "options" prop gives their allowed values.
#
# The same markers are what the tests drive the dialog through
# (test/common/dialoglib.py), so they double as the selectors a caller needs.
# Older builds stamp them as a plain "id", current ones as an OUIA component
# id; both spellings are recognised and the selector follows suit.

_STATE_MARKER_RE = re.compile(
    r'\b(?P<attr>ouiaId|id)\s*:\s*"dialog-(?P<role>apply|cancel|error-message)"')
# "field: dlg.field("ssid")", or a nested "dlg.field("addresses").sub(0)"
_FIELD_PROP_RE = re.compile(
    r'\bfield\s*:\s*[\w$]+((?:\.(?:field|sub)\(\s*"[\w.-]+"\s*\))+)')
_HANDLE_STEP_RE = re.compile(r'\.(?:field|sub)\(\s*"([\w.-]+)"\s*\)')
_CE_PROPS_RE = re.compile(r"\b(?:createElement|jsxs?|h)\s*\(\s*([\w$.]+)\s*,\s*\{")
# a definition that a backwards search can name: "function f(", "f=(", "f=x=>"
_DEF_NAME_RE = re.compile(
    r"(?:\bfunction\s+(?P<fn>[\w$]+)\s*\(|(?<![\w$])(?P<var>[\w$]+)\s*=\s*"
    r"(?:function\b|\(|[\w$]+\s*=>))")
# "Dialogs.show(<SomeDialog …/>)" — how a button reaches a dialog component
_SHOW_DIALOG_RE = re.compile(r"\.show\(\s*[\w$.]+\.createElement\(\s*([\w$]+)")
# a modal id built from a local constant: id: prefix + "-dialog"
_ID_CONCAT_RE = re.compile(r'\bid\s*:\s*([\w$]+)\s*\+\s*"([^"]*)"')


def _def_positions(js: JSSource) -> list[tuple[int, str]]:
    """Every (position, name) a definition starts at, in source order."""
    out: list[tuple[int, str]] = []
    for match in _DEF_NAME_RE.finditer(js.masked):
        out.append((match.start(), match.group("fn") or match.group("var")))
    return out


def _enclosing_def(defs: list[tuple[int, str]], pos: int) -> str | None:
    """Name of the innermost definition that starts before ``pos``."""
    index = bisect.bisect_right(defs, (pos, "￿")) - 1
    return defs[index][1] if index >= 0 else None


def _dialog_components(js: JSSource) -> list[tuple[int, int, str]]:
    """(start, end, name) of every component some button passes to Dialogs.show().

    This is the only link between a dialog and the control that opens it, and
    it is read from the opener's side on purpose: searching backwards from the
    dialog's own markup would land on whichever local arrow function happens
    to be defined last, not on the component the markup belongs to.
    """
    out: list[tuple[int, int, str]] = []
    for name in {m.group(1) for m in _SHOW_DIALOG_RE.finditer(js.masked)}:
        span = _definition_span(js, name)
        if span:
            out.append((span[0], span[1], name))
    return out


def _state_porcelain(js: JSSource,
                     defs: list[tuple[int, str]]) -> tuple[dict[str, str], str]:
    """Map the framework's button components to their role, and say how marked.

    Returns ``({ident: "apply"|"cancel"|"error-message"}, "id"|"ouiaId")``.
    """
    roles: dict[str, str] = {}
    attr = "ouiaId"
    # the markers are string literals, so they are only legible in the raw text
    for match in _STATE_MARKER_RE.finditer(js.text):
        attr = match.group("attr")
        name = _enclosing_def(defs, match.start())
        if name:
            roles[name] = match.group("role")
    return roles, attr


def _modal_calls(js: JSSource) -> list[tuple[int, int, tuple[int, int]]]:
    """(call start, call end, props span) of every ``<Modal isOpen onClose=…>``."""
    out: list[tuple[int, int, tuple[int, int]]] = []
    for match in _CE_PROPS_RE.finditer(js.masked):
        brace = match.end() - 1
        props_span = _balanced(js.masked, brace, "{", "}")
        if props_span is None:
            continue
        props = js.props(props_span[0], props_span[1] + 1)
        if "isOpen" not in props or "onClose" not in props:
            continue
        paren = js.masked.find("(", match.start())
        call = _balanced(js.masked, paren, "(", ")")
        if call is None:
            continue
        out.append((call[0], call[1], (props_span[0], props_span[1] + 1)))
    return out


def _state_field_kind(js: JSSource, component: str | None,
                      props: dict[str, str | None], has_choices: bool) -> str:
    """What kind of input a porcelain component renders.

    The component's own name is minified away, so its *body* is read instead:
    a password input is the only one that renders a "password" type, and a
    checkbox the only one that drives ``isChecked`` without a list of options.
    """
    if has_choices or "options" in props:
        return "select"
    body = _definition_body(js, component) if component else ""
    if _PASSWORD_MARKUP_RE.search(body):
        return "input[password]"
    if "isChecked" in body:
        return "checkbox"
    return "input"


def _state_selector(attr: str, tag: str) -> str:
    name = f"dialog-{tag}"
    return f"#{name}" if attr == "id" else f"[data-ouia-component-id='{name}']"


def _resolve_modal_id(js: JSSource, props_span: tuple[int, int],
                      call_start: int) -> str | None:
    """The id a modal really carries, including ``id={prefix + "-dialog"}``."""
    literal = js.props(*props_span).get("id")
    if literal:
        return literal
    concat = _ID_CONCAT_RE.search(js.text, *props_span)
    if concat is None:
        return None
    variable, suffix = concat.group(1), concat.group(2)
    # the constant is assigned inside the component, just above the markup
    assign = None
    for found in re.finditer(r"\b" + re.escape(variable) + r'\s*=\s*"',
                             js.masked, 0):
        if found.end() <= call_start and found.start() > call_start - 6000:
            assign = found
    if assign is None:
        return None
    value = js._string_at(assign.end() - 1)
    return f"{value}{suffix}" if value else None


def _discover_state_dialogs(js: JSSource, source: str) -> list[Subpage]:
    """Recover the dialogs built with useDialogState + the Dialog* porcelain."""
    defs = _def_positions(js)
    roles, attr = _state_porcelain(js, defs)
    if not roles:
        return []
    components = _dialog_components(js)
    apply_idents = {name for name, role in roles.items() if role == "apply"}
    cancel_idents = {name for name, role in roles.items() if role == "cancel"}

    out: list[Subpage] = []
    seen: set[str] = set()
    for call_start, call_end, props_span in _modal_calls(js):
        controls: list[Control] = []
        labels: dict[str, str] = {}
        actions: list[tuple[str, str]] = []

        for found in _FIELD_PROP_RE.finditer(js.text, call_start, call_end):
            tag = ".".join(_HANDLE_STEP_RE.findall(found.group(1)))
            if not tag or tag in labels:
                continue
            span = js.enclosing_object(found.start())
            if span is None:
                continue
            props = js.props(*span)
            component = _enclosing_component(js, span[0])
            choices = _choices(js, _top_level_value(js, span[0], span[1], "options"))
            label = next((props[p] for p in LABEL_PROPS if props.get(p)), None)
            labels[tag] = label or _humanise(tag)
            controls.append(Control(
                kind=_state_field_kind(js, component, props, bool(choices)),
                label=label, id=tag, source=source, choices=choices,
                attrs={"required": "true"} if "isRequired" in props else {},
                selector=_state_selector(attr, f"field-{tag}")))

        for found in _CE_PROPS_RE.finditer(js.masked, call_start, call_end):
            name = found.group(1).rpartition(".")[2]
            if name not in apply_idents and name not in cancel_idents:
                continue
            props_end = _balanced(js.masked, found.end() - 1, "{", "}")
            if props_end is None:
                continue
            caption = js.child_string(props_end[1] + 1,
                                      min(len(js.text), props_end[1] + 300))
            if name in cancel_idents:
                continue               # the framework's own abandon button
            style = "danger" if js.props(found.end() - 1, props_end[1] + 1)\
                .get("variant") == "danger" else ""
            if caption and (caption, style) not in actions:
                actions.append((caption, style))

        if not controls and not actions:
            continue

        dom_id = _resolve_modal_id(js, props_span, call_start)
        title = _TITLE_LIT_RE.search(js.text[call_start:call_end])
        caption = title.group(1) if title else js.child_string(props_span[1], call_end)
        base = (_dialog_prefix(dom_id) if dom_id
                else _slug(caption or (actions[0][0] if actions else "")).replace("_", "-"))
        dialog_id = f"{base or 'dialog'}-dialog"
        n = 2
        while dialog_id in seen:
            dialog_id, n = f"{base}-{n}-dialog", n + 1
        seen.add(dialog_id)

        selectors = {"dialog": f"#{dom_id}" if dom_id else ".pf-v6-c-modal-box",
                     "apply": _state_selector(attr, "apply"),
                     "cancel": _state_selector(attr, "cancel"),
                     "error": _state_selector(attr, "error-message")}
        out.append(Subpage(id=dialog_id, title=caption, actions=actions,
                           controls=controls, fields=list(labels),
                           field_labels=labels, framework="state",
                           dom_id=dom_id, selectors=selectors,
                           component=next((name for lo, hi, name in components
                                           if lo <= call_start < hi), None)))
    return out


def _enclosing_component(js: JSSource, props_start: int) -> str | None:
    """The component of ``createElement(C, {…})`` whose props start here."""
    match = _CREATE_ELEMENT_RE.search(js.masked, max(0, props_start - 60), props_start)
    return match.group(1).rpartition(".")[2] if match else None


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

        cid = props.get("id")
        attrs = {k: props[k] for k in ("variant", "type", "name", "placeholder")
                 if props.get(k)}
        # isRequired is a boolean prop, so props() has no value for it; read the
        # literal from the object itself ("isRequired:!0" is a minified true).
        if "isRequired" in props and _IS_REQUIRED_RE.search(js.masked, start, end):
            attrs["required"] = "true"
        control = Control(
            kind=kind,
            label=label,
            id=cid,
            attrs=attrs,  # type: ignore[arg-type]
            source=source,
            hint=_purpose_hint(js, end, label, cid) if kind in HINT_KINDS else None,
            # Dialogs.show(<SomeDialog/>) in the handler names the dialog this
            # button opens — the only link between the two in minified code.
            opens=(shows.group(1) if (shows := _SHOW_DIALOG_RE.search(js.masked, start, end))
                   else None),
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
    left, subpages = _attribute(page_controls, field_ids, decls, field_labels)
    # Dialogs built from a spec or from dialog state are already complete: they
    # own their fields outright and so take part in no prefix attribution.
    subpages += _discover_spec_dialogs(js, source, _spec_kind_map(js))
    state = _discover_state_dialogs(js, source)
    subpages += state

    # a button whose handler shows one of those components opens that dialog
    by_component = {s.component: s.id for s in state if s.component}
    for control in left:
        if control.opens and control.opens in by_component:
            control.attrs["opens"] = by_component[control.opens]
            control.opens = by_component[control.opens]
        else:
            control.opens = None
    return left, sorted(subpages, key=lambda s: s.id)


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
        for fid, flabel in sub.field_labels.items():
            existing.field_labels.setdefault(fid, flabel)
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
# Tool synthesis: the same findings, as callable tools
# ==========================================================================
#
# The tree above is a *map* of the UI. A caller that wants to act on the host
# needs something else: a flat list of the actions it can take, each with a
# name, a description of what it does, and the values it needs to be given —
# i.e. an MCP tool definition (name / description / inputSchema).
#
# The mapping from what we recovered to that shape:
#
#   * a dialog is an action with inputs. Its non-cancel action captions say
#     what performing it means ("Create", "Delete"), its fields are the input
#     values, its title is the description. One tool per action caption, since
#     a dialog offering both "Rename" and "Delete" is two different actions.
#   * a page-level button or menu item is an action without inputs: pressing
#     it is the whole operation (a page-level switch takes one boolean).
#   * a manifest command is a tool whose inputs are its declared args.
#
# Controls that only navigate, filter or fold the view are not actions, and
# are dropped (NON_ACTION_LABELS) — as are a dialog's "Cancel"/"Close".
#
# Input *types* are inferred from the control: a checkbox is a boolean, a
# NumberInput an integer, everything else a string. Select options are not
# recovered, so a select is an unconstrained string rather than an enum.

# Kinds that take a value from the user: the inputs of a tool.
PARAM_KINDS = frozenset(("checkbox", "switch", "radio", "select", "textarea", "slider"))

# Kinds that *do* something when pressed: the tools themselves.
ACTION_KINDS = frozenset(("button", "menu-item"))

# Dialog actions that abandon the dialog instead of performing it.
CANCEL_CAPTIONS = frozenset(("cancel", "close", "dismiss", "back", "never mind"))

# Labels of controls that move around the UI rather than change the host.
NON_ACTION_LABELS = frozenset((
    "cancel", "close", "dismiss", "back", "next", "previous", "continue",
    "ok", "done", "yes", "no", "help", "learn more", "read more",
    "show more", "show less", "view more", "view all", "see more",
    "expand", "collapse", "expand all", "collapse all", "toggle",
    "search", "filter", "clear filters", "clear all filters", "refresh",
    "menu", "more", "more options", "options", "details", "overview", "home",
    "loading", "select", "select all", "deselect all", "sort", "copy",
))

# A page-level control is only an action if it says so: either its id names an
# action verb (ACTION_VERBS, used for the purpose hints) or its caption starts
# with one of these. Without such a gate the listing fills up with the strings
# the control scan attaches to a button from its surroundings — column headers
# ("UUID", "Mount point"), statuses ("No carrier", "Synchronized") and field
# captions, none of which do anything. Navigation verbs (view, go to) are
# deliberately absent: they move around the UI, they don't change the host.
TOOL_VERBS = frozenset(ACTION_VERBS) | frozenset((
    "abort", "activate", "allow", "assign", "authenticate", "deactivate",
    "deny", "disallow", "dismiss", "download", "erase", "fill", "force",
    "forget", "format", "generate", "grant", "grow", "join", "leave", "limit",
    "lock", "log", "manage", "mask", "mount", "pause", "pin", "reapply",
    "register", "regenerate", "rename", "repair", "replace", "restore",
    "resume", "resize", "revoke", "run", "scan", "send", "set", "shrink",
    "shut", "switch", "terminate", "test", "troubleshoot", "trust", "undo",
    "unlock", "unmask", "unmount", "unpin", "unregister", "upload", "validate",
    "wipe",
))

# Labels belonging to the UI's own machinery — PatternFly's a11y captions for
# menus, drag handles and clear buttons, and the keyboard keys its shortcut
# help names. They are controls, but not actions on the host.
_UI_NOISE_RE = re.compile(
    r"\b(toggle|dropdown|kebab|draggable|caret|chevron|breadcrumb|tooltip|popover|"
    r"sidebar|scrollbar|pagination|input value|escape|tab|enter|backspace)\b", re.I)

# Words that carry no meaning in a dialog id ("account-confirm-delete").
_FILLER_TOKENS = frozenset(("confirm", "dialog", "modal", "popup", "the", "a", "an"))

# Names that make an input a number rather than a string.
_INT_PARAM_RE = re.compile(
    r"\b(uid|gid|port|size|count|quota|priority|limit|days?|hours?|minutes?|seconds?|"
    r"timeout|interval|number|percent|mtu|vlan|index|id)\b")

@dataclass
class ToolParam:
    """One input value of a tool, with the control it is typed into."""
    name: str
    type: str                       # JSON Schema type: string / boolean / integer
    description: str
    required: bool = False
    control_id: str | None = None
    kind: str = ""                  # the control kind it came from
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, caption)
    selector: str | None = None     # CSS selector that reaches the control

    def schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.description:
            out["description"] = self.description
        if self.choices:
            out["enum"] = [value for value, _ in self.choices]
            captions = ", ".join(f"{value} ({caption})" if caption != value else value
                                 for value, caption in self.choices)
            said = (out.get("description") or "").rstrip(" .")
            out["description"] = f"{said}. One of: {captions}." if said \
                else f"One of: {captions}."
        if self.kind.startswith("input[password]"):
            out["format"] = "password"
        return out

    def render(self, width: int) -> str:
        flag = "required" if self.required else "optional"
        out = f"{self.name:<{width}}  {self.type:<7}  {flag:<8}"
        if self.description:
            out += f"  {self.description}"
        if self.choices:
            out += "  one of: " + ", ".join(value for value, _ in self.choices)
        if self.control_id:
            out += f"  [#{self.control_id}]"
        return out


@dataclass
class Tool:
    """An action a caller can perform, in MCP tool shape."""
    name: str
    title: str
    description: str
    package: str
    page_label: str
    page_path: str
    section: str
    params: list[ToolParam] = field(default_factory=list)
    dialog: str | None = None       # the dialog that performs it, if any
    action: str | None = None       # the caption of the confirming button
    opens_via: str | None = None    # the control that reaches the dialog
    control_id: str | None = None   # the control that *is* the action
    destructive: bool = False
    framework: str = ""             # the dialog idiom: markup / spec / state
    selectors: dict[str, str] = field(default_factory=dict)  # how to drive it

    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {p.name: p.schema() for p in self.params},
        }
        required = [p.name for p in self.params if p.required]
        if required:
            schema["required"] = required
        schema["additionalProperties"] = False
        return schema

    def to_json(self) -> dict[str, Any]:
        """MCP tool definition, plus the Cockpit coordinates needed to drive it."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "annotations": {"destructiveHint": self.destructive},
            "cockpit": {
                "package": self.package,
                "section": self.section,
                "page": self.page_label,
                "path": self.page_path,
                "dialog": self.dialog,
                "action": self.action,
                "opens_via": self.opens_via,
                "control": self.control_id,
                "framework": self.framework or None,
                "selectors": self.selectors or None,
                "inputs": [{"name": p.name, "control": p.control_id, "kind": p.kind,
                            "selector": p.selector}
                           for p in self.params],
            },
        }


def _is_param(kind: str) -> bool:
    return kind.startswith("input") or kind in PARAM_KINDS


def _param_type(kind: str, name: str, label: str | None) -> str:
    if kind in ("checkbox", "switch", "radio"):
        return "boolean"
    if kind in ("input[number]", "input[range]", "slider"):
        return "integer"
    if _INT_PARAM_RE.search(f"{name} {label or ''}".lower()):
        return "integer"
    return "string"


def _param_name(control_id: str | None, label: str | None, prefix: str) -> str:
    """Name an input after its id with the dialog's own prefix stripped off.

    ``accounts-create-real-name`` in ``accounts-create-dialog`` → ``real_name``.
    """
    base = control_id or ""
    if prefix and base.startswith(prefix):
        base = base[len(prefix):]
    return _slug(base) or _slug(label or "") or "value"


def _make_param(control_id: str | None, label: str | None, kind: str,
                prefix: str, required: bool, placeholder: str | None = None,
                choices: list[tuple[str, str]] | None = None,
                selector: str | None = None) -> ToolParam:
    name = _param_name(control_id, label, prefix)
    description = label or _humanise(name)
    if placeholder and placeholder.strip().lower() != description.strip().lower():
        description += f" (e.g. {placeholder})"
    return ToolParam(
        name=name,
        # a recovered choice list pins the type down: it is whatever the
        # values are, never the integer the name alone would suggest
        type="string" if choices else _param_type(kind, name, label),
        description=description,
        required=required,
        control_id=control_id,
        kind=kind,
        choices=list(choices or []),
        selector=selector,
    )


def _dialog_params(sub: Subpage) -> list[ToolParam]:
    """The input values a dialog asks for: its own controls, then its bare fields."""
    prefix = sub.prefix
    params: dict[str, ToolParam] = {}
    for control in sub.controls:
        if not _is_param(control.kind):
            continue
        param = _make_param(control.id, control.label, control.kind, prefix,
                            required=control.attrs.get("required") == "true",
                            placeholder=control.attrs.get("placeholder"),
                            choices=control.choices, selector=control.selector)
        params.setdefault(param.name, param)
    # fields the ownership pass found but no control was recovered for: they
    # are real inputs whenever a form group gave them a caption.
    have = {c.id for c in sub.controls if c.id}
    for field_id in sub.fields:
        label = sub.field_labels.get(field_id)
        if field_id in have or not label:
            continue
        param = _make_param(field_id, label, "input", prefix, required=False)
        params.setdefault(param.name, param)
    return sorted(params.values(), key=lambda p: (not p.required, p.name))


def _useful_hint(hint: str | None, label: str) -> str | None:
    """Keep a purpose hint only where it says more than the label already does.

    _purpose_hint() is deliberately generous; for a tool description most of
    what it returns is noise — the label again, a neighbouring "Cancel", the
    "a delete action" restatement of the id, or a fragment of minified code.
    """
    text = (hint or "").strip()
    low = text.lower()
    if not text or low == label.strip().lower():
        return None
    if low in NON_ACTION_LABELS or low in CANCEL_CAPTIONS:
        return None
    if text.startswith("a ") and text.endswith(" action"):
        return None                    # only restates the verb in the id
    if not text[0].isupper() or _UI_NOISE_RE.search(text):
        return None
    if " " not in text and (_label_verb(label) or "\0") not in low:
        return None                    # a lone neighbouring word ("Name", "Actions")
    return text


def _is_noise(label: str) -> bool:
    return len(label.strip()) < 3 or bool(_UI_NOISE_RE.search(label))


def _label_verb(label: str) -> str | None:
    """The leading verb of a caption ("Shut down" → shut), if it names an action."""
    first = _slug(label).partition("_")[0]
    return first if first in TOOL_VERBS else None


def _dialog_title(sub: Subpage, entry: Control | None, caption: str | None) -> str:
    """Name the action a dialog performs.

    Most dialogs carry a title; the confirm-style ones do not, and their id is
    the only description there is. Reading it verb-first against the action
    caption turns "account-confirm-delete" + "Delete" into "Delete account".
    """
    if sub.title:
        return sub.title
    if entry and entry.label:
        return entry.label
    if not caption:
        return _humanise(sub.prefix)
    spelling = _slug(caption).replace("_", "")
    rest = [token for token in re.split(r"[-_./\s]+", sub.prefix)
            if token and token.lower() not in _FILLER_TOKENS and token.lower() not in spelling]
    return f"{caption} {' '.join(rest)}" if rest else caption


def _entry_control(sub: Subpage, page: Page | None = None) -> Control | None:
    """The button that opens this dialog.

    A dialog built with the state framework says so outright — its opener runs
    ``Dialogs.show(<ThatDialog/>)``, which the scan resolved back to the
    dialog's id. The older idioms have no such link, and the convention that
    the opener carries the dialog's prefix as its id is all there is.
    """
    for control in (page.controls if page else []):
        if control.opens == sub.id:
            return control
    fallback = None
    for control in sub.controls:
        if control.kind != "button" or not control.id:
            continue
        if control.id == sub.prefix:
            return control
        fallback = fallback or control
    return fallback


def _unique(name: str, used: set[str]) -> str:
    name = name or "action"
    candidate, n = name, 2
    while candidate in used:
        candidate, n = f"{name}_{n}", n + 1
    used.add(candidate)
    return candidate


def _tool_name(package: str, parts: list[str], used: set[str]) -> str:
    name = "_".join(s for s in (_slug(p) for p in parts if p) if s)
    pkg = _slug(package)
    if name != pkg and not name.startswith(f"{pkg}_"):
        name = f"{pkg}_{name}" if name else pkg
    return _unique(name, used)


def _where(pkg: Package, page: Page) -> str:
    return f"Page “{page.label}” ({page.section}) of package {pkg.name}, {page.path}."


def _dialog_tools(pkg: Package, page: Page, sub: Subpage,
                  used: set[str], covered: set[str]) -> list[Tool]:
    """One tool per non-cancel action of a dialog; its fields are the inputs."""
    params = _dialog_params(sub)
    entry = _entry_control(sub, page)
    captions = [(c, s) for c, s in sub.actions if c.strip().lower() not in CANCEL_CAPTIONS]
    if not captions and not params:
        return []                      # nothing to perform and nothing to fill in

    for label in (sub.title, entry.label if entry else None):
        if label:
            covered.add(label.strip().lower())

    base = sub.prefix
    out: list[Tool] = []
    for caption, style in captions or [(None, "")]:
        if caption:
            covered.add(caption.strip().lower())
        title = _dialog_title(sub, entry, caption)
        if caption and len(captions) > 1 and caption.lower() not in title.lower():
            title = f"{title}: {caption}"

        description = f"{title.rstrip('.')}. "
        description += (f"Performed by pressing “{caption}” in the Cockpit dialog "
                        f"“{sub.id}”. " if caption else
                        f"Filled in and submitted in the Cockpit dialog “{sub.id}”. ")
        if entry and entry.label:
            description += f"The dialog is opened from the “{entry.label}” control. "
        if style == "danger":
            description += "Destructive: it removes or disrupts what it acts on. "
        description += _where(pkg, page)

        out.append(Tool(
            name=_tool_name(pkg.name, [base, caption if len(captions) > 1 else ""], used),
            title=title,
            description=description,
            package=pkg.name,
            page_label=page.label,
            page_path=page.path,
            section=page.section,
            params=params,
            dialog=sub.id,
            action=caption,
            # an opener found through Dialogs.show() often has no id of its
            # own; its caption is then the only handle on it
            opens_via=(entry.id or entry.label) if entry else None,
            destructive=style == "danger",
            framework=sub.framework,
            selectors=_tool_selectors(sub, caption, entry),
        ))
    return out


def _tool_selectors(sub: Subpage, caption: str | None,
                    entry: Control | None) -> dict[str, str]:
    """The selectors a caller needs to drive one action of a dialog.

    Which button confirms depends on the caption, because a spec dialog with
    two variants ("Create snapshot and mount" / "Create snapshot only")
    renders them as two apply buttons that only their position tells apart.
    """
    out = {k: v for k, v in sub.selectors.items() if not k.startswith("action:")}
    if caption and f"action:{caption}" in sub.selectors:
        out["apply"] = sub.selectors[f"action:{caption}"]
    if entry and entry.selector:
        out["open"] = entry.selector
    elif entry and entry.id:
        out["open"] = f"#{entry.id}"
    return out


def _page_tools(pkg: Package, page: Page, used: set[str], covered: set[str]) -> list[Tool]:
    """Page-level buttons and switches: an action that needs no dialog."""
    out: list[Tool] = []
    for control in page.controls:
        if control.kind not in ACTION_KINDS and control.kind != "switch":
            continue
        if control.opens:
            continue                   # it only opens a dialog, which is its own tool
        label = (control.label or "").strip()
        key = label.lower()
        verb = _id_verb(control.id)
        if key in covered or key in NON_ACTION_LABELS:
            continue
        if not label and not verb:
            continue                   # unlabelled and not named after an action
        title = label or _humanise(control.id or "")
        if _is_noise(title):
            continue                   # a drag handle, a menu toggle, a key name
        # a switch is an action whatever it is called; a button has to say so
        if control.kind != "switch" and not verb and not _label_verb(title):
            continue
        if key:
            covered.add(key)

        params: list[ToolParam] = []
        if control.kind == "switch":
            params = [ToolParam(name="enabled", type="boolean",
                                description=f"whether “{title}” is on",
                                required=True, control_id=control.id, kind=control.kind)]

        description = f"{title.rstrip('.')}. "
        what = {"button": "Presses", "menu-item": "Selects the menu entry",
                "switch": "Flips the switch"}[control.kind]
        description += f"{what} “{title}” directly on the page"
        description += f" (#{control.id}). " if control.id else ". "
        hint = _useful_hint(control.hint, title)
        if hint:
            description += f"Nearby text suggests it relates to: {hint}. "
        description += _where(pkg, page)

        out.append(Tool(
            name=_tool_name(pkg.name, [title or control.id or ""], used),
            title=title,
            description=description,
            package=pkg.name,
            page_label=page.label,
            page_path=page.path,
            section=page.section,
            params=params,
            control_id=control.id,
            destructive=verb in ("delete", "remove", "reboot", "shutdown", "stop"),
            selectors={"control": control.selector or f"#{control.id}"} if control.id
                      or control.selector else {},
        ))
    return out


def _command_tool(pkg: Package, cmd: Command, used: set[str]) -> Tool:
    params = [
        ToolParam(name=_slug(arg) or f"arg{i + 1}", type="string",
                  description=f"command argument {arg}", required=True)
        for i, arg in enumerate(cmd.args)
    ]
    description = (cmd.description or f"Run the “{cmd.name}” command.").rstrip(".") + ". "
    description += f"Shell command declared by package {pkg.name}"
    description += f", runs: {cmd.command}." if cmd.command else "."
    return Tool(
        name=_tool_name(pkg.name, [cmd.name], used),
        title=cmd.name,
        description=description,
        package=pkg.name,
        page_label="(shell command)",
        page_path="",
        section="commands",
        params=params,
    )


def build_tools(packages: list[Package]) -> list[Tool]:
    """Turn the package/page/control model into a flat list of callable tools."""
    tools: list[Tool] = []
    used: set[str] = set()
    for pkg in packages:
        for cmd in pkg.commands:
            tools.append(_command_tool(pkg, cmd, used))
        for page in pkg.pages:
            covered: set[str] = set()   # labels a dialog tool already offers
            for sub in page.subpages:
                tools += _dialog_tools(pkg, page, sub, used, covered)
            tools += _page_tools(pkg, page, used, covered)
    return tools


def render_tools(tools: list[Tool], title: str, out=sys.stdout) -> None:
    print(f"{title} — {len(tools)} tool(s)", file=out)
    package = None
    for tool in tools:
        if tool.package != package:
            package = tool.package
            print(f"\n=== {package} " + "=" * max(0, 70 - len(package)), file=out)
        print(f"\n  {tool.name}" + ("   [destructive]" if tool.destructive else ""), file=out)
        print(f"    title: {tool.title}", file=out)
        for line in textwrap.wrap(tool.description, 92,
                                  initial_indent="    ", subsequent_indent="      "):
            print(line, file=out)
        if not tool.params:
            print("    inputs: (none)", file=out)
            continue
        print("    inputs:", file=out)
        width = max(len(p.name) for p in tool.params)
        for param in tool.params:
            print("      " + param.render(width), file=out)


def tools_to_json(tools: list[Tool]) -> str:
    return json.dumps([t.to_json() for t in tools], indent=2)


# --------------------------------------------------------------------------
# The same tools as a tree
# --------------------------------------------------------------------------
#
# render_tools() above prints every tool in full, which is the right shape for
# reading one tool but a poor one for reviewing 150. The tree puts each tool on
# a single line — its call signature — under the page it came from, with the
# detail folded underneath, so the whole catalog can be scanned at once.
#
# They are *mock* tools: definitions recovered from the UI, with the selectors
# a driver would need. Nothing here is wired up to the host.

def _tool_signature(tool: Tool) -> str:
    """``name(arg, required_arg*)`` — a tool's call shape on one line."""
    if not tool.params:
        return f"{tool.name}()"
    return f"{tool.name}(" + ", ".join(
        p.name + ("*" if p.required else "") for p in tool.params) + ")"


def _clip(text: str, width: int = 96) -> str:
    """Collapse whitespace and cut to ``width`` — tree lines do not wrap."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _tool_node(tool: Tool) -> Node:
    """One tool as a subtree: signature, what it does, its inputs, how to drive it."""
    head = _tool_signature(tool)
    if tool.destructive:
        head += "   [destructive]"
    node = Node(head)
    node.add(_clip(tool.title))

    if tool.dialog:
        via = f"dialog “{tool.dialog}”"
        if tool.action:
            via += f" · action “{tool.action}”"
        if tool.opens_via:
            via += f" · opened from “{tool.opens_via}”"
        if tool.framework:
            via += f"   [{tool.framework}]"
        node.add(via)
    elif tool.control_id:
        node.add(f"page control #{tool.control_id}")

    if tool.params:
        inode = node.add(f"inputs ({len(tool.params)})")
        width = max(len(p.name) for p in tool.params)
        for param in tool.params:
            inode.add(param.render(width))
    else:
        node.add("inputs: (none)")

    if tool.selectors:
        node.add("drive: " + "  ".join(f"{k}={v}" for k, v in sorted(tool.selectors.items())))
    return node


def build_tools_tree(tools: list[Tool], title: str) -> Node:
    """The tool catalog as a tree: package → page → tool → inputs.

    Tools arrive grouped by package already (build_tools walks the packages in
    order), so plain insertion-ordered dicts keep the listing stable.
    """
    root = Node(f"{title} — mock tool catalog: {len(tools)} tool(s), none callable")

    by_package: dict[str, dict[tuple[str, str, str], list[Tool]]] = {}
    for tool in tools:
        pages = by_package.setdefault(tool.package, {})
        pages.setdefault((tool.section, tool.page_label, tool.page_path), []).append(tool)

    for package, pages in by_package.items():
        total = sum(len(owned) for owned in pages.values())
        pnode = root.add(f"{package}   {total} tool(s)")
        for (section, label, path), owned in pages.items():
            head = f"[{section}] {label}"
            if path:
                head += f"  →  {path}"
            gnode = pnode.add(f"{head}   {len(owned)} tool(s)")
            for tool in owned:
                gnode.children.append(_tool_node(tool))
    return root


# ==========================================================================
# Main
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    global INSECURE

    parser = argparse.ArgumentParser(
        description="List the capabilities (pages, commands, buttons and inputs) of a "
                    "running Cockpit server, as a tree or — with --tools — as the list "
                    "of actions it offers, with their descriptions and input values.",
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
    parser.add_argument("--tools", action="store_true",
                        help="show the actions as a tree of mock tools — name, signature, "
                             "inputs and selectors — instead of the capability tree")
    parser.add_argument("--flat", action="store_true",
                        help="with --tools: the detailed one-tool-at-a-time listing "
                             "instead of the tree")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the server's TLS certificate")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a tree")
    args = parser.parse_args(argv)

    if args.tools and not args.controls:
        parser.error("--tools needs the per-page scan: drop --no-controls")
    if args.flat and not args.tools:
        parser.error("--flat only applies to --tools")

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
    if args.tools:
        tools = build_tools(packages)
        if args.json:
            print(tools_to_json(tools))
        elif args.flat:
            render_tools(tools, title)
            print()
        else:
            render_tree(build_tools_tree(tools, title))
            print()
        log(f"{summarise(packages)} → {len(tools)} mock tool(s), "
            f"{sum(len(t.params) for t in tools)} input(s), "
            f"{sum(1 for t in tools if not t.params)} without inputs")
        return 0

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
