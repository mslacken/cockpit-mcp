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
    cockpit_invoke(name, arguments, …)      perform it, for real, over the bridge
    cockpit_refresh(full)                   re-read the host

Every answer carries the live state of the page it concerns, which is the half
a caller cannot infer: "Delete account" means nothing without knowing which
accounts there are, and `cockpit_search_tools("delete account")` says both.

INVOKING IS REAL
===============
Cockpit's UI *is* its API: a page is a React bundle talking to cockpit-bridge
over its own WebSocket. For most dialog buttons the bundle only says which
button exists, not what it does, so there is no generic way to press one.
cockpit_invoke therefore performs an action the one way the call is actually
known:

  * **the bridge** (cockpit_bridge.py) — the same
    ``cockpit.spawn(["/usr/sbin/userdel", "-r", name], {superuser:
    "require"})`` the dialog's own button makes, sent on the same protocol the
    page uses. No browser, about a tenth of a second. The recipes are
    hand-written from the page sources, because a bundle says which button
    exists but not what it does.

Only the actions cockpit_bridge.ADAPTERS has a recipe for can be performed;
every other action in the catalog is reported as one that this server cannot
do rather than attempted (there is no UI to drive it through).

Two gates sit in front of it:

  * an action the catalog marks destructive needs ``confirm=true`` — without
    it the call returns the plan it would have run and changes nothing;
  * ``dry_run=true`` performs nothing: over the bridge it reports the exact
    calls it would make.

``--read-only`` turns every invocation into a dry run, and ``--no-bridge``
leaves nothing that can perform an action (``--read-only`` is what you want).

USAGE
=====
    pip install mcp                       # the official SDK
    COCKPIT_PASSWORD=… ./cockpit_mcp_server.py --insecure https://myhost:9090

    # as an MCP client would start it (claude_desktop_config.json, .mcp.json):
    {"mcpServers": {"cockpit": {
        "command": "/path/to/.venv-mcp/bin/python",
        "args": ["/path/to/cockpit_mcp_server.py", "--insecure", "https://myhost:9090"],
        "env": {"COCKPIT_USER": "chris", "COCKPIT_PASSWORD": "…"}}}}

The password is needed twice over: once for the session that reads the catalog,
once for the privileged bridge session that performs the calls.
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
    read_only: bool = False
    use_bridge: bool = True             # perform what can be performed directly

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

    # The account-details page has the same problem, and worse: most of its
    # controls never become actions at all. "Unlock" is the other way of the
    # "Disallow interactive password" checkbox the scan does find (which it
    # labels "Lock account"); the full name is an editable text box that
    # commits on blur, so there is no button to name; the two expiry radios in
    # the "Password expiration" dialog and the group chips are controls the
    # heuristic does not recover as actions. Each has a dialog button the
    # scan does not see, so each is reached by name as system_cancel_reboot is.
    Missed(name="users_account_confirm_unlock",
           title="Unlock account",
           description="Allow interactive password login again, the other way "
                       "of the account-details page's \"Disallow interactive "
                       "password\" checkbox. Locking it (the checkbox's one "
                       "way) is what users_account_confirm_lock performs; this "
                       "is how that is taken back.",
           package="users", page_path="index.html",
           control_id="account-locked"),
    Missed(name="user_set_real_name",
           title="Set account full name",
           description="Set an account's full name (the comment field in "
                       "/etc/passwd). The account-details page edits this as a "
                       "text box that saves when you leave it, so the scan "
                       "finds no button. Needs the name, e.g. "
                       "extra={\"real_name\": \"Chris P.\"}.",
           package="users", page_path="index.html",
           control_id="account-real-name"),
    Missed(name="user_set_password_expiry",
           title="Set password expiry",
           description="Set how long before an account's password expires. 0 "
                       "or \"never\" lifts the expiry (the \"Never expire\" "
                       "radio); a number of days makes it expire that long "
                       "from now. Needs e.g. extra={\"days\": 90} or "
                       "extra={\"mode\": \"never\"}.",
           package="users", page_path="index.html",
           control_id="password-expiration-button"),
    Missed(name="user_add_to_group",
           title="Add account to group",
           description="Put an account into a group, which the account-details "
                       "page does one group at a time by clicking a group "
                       "chip. Needs the group, e.g. extra={\"group\": "
                       "\"chris\"}, and names the account with at=.",
           package="users", page_path="index.html",
           control_id="account-groups-form-group"),
    Missed(name="user_remove_from_group",
           title="Remove account from group",
           description="Take an account out of a group, the reverse of "
                       "user_add_to_group. Needs the group, e.g. "
                       "extra={\"group\": \"chris\"}, and names the account "
                       "with at=.",
           package="users", page_path="index.html",
           control_id="account-groups-form-group"),
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
_DRIVE_LOCK = asyncio.Lock()        # one bridge session, one action at a time


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
    adapter = cbg.adapter_for(tool.name) if CONFIG.use_bridge else None
    out["backend"] = "bridge" if adapter else "none"
    if adapter:
        out["bridge_call"] = adapter.summary
    else:
        out["note"] = ("this action has no bridge recipe, so this server can "
                       "only describe it, not perform it")
    return out


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
      host: the call is sent straight to cockpit-bridge, exactly as the page's
      own button would send it. Only actions that this server has a recipe for
      can be performed; for the rest cockpit_describe_tool says so (backend)
      and cockpit_invoke reports that it cannot. Pass dry_run=true first to
      see what it would do; an action that deletes, disables or restarts
      something additionally requires confirm=true.

An action that belongs to one of the objects a page lists — this account,
that unit, that interface — needs to be told which one: pass at="mcptest",
the name the page uses in its own URL. Inputs the catalog does not name go in
extra, keyed by the caption the dialog puts next to them: that is where the
password for a new account goes, and the shell, group name or similar that an
action asks for.

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
        pkg_page = cat.page_for(tool)
        page_kw = " ".join(pkg_page[1].keywords).lower() if pkg_page else ""
        tool_aliases = " ".join(getattr(tool, "aliases", [])).lower()
        context_text = " ".join(r.line() for r in tool.context).lower() if tool.context else ""
        haystacks = (tool.name.lower().replace("_", " "), tool.title.lower(),
                     tool.page_label.lower(), tool.description.lower(),
                     " ".join(p.name for p in tool.params).lower(),
                     tool_aliases, page_kw, context_text)
        # a word in the name or title says more than the same word buried in
        # the description, so hits are ranked by where they landed
        score = 0
        for word in words:
            weights = (8, 6, 3, 1, 2, 6, 4, 3)
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
                "(the accounts, units, devices it can be aimed at), whether it "
                "can be done over this server's bridge, and — if it can — the "
                "call cockpit_invoke would send.",
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
                "The call is sent straight to cockpit-bridge, exactly as the action's "
                "dialog would send it (cockpit_describe_tool reports backend=bridge "
                "and the call); only actions with a bridge recipe can be performed, "
                "and every other action in the catalog is reported as one that this "
                "server cannot do. Pass dry_run=true to see what would be done "
                "without doing it. An action that deletes, disables, restarts or "
                "otherwise disrupts something is refused unless confirm=true is "
                "passed with it; an action with a recipe that needs a field the "
                "catalog does not name tells you which, and it is supplied through "
                "`extra`, keyed by the caption the dialog puts next to it — which "
                "is how the bridge is told a password, a shell or a new name.",
)
async def cockpit_invoke(name: str, arguments: dict[str, Any] | None = None,
                         dry_run: bool = False, confirm: bool = False,
                         extra: dict[str, Any] | None = None,
                         at: str | None = None,
                         backend: str = "auto") -> dict[str, Any]:
    """
    Args:
        name: the action to perform, as returned by cockpit_search_tools.
        arguments: its inputs, as described by cockpit_describe_tool.
        dry_run: report what would be done without doing it — over the bridge
            the exact calls it would make.
        confirm: required for destructive actions; ignored for the others.
        extra: further inputs, keyed by their visible label rather than by a
            schema name — for the fields the scan did not recover, such as the
            passwords the create-account dialog insists on.
        at: which of the objects the page lists to act on, named the way the
            page addresses it in its hash — "mcptest" for an account,
            "sshd.service" for a unit, "ens4" for an interface. Required
            whenever the action belongs to one object rather than to the page.
        backend: "auto" (the default) or "bridge"; both send the call over the
            bridge, and this parameter is kept only so that an older client
            that still passes it does not break. "browser" is no longer
            supported.
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

    if backend not in ("auto", "bridge"):
        return {"error": f"backend must be \"auto\" or \"bridge\", not {backend!r} "
                         f"(the browser path was removed; there is no UI to drive)"}

    notes: list[str] = []
    if CONFIG.read_only and not dry_run:
        dry_run = True
        notes.append("the server runs with --read-only, so this was a dry run")

    # The bridge is the only way this server performs an action, so an adapter
    # is required and its ability to build the calls is itself the plan.
    if not CONFIG.use_bridge:
        return {"error": "this server runs with --no-bridge, so it cannot "
                         "perform any action", "performed": False,
                "backend": "none"}
    adapter = cbg.adapter_for(name)
    if adapter is None:
        return {"error": f"there is no bridge recipe for {name!r}, and this "
                         "server has no other way to perform it",
                "performed": False, "backend": "none"}
    try:
        calls = cbg.plan(name, arguments, extra, at)
    except cbg.Unsupported as exc:
        return {"error": f"the bridge has a recipe for {name} but could not build "
                         f"it from what was passed: {exc}",
                "performed": False, "backend": "bridge",
                "bridge_call": adapter.summary}

    if needs_confirm(tool) and not confirm and not dry_run:
        return {
            "error": f"{name} changes or destroys something and needs confirm=true",
            "would_do": tool.title,
            "backend": "bridge",
            "state": _state(tool.context),
            "plan": [c.as_json() for c in calls],
            "performed": False,
        }

    async with _DRIVE_LOCK:
        result: dict[str, Any]
        try:
            result = await asyncio.to_thread(cbg.BRIDGE.perform, calls, dry_run)
        except cbg.BridgeError as exc:
            result = {"performed": False, "backend": "bridge", "error": str(exc)}

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
                   "arguments": arguments, "backend": "bridge"})
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
                    "its live state, the actions its UI offers, and the bridge calls "
                    "that perform them.",
        epilog="Environment: COCKPIT_USER, COCKPIT_PASSWORD, COCKPIT_MCP_LIVE_TTL")
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
    parser.add_argument("--no-bridge", action="store_true",
                        help="never perform an action over the bridge: describe "
                             "them, but refuse to do any of them")
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
    CONFIG.read_only = args.read_only
    CONFIG.use_bridge = not args.no_bridge
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
        cbg.BRIDGE.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cbg.BRIDGE.close()
        raise SystemExit(130) from None
