#!/usr/bin/env python3
"""
cockpit_list_tools.py — minimal, dependency-free Cockpit protocol client.

WHAT IT DOES
============
1.  Logs in to a running Cockpit server (default ws://localhost:9090) using
    the exact same login the browser does (pkg/static/login.js):
        GET  /cockpit/login
        Authorization: Basic base64(user + ":" + password + "\\0")
    The reply carries a Set-Cookie; that cookie is the session ticket
    every other request must present.

2.  Opens the WebSocket session at  /cockpit/socket  (subprotocol:
    cockpit1) and performs the *init* handshake. Per doc/protocol.md
    "Command: init" and pkg/base1/test-protocol.ts: the server sends its
    own {"command":"init"} first; the client reads it, then replies with
    its own init ack.

3.  Fetches /cockpit/@<host>/manifests.json — the same endpoint the Cockpit
    shell fetches (src/cockpit/packages.py)
    to build the left-hand nav (served by cockpit-bridge, see
    src/cockpit/packages.py) — and prints every menu/dashboard/tool entry
    grouped by package.

WHY
===
This is the proof-of-concept for "can I drive Cockpit from a script / MCP
server?". The tool list doesn't even strictly need the WebSocket — but the
point is to show the init handshake too, because that's the part an MCP
wrapper needs before it can open any channels (dbus, stream, file, ...).

DEPENDENCIES
============
Python 3 stdlib only. No pip installs.

USAGE
=====
    ./cockpit_list_tools.py
        # prompts for username + password, targets ws://localhost:9090

    COCKPIT_USER=root COCKPIT_PASSWORD=... ./cockpit_list_tools.py
        # non-interactive

    COCKPIT_HOST=myhost COCKPIT_PORT=9090 COCKPIT_TLS=1 ./cockpit_list_tools.py
        # remote + TLS

    COCKPIT_CA=/path/to/ca.pem COCKPIT_TLS=1 ./cockpit_list_tools.py
        # pin a custom CA (useful for self-signed certs); set COCKPIT_TLS_INSECURE=1 to skip verification
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import http.client
import json
import os
import socket
import ssl
import struct
import sys
from typing import Any, BinaryIO


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


# --------------------------------------------------------------------------
# HTTP (login + /manifests.json)
# --------------------------------------------------------------------------

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

    The browser (pkg/static/login.js:666) does:
        Authorization: "Basic " + btoa(user + ":" + password + '\0' + known_hosts)
    known_hosts is empty for localhost; we mirror that exactly.
    """
    basic = base64.b64encode(f"{user}:{password}\0".encode()).decode("ascii")
    resp, _ = http_get("/cockpit/login", {
        "Authorization": f"Basic {basic}",
        # browser sends X-Superuser: none|require|try (login.js:667).
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


# --------------------------------------------------------------------------
# Pretty printing
# --------------------------------------------------------------------------

def print_manifests(manifests: dict[str, Any]) -> None:
    print(f"\nFound {len(manifests)} package(s):\n")
    for name in sorted(manifests):
        print(f"  {name}")
        package: dict[str, Any] = manifests[name] or {}
        for section in ("menu", "tools", "dashboard"):
            entries: dict[str, Any] = package.get(section) or {}
            for key, value in entries.items():
                entry: dict[str, Any] = value if isinstance(value, dict) else {}
                label = entry.get("label") or key
                path = entry.get("path")
                print(f"    [{section:9}]  {label}" + (f"   →  {path}" if path else ""))
        print()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
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
    log(f"   packages(init)  = {init_msg.get('packages')}")

    log(f"3. fetching {SCHEME}://{HOST}:{PORT}/manifests.json")
    print_manifests(fetch_manifests(cookie))
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
