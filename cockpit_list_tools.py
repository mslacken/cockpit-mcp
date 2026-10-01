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
Python 3 stdlib only (http.client, socket, ssl, base64, hashlib, struct,
getpass, json, os, sys). No pip installs.

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
import getpass
import http.client
import json
import os
import socket
import ssl
import struct
import sys
from typing import Any


DEFAULT_HOST = os.environ.get("COCKPIT_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("COCKPIT_PORT", "9090"))
USE_TLS = os.environ.get("COCKPIT_TLS", "").strip().lower() in ("1", "true", "yes")
CA_CERT = os.environ.get("COCKPIT_CA") or os.environ.get("SSL_CERT_FILE")
INSECURE = os.environ.get("COCKPIT_TLS_INSECURE", "").strip().lower() in ("1", "true", "yes")
TIMEOUT = int(os.environ.get("COCKPIT_TIMEOUT", "10"))


# --------------------------------------------------------------------------
# HTTP (login + /manifests.json)
# --------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext | None:
    """Return an ssl.SSLContext if TLS is wanted, else None."""
    if not USE_TLS:
        return None
    if INSECURE:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=CA_CERT) if CA_CERT \
          else ssl.create_default_context()


def http_request(host: str, port: int, method: str, path: str,
                 headers: dict[str, str] | None = None,
                 body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    """Small blocking HTTP client. Returns (status, headers, body)."""
    extra = {**(headers or {}), "Connection": "close"}
    if USE_TLS:
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=TIMEOUT, context=_tls_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        conn.request(method, path, body=body, headers=extra)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def do_login(host: str, port: int, user: str, password: str) -> tuple[bool, str, dict[str, str]]:
    """
    Log in. The browser (pkg/static/login.js:666) does:
        Authorization: "Basic " + btoa(user + ":" + password + '\0' + known_hosts)
    known_hosts is empty for localhost; we mirror that exactly.

    Returns (ok, cookie_name_value, response_headers). cookie_name_value is
    "name=value", ready to go into a "Cookie:" request header.
    """
    basic = "Basic " + base64.b64encode(
        (f"{user}:{password}\0").encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": basic,
        # browser sends X-Superuser: none|require|try (login.js:667).
        "X-Superuser": os.environ.get("COCKPIT_SUPERUSER", "none"),
    }
    status, headers_out, _ = http_request(host, port, "GET", "/cockpit/login",
                                          headers=headers)
    if status != 200:
        return False, "", headers_out
    set_cookie = headers_out.get("Set-Cookie") or headers_out.get("set-cookie") or ""
    name_value = set_cookie.split(";", 1)[0].strip()
    if "=" not in name_value:
        return False, "", headers_out
    return True, name_value, headers_out


def fetch_manifests(host: str, port: int, cookie: str) -> dict[str, Any]:
    url = f"/cockpit/@{host}/manifests.json"
    status, _, body = http_request(host, port, "GET", url,
                                   headers={"Cookie": cookie})
    if status != 200:
        raise RuntimeError(f"GET {url} failed: HTTP {status}")
    return json.loads(body.decode("utf-8"))


# --------------------------------------------------------------------------
# WebSocket client (RFC 6455 subset — text frames only, no extensions)
# --------------------------------------------------------------------------

class WebSocket:
    """Minimal WebSocket connection wrapping a socket plus a recv leftover buffer.

    ``socket.socket`` (and ``ssl.SSLSocket``) are C extension types without a
    ``__dict__``, so we cannot stash state on them. Instead we keep a small
    wrapper that owns the socket and the leftover bytes pulled off the wire
    during the HTTP upgrade handshake.
    """

    def __init__(self, sock: "socket.socket | ssl.SSLSocket",
                 leftover: bytes = b"") -> None:
        self.sock: "socket.socket | ssl.SSLSocket" = sock
        self._recv_leftover: bytes = leftover

    def recv_into(self, n: int) -> bytes:
        """Read exactly n bytes.

        Drains ``self._recv_leftover`` first: the WS handshake read may have
        pulled the first data frame(s) off the socket in the same TCP segment
        as the 101 response. Those bytes must not be lost, or the first
        (``init``) message is silently dropped and the client blocks until a
        server keepalive triggers a protocol-error close.
        """
        out = b""
        while len(out) < n:
            leftover = self._recv_leftover
            if leftover:
                take = min(len(leftover), n - len(out))
                out += leftover[:take]
                self._recv_leftover = leftover[take:]
                continue
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("peer closed socket")
            out += chunk
        return out

    def send_frame(self, opcode: int, payload: bytes) -> None:
        """Send one unfragmented frame. Client→server frames MUST be masked (RFC 6455)."""
        h = bytearray([0x80 | (opcode & 0x0F)])
        n = len(payload)
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127)
            h += struct.pack(">Q", n)
        mask = os.urandom(4)
        h += mask
        h += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(h))

    def recv_frame(self) -> tuple[int, bytes]:
        hdr = self.recv_into(2)
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.recv_into(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.recv_into(8))[0]
        mask = self.recv_into(4) if masked else b""
        payload = self.recv_into(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))  # opcode 0x1 = text

    def wait_text(self) -> str | None:
        """Read until a text frame arrives. Returns None if the peer closed cleanly."""
        while True:
            op, payload = self.recv_frame()
            if op == 0x8:                      # close
                return None
            if op == 0x9:                      # ping → answer pong (same payload)
                self.send_frame(0xA, payload)
                continue
            if op == 0xA:                      # pong
                continue
            if op == 0x1:                      # text
                return payload.decode("utf-8")
            continue                            # ignore others

    def send_control(self, msg: dict[str, Any]) -> None:
        """Control channel = empty channel id prefix ("\n" + json)."""
        self.send_text("\n" + json.dumps(msg))

    def wait_control(self) -> dict[str, Any] | None:
        """Read and parse the next *substantive* control message, skipping protocol keepalives.

        Per doc/protocol.md "Command: ping": a "ping" control message is a keepalive
        and must be answered with an identical "pong" (echoing options) — see
        cockpitwebservice.c:process_ping and cockpittransport.c:140. The server sends
        these every ~5s (cockpit_ws_ping_interval), and they can arrive *before*
        init on a slow client, so we must not blindly return them as "the" message.

        None on clean close.
        """
        while True:
            text = self.wait_text()
            if text is None:
                return None
            if not text.startswith("\n"):
                raise RuntimeError(
                    f"unexpected payload frame before a control message: {text[:120]!r}…")
            msg = json.loads(text[1:])
            cmd = msg.get("command")
            if cmd == "pong":
                # Keepalive answer from the server; ignore, keep waiting.
                continue
            if cmd == "ping":
                # Protocol-level keepalive. Answer with an identical "pong".
                pong = dict(msg)
                pong["command"] = "pong"
                self.send_control(pong)
                continue
            return msg

    def init_session(self) -> dict[str, Any]:
        """Run the init handshake. Returns the server's init message."""
        first = self.wait_control()
        if first is None:
            raise RuntimeError("socket closed before any message")
        if first.get("command") != "init":
            raise RuntimeError(f"first message was not 'init', got: {first}")
        self.send_control({"command": "init", "version": 1})
        return first

    def close(self) -> None:
        """Send a clean WS close frame if possible, then close the socket."""
        try:
            self.send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def ws_connect(host: str, port: int, cookie: str) -> WebSocket:
    """Open a TCP(/TLS) socket, perform the WS handshake, return a WebSocket."""
    sec_key_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
    origin = f"{'https' if USE_TLS else 'http'}://{host}:{port}"
    request = (
        "GET /cockpit/socket HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Origin: {origin}\r\n"
        f"Sec-WebSocket-Key: {sec_key_b64}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: cockpit1\r\n"
        f"Cookie: {cookie}\r\n"
        "\r\n"
    ).encode("ascii")

    sock: "socket.socket | ssl.SSLSocket" = socket.create_connection(
        (host, port), timeout=TIMEOUT)
    ctx = _tls_context()
    if ctx is not None:
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.sendall(request)

    # Read the full HTTP response head up to the blank line.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket server closed during handshake")
        buf += chunk
        if len(buf) > 1_048_576:
            raise RuntimeError("WS handshake response too large")
    head_b, _, leftover = buf.partition(b"\r\n\r\n")
    head = head_b.decode("latin-1")
    first_line = head.splitlines()[0] if head else ""
    if not first_line.startswith("HTTP/1.1 101"):
        hint = ""
        if "403" in first_line:
            hint = ("\n   403 = Origin check failed (src/ws/websocketserver.c). The server "
                    "requires an 'Origin' header matching the connection, e.g. "
                    f"{origin!r}. A [WebService] Origins= entry can also extend the allowlist.")
        raise RuntimeError(f"WS handshake failed: {first_line!r}{hint}\n---\n{head}\n---")
    # Keep any data frames that arrived in the same TCP segment as the 101
    # response; WebSocket.recv_into drains these before touching the socket, so
    # the first (init) message is not lost.
    return WebSocket(sock, leftover)


# --------------------------------------------------------------------------
# Pretty printing
# --------------------------------------------------------------------------

def print_manifests(manifests: dict[str, Any]) -> None:
    print(f"\nFound {len(manifests)} package(s):\n")
    for name in sorted(manifests):
        m: dict[str, Any] = manifests[name] or {}
        print(f"  {name}")
        for section in ("menu", "tools", "dashboard"):
            sections_value: Any = m.get(section)
            if not isinstance(sections_value, dict) or not sections_value:
                continue
            sections: dict[str, Any] = sections_value  # type: ignore[assignment]
            for key, entry in sections.items():
                if isinstance(entry, dict):
                    entry_dict: dict[str, Any] = entry  # type: ignore[assignment]
                    label: Any = entry_dict.get("label")
                    if label is None:
                        label = key
                    path: Any = entry_dict.get("path")
                    line = f"    [{section:9}]  {label}"
                    if path:
                        line += f"   →  {path}"
                else:
                    line = f"    [{section:9}]  {key}"
                print(line)
        print()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    user = os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD")
    if not password:
        password = getpass.getpass(f"Cockpit password for {user}@{host}:{port}: ")

    scheme = "wss" if USE_TLS else "ws"
    print(f"1. logging in as {user}@{host}:{port}  ({scheme}://…)", file=sys.stderr)
    ok, cookie, login_headers = do_login(host, port, user, password)
    if not ok:
        print("   login failed — no usable Set-Cookie in response; "
              f"headers: {login_headers}", file=sys.stderr)
        return 2
    cookie_name, _, cookie_val = cookie.partition("=")
    print(f"   -> session cookie: {cookie_name}={cookie_val[:20]}…", file=sys.stderr)

    print(f"2. opening WebSocket {scheme}://{host}:{port}/cockpit/socket "
          f"(subprotocol: cockpit1)", file=sys.stderr)
    ws = ws_connect(host, port, cookie)
    try:
        init_msg = ws.init_session()
    finally:
        ws.close()
    print(f"   session inited:  host={init_msg.get('host')!r}", file=sys.stderr)
    print(f"   channel-seed    = {init_msg.get('channel-seed')!r}", file=sys.stderr)
    print(f"   packages(init)  = {init_msg.get('packages')}", file=sys.stderr)

    print(f"3. fetching {scheme}://{host}:{port}/manifests.json", file=sys.stderr)
    manifests = fetch_manifests(host, port, cookie)
    print_manifests(manifests)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
