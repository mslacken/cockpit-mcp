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
Cockpit's UI *is* its API: a page is a React bundle talking to cockpit-bridge
over its own WebSocket, so there is no endpoint behind "Create account" to
call instead. cockpit_invoke performs an action one of two ways, and either
way what the action does to the host, it really does:

  * **the bridge** (cockpit_bridge.py) for the actions whose call is known —
    the same ``cockpit.spawn(["/usr/sbin/userdel", "-r", name], {superuser:
    "require"})`` the dialog's own button makes, sent on the same protocol the
    page uses. No browser, about a tenth of a second. The recipes are
    hand-written from the page sources, because a bundle says which button
    exists but not what it does;
  * **a headless Chromium** (cockpit_browser.py) for everything else: log in,
    open the page, press the button that opens the dialog, fill the fields
    from ``arguments``, press the one that confirms it.

``backend`` chooses; by default an action with a bridge recipe uses it and the
rest use the browser, so a host with no chromium installed can still perform
everything in cockpit_bridge.ADAPTERS (``--no-browser`` makes that explicit,
``--no-bridge`` sends everything through the UI).

Two gates sit in front of both:

  * an action the catalog marks destructive needs ``confirm=true`` — without
    it the call returns the plan it would have run and changes nothing;
  * ``dry_run=true`` performs nothing: in the browser it walks the whole path
    — navigate, open the dialog, fill every field — and presses Cancel
    instead of the confirming button, which is the way to check that a tool's
    recovered selectors still fit the UI; over the bridge it reports the
    exact calls it would make.

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

import cockpit_bridge as cbg
import cockpit_browser as cbr
import cockpit_list_tools as clt
from mcp.server.fastmcp import FastMCP

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
    use_bridge: bool = True             # perform what can be performed directly
    use_browser: bool = True            # … and drive the UI for the rest

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


# --------------------------------------------------------------------------
# Actions the scan cannot see at all
# --------------------------------------------------------------------------
#
# The overview's "Cancel reboot" link is in the bundle like any other button,
# but it never becomes an action: shutdownStatus.tsx builds its caption in a
# variable (it reads "Cancel poweroff" for the other kind), so the heuristic
# that names an action after its caption labels this one from the nearest
# string it can find — "still running", off the neighbouring card — and drops
# it for not being an action verb.
#
# It has to be reachable all the same. cockpit_invoke("system_reboot")
# schedules a reboot a minute out, as the dialog does, and that minute is
# only a margin for error if calling it off is one call away.

@dataclass
class Missed:
    """An action a human found on a page and the scan did not."""
    name: str
    title: str
    description: str
    package: str
    page_path: str
    control_id: str
    destructive: bool = False


MISSED = [
    Missed(name="system_cancel_reboot",
           title="Cancel reboot",
           description="Call off a reboot or power off that is scheduled and "
                       "has not happened yet, which is what the overview's "
                       "\"Cancel reboot\" link does. Rebooting through Cockpit "
                       "schedules it a minute out unless a delay says "
                       "otherwise, so this is how a reboot is taken back.",
           package="system", page_path="index.html",
           control_id="system-health-shutdown-status-cancel-btn"),
]


def missed_tools(packages: list[clt.Package]) -> list[clt.Tool]:
    """The hand-written actions, bound to the pages they live on.

    Only the ones whose page this host actually has. ``context`` is the
    page's own live-state list — the very object build_tools() hands its own
    tools — so these describe the host as freshly as everything else does.
    """
    out: list[clt.Tool] = []
    for missed in MISSED:
        for pkg in packages:
            if pkg.name != missed.package:
                continue
            for page in pkg.pages:
                if page.path != missed.page_path:
                    continue
                out.append(clt.Tool(
                    name=missed.name, title=missed.title,
                    description=missed.description, package=pkg.name,
                    page_label=page.label, page_path=page.path,
                    section=page.section, control_id=missed.control_id,
                    destructive=missed.destructive,
                    selectors={"control": f"#{missed.control_id}"},
                    context=page.resources))
    return out


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
        tools = clt.build_tools(packages) + missed_tools(packages)

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


# … and the other way round, for the actions whose wording only matches
# because of the thing they undo. "Cancel reboot" reads as a reboot to the
# regex above, and a confirmation round trip is the last thing it needs:
# whoever calls it has a minute before the host goes down.
_UNDOES_RE = re.compile(r"\bcancel\w*\s+(a\s+)?(reboot|restart|shutdown|"
                        r"poweroff|power off|scheduled)", re.I)


def needs_confirm(tool: clt.Tool) -> bool:
    wording = f"{tool.name.replace('_', ' ')} {tool.title} {tool.action or ''}"
    if _UNDOES_RE.search(wording):
        return False
    return bool(tool.destructive or _RISKY_RE.search(wording))


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
    adapter = cbg.adapter_for(tool.name) if CONFIG.use_bridge else None
    out["backend"] = "bridge" if adapter else "browser"
    if adapter:
        # The plan above is the UI path, which backend="browser" still takes;
        # this is what the action actually runs by default.
        out["bridge_call"] = adapter.summary
    fix = UI_FIXUPS.get(tool.name)
    if fix:
        # The scan read this button wrong; say so, because the description and
        # the control it names come from the scan and still read as fact.
        out["ui_note"] = fix.why
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


# --------------------------------------------------------------------------
# Where the scan reads the page wrong, and a human has read it right
# --------------------------------------------------------------------------
#
# The scan recovers a control and, when the bundle shows one, the dialog it
# opens. Both halves can be wrong about the same button: the overview's
# "Reboot" is an <id="reboot"> item of a dropdown that is not in the DOM until
# its toggle is pressed, and pressing it does not reboot anything — it opens
# #shutdown-dialog, whose danger button does. The catalog therefore lists
# system_reboot as a page-level action on #reboot, and invoking it waits 20
# seconds for a control that is not there.
#
# Nothing in the bundle says otherwise (the onClick is
# ``Dialogs.show(<ShutdownModal />)``, which is exactly the React indirection
# the scan cannot follow), so this is the browser's equivalent of
# cockpit_bridge.ADAPTERS: hand-written entries, each transcribed from the
# page source and from the selectors Cockpit's own test for it uses.

@dataclass
class Fixup:
    """What the page really does, for an action the scan read wrong."""
    open: list[str] = field(default_factory=list)   # clicks that open the dialog
    dialog: str = ""
    confirm: str = ""
    action: str = ""                                # what that button is captioned
    why: str = ""
    # Fills the dialog's own fields. It replaces the generic `extra` handling,
    # because these fields are not labelled inputs the browser can find by
    # caption — the Delay is a PatternFly menu, which is two clicks.
    fill: Any = None                                # (cbg.Request) -> [Step]
    # True when performing this leaves no page behind to confirm it with.
    ends_session: Any = None                        # (cbg.Request) -> bool


# The Delay select offers these and nothing else (cockpit-components-shutdown
# .jsx render()); anything else is a job for the bridge, which passes the
# delay straight to shutdown(1).
_DELAY_OPTIONS = {"+0": "No delay", "+1": "1 minute", "+5": "5 minutes",
                  "+20": "20 minutes", "+40": "40 minutes", "+60": "60 minutes"}


def _fill_shutdown_dialog(req: cbg.Request) -> list[cbr.Step]:
    """#shutdown-dialog's two fields, pressed as check-system-shutdown-restart
    presses them: #message is a textarea, Delay is a menu toggle plus an item.
    """
    steps: list[cbr.Step] = []
    message = req.value("message", "wall", "message_to_logged_in_users")
    if message:
        steps.append(cbr.Step("set", selector="#message", value=str(message),
                              detail="the message logged-in users are warned with"))
    if req.value("delay", "when", "minutes", "time") is None:
        return steps                        # leave the dialog's own 1 minute
    option = _DELAY_OPTIONS.get(cbg.when(req)[0])
    if option is None:
        steps.append(cbr.Step("set", value=req.value("delay", "when"), status="skipped",
                              detail="the dialog's Delay menu only offers " +
                                     ", ".join(_DELAY_OPTIONS.values()) +
                                     "; backend=\"bridge\" takes any delay"))
        return steps
    steps.append(cbr.Step("click", selector="#delay", detail="open the Delay menu"))
    steps.append(cbr.Step("click", selector=f'button:contains("{option}")',
                          detail=f"choose {option}"))
    return steps


def _immediately(req: cbg.Request) -> bool:
    try:
        return cbg.when(req)[1]
    except cbg.Unsupported:
        return False


def ends_session(name: str, arguments: dict[str, Any], extra: dict[str, Any],
                 at: str | None) -> bool:
    """Whether performing this leaves no host behind to ask about it.

    Both backends need the same answer, and neither can be trusted to notice
    on its own: the browser's dialog closes normally on its way down, and the
    bridge's "shutdown --reboot +0" usually answers before logind kills the
    session. What settles it is the delay that was asked for.
    """
    fix = UI_FIXUPS.get(name)
    if not (fix and fix.ends_session):
        return False
    return bool(fix.ends_session(cbg.Request(dict(arguments), dict(extra), at)))


_SHUTDOWN_DIALOG = {
    "dialog": "#shutdown-dialog",
    # The footer's danger button, which is how Cockpit's own test addresses it
    # — the caption differs between Reboot and Shut down, the styling does not.
    "confirm": "#shutdown-dialog button.pf-m-danger",
    "fill": _fill_shutdown_dialog,
    "ends_session": _immediately,
}

UI_FIXUPS: dict[str, Fixup] = {
    "system_reboot": Fixup(
        # the split button, which is always there when the session is
        # privileged — #reboot, which the scan found, is the same action
        # hidden inside the menu that #shutdown-group opens
        open=["#reboot-button"],
        action="Reboot",
        why="#reboot is a menu item that only exists while the shutdown menu "
            "is open, and it opens #shutdown-dialog rather than rebooting; the "
            "split button #reboot-button opens the same dialog directly",
        **_SHUTDOWN_DIALOG),
    "system_shutdown": Fixup(
        open=["#shutdown-group", "#shutdown"],
        action="Shut down",
        why="#shutdown is an item of the menu that #shutdown-group opens, and "
            "it opens #shutdown-dialog rather than powering the host off",
        **_SHUTDOWN_DIALOG),
    "system_shut_down": Fixup(
        open=["#shutdown-group", "#shutdown"],
        action="Shut down",
        why="\"Shut down\" is #shutdown-dialog's own confirming button; the "
            "menu item that opens that dialog is #shutdown",
        **_SHUTDOWN_DIALOG),
}


def plan_steps(tool: clt.Tool, arguments: dict[str, Any], dry_run: bool,
               extra: dict[str, Any] | None = None, at: str | None = None,
               open_with: str | None = None) -> list[cbr.Step]:
    """The steps that perform ``tool`` with ``arguments``."""
    fix = UI_FIXUPS.get(tool.name)
    req = cbg.Request(dict(arguments), dict(extra or {}), at)
    sel = dict(tool.selectors or {})
    action = tool.action
    if fix:
        sel["dialog"] = fix.dialog or sel.get("dialog")
        action = fix.action or action
        if fix.confirm:
            sel["apply"] = fix.confirm
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
    elif fix and fix.open:
        steps += [cbr.Step("click", selector=selector, detail=fix.why)
                  for selector in fix.open]
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

    if fix and fix.fill:
        # This dialog's fields are written out by hand, so `extra` is read by
        # the fixup rather than guessed at by caption.
        steps += fix.fill(req)
    else:
        # Fields the scan never recovered. The create-account dialog's two
        # password boxes are the standing example: they are built by a shared
        # React component, so no selector for them survives into the bundle's
        # page code, and without them the dialog's Create button never enables.
        for label, value in (extra or {}).items():
            steps.append(cbr.Step("set-label", selector=label, value=value,
                                  detail="a field the catalog did not name"))

    if dialog:
        confirm = sel.get("apply") or _caption_selector(dialog, action or "Apply")
        cancel = sel.get("cancel") or _caption_selector(dialog, "Cancel")
        if dry_run:
            steps.append(cbr.Step("cancel", selector=cancel,
                                  detail="dry run: the action is abandoned here"))
        else:
            steps.append(cbr.Step("confirm", selector=confirm,
                                  detail=f"confirms the action ({action or 'apply'})"))
            gone = cbr.Step("wait-gone", selector=dialog,
                            detail="the dialog closes when the action succeeded")
            if fix and fix.ends_session and fix.ends_session(req):
                # The dialog closes when the spawn resolves — but the host is
                # on its way down, and a page that dies first never gets to
                # close it. Either way the action happened.
                gone.tolerate = ("the page stopped answering, which is what "
                                 "this action does to it")
            steps.append(gone)
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
    result: dict[str, Any] = {"performed": False, "backend": "browser"}
    dialog = (tool.selectors or {}).get("dialog")

    try:
        # Inside the try: on a machine with no chromium this is where the
        # whole path fails, and the caller is owed the reason rather than an
        # exception out of the tool call.
        browser = _browser()
        result["access"] = browser.superuser
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
                if step.tolerate:
                    step.status = "done"
                    step.detail = f"{exc} — {step.tolerate}"
                    result["host_going_down"] = True
                    break
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

    # A host on its way down takes the browser's session with it; keeping the
    # window would only mean the next call inherits a "Disconnected" curtain.
    if result.get("host_going_down"):
        _drop_browser()
        result.pop("dialog_still_open", None)

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
  4. cockpit_invoke(name, arguments) — perform it. This really changes the
     host: the action is either sent straight to cockpit-bridge, exactly as
     the page's own button would send it, or performed by driving the real UI
     in a browser — cockpit_describe_tool says which, as backend. Pass
     dry_run=true first to see what it would do; an action that deletes,
     disables or restarts something additionally requires confirm=true.

An action that belongs to one of the objects a page lists — this account,
that unit, that interface — needs to be told which one: pass at="mcptest",
the name the page uses in its own URL. Inputs the catalog does not name go in
extra, keyed by the caption the dialog puts next to them: that is where the
password for a new account goes, and the shell, group name or similar that an
action whose backend is the bridge asks for. If a dialog refuses to proceed,
the result lists the fields it is showing as dialog_fields.

Every answer carries the state of the page it concerns. That state is read
from the host, but it is a snapshot — call cockpit_refresh() after an action
to see its effect.
"""

mcp = FastMCP(
    SERVER_NAME,
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
    description="Perform an action from the catalog. THIS REALLY CHANGES THE HOST. "
                "An action whose call is known is sent straight to cockpit-bridge, "
                "exactly as its dialog would send it (cockpit_describe_tool reports "
                "backend=bridge and the call); every other action is performed by "
                "driving this host's Cockpit UI in a browser — open the page, open "
                "its dialog, fill the inputs, press the button. Pass dry_run=true to "
                "see what would be done without doing it. An action that deletes, "
                "disables, restarts or otherwise disrupts something is refused "
                "unless confirm=true is passed with it. If a dialog turns out to "
                "want a field the catalog never recovered, the result lists what it "
                "is showing as dialog_fields; pass those through `extra`, keyed by "
                "the caption the dialog puts next to them — which is also how the "
                "bridge is told a password, a shell or a new name.",
)
async def cockpit_invoke(name: str, arguments: dict[str, Any] | None = None,
                         dry_run: bool = False, confirm: bool = False,
                         extra: dict[str, Any] | None = None,
                         at: str | None = None,
                         open_with: str | None = None,
                         backend: str = "auto") -> dict[str, Any]:
    """
    Args:
        name: the action to perform, as returned by cockpit_search_tools.
        arguments: its inputs, as described by cockpit_describe_tool.
        dry_run: report what would be done without doing it — over the bridge
            the exact calls, in the browser the whole path up to Cancel.
        confirm: required for destructive actions; ignored for the others.
        extra: further inputs, keyed by their visible label rather than by a
            schema name — for the fields the scan did not recover, such as the
            passwords the create-account dialog insists on.
        at: which of the objects the page lists to act on, named the way the
            page addresses it in its hash — "mcptest" for an account,
            "sshd.service" for a unit, "ens4" for an interface. Required
            whenever the action belongs to one object rather than to the page;
            in the browser, without it the action is performed on whichever
            object the page happens to be showing.
        open_with: the selector of the control that opens this action's dialog,
            for the actions whose opener the scan could not recover. The plan
            in cockpit_describe_tool says when it is guessing one. Browser only.
        backend: "auto" (the default: the bridge where there is a recipe for
            the action, the browser otherwise), "bridge" (fail rather than
            fall back — no browser is started), or "browser" (drive the real
            UI, which is the way to check that an action's selectors still fit).
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

    if backend not in ("auto", "bridge", "browser"):
        return {"error": f"backend must be auto, bridge or browser, not {backend!r}"}

    notes: list[str] = []
    if CONFIG.read_only and not dry_run:
        dry_run = True
        notes.append("the server runs with --read-only, so this was a dry run")

    # Which of the two performs it. An adapter that cannot build its calls
    # from what was passed (no `at`, no password) is not an error: the browser
    # needs no recipe, because it presses the real button.
    calls: list[cbg.Call] | None = None
    why_browser = ""                      # why this is not going over the bridge
    adapter = cbg.adapter_for(name)
    if backend == "browser":
        why_browser = 'backend="browser" was asked for'
    elif not CONFIG.use_bridge:
        why_browser = "this server runs with --no-bridge"
    elif adapter is None:
        why_browser = f"there is no bridge recipe for {name}"
        if backend == "bridge":
            return {"error": f"{why_browser}; it can only be performed in the "
                             f"browser (backend=\"browser\")", "performed": False}
    else:
        try:
            calls = cbg.plan(name, arguments, extra, at)
        except cbg.Unsupported as exc:
            why_browser = f"the bridge has a recipe for {name} but could not use it ({exc})"
            if backend == "bridge":
                return {"error": str(exc), "performed": False,
                        "bridge_call": adapter.summary}
            notes.append(f"{why_browser}, so this went to the browser")

    if needs_confirm(tool) and not confirm and not dry_run:
        plan = ([c.as_json() for c in calls] if calls else
                [s.as_json() for s in plan_steps(tool, arguments, False, extra, at, open_with)])
        return {
            "error": f"{name} changes or destroys something and needs confirm=true",
            "would_do": tool.title,
            "backend": "bridge" if calls else "browser",
            "state": _state(tool.context),
            "plan": plan,
            "performed": False,
        }

    async with _DRIVE_LOCK:
        result: dict[str, Any] | None = None
        if calls is not None:
            try:
                result = await asyncio.to_thread(cbg.BRIDGE.perform, calls, dry_run)
            except cbg.BridgeError as exc:
                # Raised while opening the session or asking for root, which
                # is before any of the calls ran — so falling back performs
                # the action once, not twice.
                if backend == "bridge" or not CONFIG.use_browser:
                    result = {"performed": False, "backend": "bridge", "error": str(exc)}
                else:
                    why_browser = f"the bridge was not available ({exc})"
                    notes.append(f"{why_browser}, so this went to the browser")
                    calls = None
        if result is None:
            if not CONFIG.use_browser:
                refused: dict[str, Any] = {
                    "error": f"this server runs with --no-browser, and {why_browser}",
                    "performed": False}
                if adapter is not None:
                    refused["bridge_call"] = adapter.summary
                return refused
            steps = plan_steps(tool, arguments, dry_run, extra, at, open_with)
            result = await asyncio.to_thread(run_steps, tool, steps)
        if result.get("performed") and ends_session(name, arguments, extra, at):
            result["host_going_down"] = True

        # the action changed the host; the state everything else reports must
        # not keep describing the host as it was before it
        if result.get("performed") and not dry_run:
            if result.get("host_going_down"):
                # There is nothing left to read it from, and trying would spend
                # a connection timeout only to report a host with nothing on it.
                notes.append("the host is going down, so its state was not read "
                             "again; cockpit_refresh() once it is back")
            else:
                await asyncio.to_thread(CATALOG.refresh_live)

    carried = result.pop("note", None)          # what the backend itself said
    if carried:
        notes.insert(0, carried)
    result.update({"tool": name, "title": tool.title, "dry_run": dry_run,
                   "arguments": arguments})
    # A host on its way down has a state, and it is the one from before the
    # call: four days of uptime printed under a reboot that just succeeded.
    # Better to say there is nothing to report than to report that.
    result["state_after"] = ("not read: the host is going down"
                             if result.get("host_going_down")
                             else _state(tool.context))
    if extra:
        result["extra"] = {k: "…" if _SECRET_RE.search(k) else v for k, v in extra.items()}
    if at:
        result["at"] = at
    if notes:
        result["note"] = " · ".join(notes)
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
    parser.add_argument("--no-bridge", action="store_true",
                        help="never perform an action over the bridge: drive the UI "
                             "for all of them")
    parser.add_argument("--no-browser", action="store_true",
                        help="never start a browser: only the actions cockpit_bridge "
                             "has a recipe for can be performed")
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
    CONFIG.use_bridge = not args.no_bridge
    CONFIG.use_browser = not args.no_browser
    if args.no_bridge and args.no_browser:
        parser.error("--no-bridge and --no-browser together leave nothing that "
                     "can perform an action; --read-only is what you want")
    CONFIG.apply()

    if args.prefetch:
        clt.log("scanning the host before serving (--prefetch)")
        CATALOG.build()

    mcp.settings.port = args.port
    try:
        if args.transport == "stdio":
            mcp.run("stdio")
        else:
            mcp.run(args.transport)
    finally:
        _drop_browser()
        cbg.BRIDGE.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _drop_browser()
        cbg.BRIDGE.close()
        raise SystemExit(130) from None
