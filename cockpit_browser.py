#!/usr/bin/env python3
"""
cockpit_browser.py — drive a running Cockpit in a headless Chromium.

WHY A BROWSER
=============
cockpit_list_tools.py recovers *what* the UI can do: for every action, the
dialog that performs it, the button that opens it, the one that confirms it
and a selector per input. Those coordinates are only worth anything if
something presses the buttons, and in Cockpit the buttons are the API — a page
is a React bundle talking to the bridge over its own WebSocket, so there is no
HTTP endpoint behind "Create account" to call instead.

So this module does what Cockpit's own browser tests do (test/common/testlib.py
and test/common/test-functions.js, which the DOM helpers below mirror): it runs
a real browser, logs in the way a person does, opens the page, fills the
dialog's fields and presses its button.

It speaks the Chrome DevTools Protocol over a WebSocket, reusing the frame
codec cockpit_list_tools.py already has, so the only dependency outside the
standard library is a chromium binary that is already installed.

    browser = CockpitBrowser(Target("172.16.190.83", 9090, tls=True, insecure=True))
    browser.login("chris", "linux")
    browser.enter_page("/users")                     # the shell route
    browser.click("#accounts-create")
    browser.set_value("#accounts-create-user-name", "bob")
    browser.click('#accounts-create-dialog button:contains("Create")')

THE TWO FRAMES
==============
A Cockpit page does not live in the top-level document: the shell at "/" loads
each page into an <iframe name="cockpit1:<host><route>">. Everything the tool
catalog points at is inside that frame, so enter_page() waits for the frame the
shell creates, makes an isolated world in it (CDP Page.createIsolatedWorld) and
evaluates every later selector there. The login form and the administrative
access indicator are in the top document, and are reached through a second
context.

WHAT IT CHANGES ON THE HOST
===========================
Everything the action itself changes: this presses the real buttons of a real
session. The caller decides what to press — see cockpit_mcp_server.py, which
asks for confirmation before anything destructive and offers a dry run that
stops in front of the confirming button and cancels out.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import cockpit_list_tools as clt

CHROMIUM_CANDIDATES = ("chromium", "chromium-browser", "google-chrome",
                       "google-chrome-stable", "chrome")

# How long to wait for a page, a frame, a dialog or a control to turn up.
WAIT_TIMEOUT = float(os.environ.get("COCKPIT_MCP_WAIT_TIMEOUT", "20"))
# How long a single DevTools command may take (a navigation is the slow one).
CDP_TIMEOUT = float(os.environ.get("COCKPIT_MCP_CDP_TIMEOUT", "60"))
POLL_INTERVAL = 0.25


class BrowserError(RuntimeError):
    """The browser could not be started, reached, or told what to do."""


# ==========================================================================
# The DOM helpers, injected into whichever frame we are driving
# ==========================================================================
#
# These mirror test/common/test-functions.js — the same ":contains()" support,
# the same synthetic mouse event, the same "it really had to work" check —
# because that is the vocabulary the recovered selectors were written in
# (cockpit_list_tools.py takes them from the idioms those tests drive).
#
# Two deliberate differences:
#   * where ph_only() rejects an ambiguous selector, we take the first visible
#     match and report how many there were: a catalog selector like
#     '#dialog button.apply' legitimately matches a button that is rendered
#     twice, and a caller that stopped there could not act at all;
#   * a text input is written through the native value setter before the
#     events are dispatched, which is what makes React notice the change when
#     the component holds the value in state.

HELPER_JS = r"""
(() => {
  if (globalThis.__cmcp) return "already";
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  // ":contains(text)", the one Sizzle extension Cockpit's selectors use.
  function splitClauses(sel) {
    const clauses = [];
    let cur = "", inParen = 0, inQuote = false, qChar = "";
    for (let i = 0; i < sel.length; i++) {
      const c = sel[i];
      if (inQuote) {
        cur += c;
        if (c === qChar) inQuote = false;
      } else if (c === "'" || c === '"') {
        cur += c; inQuote = true; qChar = c;
      } else if (c === "(") {
        cur += c; inParen++;
      } else if (c === ")") {
        cur += c; if (inParen > 0) inParen--;
      } else if (c === "," && inParen === 0) {
        if (cur.trim()) clauses.push(cur.trim());
        cur = "";
      } else {
        cur += c;
      }
    }
    if (cur.trim()) clauses.push(cur.trim());
    return clauses;
  }

  function selectClause(clause) {
    const m = /:contains\((?:'([^']*)'|"([^"]*)"|([^)]*))\)/.exec(clause);
    if (!m) return Array.from(document.querySelectorAll(clause));
    const text = (m[1] ?? m[2] ?? m[3] ?? "").trim();
    const base = clause.replace(m[0], "").trim() || "*";
    return Array.from(document.querySelectorAll(base))
                .filter(el => (el.innerText || el.textContent || "").includes(text));
  }

  function selectAll(sel) {
    const clauses = splitClauses(sel);
    if (clauses.length <= 1 && !/:contains\(/.test(sel))
      return Array.from(document.querySelectorAll(sel));
    const results = [];
    for (const clause of clauses) {
      for (const el of selectClause(clause)) {
        if (!results.includes(el)) results.push(el);
      }
    }
    return results;
  }

  function find(sel) {
    const all = selectAll(sel);
    const shown = all.filter(visible);
    if (!shown.length)
      throw new Error(sel + (all.length ? ": found but not visible" : ": not found"));
    return shown[0];
  }

  function enabled(el) {
    return !el.disabled && el.getAttribute("aria-disabled") !== "true";
  }

  function mouse(el, type) {
    let node = el, left = 0, top = 0;
    while (node) { left += node.offsetLeft || 0; top += node.offsetTop || 0; node = node.offsetParent; }
    let seen = false;
    const spy = () => { seen = true; };
    el.addEventListener(type, spy, true);
    el.dispatchEvent(new MouseEvent(type, {
      bubbles: true, cancelable: true, view: window, detail: 1,
      clientX: left, clientY: top, screenX: left, screenY: top, button: 0,
    }));
    el.removeEventListener(type, spy, true);
    return seen;
  }

  // The caption a human would read this field by, wherever PatternFly put it.
  function labelOf(el) {
    const by = el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (by) return (by.innerText || "").trim();
    const group = el.closest(".pf-v6-c-form__group, .pf-v5-c-form__group, .ct-form-group, tr");
    const text = group && group.querySelector("label, .pf-v6-c-form__label-text, th");
    if (text) return (text.innerText || "").trim();
    return (el.getAttribute("aria-label") || el.placeholder || "").trim() || null;
  }

  // Write a value into whatever kind of control this element turns out to be.
  // "kind" is the catalog's guess and only breaks ties; the DOM decides.
  function setOn(el, value, kind, sel) {
    if (el.tagName !== "SELECT" && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {
      const child = el.querySelector("select, input, textarea, button, [role=button], [role=combobox]");
      if (child) el = child;
    }
    const box = el.type === "checkbox" || el.type === "radio" ||
                el.getAttribute("role") === "switch";
    if (box || kind === "checkbox" || kind === "switch" || kind === "radio") {
      const on = el.checked ?? el.getAttribute("aria-checked") === "true";
      if (!!value !== !!on) el.click();
      return { set: !!value, via: "click" };
    }

    // a native <select> takes its value directly…
    if (el.tagName === "SELECT") {
      el.value = String(value);
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (el.value !== String(value))
        throw new Error(sel + ": no option with value " + JSON.stringify(value));
      return { set: el.value, via: "select" };
    }

    // …a PatternFly select is a button plus a listbox: open it and pick.
    if (el.getAttribute("role") === "button" || el.tagName === "BUTTON" ||
        el.getAttribute("aria-haspopup") === "listbox" || kind === "select") {
      el.click();
      const wanted = String(value).toLowerCase();
      const options = Array.from(document.querySelectorAll(
        '[role="option"], [role="menuitem"], .pf-v6-c-menu__item, .pf-v5-c-select__menu-item'));
      const hit = options.find(o => (o.getAttribute("value") || "").toLowerCase() === wanted) ||
                  options.find(o => (o.innerText || "").trim().toLowerCase() === wanted) ||
                  options.find(o => (o.innerText || "").toLowerCase().includes(wanted));
      if (!hit) {
        if (document.activeElement === el) el.click();   // close it again
        throw new Error(sel + ": no option matching " + JSON.stringify(value) +
                        " among [" + options.map(o => (o.innerText || "").trim()).join(", ") + "]");
      }
      hit.click();
      return { set: (hit.innerText || "").trim(), via: "listbox" };
    }

    // a text input: the native setter, so React's value tracker sees it
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, String(value)); else el.value = String(value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { set: el.value, via: "input" };
  }

  globalThis.__cmcp = {
    count: sel => selectAll(sel).length,
    visible: sel => selectAll(sel).some(visible),
    // visible *and* ready to be acted on: PatternFly disables a dialog's
    // confirming button until the form validates, and a React page renders a
    // moment after the shell marks its frame loaded.
    ready: sel => selectAll(sel).some(el => visible(el) && enabled(el)),
    text(sel) {
      const els = selectAll(sel);
      return els.length ? (els[0].innerText || els[0].textContent || "").trim() : null;
    },
    click(sel) {
      const el = find(sel);
      if (!enabled(el)) throw new Error(sel + ": disabled");
      // counted before the click: pressing it often removes it from the page
      const matched = selectAll(sel).length;
      const label = (el.innerText || el.value || "").trim();
      el.scrollIntoView({ behavior: "instant", block: "center" });
      mouse(el, "mousedown");
      mouse(el, "mouseup");
      if (!mouse(el, "click")) throw new Error(sel + ": does not process click events");
      return { matched, label };
    },
    setValue(sel, value, kind) {
      return setOn(find(sel), value, kind, sel);
    },
    // The inputs a dialog is actually showing. The catalog is recovered from
    // a minified bundle and does not always name every one of them — a
    // dialog whose confirming button stays disabled is usually asking for a
    // field nobody passed, and this is what says which.
    fields(scope) {
      const root = scope ? document.querySelector(scope) : document;
      if (!root) return [];
      return Array.from(root.querySelectorAll(
        "input, select, textarea, [role=combobox], [aria-haspopup=listbox]"))
        .filter(visible)
        .map(el => ({
          id: el.id || null,
          name: el.name || null,
          type: (el.type || el.tagName).toLowerCase(),
          label: labelOf(el),
          required: !!el.required || el.getAttribute("aria-required") === "true",
          value: el.type === "password" ? null : (el.value ?? null),
        }));
    },
    // Fill a field the catalog did not name, by the caption next to it.
    setByLabel(scope, label, value, kind) {
      const root = scope ? document.querySelector(scope) : document;
      if (!root) throw new Error(scope + ": not found");
      const wanted = String(label).trim().toLowerCase();
      const all = Array.from(root.querySelectorAll("input, select, textarea")).filter(visible);
      const score = el => {
        const caption = (labelOf(el) || "").trim().toLowerCase();
        if (caption === wanted) return 3;
        if ((el.id || "").toLowerCase() === wanted || (el.name || "").toLowerCase() === wanted) return 2;
        if (caption.includes(wanted)) return 1;
        return 0;
      };
      const best = all.map(el => [score(el), el]).filter(p => p[0]).sort((a, b) => b[0] - a[0])[0];
      if (!best)
        throw new Error("no field labelled " + JSON.stringify(label) + " among [" +
                        all.map(el => labelOf(el)).filter(t => t).join(", ") + "]");
      return setOn(best[1], value, kind, String(label));
    },
    // Whatever the dialog is complaining about, in the three places it shows it.
    errors(scope) {
      const root = scope ? document.querySelector(scope) : document;
      if (!root) return [];
      const sel = ".pf-v6-c-helper-text__item.pf-m-error, .pf-v5-c-form__helper-text.pf-m-error, " +
                  '#dialog-error-message, .pf-v6-c-alert.pf-m-danger, .pf-v5-c-alert.pf-m-danger, ' +
                  '[aria-invalid="true"] + *, .dialog-error';
      return Array.from(root.querySelectorAll(sel))
                  .map(el => (el.innerText || "").trim()).filter(t => t);
    },
  };
  return "ok";
})()
"""


# ==========================================================================
# DevTools protocol client
# ==========================================================================

def _ws_connect(url: str, timeout: float) -> clt.WebSocket:
    """Open a WebSocket to a ws://host:port/path URL (DevTools is plain HTTP)."""
    rest = url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
    sock.sendall((
        f"GET /{path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii"))
    reader = sock.makefile("rb")
    status = reader.readline().decode("latin-1").strip()
    while reader.readline().strip():
        pass
    if not status.startswith("HTTP/1.1 101"):
        raise BrowserError(f"DevTools handshake failed: {status!r}")
    return clt.WebSocket(sock, reader)


class CDP:
    """A DevTools connection to one browser, with one attached page target.

    Commands are request/response over the same socket the browser pushes its
    events down, so ``call`` reads past anything that is not its own reply.
    Events are kept (bounded) rather than dispatched: nothing here needs to
    react to one, but ``Runtime.consoleAPICalled`` is worth having in a report
    when a page misbehaves.
    """

    def __init__(self, ws: clt.WebSocket) -> None:
        self.ws = ws
        self._id = 0
        self.session: str | None = None
        self.events: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None,
             session: str | None = ..., timeout: float = CDP_TIMEOUT) -> dict[str, Any]:
        self._id += 1
        message: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        session = self.session if session is ... else session
        if session:
            message["sessionId"] = session
        self.ws.sock.settimeout(timeout)
        self.ws.send_frame(clt.OP_TEXT, json.dumps(message).encode())

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                opcode, payload = self.ws.recv_frame()
            except (OSError, ConnectionError) as exc:
                raise BrowserError(f"{method}: devtools connection lost ({exc})") from exc
            if opcode == clt.OP_PING:
                self.ws.send_frame(clt.OP_PONG, payload)
                continue
            if opcode == clt.OP_CLOSE:
                raise BrowserError(f"{method}: devtools connection closed")
            if opcode != clt.OP_TEXT:
                continue
            reply = json.loads(payload.decode("utf-8", "replace"))
            if reply.get("id") != self._id:
                if "method" in reply:
                    self.events.append(reply)
                    del self.events[:-200]
                continue
            if "error" in reply:
                raise BrowserError(f"{method}: {reply['error'].get('message', reply['error'])}")
            return reply.get("result") or {}
        raise BrowserError(f"{method}: no reply within {timeout:g}s")

    def close(self) -> None:
        with contextlib.suppress(OSError, BrowserError):
            self.ws.close()


@dataclass
class Target:
    """The Cockpit server to drive."""
    host: str
    port: int = 9090
    tls: bool = True
    insecure: bool = False

    @property
    def url(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"


def _find_chromium(explicit: str | None = None) -> str:
    if explicit:
        # An explicit --browser that is not there is a mistake worth hearing
        # about: falling back to another binary would drive something the
        # caller did not ask for, and say nothing.
        path = shutil.which(explicit)
        if path:
            return path
        raise BrowserError(f"no chromium at {explicit!r}")
    for name in CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise BrowserError(
        "no chromium binary found (looked for: " + ", ".join(CHROMIUM_CANDIDATES) +
        "); install one or pass --browser /path/to/chromium")


# ==========================================================================
# The Cockpit browser
# ==========================================================================

@dataclass
class Step:
    """One thing that was done (or would be done) to the page."""
    op: str                        # goto / click / set / wait / cancel
    selector: str | None = None
    value: Any = None
    status: str = "pending"        # done / skipped / failed
    detail: Any = None
    # Why a failure of this step is not a failure of the action. The reboot
    # dialog is the standing case: it closes when the host agrees to go down,
    # and a page that the reboot has already torn down never gets to.
    tolerate: str = ""

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op, "status": self.status}
        for key, value in (("selector", self.selector), ("value", self.value),
                           ("detail", self.detail)):
            if value is not None:
                out[key] = value
        return out


class CockpitBrowser:
    """A headless Chromium logged into one Cockpit, parked on one page.

    The browser is started on first use and kept for the life of the object:
    logging in costs a session on the server and a few seconds, and an MCP
    client typically performs one action after another on the same host.
    """

    def __init__(self, target: Target, *, browser: str | None = None,
                 headless: bool = True, manifest_host: str = "localhost") -> None:
        self.target = target
        self.manifest_host = manifest_host
        self._binary = browser
        self._headless = headless
        self._proc: subprocess.Popen[bytes] | None = None
        self._profile: str | None = None
        self.cdp: CDP | None = None
        self.user: str | None = None
        self.page_context: int | None = None    # isolated world in the content frame
        self.route: str | None = None
        self.superuser: str | None = None       # what the shell indicator says

    # ---------------------------------------------------------------- browser

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        binary = _find_chromium(self._binary)
        self._profile = tempfile.mkdtemp(prefix="cockpit-mcp-chromium-")
        argv = [
            binary, "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-gpu",
            "--disable-dev-shm-usage", "--disable-extensions", "--hide-scrollbars",
            "--window-size=1280,1024", "--no-sandbox",
        ]
        if self._headless:
            argv.append("--headless=new")
        if self.target.insecure:
            argv.append("--ignore-certificate-errors")
        argv.append("about:blank")

        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        endpoint = self._await_endpoint()
        self.cdp = CDP(_ws_connect(endpoint, CDP_TIMEOUT))

        page = self.cdp.call("Target.createTarget", {"url": "about:blank"}, session=None)
        attached = self.cdp.call("Target.attachToTarget",
                                 {"targetId": page["targetId"], "flatten": True}, session=None)
        self.cdp.session = attached["sessionId"]
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")
        self.cdp.call("Network.enable")
        if self.target.insecure:
            with contextlib.suppress(BrowserError):
                self.cdp.call("Security.setIgnoreCertificateErrors", {"ignore": True})

    def _await_endpoint(self) -> str:
        """Read the "DevTools listening on ws://…" line chromium prints on startup."""
        assert self._proc is not None and self._proc.stderr is not None
        deadline = time.monotonic() + CDP_TIMEOUT
        noise: list[str] = []
        while time.monotonic() < deadline:
            line = self._proc.stderr.readline().decode("utf-8", "replace")
            if not line:
                break
            noise.append(line.strip())
            match = re.search(r"(ws://\S+)", line)
            if match and "devtools" in match.group(1):
                return match.group(1)
        self.stop()
        raise BrowserError("chromium did not report a DevTools endpoint: " +
                           " | ".join(noise[-5:]))

    def stop(self) -> None:
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=5)
            if self._proc.poll() is None:
                self._proc.kill()
            if self._proc.stderr is not None:
                self._proc.stderr.close()
            self._proc = None
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None
        self.page_context = self.route = None

    def __enter__(self) -> "CockpitBrowser":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------ evaluation

    def _eval(self, expression: str, context: int | None = None,
              timeout: float = CDP_TIMEOUT) -> Any:
        assert self.cdp is not None
        params: dict[str, Any] = {"expression": expression, "returnByValue": True,
                                  "awaitPromise": True}
        if context is not None:
            params["contextId"] = context
        result = self.cdp.call("Runtime.evaluate", params, timeout=timeout)
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = ((detail.get("exception") or {}).get("description")
                    or detail.get("text") or "evaluation failed")
            raise BrowserError(text.split("\n")[0])
        return (result.get("result") or {}).get("value")

    def top(self, expression: str) -> Any:
        """Evaluate in the shell's own document (login form, access indicator)."""
        return self._eval(expression, None)

    def page(self, expression: str) -> Any:
        """Evaluate in the content frame of the page we entered."""
        if self.page_context is None:
            raise BrowserError("no page entered — call enter_page() first")
        return self._eval(expression, self.page_context)

    def _wait(self, probe: str, what: str, context: int | None,
              timeout: float = WAIT_TIMEOUT) -> None:
        """Poll a JS predicate until it is true, or give up saying what was missed."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                if self._eval(probe, context, timeout=min(timeout, 15)):
                    return
                last = ""
            except BrowserError as exc:
                last = f" ({exc})"
            time.sleep(POLL_INTERVAL)
        raise BrowserError(f"timed out after {timeout:g}s waiting for {what}{last}")

    # ----------------------------------------------------------------- login

    def login(self, user: str, password: str, superuser: bool = True) -> str:
        """Log in through the login form, as a person would.

        The form, not the session cookie cockpit_list_tools.py already holds:
        logging in here is what lets the shell offer administrative access —
        it keeps the password for the sudo the bridge will ask for — and that
        is what most of the catalog's actions need. ``superuser`` sets the
        same localStorage flag Cockpit's own tests set (testlib.try_login) to
        say the session should try to escalate.
        """
        self.start()
        assert self.cdp is not None
        self.user = user
        self.cdp.call("Page.navigate", {"url": self.target.url + "/"})
        self._wait("document.readyState === 'complete'", "the login page to load", None)

        if self.top("!!document.querySelector('#login-user-input')"):
            self.top(f"window.localStorage.setItem('superuser:{user}', "
                     f"{json.dumps('any' if superuser else 'none')})")
            self._inject(None)
            self.top(f"__cmcp.setValue('#login-user-input', {json.dumps(user)})")
            self.top(f"__cmcp.setValue('#login-password-input', {json.dumps(password)})")
            self.top("__cmcp.click('#login-button')")
            try:
                self._wait("!document.querySelector('#login-user-input')",
                           "the login to be accepted", None)
            except BrowserError as exc:
                message = self.top("(document.querySelector('#login-error-message')||{}).textContent")
                raise BrowserError(f"login failed: {message or exc}") from None

        self._wait("!!document.querySelector('#shell-nav-toggle, #nav-system, .pf-v6-c-page, #content')",
                   "the Cockpit shell", None)
        self.superuser = self.read_superuser()
        return self.superuser or "unknown"

    INDICATOR = ("(document.querySelector('#super-user-indicator, "
                 "#super-user-indicator-mobile')||{}).innerText || null")

    def read_superuser(self, timeout: float = 10) -> str | None:
        """What the shell's access indicator says: "Administrative access" or "Limited access".

        Polled, because the indicator is rendered a moment after the shell
        itself and an empty reading would look like a session without any
        administrative access at all.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(BrowserError):
                text = self.top(self.INDICATOR)
                if text and text.strip():
                    return text.strip()
            time.sleep(POLL_INTERVAL)
        return None

    def become_superuser(self, password: str) -> str:
        """Switch the session to administrative access, as the shell menu does.

        Best effort: a host that grants it passwordlessly shows a different
        dialog, and one that refuses leaves the indicator where it was — the
        caller is told what the indicator ended up saying either way.
        """
        state = self.read_superuser() or ""
        if "Administrative" in state:
            return state
        self._inject(None)
        with contextlib.suppress(BrowserError):
            self.top("__cmcp.click('#super-user-indicator button')")
            self._wait("!!document.querySelector('div[role=dialog]')",
                       "the administrative access dialog", None, timeout=10)
            if self.top("!!document.querySelector('div[role=dialog] input')"):
                self.top(f"__cmcp.setValue('div[role=dialog] input', {json.dumps(password)})")
            self.top("__cmcp.click('div[role=dialog] button.pf-m-primary')")
            self._wait("!document.querySelector('div[role=dialog]')",
                       "the administrative access dialog to close", None, timeout=15)
        self.superuser = self.read_superuser()
        return self.superuser or "unknown"

    # ------------------------------------------------------------ page entry

    def _inject(self, context: int | None) -> None:
        self._eval(HELPER_JS, context)

    def enter_page(self, route: str, force: bool = False) -> str:
        """Navigate the shell to ``route`` (e.g. "/users") and enter its iframe.

        The route may carry a hash — "/users#/chris" is how a Cockpit page
        addresses one of the objects it lists, and the only way to reach the
        buttons that act on that object. The hash is the page's own business,
        so the frame is still named after the path alone:
        "cockpit1:<host><path>", the name Cockpit's tests use.
        """
        assert self.cdp is not None, "browser not started"
        path = route.split("#", 1)[0]
        frame_name = f"cockpit1:{self.manifest_host}{path}"
        if self.route == route and not force and self.page_context is not None:
            with contextlib.suppress(BrowserError):
                self.page("1")              # context still alive? then stay put
                return frame_name
        self.cdp.call("Page.navigate", {"url": self.target.url + route})
        self._wait("document.readyState !== 'loading'", f"the shell at {route}", None)
        self._wait(
            "!!document.querySelector(" +
            json.dumps(f"iframe.container-frame[name='{frame_name}'][data-loaded]") + ")",
            f"the {route} page to load in the shell", None)

        tree = self.cdp.call("Page.getFrameTree")["frameTree"]
        frame_id = _find_frame(tree, frame_name)
        if frame_id is None:
            raise BrowserError(f"the shell did not create a frame named {frame_name!r}")
        world = self.cdp.call("Page.createIsolatedWorld",
                              {"frameId": frame_id, "worldName": "cockpit-mcp",
                               "grantUniveralAccess": True})
        self.page_context = world["executionContextId"]
        self.route = route
        self._inject(self.page_context)
        # data-loaded says the shell handed the frame over, not that React has
        # rendered into it; the page is usable once its body has children.
        self._wait("!!(document.body && document.body.children.length)",
                   f"the {route} page to render", self.page_context)
        return frame_name

    # ------------------------------------------------------- page operations

    def click(self, selector: str, timeout: float = WAIT_TIMEOUT) -> Any:
        self.wait_ready(selector, timeout)
        return self.page(f"__cmcp.click({json.dumps(selector)})")

    def set_value(self, selector: str, value: Any, kind: str = "",
                  timeout: float = WAIT_TIMEOUT) -> Any:
        self.wait_ready(selector, timeout)
        return self.page(f"__cmcp.setValue({json.dumps(selector)}, {json.dumps(value)}, "
                         f"{json.dumps(kind)})")

    def set_by_label(self, label: str, value: Any, scope: str | None = None,
                     kind: str = "") -> Any:
        """Fill a field the catalog never named, by its visible caption."""
        return self.page(f"__cmcp.setByLabel({json.dumps(scope)}, {json.dumps(label)}, "
                         f"{json.dumps(value)}, {json.dumps(kind)})")

    def fields(self, scope: str | None = None) -> list[dict[str, Any]]:
        """What the dialog is actually asking for — diagnostics, not a schema."""
        try:
            return list(self.page(f"__cmcp.fields({json.dumps(scope)})") or [])
        except BrowserError:
            return []

    def dismiss(self, selectors: list[str]) -> str | None:
        """Get rid of a dialog left standing by a failed step.

        The browser outlives a single invocation, so an abandoned modal would
        block every later one. Try the action's own cancel button, then the
        generic ones, then Escape.
        """
        for selector in [s for s in selectors if s] + [
                'div[role="dialog"] button[aria-label="Close"]',
                'div[role="dialog"] button.pf-m-link:contains("Cancel")',
                'div[role="dialog"] button:contains("Cancel")']:
            try:
                if self.is_visible(selector):
                    self.page(f"__cmcp.click({json.dumps(selector)})")
                    self.wait_gone('div[role="dialog"]', timeout=5)
                    return selector
            except BrowserError:
                continue
        try:
            if not self.is_visible('div[role="dialog"]'):
                return None
            self.page("document.activeElement && document.activeElement.blur(), "
                      "document.dispatchEvent(new KeyboardEvent('keydown', "
                      "{ key: 'Escape', keyCode: 27, bubbles: true }))")
            self.wait_gone('div[role="dialog"]', timeout=5)
            return "Escape"
        except BrowserError:
            return None

    def wait_ready(self, selector: str, timeout: float = WAIT_TIMEOUT) -> None:
        """Wait until the control is there, shown and not disabled.

        Every interaction goes through this, the way testlib.click() waits
        before it clicks: a Cockpit page renders a moment after the shell has
        marked its frame loaded, and a dialog's confirming button stays
        disabled until its form validates.
        """
        self._wait(f"__cmcp.ready({json.dumps(selector)})", f"{selector} to be ready",
                   self.page_context, timeout)

    def wait_visible(self, selector: str, timeout: float = WAIT_TIMEOUT) -> None:
        self._wait(f"__cmcp.visible({json.dumps(selector)})", f"{selector} to appear",
                   self.page_context, timeout)

    def wait_gone(self, selector: str, timeout: float = WAIT_TIMEOUT) -> None:
        self._wait(f"!__cmcp.visible({json.dumps(selector)})", f"{selector} to disappear",
                   self.page_context, timeout)

    def is_visible(self, selector: str) -> bool:
        return bool(self.page(f"__cmcp.visible({json.dumps(selector)})"))

    def text(self, selector: str) -> str | None:
        return self.page(f"__cmcp.text({json.dumps(selector)})")

    def errors(self, scope: str | None = None) -> list[str]:
        return list(self.page(f"__cmcp.errors({json.dumps(scope)})") or [])

    def screenshot(self, path: str) -> str | None:
        """PNG of the whole window — the quickest way to see why a step failed."""
        assert self.cdp is not None
        try:
            shot = self.cdp.call("Page.captureScreenshot", {"format": "png"})
            with open(path, "wb") as out:
                out.write(base64.b64decode(shot["data"]))
            return path
        except (BrowserError, OSError, ValueError):
            return None


def _find_frame(node: dict[str, Any], name: str) -> str | None:
    """Depth-first search of a CDP frame tree for the frame with this name."""
    frame = node.get("frame") or {}
    if frame.get("name") == name:
        return frame.get("id")
    for child in node.get("childFrames") or []:
        found = _find_frame(child, name)
        if found:
            return found
    return None
