#!/usr/bin/env python3
"""
cockpit_list_tools.py — list the capabilities of a *running* Cockpit server.

WHAT IT DOES
============
Cockpit's UI is a set of *packages*; each package ships a manifest.json that
declares its pages (menu/tools/dashboard entries, see doc/packages.md), and
may expose shell commands. Everything a package can *do* is announced in that
manifest — that is the "capabilities" an outside tool (e.g. an MCP server)
would want to discover, without needing the package source.

This script talks to a running Cockpit server the way the browser does:

    1. GET /cockpit/login with Authorization: Basic base64(user:password\\0)
       (pkg/static/login.js) → session cookie.
    2. WebSocket /cockpit/socket, subprotocol cockpit1, + the "init" handshake
       (doc/protocol.md, "Command: init"). This is the channel any MCP wrapper
       needs before it can open dbus/stream/fsread channels — we verify it
       works, but don't fail the listing if the server rejects the handshake
       (e.g. an Origin allow-list) since the manifest only needs the cookie.
    3. GET /cockpit/@<host>/manifests.json (src/cockpit/packages.py) → the
       pages and commands actually *installed* on that host.

The output is a tree of package → pages / commands; or JSON with --json.

DEPENDENCIES
============
Python 3 stdlib only. No pip installs, and no local checkout of the source —
every piece of data comes from the server.

USAGE
=====
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py                 # whole server, tree
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --package pinger
    COCKPIT_PASSWORD=… ./cockpit_list_tools.py --json          # machine readable (for MCP)

    COCKPIT_HOST=myhost COCKPIT_PORT=9090 COCKPIT_TLS=1 COCKPIT_PASSWORD=… ./cockpit_list_tools.py
    COCKPIT_CA=/path/ca.pem COCKPIT_PASSWORD=… ./cockpit_list_tools.py   # pin a CA
    COCKPIT_TLS_INSECURE=1 COCKPIT_PASSWORD=… ./cockpit_list_tools.py    # skip verification

If COCKPIT_PASSWORD is unset you are prompted for it on the (tty) command line.
The default user is the current local user; override with COCKPIT_USER.
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
import socket
import ssl
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator


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
# Cockpit client (login, WebSocket init, manifests.json)
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


def get_capabilities() -> dict[str, Any]:
    """Log in to the server and return its manifests.json — its capabilities."""
    user = os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD") or getpass.getpass(
        f"Cockpit password for {user}@{HOST}:{PORT}: ")

    log(f"logging in as {user}@{HOST}:{PORT}  ({SCHEME}://…)")
    cookie = do_login(user, password)
    cookie_name, _, cookie_val = cookie.partition("=")
    log(f"   -> session cookie: {cookie_name}={cookie_val[:20]}…")

    # Verify the WebSocket endpoint + init handshake work (what an MCP wrapper
    # needs), but don't fail the listing over it — the manifest only needs the
    # cookie, and some servers gate the socket on an Origin allow-list.
    log(f"verifying WebSocket {SCHEME}://{HOST}:{PORT}/cockpit/socket (subprotocol: cockpit1)")
    try:
        with contextlib.closing(ws_connect(cookie)) as ws:
            init_msg = ws.init_session()
        log(f"   session inited: host={init_msg.get('host')!r}  seed={init_msg.get('channel-seed')!r}")
    except (OSError, RuntimeError) as exc:
        log(f"   (warning) websocket init failed: {exc}")

    log(f"fetching {SCHEME}://{HOST}:{PORT}/cockpit/@{HOST}/manifests.json")
    return fetch_manifests(cookie)


# ==========================================================================
# The model: package → pages / commands (everything from manifests.json)
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
class Page:
    """A manifest entry (menu/tools/dashboard, or a package-private section)."""
    section: str
    key: str
    label: str
    path: str                      # the HTML file, relative to the package
    keywords: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)

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
        if not pkg.pages and not pkg.commands:
            pnode.add("(no menu/tools/dashboard/commands entries)")
    return root


def to_json(packages: list[Package]) -> str:
    """Same tree as the ASCII output, as JSON — the shape an MCP server would serve."""
    return json.dumps([dataclasses.asdict(p) for p in packages], indent=2)


def summarise(packages: list[Package]) -> str:
    pages = sum(len(p.pages) for p in packages)
    commands = sum(len(p.commands) for p in packages)
    return f"{len(packages)} package(s), {pages} page(s), {commands} command(s)"


# ==========================================================================
# Main
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the capabilities (pages & commands) of a running Cockpit server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment: COCKPIT_HOST/PORT/USER/PASSWORD/TLS/TLS_INSECURE/CA/SUPERUSER/TIMEOUT")
    parser.add_argument("--package", metavar="NAME", action="append", default=[],
                        help="restrict to this package (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a tree")
    args = parser.parse_args(argv)

    manifests = get_capabilities()
    packages = build_packages(manifests, args.package)
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
