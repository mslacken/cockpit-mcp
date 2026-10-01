#!/usr/bin/env python3
"""
cockpit_list_tools.py — map Cockpit to a tree of pages → interactions → commands.

WHAT IT DOES
============
Cockpit's UI is a set of *packages*; each package ships a manifest.json that
declares its pages (menu/tools/dashboard entries, see doc/packages.md), and each
page is an HTML file plus the JavaScript that drives it. Everything the page can
*do* to the machine goes through the cockpit API (cockpit.spawn, cockpit.dbus,
cockpit.file, …, see doc/latest/cockpit.html).

This script turns that into a tree:

    package
      └── page               (manifest entry: label, path, keywords)
            ├── actions      (buttons & co, with the handler they fire and the
            │                 cockpit.* calls that handler makes)
            └── variables    (inputs/selects/checkboxes the actions read)

For the example plugin from
https://cockpit-project.org/blog/creating-plugins-for-the-cockpit-user-interface.html
(shipped here as examples/pinger) that is:

    pinger
      └── [tools] Pinger → ping.html
            ├── actions
            │     └── button #ping "Ping" → ping_run()
            │           ├── spawn: ping -c 4 $address
            │           └── reads: $address
            └── variables
                  └── $address  input[text] = "8.8.8.8"  (label "Address")

i.e. exactly the ``ping_run`` command and the ``address`` variable.

TWO SOURCES OF TRUTH
====================
--source DIR (default: the checkout this script lives in)
    Static analysis of the Cockpit *source*. This is where the interactions
    live: the shipped packages are esbuild bundles (see build.js), so the
    buttons are only readable in pkg/<name>/*.{html,js,jsx,ts,tsx}.

--live
    Talk to a running Cockpit server the way the browser does:
      1. GET /cockpit/login with Authorization: Basic base64(user:password\\0)
         (pkg/static/login.js) → session cookie.
      2. WebSocket /cockpit/socket, subprotocol cockpit1, + the "init"
         handshake (doc/protocol.md, "Command: init"). Not needed for the
         listing, but it is the part any MCP wrapper needs before it can open
         dbus/stream/fsread channels.
      3. GET /cockpit/@<host>/manifests.json (src/cockpit/packages.py) → the
         pages actually *installed* on that host.
    The page list then comes from the server and the interactions are still
    filled in from the local source, so the two can be cross-checked.

DEPENDENCIES
============
Python 3 stdlib only. No pip installs.

USAGE
=====
    ./cockpit_list_tools.py                     # whole checkout, tree output
    ./cockpit_list_tools.py --package pinger    # one package
    ./cockpit_list_tools.py --json              # machine readable (for MCP)
    ./cockpit_list_tools.py --follow-lib        # also descend into pkg/lib

    COCKPIT_USER=root COCKPIT_PASSWORD=… ./cockpit_list_tools.py --live
    COCKPIT_HOST=myhost COCKPIT_PORT=9090 COCKPIT_TLS=1 ./cockpit_list_tools.py --live
    COCKPIT_CA=/path/ca.pem ./cockpit_list_tools.py --live   # pin a CA;
        # COCKPIT_TLS_INSECURE=1 skips verification altogether

CAVEAT
======
The source analysis is lexical, not a JS parser: it brace-matches and pattern
matches. It finds the common Cockpit/PatternFly idioms and will miss
dynamically built UI. Treat the output as a map, not as a contract.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import getpass
import http.client
import json
import os
import re
import socket
import ssl
import struct
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator


def _envbool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


HOST = os.environ.get("COCKPIT_HOST", "localhost")
PORT = int(os.environ.get("COCKPIT_PORT", "9090"))
USE_TLS = _envbool("COCKPIT_TLS")
INSECURE = _envbool("COCKPIT_TLS_INSECURE")
CA_CERT = os.environ.get("COCKPIT_CA") or os.environ.get("SSL_CERT_FILE")
TIMEOUT = int(os.environ.get("COCKPIT_TIMEOUT", "10"))
SCHEME = "wss" if USE_TLS else "ws"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ==========================================================================
# Part 1 — live Cockpit client (login, WebSocket init, manifests.json)
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
    """Small blocking HTTP client. Returns the response (for status/headers) and its body."""
    conn: http.client.HTTPConnection = (
        http.client.HTTPSConnection(HOST, PORT, timeout=TIMEOUT, context=_tls_context())
        if USE_TLS else http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT))
    try:
        conn.request("GET", path, headers={**headers, "Connection": "close"})
        resp = conn.getresponse()
        return resp, resp.read()
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
    path = f"/cockpit/@{HOST}/manifests.json"
    resp, body = http_get(path, {"Cookie": cookie})
    if resp.status != 200:
        raise RuntimeError(f"GET {path} failed: HTTP {resp.status}")
    return json.loads(body)


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
        if data is None or len(data) != n:
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


def live_manifests() -> dict[str, Any]:
    """Log in, run the init handshake, and return the server's manifests.json."""
    user = os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD") or getpass.getpass(
        f"Cockpit password for {user}@{HOST}:{PORT}: ")

    log(f"1. logging in as {user}@{HOST}:{PORT}  ({SCHEME}://…)")
    cookie = do_login(user, password)
    cookie_name, _, cookie_val = cookie.partition("=")
    log(f"   -> session cookie: {cookie_name}={cookie_val[:20]}…")

    log(f"2. opening WebSocket {SCHEME}://{HOST}:{PORT}/cockpit/socket (subprotocol: cockpit1)")
    with contextlib.closing(ws_connect(cookie)) as ws:
        init_msg = ws.init_session()
    log(f"   session inited:  host={init_msg.get('host')!r}")
    log(f"   channel-seed    = {init_msg.get('channel-seed')!r}")

    log(f"3. fetching {SCHEME}://{HOST}:{PORT}/cockpit/@{HOST}/manifests.json")
    return fetch_manifests(cookie)


# ==========================================================================
# Part 2 — the model: package → page → actions / variables / calls
# ==========================================================================

@dataclass
class Call:
    """One cockpit API call: what the page can make the machine do."""
    api: str                       # spawn | script | dbus | file | http | channel | jump | …
    detail: str                    # rendered argv / path / method
    options: str | None            # e.g. superuser: require
    file: str
    line: int
    via: str | None = None         # reached through this intermediate function
    offset: int = field(default=0, repr=False)

    def render(self) -> str:
        out = f"{self.api}: {self.detail}" if self.detail else self.api
        if self.options:
            out += f"   [{self.options}]"
        if self.via:
            out += f"   (via {self.via}())"
        return f"{out}   —  {self.file}:{self.line}"


@dataclass
class Variable:
    """An input the user can fill in; the "input variables" of a page."""
    name: str                      # element id, else name= / label
    kind: str                      # input[text], TextInput, Checkbox, FormSelect, …
    label: str | None
    default: str | None
    file: str
    line: int

    def render(self) -> str:
        out = f"${self.name}  {self.kind}"
        if self.default is not None:
            out += f" = {self.default!r}"
        if self.label and self.label != self.name:
            out += f'  (label "{self.label}")'
        return f"{out}   —  {self.file}:{self.line}"


@dataclass
class Action:
    """Something the user can click; the leaf an MCP wrapper would expose as a tool."""
    kind: str                      # button | Button | MenuItem | onClick:<Tag> | …
    name: str                      # id if there is one, else the label
    label: str | None
    handler: str | None            # function name, "<inline>", or None
    file: str
    line: int
    calls: list[Call] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    # body of the handler, as (module path, start, end) — used to attach calls
    body: tuple[str, int, int] | None = field(default=None, repr=False)

    def render(self) -> str:
        out = f"{self.kind}"
        if self.name and self.name != self.label:
            out += f" #{self.name}"
        if self.label:
            out += f' "{self.label}"'
        if self.handler:
            out += f" → {self.handler}" + ("" if self.handler == "<inline>" else "()")
        return f"{out}   —  {self.file}:{self.line}"


@dataclass
class Page:
    """A manifest entry (menu/tools/dashboard) and everything behind it."""
    package: str
    section: str                   # menu | tools | dashboard | <package-private>
    key: str
    label: str
    path: str                      # the HTML file, relative to the package
    keywords: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)   # not tied to an action
    note: str | None = None        # why there is nothing below (no source, …)


@dataclass
class Package:
    name: str
    directory: str | None          # relative to --source, None when live-only
    pages: list[Page] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass(frozen=True)
class Options:
    """Knobs for the source analysis."""
    follow_lib: bool = False       # descend into the shared components in pkg/lib
    max_files: int = 120           # module graph budget per page


# ==========================================================================
# Part 3 — lexical helpers (we brace-match; we do not parse JavaScript)
# ==========================================================================

_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _skip_string(text: str, i: int) -> int:
    """Index just past the string literal starting at `text[i]` (', " or `)."""
    quote = text[i]
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if quote == "`" and c == "$" and text[i + 1:i + 2] == "{":
            i = _match(text, i + 1)        # template substitution can nest anything
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def _skip_comment(text: str, i: int) -> int:
    if text.startswith("//", i):
        j = text.find("\n", i)
    else:
        j = text.find("*/", i)
        j = j + 2 if j >= 0 else -1
    return len(text) if j < 0 else j


def _match(text: str, start: int) -> int:
    """Index just past the bracket opened at `start`, skipping strings and comments."""
    stack = [_PAIRS[text[start]]]
    n = len(text)
    i = start + 1
    while i < n and stack:
        c = text[i]
        if c in "\"'`":
            i = _skip_string(text, i)
        elif c == "/" and text[i + 1:i + 2] in ("/", "*"):
            i = _skip_comment(text, i)
        elif c in _PAIRS:
            stack.append(_PAIRS[c])
            i += 1
        elif c == stack[-1]:
            stack.pop()
            i += 1
        elif c in ")]}":
            return i                        # unbalanced: bail out rather than run away
        else:
            i += 1
    return i


def _inner(text: str, start: int) -> str:
    """Contents of the bracket opened at `start`, brackets excluded."""
    return text[start + 1:_match(text, start) - 1]


def _split_top(text: str) -> list[str]:
    """Split on commas that are not inside brackets or strings."""
    parts: list[str] = []
    depth = 0
    cur = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            i = _skip_string(text, i)
            continue
        if c == "/" and text[i + 1:i + 2] in ("/", "*"):
            i = _skip_comment(text, i)
            continue
        if c in _PAIRS:
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(text[cur:i])
            cur = i + 1
        i += 1
    parts.append(text[cur:])
    return [p.strip() for p in parts if p.strip()]


_STR_RE = re.compile(r"""^(['"])(.*)\1$""", re.S)
_TPL_RE = re.compile(r"^`(.*)`$", re.S)


def _as_literal(expr: str) -> str | None:
    """The value of `expr` if it is a plain string literal, else None."""
    m = _STR_RE.match(expr.strip())
    return m.group(2) if m else None


def _render_value(expr: str) -> str:
    """Render one JS expression as a shell-ish word: literals verbatim, else $name."""
    expr = " ".join(expr.split())
    if re.fullmatch(r"null|undefined|true|false|-?\d+(\.\d+)?", expr):
        return expr
    if (lit := _as_literal(expr)) is not None:
        return lit if lit and " " not in lit else repr(lit)
    if m := _TPL_RE.match(expr):
        return m.group(1).replace("${", "$").replace("}", "")
    if expr.startswith("..."):
        return "$" + _render_value(expr[3:]).lstrip("$") + "[…]"
    # address.value / this.state.name / props.foo → $address, $name, $foo
    if m := re.fullmatch(r"[\w$.]+", expr):
        parts = [p for p in m.group(0).split(".") if p not in ("value", "current", "this")]
        if parts:
            return "$" + parts[-1]
    return "${" + (expr if len(expr) <= 40 else expr[:37] + "…") + "}"


def _render_argv(expr: str) -> str:
    """Render the first argument of cockpit.spawn()/script() as a command line."""
    expr = expr.strip()
    if expr.startswith("["):
        return " ".join(_render_value(p) for p in _split_top(_inner(expr, 0)))
    if (lit := _as_literal(expr)) is not None:
        return lit.strip()
    if m := _TPL_RE.match(expr):
        return " ".join(m.group(1).split())
    return _render_value(expr)


def _render_options(expr: str | None) -> str | None:
    """Pick the interesting bits out of a cockpit API options object."""
    if not expr or not expr.strip().startswith("{"):
        return None
    wanted = []
    for key in ("superuser", "host", "directory", "err", "bus", "payload", "pty"):
        if m := re.search(rf"\b{key}\s*:\s*([^,}}]+)", expr):
            wanted.append(f"{key}: {_render_value(m.group(1))}")
    return ", ".join(wanted) or None


def _clean_label(raw: str | None) -> str | None:
    """Turn a JSX child / prop value into display text: {_("Ping")} → Ping."""
    if raw is None:
        return None
    raw = raw.strip()
    if m := re.search(r"""\b_\(\s*(['"])(.*?)\1""", raw, re.S):
        return " ".join(m.group(2).split())
    if (lit := _as_literal(raw)) is not None:
        return " ".join(lit.split())
    if "{" in raw or "<" in raw:
        return None
    return " ".join(raw.split()) or None


# ==========================================================================
# Part 4 — HTML scanning
# ==========================================================================

HTML_ACTION_TAGS = {"button"}
HTML_INPUT_TAGS = {"input", "select", "textarea"}


class PageHtml(HTMLParser):
    """Collect <script src>, buttons, inputs and <label for> from a page's HTML."""

    def __init__(self, relpath: str) -> None:
        super().__init__(convert_charrefs=True)
        self.relpath = relpath
        self.scripts: list[str] = []
        self.actions: list[Action] = []
        self.variables: list[Variable] = []
        self.labels: dict[str, str] = {}       # input id → <label for=…> text
        self._open: tuple[str, Action | None] | None = None
        self._label_for: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag in HTML_ACTION_TAGS:
            action = Action(kind=tag, name=a.get("id", ""), label=None, handler=None,
                            file=self.relpath, line=line)
            self.actions.append(action)
            self._open, self._text = (tag, action), []
        elif tag in HTML_INPUT_TAGS:
            kind = f"input[{a.get('type', 'text')}]" if tag == "input" else tag
            name = a.get("id") or a.get("name") or f"<{tag}>"
            self.variables.append(Variable(name=name, kind=kind, label=None,
                                           default=a.get("value") or a.get("placeholder"),
                                           file=self.relpath, line=line))
        elif tag == "label" and a.get("for"):
            self._label_for, self._text = a["for"], []

    def handle_data(self, data: str) -> None:
        self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        text = " ".join("".join(self._text).split())
        if self._open and self._open[0] == tag:
            action = self._open[1]
            if action is not None:
                action.label = text or None
                action.name = action.name or text
            self._open = None
        elif tag == "label" and self._label_for:
            if text:
                self.labels[self._label_for] = text
            self._label_for = None
        self._text = []


# ==========================================================================
# Part 5 — JavaScript / JSX scanning
# ==========================================================================

# cockpit.<api>(…) — the whole surface a page uses to reach the machine
# (doc/latest/cockpit.html). These are what an MCP server would ultimately call.
COCKPIT_APIS = ("spawn", "script", "dbus", "file", "http", "channel", "jump", "upload")
_CALL_RE = re.compile(r"\bcockpit\s*\.\s*(" + "|".join(COCKPIT_APIS) + r")\s*\(")
_DBUS_CALL_RE = re.compile(r"\.call\s*\(")
_SUPERUSER_RE = re.compile(r"\bsuperuser\s*\.\s*(require|reload_page_on_change)\b")

# PatternFly / HTML components that hold a value the user types or picks
INPUT_COMPONENTS = {
    "TextInput", "TextArea", "TextInputGroup", "Checkbox", "Switch", "Radio",
    "FormSelect", "Select", "SimpleSelect", "NumberInput", "Slider", "FileUpload",
    "FileAutoComplete", "TypeaheadSelect", "MultiTypeaheadSelect", "DatePicker",
    "TimePicker", "SearchInput", "input", "select", "textarea",
}
ACTION_COMPONENTS = {"Button", "MenuItem", "DropdownItem", "ToggleGroupItem", "Tab", "button", "a"}
EVENT_PROPS = ("onClick", "onSelect", "onConfirm", "onApply", "onSubmit")

_IMPORT_RE = re.compile(r"""\bimport\s+(?:[^'"]*?\bfrom\s*)?['"]([^'"]+)['"]""")
_GET_BY_ID_RE = re.compile(
    r"""\b(?:const|let|var)\s+([\w$]+)\s*=\s*document\s*\.\s*getElementById\(\s*['"]([^'"]+)['"]""")
# x.addEventListener("click", …) / document.getElementById("x").addEventListener(…)
_LISTENER_RE = re.compile(
    r"""(?:[\w$.]*\(\s*['"]([^'"]+)['"]\s*\)|([\w$]+))"""
    r"""\s*\.\s*addEventListener\s*\(\s*['"](\w+)['"]\s*,\s*""")
# the older idiom: x.onclick = …  (pkg/playground/index.js)
_ONEVENT_RE = re.compile(
    r"""(?:[\w$.]*\(\s*['"]([^'"]+)['"]\s*\)|([\w$]+))"""
    r"""\s*\.\s*on(click|change|submit|input)\s*=\s*""")
DOM_EVENTS = ("click", "change", "submit", "input")
_JSX_RE = re.compile(r"<([A-Z][\w.]*|input|select|textarea|button|a)(?=[\s/>])")

_FUNC_RES = (
    re.compile(r"\bfunction\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?=\()"),
    re.compile(r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
               r"(?:async\s+)?(?:function\s*)?(?=\(|[A-Za-z_$])"),
    re.compile(r"^[ \t]+(?:async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*(?=\([^;]*\)\s*\{)", re.M),
    re.compile(r"\bthis\s*\.\s*(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?=\(|function|async)"),
)


def _body_span(text: str, pos: int) -> tuple[int, int] | None:
    """Span of the function body whose signature starts at `pos`.

    Handles ``f(a) { … }``, ``(a) => { … }`` and the expression form
    ``(a) => cockpit.spawn([…])`` (which has no braces to match).
    """
    n = len(text)
    i = pos
    if i < n and text[i] == "(":            # parameter list
        i = _match(text, i)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if text.startswith("=>", i):
        i += 2
        while i < n and text[i] in " \t\r\n":
            i += 1
    if i < n and text[i] == "{":
        return (i, _match(text, i))
    if i >= n or text[i] in ";,)":
        return None
    # expression body: run to the end of the statement at depth 0
    depth = 0
    j = i
    while j < n:
        c = text[j]
        if c in "\"'`":
            j = _skip_string(text, j)
            continue
        if c == "/" and text[j + 1:j + 2] in ("/", "*"):
            j = _skip_comment(text, j)
            continue
        if c in _PAIRS:
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == ";" and depth == 0:
            break
        j += 1
    return (i, j) if j > i else None


def _jsx_open_end(text: str, i: int) -> int:
    """Index of the '>' closing the JSX opening tag that starts at `i`."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            i = _skip_string(text, i)
        elif c == "{":
            i = _match(text, i)
        elif c == "/" and text[i + 1:i + 2] in ("/", "*"):
            i = _skip_comment(text, i)
        elif c == ">":
            return i
        else:
            i += 1
    return n


_PROP_RE = re.compile(r"([A-Za-z_][\w:.-]*)\s*=\s*")


def _jsx_props(attrs: str) -> dict[str, str]:
    """Parse a JSX attribute blob into {prop: raw source of its value}."""
    props: dict[str, str] = {}
    i = 0
    n = len(attrs)
    while i < n:
        m = _PROP_RE.search(attrs, i)
        if not m:
            break
        j = m.end()
        if j < n and attrs[j] == "{":
            end = _match(attrs, j)
        elif j < n and attrs[j] in "\"'`":
            end = _skip_string(attrs, j)
        else:                                   # bare value, ends at the next space
            space = attrs.find(" ", j)
            end = n if space < 0 else space
        props[m.group(1)] = attrs[j:end].strip()
        i = max(end, j + 1)
    return props


def _prop_text(raw: str | None) -> str | None:
    """Unwrap a prop value: {…} → …, "…" → …."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    return raw or None


class Module:
    """One analysed source file."""

    def __init__(self, path: Path, relpath: str, text: str) -> None:
        self.path = path
        self.rel = relpath
        self.text = text
        self.imports: list[str] = []
        self.functions: dict[str, tuple[int, int]] = {}
        self.dom_ids: dict[str, str] = {}      # JS variable name → DOM element id
        self.actions: list[Action] = []
        self.variables: list[Variable] = []
        self.calls: list[Call] = []
        self._line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def line_of(self, offset: int) -> int:
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    # -- scanning ---------------------------------------------------------

    def scan(self) -> None:
        self._scan_imports()
        self._scan_functions()
        self._scan_dom()
        self._scan_jsx()
        self._scan_calls()

    def _scan_imports(self) -> None:
        self.imports = [m.group(1) for m in _IMPORT_RE.finditer(self.text)]

    def _scan_functions(self) -> None:
        for regex in _FUNC_RES:
            for m in regex.finditer(self.text):
                name = m.group("name")
                if name in self.functions or name in ("if", "for", "while", "switch", "catch"):
                    continue
                if span := _body_span(self.text, m.end()):
                    self.functions[name] = span

    def _handler_at(self, pos: int) -> tuple[str, tuple[str, int, int] | None]:
        """Read the event handler written at `pos`: a name, or an inline function."""
        text = self.text
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        m = re.compile(r"[A-Za-z_$][\w$.]*").match(text, pos)
        word = m.group(0) if m else ""
        if word and word not in ("function", "async"):
            if text[m.end():m.end() + 4].lstrip().startswith("=>"):   # event => { … }
                span = _body_span(text, text.index("=>", m.end()))
                return "<inline>", (self.rel, *span) if span else None
            return word.split(".")[-1], None
        paren = text.find("(", pos)                                   # (…) => … / function (…)
        span = _body_span(text, paren) if 0 <= paren < pos + 40 else None
        return "<inline>", (self.rel, *span) if span else None

    def _scan_dom(self) -> None:
        """Plain-DOM pages: ids bound to variables, and click handlers (examples/pinger)."""
        self.dom_ids = {m.group(1): m.group(2) for m in _GET_BY_ID_RE.finditer(self.text)}
        for regex in (_LISTENER_RE, _ONEVENT_RE):
            for m in regex.finditer(self.text):
                selector, var, event = m.group(1), m.group(2), m.group(3)
                if event not in DOM_EVENTS or var in ("document", "window"):
                    continue
                target = selector or self.dom_ids.get(var or "", "") or (var or "?")
                handler, body = self._handler_at(m.end())
                self.actions.append(Action(
                    kind=f"on{event}", name=target.lstrip("#."), label=None, handler=handler,
                    file=self.rel, line=self.line_of(m.start()), body=body))

    def _scan_jsx(self) -> None:
        """React pages: <Button onClick={…}>Label</Button>, <TextInput id=… />."""
        for m in _JSX_RE.finditer(self.text):
            tag = m.group(1)
            short = tag.rsplit(".", 1)[-1]
            end = _jsx_open_end(self.text, m.end())
            attrs = self.text[m.end():end].rstrip()
            self_closing = attrs.endswith("/")
            props = _jsx_props(attrs.rstrip("/"))
            line = self.line_of(m.start())
            label = (_clean_label(props.get("label")) or _clean_label(props.get("aria-label"))
                     or _clean_label(props.get("title")))

            if short in INPUT_COMPONENTS and not (set(props) & set(EVENT_PROPS) - {"onSelect"}):
                name = (_prop_text(props.get("id")) or _prop_text(props.get("name")) or "")
                name = _as_literal(name) or (name.lstrip("`").rstrip("`") if name else None) \
                    or label or f"<{short}>"
                kind = short
                if short == "input":           # <input> defaults to type=text, like in HTML
                    t = _as_literal(_prop_text(props.get("type")) or "")
                    kind = f"input[{t or 'text'}]" if "type" not in props or t else "input"
                default = _clean_label(props.get("value") or props.get("defaultValue"))
                self.variables.append(Variable(name=name, kind=kind, label=label,
                                               default=default, file=self.rel, line=line))
                continue

            event = next((p for p in EVENT_PROPS if p in props), None)
            if short not in ACTION_COMPONENTS and event is None:
                continue
            if short in ACTION_COMPONENTS and event is None and short in ("a",):
                continue

            children = None
            if not self_closing:
                close = self.text.find(f"</{tag}>", end)
                if 0 <= close <= end + 400:
                    children = self.text[end + 1:close]
            name = _as_literal(_prop_text(props.get("id")) or "") or ""
            handler = None
            body = None
            if event:
                raw = props[event]
                expr = _prop_text(raw) or ""
                if re.fullmatch(r"[\w$.]+", expr):
                    handler = expr.split(".")[-1]
                else:
                    handler = "<inline>"
                    base = m.end() + attrs.find(raw)
                    body = (self.rel, base, base + len(raw))
            self.actions.append(Action(
                kind=short if short in ACTION_COMPONENTS else f"{event}:{short}",
                name=name or _clean_label(children) or label or handler or "?",
                label=_clean_label(children) or label,
                handler=handler, file=self.rel, line=line, body=body))

    def _scan_calls(self) -> None:
        for m in _CALL_RE.finditer(self.text):
            api = m.group(1)
            open_paren = m.end() - 1
            args = _split_top(_inner(self.text, open_paren))
            detail = ""
            options = None
            if api in ("spawn", "script"):
                detail = _render_argv(args[0]) if args else ""
                options = _render_options(args[1] if len(args) > 1 else None)
            elif api == "dbus":
                detail = _render_value(args[0]) if args else "<internal>"
                options = _render_options(args[1] if len(args) > 1 else None)
                detail += self._dbus_method(_match(self.text, open_paren))
            elif api in ("file", "upload", "http", "jump"):
                detail = _render_value(args[0]) if args else ""
                options = _render_options(args[1] if len(args) > 1 else None)
            elif api == "channel":
                detail = _render_options(args[0] if args else None) or ""
            self.calls.append(Call(api=api, detail=detail, options=options, file=self.rel,
                                   line=self.line_of(m.start()), offset=m.start()))
        for m in _SUPERUSER_RE.finditer(self.text):
            self.calls.append(Call(api="superuser", detail=m.group(1), options=None,
                                   file=self.rel, line=self.line_of(m.start()),
                                   offset=m.start()))

    def _dbus_method(self, after: int) -> str:
        """For cockpit.dbus(…).call(path, iface, method, …) append ' → iface.method'."""
        tail = self.text[after:after + 200]
        if not (m := _DBUS_CALL_RE.match(tail.lstrip())):
            return ""
        start = after + (len(tail) - len(tail.lstrip())) + m.end() - 1
        args = _split_top(_inner(self.text, start))
        if len(args) >= 3:
            return f" → {_render_value(args[1])}.{_render_value(args[2])}"
        return ""


# ==========================================================================
# Part 6 — wiring modules into pages
# ==========================================================================

SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")
# Framework scripts every page pulls in; they contain no page interactions.
SKIP_SCRIPTS = {"cockpit.js", "po.js", "manifests.js", "polyfills.js"}


def _resolve_script(pkgdir: Path, ref: str, root: Path, opts: Options) -> Path | None:
    """Map a <script src>/import specifier to a source file.

    The HTML references the *built* bundle (``overview.js``), but the source
    is ``overview.jsx`` — see the entries list in files.js. So try the exact
    name first, then the JSX/TS spellings.
    """
    ref = ref.split("?")[0]
    if Path(ref).name in SKIP_SCRIPTS:
        return None
    # <script src="pinger.js"> is directory-relative; so is import "./kdump-view.jsx".
    base = (pkgdir / ref).resolve()
    candidates = [base, *(base.with_suffix(s) for s in SOURCE_SUFFIXES),
                  *(base / f"index{s}" for s in SOURCE_SUFFIXES),
                  *(Path(f"{base}{s}") for s in SOURCE_SUFFIXES)]
    if opts.follow_lib and not ref.startswith((".", "/")):
        lib = root / "pkg" / "lib"            # bare specifiers resolve here (tsconfig.json paths)
        candidates += [lib / ref, *(Path(f"{lib / ref}{s}") for s in SOURCE_SUFFIXES),
                       *((lib / ref).with_suffix(s) for s in SOURCE_SUFFIXES)]
    for c in candidates:
        if c.is_file() and c.suffix in SOURCE_SUFFIXES:
            with contextlib.suppress(ValueError):
                c.relative_to(root)           # never wander outside the checkout
                return c
    return None


def analyse_page(page: Page, pkgdir: Path, root: Path, opts: Options) -> None:
    """Fill in actions/variables/calls for one page by walking its source files."""
    html_file = pkgdir / page.path
    if not html_file.is_file():
        page.note = f"no HTML at {page.path}"
        return

    html = PageHtml(str(html_file.relative_to(root)))
    html.feed(html_file.read_text(errors="replace"))
    page.sources.append(html.relpath)
    page.actions += html.actions
    page.variables += html.variables
    for var in page.variables:
        var.label = var.label or html.labels.get(var.name)

    # Breadth-first through the module graph, starting at the page's scripts.
    modules: dict[str, Module] = {}
    queue = [p for s in html.scripts if (p := _resolve_script(pkgdir, s, root, opts))]
    seen = {str(p) for p in queue}
    while queue and len(modules) < opts.max_files:
        path = queue.pop(0)
        rel = str(path.relative_to(root))
        try:
            mod = Module(path, rel, path.read_text(errors="replace"))
            mod.scan()
        except (OSError, RecursionError, ValueError) as exc:   # never let one file kill the run
            log(f"   warning: skipping {rel}: {exc}")
            continue
        modules[rel] = mod
        page.sources.append(rel)
        for spec in mod.imports:
            dep = _resolve_script(path.parent, spec, root, opts)
            if dep and str(dep) not in seen:
                seen.add(str(dep))
                queue.append(dep)

    if queue:
        page.note = f"truncated at --max-files {opts.max_files}, {len(queue)} module(s) left"

    for mod in modules.values():
        page.actions += mod.actions
        page.variables += mod.variables

    _merge_html_and_js(page)
    _attach_calls(page, modules)
    _dedup(page)


def _merge_html_and_js(page: Page) -> None:
    """A <button id=ping> in HTML and its addEventListener in JS are one action."""
    by_id: dict[str, Action] = {}
    merged: list[Action] = []
    for action in page.actions:
        key = action.name
        if key and key in by_id and (by_id[key].handler is None or action.handler is None):
            first = by_id[key]
            first.label = first.label or action.label
            first.handler = first.handler or action.handler
            first.body = first.body or action.body
            if first.kind.startswith("on"):     # prefer the element kind over the event kind
                first.kind, first.file, first.line = action.kind, action.file, action.line
            continue
        if key:
            by_id[key] = action
        merged.append(action)
    page.actions = merged


def _attach_calls(page: Page, modules: dict[str, Module], depth: int = 2) -> None:
    """Hang every cockpit.* call under the action whose handler makes it."""
    functions: dict[str, tuple[Module, tuple[int, int]]] = {}
    for mod in modules.values():
        for name, span in mod.functions.items():
            functions.setdefault(name, (mod, span))

    all_calls = [c for mod in modules.values() for c in mod.calls]
    claimed: set[int] = set()
    var_names = {v.name for v in page.variables}

    def collect(mod: Module, span: tuple[int, int], via: str | None,
                left: int, seen: set[str]) -> tuple[list[Call], list[str]]:
        start, end = span
        body = mod.text[start:end]
        calls = []
        for call in mod.calls:
            if start <= call.offset < end:
                claimed.add(id(call))
                calls.append(dataclasses.replace(call, via=via))
        # which inputs does this handler read? either by id, or through the JS
        # variable that document.getElementById() put the element into
        reads = [n for n in sorted(var_names) if re.search(rf"\b{re.escape(n)}\b", body)]
        reads += [elid for jsvar, elid in mod.dom_ids.items()
                  if elid in var_names and re.search(rf"\b{re.escape(jsvar)}\b", body)]
        # one more hop: handleSave() → saveSettings() → cockpit.spawn(…)
        if left > 0:
            for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", body):
                name = m.group(1)
                # a local definition wins over a same-named one in another module
                target = ((mod, mod.functions[name]) if name in mod.functions
                          else functions.get(name))
                if name in seen or target is None:
                    continue
                seen.add(name)
                sub_mod, sub_span = target
                sub_calls, sub_reads = collect(sub_mod, sub_span, via or name, left - 1, seen)
                calls += sub_calls
                reads += sub_reads
        return calls, reads

    for action in page.actions:
        span: tuple[int, int] | None = None
        mod: Module | None = None
        if action.body:
            rel, start, end = action.body
            mod, span = modules.get(rel), (start, end)
        elif action.handler and action.handler in functions:
            mod, span = functions[action.handler]
        if mod is None or span is None:
            continue
        calls, reads = collect(mod, span, None, depth, {action.handler or ""})
        action.calls = calls
        action.reads = sorted(set(reads))

    page.calls = [c for c in all_calls if id(c) not in claimed]


def _dedup(page: Page) -> None:
    """Collapse repeats (the same <Button> rendered in a loop, the same spawn twice)."""
    def uniq(items: Iterable[Any], key) -> list[Any]:
        out, seen = [], set()
        for item in items:
            k = key(item)
            if k not in seen:
                seen.add(k)
                out.append(item)
        return out

    page.actions = uniq(page.actions, lambda a: (a.kind, a.name, a.label, a.handler))
    page.variables = uniq(page.variables, lambda v: (v.name, v.kind))
    page.calls = uniq(page.calls, lambda c: (c.api, c.detail, c.options))
    for action in page.actions:
        action.calls = uniq(action.calls, lambda c: (c.api, c.detail, c.options))


# ==========================================================================
# Part 7 — package discovery
# ==========================================================================

MANIFEST_SECTIONS = ("menu", "tools", "dashboard")


def _page_sections(manifest: dict[str, Any], pkgdir: Path | None) -> Iterator[str]:
    """menu/tools/dashboard, plus package-private sections of the same shape.

    pkg/playground/manifest.json declares its demo pages under a "playground"
    key that only its own index page reads; those are still pages.
    """
    yield from MANIFEST_SECTIONS
    for key, value in manifest.items():
        if key in MANIFEST_SECTIONS or not isinstance(value, dict) or not value:
            continue
        if not all(isinstance(v, dict) and "label" in v for v in value.values()):
            continue
        if pkgdir is None or all((pkgdir / (v.get("path") or f"{k}.html")).is_file()
                                 for k, v in value.items()):
            yield key


def _manifest_pages(pkgname: str, manifest: dict[str, Any],
                    pkgdir: Path | None = None) -> Iterator[Page]:
    """Yield a Page per manifest entry. doc/packages.md: menu/tools/dashboard."""
    for section in _page_sections(manifest, pkgdir):
        entries = manifest.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            entry = value if isinstance(value, dict) else {}
            keywords: list[str] = []
            for group in entry.get("keywords") or []:
                keywords += (group or {}).get("matches") or []
            yield Page(
                package=pkgname,
                section=section,
                key=key,
                label=entry.get("label") or key,
                path=entry.get("path") or f"{key}.html",
                keywords=keywords,
                docs=[d.get("url", "") for d in (entry.get("docs") or []) if isinstance(d, dict)],
            )


def discover_source_packages(root: Path) -> dict[str, Path]:
    """Find every package directory (one that has a manifest.json) under `root`."""
    found: dict[str, Path] = {}
    for parent in ("pkg", "examples", "."):
        base = root / parent
        if not base.is_dir():
            continue
        for manifest in sorted(base.glob("*/manifest.json")):
            found.setdefault(manifest.parent.name, manifest.parent)
    return found


def build_from_source(root: Path, only: list[str], opts: Options) -> list[Package]:
    packages = []
    for name, pkgdir in sorted(discover_source_packages(root).items()):
        if only and not any(o in (name, str(pkgdir.relative_to(root))) for o in only):
            continue
        manifest = json.loads((pkgdir / "manifest.json").read_text())
        pkg = Package(name=name, directory=str(pkgdir.relative_to(root)),
                      requires=manifest.get("requires") or {})
        for page in _manifest_pages(name, manifest, pkgdir):
            analyse_page(page, pkgdir, root, opts)
            pkg.pages.append(page)
        packages.append(pkg)
    return packages


def build_from_live(manifests: dict[str, Any], root: Path, only: list[str],
                    opts: Options) -> list[Package]:
    """Pages come from the server; interactions still come from the local checkout."""
    source_dirs = discover_source_packages(root)
    packages = []
    for name in sorted(manifests):
        if only and name not in only:
            continue
        manifest = manifests[name] or {}
        pkgdir = source_dirs.get(name)
        pkg = Package(name=name,
                      directory=str(pkgdir.relative_to(root)) if pkgdir else None,
                      requires=manifest.get("requires") or {},
                      note=None if pkgdir else "installed, but not in this checkout")
        for page in _manifest_pages(name, manifest, pkgdir):
            if pkgdir:
                analyse_page(page, pkgdir, root, opts)
            else:
                page.note = "no local source"
            pkg.pages.append(page)
        packages.append(pkg)
    return packages


# ==========================================================================
# Part 8 — output
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
        if pkg.directory:
            head += f"   [{pkg.directory}]"
        if pkg.requires:
            head += f"   requires {pkg.requires}"
        if pkg.note:
            head += f"   ({pkg.note})"
        pnode = root.add(head)
        if not pkg.pages:
            pnode.add("(no menu/tools/dashboard entries)")
        for page in pkg.pages:
            label = f"[{page.section}] {page.label}  →  {page.path}"
            if page.note:
                label += f"   ({page.note})"
            gnode = pnode.add(label)
            if page.keywords:
                gnode.add("keywords: " + ", ".join(sorted(set(page.keywords))))
            if page.actions:
                anode = gnode.add(f"actions ({len(page.actions)})")
                for action in page.actions:
                    node = anode.add(action.render())
                    for call in action.calls:
                        node.add(call.render())
                    if action.reads:
                        node.add("reads: " + ", ".join("$" + r for r in action.reads))
            if page.variables:
                vnode = gnode.add(f"variables ({len(page.variables)})")
                for var in page.variables:
                    vnode.add(var.render())
            if page.calls:
                cnode = gnode.add(f"other calls ({len(page.calls)})")
                for call in page.calls:
                    cnode.add(call.render())
            if not (page.actions or page.variables or page.calls or page.note):
                gnode.add("(no interactions found)")
    return root


def to_json(packages: list[Package]) -> str:
    """Same tree as the ASCII output, as JSON — the shape an MCP server would serve."""
    internal = ("offset", "body")        # byte offsets into the source; of no use outside

    def keep(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        return {k: v for k, v in pairs if k not in internal}

    return json.dumps([dataclasses.asdict(p, dict_factory=keep) for p in packages], indent=2)


def summarise(packages: list[Package]) -> str:
    pages = [p for pkg in packages for p in pkg.pages]
    actions = [a for p in pages for a in p.actions]
    calls = [c for p in pages for c in (p.calls + [x for a in p.actions for x in a.calls])]
    variables = [v for p in pages for v in p.variables]
    return (f"{len(packages)} package(s), {len(pages)} page(s), {len(actions)} action(s), "
            f"{len(variables)} input variable(s), {len(calls)} cockpit API call(s)")


# ==========================================================================
# Main
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment: COCKPIT_HOST/PORT/USER/PASSWORD/TLS/TLS_INSECURE/CA/SUPERUSER")
    parser.add_argument("--source", metavar="DIR", default=str(Path(__file__).resolve().parent),
                        help="Cockpit checkout to analyse (default: this script's directory)")
    parser.add_argument("--package", metavar="NAME", action="append", default=[],
                        help="restrict to this package (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="take the page list from a running server (login + WS init + "
                             "manifests.json) instead of from the checkout")
    parser.add_argument("--follow-lib", action="store_true",
                        help="also descend into pkg/lib shared components (slower, noisier)")
    parser.add_argument("--max-files", type=int, default=Options.max_files, metavar="N",
                        help=f"module graph budget per page (default: {Options.max_files})")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a tree")
    args = parser.parse_args(argv)

    opts = Options(follow_lib=args.follow_lib, max_files=args.max_files)
    root = Path(args.source).resolve()
    if not root.is_dir():
        raise RuntimeError(f"--source {root} is not a directory")

    if args.live:
        manifests = live_manifests()
        packages = build_from_live(manifests, root, args.package, opts)
        title = f"{SCHEME}://{HOST}:{PORT}  (interactions from {root})"
    else:
        packages = build_from_source(root, args.package, opts)
        title = f"{root}  (static analysis)"

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
