#!/usr/bin/env python3
"""
cockpit_mcp_server.py — an MCP server for a running Cockpit host.

WHAT IT SERVES
==============
cockpit_list_tools.py reads a live Cockpit server and recovers two things: the
catalog of actions its UI offers (a name, a description, an input schema and
the selectors that reach each one) and the live state of the pages those
actions act on (the accounts that exist, the units that are loaded, the disks
that are plugged in). This wraps both in an MCP server, built on the official
SDK (https://github.com/modelcontextprotocol/python-sdk).

The catalog is 150-odd actions on a stock host. Rather than advertise them all
— a client would spend most of its context on tool definitions it will never
call — the server offers a handful of tools *over* the catalog:

    cockpit_overview()                      what this host is and what is on it
    cockpit_list_pages(package)             the map: packages, pages, live state
    cockpit_search_tools(query, …)          find the actions that match a need
    cockpit_describe_tool(name)             one action in full: schema + state
    cockpit_invoke(name, arguments, …)      perform it, for real, in a browser
    cockpit_refresh(full)                   re-read the host

Every answer carries the live state of the page it concerns, which is the half
a caller cannot infer: "Delete account" means nothing without knowing which
accounts there are, and `cockpit_search_tools("delete account")` says both.

INVOKING IS REAL
================
Cockpit's UI *is* its API: a page is a React bundle talking to the bridge over
its own WebSocket, so there is no endpoint behind "Create account" to call
instead. cockpit_invoke therefore drives a headless Chromium
(cockpit_browser.py): it logs in, opens the page, presses the button that
opens the dialog, fills the fields from ``arguments`` and presses the one that
confirms it. What the action does to the host, it really does.

Two gates sit in front of that:

  * an action the catalog marks destructive needs ``confirm=true`` — without
    it the call returns the plan it would have run and changes nothing;
  * ``dry_run=true`` walks the whole path — navigate, open the dialog, fill
    every field — then presses Cancel instead of the confirming button. It is
    the way to check that a tool's recovered selectors still fit the UI
    without touching the host.

``--read-only`` turns every invocation into a dry run.

USAGE
=====
    pip install mcp                       # the official SDK
    COCKPIT_PASSWORD=… ./cockpit_mcp_server.py --insecure https://myhost:9090

    # as an MCP client would start it (claude_desktop_config.json, .mcp.json):
    {"mcpServers": {"cockpit": {
        "command": "/path/to/.venv-mcp/bin/python",
        "args": ["/path/to/cockpit_mcp_server.py", "--insecure", "https://myhost:9090"],
        "env": {"COCKPIT_USER": "chris", "COCKPIT_PASSWORD": "…"}}}}

The password is needed twice over: once for the HTTP session that reads the
catalog, once for the browser session that drives it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import cockpit_browser as cbr
import cockpit_list_tools as clt
from mcp.server.mcpserver import MCPServer

SERVER_NAME = "cockpit"
LIVE_TTL = float(os.environ.get("COCKPIT_MCP_LIVE_TTL", "60"))   # seconds


# ==========================================================================
# Configuration
# ==========================================================================

@dataclass
class Config:
    target: str = clt.DEFAULT_TARGET
    user: str = ""
    password: str = ""
    packages: list[str] = field(default_factory=list)
    insecure: bool = False
    browser: str | None = None
    headless: bool = True
    read_only: bool = False
    screenshot_dir: str | None = None

    def apply(self) -> None:
        """Point cockpit_list_tools' module-level connection settings at us."""
        clt.set_target(self.target)
        clt.INSECURE = self.insecure
        os.environ["COCKPIT_USER"] = self.user
        os.environ["COCKPIT_PASSWORD"] = self.password


CONFIG = Config()


# ==========================================================================
# The catalog: the scan, kept in memory and re-read on demand
# ==========================================================================
#
# Building it costs a login, a manifest fetch, every page bundle on the host
# and a round of live probes — around a minute on a stock install. So it is
# built once, lazily, on the first call that needs it, and the live half is
# refreshed on its own (a few D-Bus calls and file reads) whenever it is
# older than LIVE_TTL. The static half only changes when the host's packages
# do, which is what cockpit_refresh(full=true) is for.

def _route(package: str, path: str) -> str:
    """The shell route for a page: ("users", "index.html") → "/users".

    The Cockpit shell addresses a page by package and manifest path with the
    extension dropped, and an "index" page by its package alone — the same
    route that testlib.enter_page() builds its frame name from.
    """
    page = path.split("#", 1)[0].strip("/")
    page = re.sub(r"\.html?$", "", page)
    if page in ("", "index"):
        return f"/{package}"
    return f"/{package}/{page}"


@dataclass
class Catalog:
    """The scan of one host: its packages, pages, live state and actions."""
    packages: list[clt.Package] = field(default_factory=list)
    tools: list[clt.Tool] = field(default_factory=list)
    by_name: dict[str, clt.Tool] = field(default_factory=dict)
    page_of: dict[str, tuple[clt.Package, clt.Page]] = field(default_factory=dict)
    cookie: str = ""
    built_at: float = 0.0
    live_at: float = 0.0
    host: str = ""

    # ------------------------------------------------------------- building

    def build(self) -> None:
        """Log in, read the manifests, scan every page, probe the live state."""
        CONFIG.apply()
        cookie, manifests = clt.connect()
        packages = clt.build_packages(manifests, CONFIG.packages)
        clt.introspect(packages, cookie)
        clt.probe_live(packages, cookie)
        # The probes run on a second WebSocket, which the host sometimes drops
        # while the scan is still reading page bundles over the first. One
        # fresh session is enough; without it the whole catalog would describe
        # a host with nothing on it.
        if not any(page.resources for pkg in packages for page in pkg.pages):
            with contextlib.suppress(OSError, RuntimeError):
                cookie, _ = clt.connect()
                clt.probe_live(packages, cookie)
        tools = clt.build_tools(packages)

        self.cookie = cookie
        self.packages = packages
        self.tools = tools
        self.by_name = {t.name: t for t in tools}
        self.page_of = {}
        for pkg in packages:
            for page in pkg.pages:
                self.page_of[f"{pkg.name}/{page.path}"] = (pkg, page)
        self.host = clt.MANIFEST_HOST
        self.built_at = self.live_at = time.time()

    def refresh_live(self) -> str:
        """Re-read what the pages are showing, leaving the static scan alone.

        The resource lists are cleared in place: build_tools() handed each
        tool the very list object its page owns, so refilling that list is
        what keeps every tool's description current.

        Clearing them is also the risk — a probe that fails (the session
        expired, the bridge dropped a channel) would otherwise leave every
        tool describing a host with nothing on it, which reads as fact. So
        the old contents are kept and put back unless the new read produced
        something, and a dead session is re-established once before giving up.
        """
        CONFIG.apply()
        pages = [page for pkg in self.packages for page in pkg.pages]
        before = [list(page.resources) for page in pages]

        def attempt() -> int:
            for page in pages:
                page.resources.clear()
            clt.probe_live(self.packages, self.cookie)
            return sum(len(page.resources) for page in pages)

        note = ""
        for retry in (False, True):
            try:
                if retry:
                    self.cookie, _ = clt.connect()
                if attempt():
                    self.live_at = time.time()
                    return "refreshed after re-login" if retry else "refreshed"
                note = "the host reported nothing"
            except (OSError, RuntimeError) as exc:
                note = str(exc)

        for page, kept in zip(pages, before):           # nothing new: keep the old
            page.resources[:] = kept
        return f"kept the previous live state ({note})"

    def ensure_live(self) -> None:
        if time.time() - self.live_at > LIVE_TTL:
            self.refresh_live()

    # -------------------------------------------------------------- reading

    def resources(self) -> list[clt.Resource]:
        """Every live inventory read from the host, deduplicated by name."""
        out: dict[str, clt.Resource] = {}
        for pkg in self.packages:
            for page in pkg.pages:
                for res in page.resources:
                    out.setdefault(res.name, res)
        return list(out.values())

    def page_for(self, tool: clt.Tool) -> tuple[clt.Package, clt.Page] | None:
        return self.page_of.get(f"{tool.package}/{tool.page_path}")

    def route_for(self, tool: clt.Tool) -> str:
        return _route(tool.package, tool.page_path)


CATALOG = Catalog()
_BUILD_LOCK = asyncio.Lock()
_DRIVE_LOCK = asyncio.Lock()        # one browser, one action at a time


async def catalog(live: bool = True) -> Catalog:
    """The catalog, built on first use; its live half refreshed when stale."""
    async with _BUILD_LOCK:
        if not CATALOG.tools:
            await asyncio.to_thread(CATALOG.build)
        elif live:
            await asyncio.to_thread(CATALOG.ensure_live)
    return CATALOG


# ==========================================================================
# Rendering: what the answers look like
# ==========================================================================

def _state(resources: list[clt.Resource], limit: int = clt.DESC_ITEMS) -> list[str]:
    """The live inventories as one line each, in order, without repeats.

    Several tools share a page, so a list of tools carries the same inventory
    several times over; the caller only needs to read it once.
    """
    seen: dict[str, None] = {}
    for res in resources:
        seen.setdefault(res.line(limit), None)
    return list(seen)


# Actions the catalog did not mark destructive, but that a caller should still
# be asked about before they happen. The flag comes from the scan, which reads
# a PatternFly "danger" styling or a delete-shaped id; a button captioned
# "Lock account" carries neither and is still not something to press by
# accident. The gate is cheap and a false positive only costs a confirm=true.
_RISKY_RE = re.compile(
    r"\b(delete|remove|erase|destroy|wipe|format|initialize|reset|revert|"
    r"reboot|restart|shutdown|power|halt|stop|disable|kill|terminate|"
    r"force|lock|logout|log out|disconnect|detach|unmount|uninstall|purge|"
    r"revoke|expire|prune|clear)\b", re.I)


# Captions whose value must not come back out in the result the client logs.
_SECRET_RE = re.compile(r"pass|secret|key|token|pin\b|confirm", re.I)


def needs_confirm(tool: clt.Tool) -> bool:
    return bool(tool.destructive or
                _RISKY_RE.search(f"{tool.name.replace('_', ' ')} {tool.title} "
                                 f"{tool.action or ''}"))


def _brief(tool: clt.Tool) -> dict[str, Any]:
    """A tool as a search hit: enough to choose it, not enough to call it."""
    params = ", ".join(p.name + ("*" if p.required else "") for p in tool.params)
    return {
        "name": tool.name,
        "signature": f"{tool.name}({params})",
        "title": tool.title,
        "package": tool.package,
        "page": tool.page_label,
        "route": _route(tool.package, tool.page_path),
        "destructive": tool.destructive,
        "needs_confirm": needs_confirm(tool),
    }


def _full(tool: clt.Tool) -> dict[str, Any]:
    """A tool in full: its MCP definition plus how and where it is performed."""
    out = tool.to_json()
    out["route"] = _route(tool.package, tool.page_path)
    out["state"] = _state(tool.context)
    out["destructive"] = tool.destructive
    out["needs_confirm"] = needs_confirm(tool)
    out["plan"] = [step.as_json() for step in plan_steps(tool, {}, dry_run=False)]
    return out


# ==========================================================================
# From a tool definition to a sequence of things to do to the page
# ==========================================================================
#
# A tool's "cockpit" metadata says where its action lives: the page, the
# dialog that performs it, the control that opens that dialog, the one that
# confirms it, and one selector per input. Turning that into steps is mostly
# bookkeeping, with one judgement call: where the scan recovered no explicit
# confirm/cancel selector, the button is found by its caption inside the
# dialog — ":contains()" the way Cockpit's own tests spell it.

def _caption_selector(dialog: str, caption: str) -> str:
    return f'{dialog} button:contains("{caption}")'


def plan_steps(tool: clt.Tool, arguments: dict[str, Any], dry_run: bool,
               extra: dict[str, Any] | None = None, at: str | None = None,
               open_with: str | None = None) -> list[cbr.Step]:
    """The steps that perform ``tool`` with ``arguments``."""
    sel = tool.selectors or {}
    route = _route(tool.package, tool.page_path)
    # An action on one of the objects a page lists lives on that object's own
    # view, which Cockpit addresses by hash: /users#/chris, /network#/ens4.
    # Without it the page shows its list, where the button is not.
    if at:
        route += at if at.startswith("#") else "#/" + at.lstrip("/")
    steps = [cbr.Step("goto", selector=route)]

    dialog = sel.get("dialog")
    if open_with:
        steps.append(cbr.Step("click", selector=open_with,
                              detail="opens the dialog (named by the caller)"))
    elif sel.get("open"):
        steps.append(cbr.Step("click", selector=sel["open"], detail="opens the dialog"))
    elif tool.opens_via and dialog:
        # the opener was recovered as a caption, not an id
        steps.append(cbr.Step("click", selector=f'button:contains("{tool.opens_via}")',
                              detail="opens the dialog"))
    elif dialog and tool.action:
        # Nothing in the bundle said what opens this dialog — the call site is
        # in another module, or in a row the page renders from data. The page
        # button that opens a dialog is almost always captioned like the
        # dialog's own confirming button ("Delete" opens "Delete mcptest",
        # which is confirmed with "Delete"), so try that, and say it is a
        # guess: the step reports what it pressed and how many matched.
        steps.append(cbr.Step("click", selector=f'button:contains("{tool.action}")',
                              detail="opens the dialog (guessed: the scan found no opener)"))

    if dialog:
        steps.append(cbr.Step("wait", selector=dialog, detail="the dialog is up"))

    for param in tool.params:
        if param.name not in arguments:
            continue
        selector = param.selector or (f"#{param.control_id}" if param.control_id else None)
        if not selector:
            steps.append(cbr.Step("set", value=arguments[param.name], status="skipped",
                                  detail=f"no selector was recovered for {param.name!r}"))
            continue
        steps.append(cbr.Step("set", selector=selector, value=arguments[param.name],
                              detail=param.kind or None))

    # Fields the scan never recovered. The create-account dialog's two
    # password boxes are the standing example: they are built by a shared
    # React component, so no selector for them survives into the bundle's
    # page code, and without them the dialog's Create button never enables.
    for label, value in (extra or {}).items():
        steps.append(cbr.Step("set-label", selector=label, value=value,
                              detail="a field the catalog did not name"))

    if dialog:
        confirm = sel.get("apply") or _caption_selector(dialog, tool.action or "Apply")
        cancel = sel.get("cancel") or _caption_selector(dialog, "Cancel")
        if dry_run:
            steps.append(cbr.Step("cancel", selector=cancel,
                                  detail="dry run: the action is abandoned here"))
        else:
            steps.append(cbr.Step("confirm", selector=confirm,
                                  detail=f"confirms the action ({tool.action or 'apply'})"))
            steps.append(cbr.Step("wait-gone", selector=dialog,
                                  detail="the dialog closes when the action succeeded"))
    else:
        # a page-level action: the control *is* the action
        control = sel.get("control") or (f"#{tool.control_id}" if tool.control_id else None)
        if not control:
            control = f'button:contains("{tool.action or tool.title}")'
        step = cbr.Step("confirm", selector=control, detail="performs the action")
        if dry_run:
            step.status, step.detail = "skipped", "dry run: the action is not performed"
        steps.append(step)
    return steps


# ==========================================================================
# Arguments: checking them against the schema the scan recovered
# ==========================================================================

def validate(tool: clt.Tool, arguments: dict[str, Any]) -> list[str]:
    """Complaints about ``arguments``, in the order a caller should read them."""
    problems: list[str] = []
    known = {p.name: p for p in tool.params}
    for name in arguments:
        if name not in known:
            problems.append(f"unknown input {name!r}; this tool takes: " +
                            (", ".join(sorted(known)) or "no inputs"))
    for name, param in known.items():
        if param.required and name not in arguments:
            problems.append(f"missing required input {name!r} ({param.description})")
        if name not in arguments:
            continue
        value = arguments[name]
        if param.type == "boolean" and not isinstance(value, bool):
            problems.append(f"{name!r} must be a boolean")
        elif param.type == "integer" and (isinstance(value, bool) or
                                          not isinstance(value, int)):
            problems.append(f"{name!r} must be an integer")
        elif param.type == "string" and not isinstance(value, (str, int, float)):
            problems.append(f"{name!r} must be a string")
        if param.choices and str(value) not in [v for v, _ in param.choices]:
            problems.append(f"{name!r} must be one of: " +
                            ", ".join(v for v, _ in param.choices))
    return problems


# ==========================================================================
# Driving: the browser, kept alive between calls
# ==========================================================================

_BROWSER: cbr.CockpitBrowser | None = None


def _browser() -> cbr.CockpitBrowser:
    """The logged-in browser, started on first use.

    One browser for the life of the server: a login costs a session on the
    host and a few seconds, and calls come one after another on the same host.
    """
    global _BROWSER
    if _BROWSER is not None and _BROWSER.cdp is not None:
        return _BROWSER
    target = cbr.Target(clt.HOST, clt.PORT, tls=clt.USE_TLS, insecure=CONFIG.insecure)
    browser = cbr.CockpitBrowser(target, browser=CONFIG.browser,
                                 headless=CONFIG.headless, manifest_host=CATALOG.host
                                 or clt.MANIFEST_HOST)
    browser.start()
    browser.login(CONFIG.user, CONFIG.password, superuser=True)
    if "Administrative" not in (browser.superuser or ""):
        browser.become_superuser(CONFIG.password)
    _BROWSER = browser
    return browser


def _drop_browser() -> None:
    global _BROWSER
    if _BROWSER is not None:
        with contextlib.suppress(Exception):
            _BROWSER.stop()
        _BROWSER = None


def run_steps(tool: clt.Tool, steps: list[cbr.Step]) -> dict[str, Any]:
    """Perform the steps in the browser. Blocking; called in a worker thread."""
    browser = _browser()
    result: dict[str, Any] = {"performed": False, "access": browser.superuser}
    dialog = (tool.selectors or {}).get("dialog")

    try:
        for step in steps:
            if step.status == "skipped":
                continue
            try:
                if step.op == "goto":
                    assert step.selector
                    result["frame"] = browser.enter_page(step.selector)
                elif step.op in ("click", "confirm", "cancel"):
                    assert step.selector
                    step.detail = browser.click(step.selector) or step.detail
                elif step.op == "set":
                    assert step.selector
                    param = next((p for p in tool.params if p.selector == step.selector
                                  or p.control_id == step.selector.lstrip("#")), None)
                    step.detail = browser.set_value(step.selector, step.value,
                                                    param.kind if param else "")
                elif step.op == "set-label":
                    assert step.selector
                    step.detail = browser.set_by_label(step.selector, step.value, dialog)
                elif step.op == "wait":
                    assert step.selector
                    browser.wait_visible(step.selector)
                elif step.op == "wait-gone":
                    assert step.selector
                    browser.wait_gone(step.selector)
                step.status = "done"
            except cbr.BrowserError as exc:
                step.status = "failed"
                step.detail = str(exc)
                result["error"] = f"{step.op} {step.selector}: {exc}"
                break

        # A selector that matched several elements acted on the first one.
        # That is right for a dialog rendered twice and wrong for the delete
        # button of a table with four rows, and nothing here can tell the two
        # apart — so the caller is told, and decides.
        ambiguous = [f"{s.selector} matched {s.detail['matched']} elements"
                     for s in steps if isinstance(s.detail, dict)
                     and s.detail.get("matched", 1) > 1]
        if ambiguous:
            result["ambiguous"] = ambiguous

        # the action happened if, and only if, the confirming button was
        # pressed and nothing after it failed
        attempted = [s for s in steps if s.status != "skipped"]
        result["performed"] = (bool(attempted) and all(s.status == "done" for s in attempted)
                               and any(s.op == "confirm" for s in attempted))

        # whatever the page is complaining about is the best explanation of a
        # failure there is — a dialog that refuses to close says why in it
        with contextlib.suppress(cbr.BrowserError):
            complaints = browser.errors(dialog if dialog and browser.is_visible(dialog) else None)
            if complaints:
                result["page_errors"] = complaints
                result["performed"] = False
        with contextlib.suppress(cbr.BrowserError):
            if dialog and browser.is_visible(dialog):
                result["dialog_still_open"] = True
                result["dialog_text"] = browser.text(dialog)
                # A confirming button that never enabled is the dialog asking
                # for something nobody passed, so say what it is asking for:
                # anything listed here and not in the schema can be supplied
                # through `extra`.
                fields = browser.fields(dialog)
                if fields:
                    result["dialog_fields"] = fields
    except cbr.BrowserError as exc:
        result["error"] = str(exc)
        _drop_browser()

    if CONFIG.screenshot_dir and (result.get("error") or result.get("page_errors")):
        os.makedirs(CONFIG.screenshot_dir, exist_ok=True)
        path = os.path.join(CONFIG.screenshot_dir, f"{tool.name}-{int(time.time())}.png")
        if _BROWSER is not None:
            result["screenshot"] = _BROWSER.screenshot(path)

    # The browser outlives this call, so a dialog left standing by a failure
    # would be in the way of the next one. Screenshot first, then clear it.
    if result.get("dialog_still_open") and _BROWSER is not None:
        with contextlib.suppress(cbr.BrowserError):
            closed = _BROWSER.dismiss([(tool.selectors or {}).get("cancel", "")])
            result["dialog_dismissed"] = closed or False

    result["steps"] = [s.as_json() for s in steps]
    return result


# ==========================================================================
# The MCP server
# ==========================================================================

INSTRUCTIONS = """\
This server exposes a running Cockpit host (the web console for Linux servers).

Cockpit has no REST API: its pages are the API. The server has scanned this
host's Cockpit UI and recovered every action it offers, together with the
live state of the pages those actions work on.

Work in this order:
  1. cockpit_overview() — what this host is, and what is currently on it.
  2. cockpit_search_tools("delete account") — find the action you need. The
     catalog is large, so search it rather than listing it.
  3. cockpit_describe_tool(name) — its inputs, with the current state of its
     page so you know which objects it can be aimed at.
  4. cockpit_invoke(name, arguments) — perform it. This drives the real UI in
     a browser and really changes the host. Pass dry_run=true first to check
     the path without performing the action; an action that deletes, disables
     or restarts something additionally requires confirm=true.

An action that belongs to one of the objects a page lists — this account,
that interface — needs to be told which one: pass at="mcptest", the name the
page uses in its own URL. If a dialog then refuses to proceed, the result
lists the fields it is showing as dialog_fields; anything there that the
schema does not cover can be passed through extra, keyed by its caption.

Every answer carries the state of the page it concerns. That state is read
from the host, but it is a snapshot — call cockpit_refresh() after an action
to see its effect.
"""

mcp = MCPServer(
    SERVER_NAME,
    title="Cockpit",
    instructions=INSTRUCTIONS,
    website_url="https://cockpit-project.org",
)


@mcp.tool(
    title="Cockpit host overview",
    description="What this Cockpit host is and what is currently on it: hostname, "
                "operating system, uptime, accounts, services, network interfaces, "
                "storage — plus which Cockpit packages and pages are installed. "
                "Start here: everything else in this server is an action on one of "
                "these objects.",
)
async def cockpit_overview() -> dict[str, Any]:
    cat = await catalog()
    pages = [f"{pkg.name}/{page.path} — {page.label}"
             for pkg in cat.packages for page in pkg.pages]
    return {
        "server": f"{clt.SCHEME}://{clt.HOST}:{clt.PORT}",
        "host": cat.host,
        "user": CONFIG.user,
        "live_state": [res.render() for res in cat.resources()],
        "packages": [pkg.name for pkg in cat.packages],
        "pages": pages,
        "actions": len(cat.tools),
        "destructive_actions": sum(1 for t in cat.tools if t.destructive),
        "read_only": CONFIG.read_only,
        "state_read_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cat.live_at)),
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cat.built_at)),
    }


@mcp.tool(
    title="List Cockpit pages",
    description="The map of this host's Cockpit UI: its packages, the pages each "
                "one has, the live state each page is showing and how many actions "
                "it offers. Use it to see what areas exist before searching for a "
                "specific action.",
)
async def cockpit_list_pages(package: str | None = None) -> dict[str, Any]:
    """
    Args:
        package: restrict the listing to this Cockpit package (e.g. "storage").
    """
    cat = await catalog()
    out = []
    for pkg in cat.packages:
        if package and pkg.name != package:
            continue
        for page in pkg.pages:
            tools = [t for t in cat.tools
                     if t.package == pkg.name and t.page_path == page.path]
            out.append({
                "package": pkg.name,
                "page": page.label,
                "path": page.path,
                "route": _route(pkg.name, page.path),
                "section": page.section,
                "actions": len(tools),
                "state": _state(page.resources),
                "keywords": page.keywords[:12],
            })
    if package and not out:
        return {"error": f"no package named {package!r}",
                "packages": [p.name for p in cat.packages]}
    return {"pages": out}


@mcp.tool(
    title="Search Cockpit actions",
    description="Find the actions this host's Cockpit UI offers. Matches the "
                "action's name, title, description, page and inputs; with an empty "
                "query it lists everything (paged). Each hit comes back as a call "
                "signature — pass its name to cockpit_describe_tool for the inputs "
                "and the page's current state.",
)
async def cockpit_search_tools(query: str = "", package: str | None = None,
                               destructive_only: bool = False,
                               limit: int = 20) -> dict[str, Any]:
    """
    Args:
        query: words to look for, e.g. "delete account" or "restart service".
        package: restrict to one Cockpit package.
        destructive_only: only actions that destroy or disrupt something (the
            ones cockpit_invoke refuses without confirm=true).
        limit: how many hits to return (1-100).
    """
    cat = await catalog()
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    hits: list[tuple[int, clt.Tool]] = []
    for tool in cat.tools:
        if package and tool.package != package:
            continue
        if destructive_only and not needs_confirm(tool):
            continue
        haystacks = (tool.name.lower().replace("_", " "), tool.title.lower(),
                     tool.page_label.lower(), tool.description.lower(),
                     " ".join(p.name for p in tool.params).lower())
        # a word in the name or title says more than the same word buried in
        # the description, so hits are ranked by where they landed
        score = 0
        for word in words:
            weights = (8, 6, 3, 1, 2)
            found = [w for w, hay in zip(weights, haystacks) if word in hay]
            if not found:
                score = -1
                break
            score += max(found)
        if score >= 0:
            hits.append((score, tool))
    hits.sort(key=lambda pair: (-pair[0], pair[1].name))
    limit = max(1, min(limit, 100))
    chosen = [tool for _, tool in hits[:limit]]
    return {
        "query": query,
        "matches": len(hits),
        "returned": len(chosen),
        "tools": [_brief(tool) for tool in chosen],
        "state": _state(sum((t.context for t in chosen), [])[:6]),
    }


@mcp.tool(
    title="Describe a Cockpit action",
    description="One action in full: its JSON Schema of inputs, whether it is "
                "destructive, the page it lives on and that page's current state "
                "(the accounts, units, devices it can be aimed at), plus the steps "
                "cockpit_invoke would perform in the UI.",
)
async def cockpit_describe_tool(name: str) -> dict[str, Any]:
    """
    Args:
        name: the action's name, as returned by cockpit_search_tools.
    """
    cat = await catalog()
    tool = cat.by_name.get(name)
    if tool is None:
        near = [t.name for t in cat.tools if name.lower() in t.name.lower()][:10]
        return {"error": f"no action named {name!r}",
                "did_you_mean": near or [t.name for t in cat.tools[:10]]}
    return _full(tool)


@mcp.tool(
    title="Perform a Cockpit action",
    description="Perform an action from the catalog by driving this host's Cockpit "
                "UI in a browser: open the page, open its dialog, fill the inputs, "
                "press the button. THIS REALLY CHANGES THE HOST. Pass dry_run=true "
                "to walk the whole path and press Cancel instead (nothing is "
                "changed). An action that deletes, disables, restarts or otherwise "
                "disrupts something is refused unless confirm=true is passed with "
                "it — cockpit_describe_tool says which ones those are. If the "
                "dialog turns out to want a field the catalog never recovered, "
                "the result lists what it is showing as dialog_fields; pass those "
                "through `extra`, keyed by the caption the dialog puts next to them.",
)
async def cockpit_invoke(name: str, arguments: dict[str, Any] | None = None,
                         dry_run: bool = False, confirm: bool = False,
                         extra: dict[str, Any] | None = None,
                         at: str | None = None,
                         open_with: str | None = None) -> dict[str, Any]:
    """
    Args:
        name: the action to perform, as returned by cockpit_search_tools.
        arguments: its inputs, as described by cockpit_describe_tool.
        dry_run: do everything except the confirming click, then cancel.
        confirm: required for destructive actions; ignored for the others.
        extra: further dialog fields, keyed by their visible label rather than
            by a schema name — for inputs the scan did not recover, such as the
            passwords the create-account dialog insists on.
        at: which of the objects the page lists to act on, named the way the
            page addresses it in its hash — "mcptest" for an account, "ens4"
            for an interface. Required whenever the action belongs to one
            object rather than to the page; without it the action is performed
            on whichever object the page happens to be showing.
        open_with: the selector of the control that opens this action's dialog,
            for the actions whose opener the scan could not recover. The plan
            in cockpit_describe_tool says when it is guessing one.
    """
    cat = await catalog()
    tool = cat.by_name.get(name)
    if tool is None:
        return {"error": f"no action named {name!r}. Use cockpit_search_tools to find one."}

    arguments = dict(arguments or {})
    extra = dict(extra or {})
    problems = validate(tool, arguments)
    if problems:
        return {"error": "the arguments do not fit this action", "problems": problems,
                "inputSchema": tool.input_schema()}

    if CONFIG.read_only and not dry_run:
        dry_run = True
        note = "the server runs with --read-only, so this was a dry run"
    else:
        note = None

    if needs_confirm(tool) and not confirm and not dry_run:
        return {
            "error": f"{name} changes or destroys something and needs confirm=true",
            "would_do": tool.title,
            "state": _state(tool.context),
            "plan": [s.as_json() for s in plan_steps(tool, arguments, False, extra, at, open_with)],
            "performed": False,
        }

    steps = plan_steps(tool, arguments, dry_run, extra, at, open_with)
    async with _DRIVE_LOCK:
        result = await asyncio.to_thread(run_steps, tool, steps)
        # the action changed the host; the state everything else reports must
        # not keep describing the host as it was before it
        if result.get("performed") and not dry_run:
            await asyncio.to_thread(CATALOG.refresh_live)

    result.update({"tool": name, "title": tool.title, "dry_run": dry_run,
                   "arguments": arguments, "state_after": _state(tool.context)})
    if extra:
        result["extra"] = {k: "…" if _SECRET_RE.search(k) else v for k, v in extra.items()}
    if at:
        result["at"] = at
    if note:
        result["note"] = note
    return result


@mcp.tool(
    title="Re-read the Cockpit host",
    description="Re-read the host: by default only the live state (accounts, "
                "services, interfaces, disks), which is quick; with full=true also "
                "re-scan every page for its actions, which takes about a minute and "
                "is only needed when Cockpit packages were installed or removed.",
)
async def cockpit_refresh(full: bool = False) -> dict[str, Any]:
    """
    Args:
        full: re-scan the UI as well, not only the live state.
    """
    async with _BUILD_LOCK:
        if full or not CATALOG.tools:
            await asyncio.to_thread(CATALOG.build)
            outcome = "rescanned"
        else:
            outcome = await asyncio.to_thread(CATALOG.refresh_live)
    return {
        "scanned": "everything" if full else "live state",
        "outcome": outcome,
        "actions": len(CATALOG.tools),
        "live_state": [res.render() for res in CATALOG.resources()],
        "read_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(CATALOG.live_at)),
    }


# --------------------------------------------------------------------------
# Resources: the same material, for a client that would rather read than call
# --------------------------------------------------------------------------

@mcp.resource("cockpit://overview", title="Cockpit host overview", mime_type="text/plain")
async def overview_resource() -> str:
    cat = await catalog()
    lines = [f"Cockpit {clt.SCHEME}://{clt.HOST}:{clt.PORT} (host {cat.host})", ""]
    lines += [res.render() for res in cat.resources()]
    lines += ["", f"{len(cat.tools)} actions on {sum(len(p.pages) for p in cat.packages)} "
                  f"pages of {len(cat.packages)} packages."]
    return "\n".join(lines)


@mcp.resource("cockpit://catalog", title="Cockpit action catalog", mime_type="application/json")
async def catalog_resource() -> str:
    cat = await catalog()
    return json.dumps([_brief(tool) for tool in cat.tools], indent=2)


@mcp.resource("cockpit://tool/{name}", title="One Cockpit action",
              mime_type="application/json")
async def tool_resource(name: str) -> str:
    cat = await catalog()
    tool = cat.by_name.get(name)
    if tool is None:
        return json.dumps({"error": f"no action named {name!r}"})
    return json.dumps(_full(tool), indent=2)


@mcp.resource("cockpit://page/{package}/{path}", title="One Cockpit page",
              mime_type="application/json")
async def page_resource(package: str, path: str) -> str:
    cat = await catalog()
    found = cat.page_of.get(f"{package}/{path}")
    if found is None:
        return json.dumps({"error": f"no page {package}/{path}",
                           "pages": sorted(cat.page_of)})
    pkg, page = found
    return json.dumps({
        "package": pkg.name,
        "page": page.label,
        "route": _route(pkg.name, page.path),
        "state": [dataclasses.asdict(res) for res in page.resources],
        "actions": [_brief(t) for t in cat.tools
                    if t.package == pkg.name and t.page_path == page.path],
    }, indent=2)


# ==========================================================================
# Main
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve a running Cockpit host over the Model Context Protocol: "
                    "its live state, the actions its UI offers, and a browser that "
                    "performs them.",
        epilog="Environment: COCKPIT_USER, COCKPIT_PASSWORD, COCKPIT_MCP_LIVE_TTL, "
               "COCKPIT_MCP_WAIT_TIMEOUT")
    parser.add_argument("target", nargs="?", default=None,
                        metavar="[SCHEME://]HOST[:PORT]",
                        help=f"the Cockpit server to serve (default: {clt.DEFAULT_TARGET})")
    parser.add_argument("--user", metavar="NAME", help="log in as this user")
    parser.add_argument("--package", metavar="NAME", action="append", default=[],
                        help="only scan this Cockpit package (repeatable)")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the server's TLS certificate")
    parser.add_argument("--read-only", action="store_true",
                        help="never perform an action: every invocation is a dry run")
    parser.add_argument("--browser", metavar="PATH",
                        help="the chromium binary to drive (default: the first one found)")
    parser.add_argument("--headful", action="store_true",
                        help="show the browser window instead of running it headless")
    parser.add_argument("--screenshot-dir", metavar="DIR",
                        help="save a screenshot here whenever an action fails")
    parser.add_argument("--prefetch", action="store_true",
                        help="scan the host at startup instead of on the first call")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "sse", "streamable-http"],
                        help="how clients connect (default: stdio)")
    parser.add_argument("--port", type=int, default=8000,
                        help="port for the http transports (default: 8000)")
    args = parser.parse_args(argv)

    user = args.user or os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            parser.error("no COCKPIT_PASSWORD in the environment and no tty to ask on")
        password = getpass.getpass(f"Cockpit password for {user}: ")

    CONFIG.target = args.target or os.environ.get("COCKPIT_HOST") or clt.DEFAULT_TARGET
    CONFIG.user, CONFIG.password = user, password
    CONFIG.packages = args.package
    CONFIG.insecure = args.insecure
    CONFIG.browser = args.browser
    CONFIG.headless = not args.headful
    CONFIG.read_only = args.read_only
    CONFIG.screenshot_dir = args.screenshot_dir
    CONFIG.apply()

    if args.prefetch:
        clt.log("scanning the host before serving (--prefetch)")
        CATALOG.build()

    try:
        if args.transport == "stdio":
            mcp.run("stdio")
        else:
            mcp.run(args.transport, port=args.port)        # type: ignore[arg-type]
    finally:
        _drop_browser()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _drop_browser()
        raise SystemExit(130) from None
