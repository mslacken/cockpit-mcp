#!/usr/bin/env python3
"""
cockpit_bridge.py — perform Cockpit actions over the bridge, without a browser.

WHY THIS EXISTS
===============
cockpit_mcp_server.py performs an action the only way the UI can be asked to:
it drives a headless Chromium through the page. That is faithful — whatever
the dialog does, it does — but it needs a browser on the machine running the
server, and it costs a page load and a render per action.

The page itself does not talk to a browser. It talks to cockpit-bridge over
the session WebSocket, in a protocol (doc/protocol.md) that
cockpit_list_tools.py already speaks for its live probes. Everything a dialog
does when its button is pressed is one or two messages on that socket:

    cockpit.spawn(["/usr/sbin/userdel", "-r", name], {superuser: "require"})
    client.call("/org/freedesktop/systemd1", …, "RestartUnit", [unit, "fail"])

So for the actions whose call is known, the browser is not needed at all.
This module is that path: a privileged bridge session, plus a table of
adapters mapping catalog action names to the calls their dialogs make.

WHAT IT CANNOT BE
=================
It cannot be derived from the scan. The bundles say which button exists, not
what it does: Cockpit's older dialogs keep their handler inline — parsing the
``{actions: [{caption, style, clicked}]}`` footers recovers the exact argv —
but there are 14 of those host-wide, and everything newer is React, where of
764 ``onClick`` props exactly two contain a bridge call; the rest delegate to
named functions. So the adapters below are *hand-written* from the page
sources, each one a transcription of a specific call site, cited in a comment.

Hence the division of labour, which cockpit_invoke implements:

    an action with an adapter   → the bridge: no browser, ~0.1s
    everything else             → the browser, as before

An adapter that is handed too little to build its calls raises Unsupported,
and the caller falls back to the browser rather than guessing.

THE PRIVILEGED BRIDGE
=====================
Most of these calls need root, and a channel asking for it before the
privileged bridge exists is refused outright (src/cockpit/superuser.py,
apply_rule → "access-denied"). Starting it from a WebSocket client is not
guessable from the docs:

  * ``{"command":"init", …, "superuser":{"id":"any"}}`` does nothing —
    cockpit-ws consumes the client's init and never forwards it to the bridge.
  * The way the shell's own menu does it is a ``dbus-json3`` channel on
    ``"bus":"internal"`` and ``Start("any")`` on /superuser.
  * That call hangs forever unless the channel first sends an ``add-match``
    for the interface: without it the sudo password ``Prompt`` signal is never
    delivered. Answer it with ``Answer([password])``.

USAGE
=====
    ./cockpit_bridge.py --insecure https://myhost:9090            # check access
    ./cockpit_bridge.py --insecure https://myhost:9090 \
        --probe bridgeprobe                     # create and delete an account

Environment: COCKPIT_USER, COCKPIT_PASSWORD — as for the other two scripts.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import cockpit_list_tools as clt

SUPERUSER_TIMEOUT = float(os.environ.get("COCKPIT_BRIDGE_SUPERUSER_TIMEOUT", "60"))


class BridgeError(RuntimeError):
    """The bridge could not be reached, or would not give us root."""


class Unsupported(Exception):
    """This action has no adapter, or not enough was passed to build one.

    Not an error: the caller is expected to fall back to the browser, which
    needs no recipe because it presses the real button.
    """


# ==========================================================================
# One call: what a dialog's button does
# ==========================================================================
#
# Two kinds, because the pages use two: a command on a "stream" channel
# (cockpit.spawn) and a method on a "dbus-json3" one (cockpit.dbus). They
# render as one list so a plan reads top to bottom whichever it mixes.

@dataclass
class Call:
    op: str                                     # "spawn" | "dbus"
    argv: list[str] = field(default_factory=list)
    stdin: str | None = None                    # never rendered: it is a password
    service: str = ""
    path: str = ""
    interface: str = ""
    method: str = ""
    args: list[Any] = field(default_factory=list)
    detail: str = ""
    status: str = "pending"                     # pending | done | failed | skipped
    output: str = ""

    def render(self) -> str:
        """The call as a line a human can check before it is run."""
        if self.op == "spawn":
            line = " ".join(shlex.quote(a) for a in self.argv)
            return line + ("  <<< (not shown)" if self.stdin is not None else "")
        return f"{self.method}({', '.join(json.dumps(a) for a in self.args)}) " \
               f"on {self.service} {self.path}"

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op, "run": self.render(),
                               "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.output:
            out["output"] = self.output[:2000]
        return out


def spawn(argv: list[str], stdin: str | None = None, detail: str = "") -> Call:
    return Call("spawn", argv=[str(a) for a in argv], stdin=stdin, detail=detail)


def dbus(service: str, path: str, interface: str, method: str, args: list[Any],
         detail: str = "") -> Call:
    return Call("dbus", service=service, path=path, interface=interface,
                method=method, args=args, detail=detail)


# ==========================================================================
# A session that has asked for, and got, a root bridge
# ==========================================================================

class PrivilegedSession(clt.Session):
    """clt.Session plus the two things the read-only scan refuses to do:
    start the superuser bridge, and open channels that use it."""

    def __init__(self, ws: clt.WebSocket) -> None:
        super().__init__(ws)
        self.access = "unprivileged"
        self.how = ""

    # ------------------------------------------------------------ superuser

    def start_superuser(self, password: str,
                        timeout: float = SUPERUSER_TIMEOUT) -> str:
        """Start the privileged bridge, the way the shell's own menu does.

        cockpit.Superuser lives on the bridge's *internal* bus: Start("any")
        launches the sudo peer, which asks for the password through a Prompt
        signal that Answer() replies to.
        """
        channel = self._next_channel()
        self.ws.send_control({"command": "open", "channel": channel,
                              "payload": "dbus-json3", "bus": "internal"})
        # Without an add-match the Prompt signal never reaches this channel —
        # the JS proxy does the same subscription behind addEventListener.
        self._send_payload(channel, json.dumps(
            {"add-match": {"path": "/superuser", "interface": "cockpit.Superuser"}}))
        self._send_payload(channel, json.dumps(
            {"call": ["/superuser", "cockpit.Superuser", "Start", ["any"]],
             "id": "start"}))

        prompts: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                opcode, payload = self.ws.recv_frame()
            except (OSError, ConnectionError, struct.error) as exc:
                raise BridgeError(f"the transport died starting superuser: {exc}") from exc
            if opcode == clt.OP_PING:
                self.ws.send_frame(clt.OP_PONG, payload)
                continue
            if opcode == clt.OP_CLOSE:
                raise BridgeError("the server closed the socket while starting superuser")
            if opcode != clt.OP_TEXT:
                continue
            head, _, body = payload.decode("utf-8", "replace").partition("\n")
            if not head:                                      # a control message
                msg = json.loads(body)
                if msg.get("command") == "ping":
                    self.ws.send_control({**msg, "command": "pong"})
                elif msg.get("command") == "close" and msg.get("channel") == channel:
                    raise BridgeError(f"the superuser channel closed: "
                                      f"{msg.get('problem') or msg}")
                continue
            if head != channel:
                continue
            msg = json.loads(body)
            if "signal" in msg:
                _path, _iface, name, args = msg["signal"]
                if name == "Prompt":
                    # (message, prompt, default, echo, error)
                    prompts.append(args[1] or args[0])
                    self._send_payload(channel, json.dumps(
                        {"call": ["/superuser", "cockpit.Superuser", "Answer",
                                  [password]], "id": "answer"}))
            elif msg.get("id") == "start":
                if "error" in msg:
                    raise BridgeError(f"Start(any) was refused: {_dbus_error(msg['error'])}")
                self._close_channel(channel)
                self.access = "root"
                self.how = (f"sudo, prompted {prompts[0]!r}" if prompts
                            else "no password was needed")
                return self.how
        raise BridgeError(f"Start(any) did not return within {timeout:g}s")

    # ----------------------------------------------------------- the calls

    def run(self, argv: list[str], stdin: str | None = None,
            superuser: bool = True) -> tuple[str, dict[str, Any]]:
        """cockpit.spawn(argv, {superuser: "require"}) — with optional stdin.

        Returns what the command wrote and the channel's close message, which
        carries "problem" (the bridge refused) or "exit-status" and "message"
        (the command ran and failed).
        """
        channel = self._next_channel()
        self.calls += 1
        options: dict[str, Any] = {"command": "open", "channel": channel,
                                   "payload": "stream", "spawn": list(argv),
                                   "environ": ["LC_ALL=C"], "err": "message"}
        if superuser:
            options["superuser"] = "require"
        self.ws.send_control(options)
        if stdin is not None:
            self._send_payload(channel, stdin)
        # A command reading stdin waits for EOF, which is what "done" is.
        self.ws.send_control({"command": "done", "channel": channel})
        frames, close = self._collect(channel)
        return "".join(frames), close

    def call(self, service: str, path: str, interface: str, method: str,
             args: list[Any], superuser: bool = True,
             bus: str = "system") -> tuple[Any, dict[str, Any]]:
        """One D-Bus method call on a privileged channel.

        Returns its reply (the out arguments) and a close-shaped dict, empty
        when the call succeeded.
        """
        channel = self._next_channel()
        self.calls += 1
        options: dict[str, Any] = {"command": "open", "channel": channel,
                                   "payload": "dbus-json3", "bus": bus,
                                   "name": service}
        if superuser:
            options["superuser"] = "require"
        self.ws.send_control(options)
        self._send_payload(channel, json.dumps(
            {"call": [path, interface, method, list(args)], "id": "call"}))
        frames, close = self._collect(channel, reply_id="call")
        self._close_channel(channel)
        for frame in frames:
            try:
                msg = json.loads(frame)
            except ValueError:
                continue
            if msg.get("id") != "call":
                continue
            if "reply" in msg:
                return (msg["reply"][0] if msg["reply"] else None), {}
            if "error" in msg:
                return None, {"problem": _dbus_error(msg["error"])}
        return None, {"problem": close.get("problem") or "no reply"}

    def get(self, service: str, path: str, interface: str, prop: str) -> Any:
        """One property, read the way the pages read it."""
        return (self.properties(service, path, interface) or {}).get(prop)

    # --------------------------------------------------------- a whole plan

    def perform(self, calls: list[Call]) -> dict[str, Any]:
        """Run the calls in order, stopping at the first that fails.

        Stopping matters: the create-account recipe is useradd *then*
        chpasswd, and carrying on after a failed useradd would set a password
        on whatever account already had that name.
        """
        result: dict[str, Any] = {"performed": False, "access": self.access,
                                  "backend": "bridge"}
        for call in calls:
            try:
                if call.op == "spawn":
                    out, close = self.run(call.argv, call.stdin)
                else:
                    reply, close = self.call(call.service, call.path,
                                             call.interface, call.method, call.args)
                    out = "" if reply in (None, []) else json.dumps(reply)
            except (OSError, ConnectionError, struct.error) as exc:
                call.status = "failed"
                call.detail = f"the transport died: {exc}"
                result["error"] = call.detail
                result["transport_lost"] = True
                break
            call.output = (out or "").strip()
            problem = close.get("problem")
            status = close.get("exit-status")
            if problem or status:
                call.status = "failed"
                # "message" is the command's own stderr, which err:"message"
                # put there — it is the best explanation of a failure there is.
                why = close.get("message") or problem or f"exit status {status}"
                call.detail = str(why).strip()
                result["error"] = f"{call.render()}: {call.detail}"
                break
            call.status = "done"
        else:
            result["performed"] = bool(calls)
        result["steps"] = [c.as_json() for c in calls]
        return result


def _dbus_error(error: Any) -> str:
    """dbus-json3 reports an error as [name, [message, …]]."""
    if isinstance(error, list) and error:
        name = str(error[0])
        rest = error[1] if len(error) > 1 else None
        if isinstance(rest, list) and rest:
            return f"{name}: {rest[0]}"
        return name
    return str(error)


# ==========================================================================
# The session, kept alive between calls
# ==========================================================================

class Bridge:
    """One privileged session for the life of the process, re-made as needed.

    A login and a sudo round trip cost about a second, and calls come one
    after another on the same host, so the session is kept. It is also the
    thing most likely to go stale — Cockpit drops idle sessions — so every
    use is preceded by a cheap privileged probe, and a session that fails it
    is replaced. The probe is deliberately in front of the *action*: silently
    retrying a mutation on a new session could apply it twice.
    """

    def __init__(self) -> None:
        self._session: PrivilegedSession | None = None

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.ws.close()
            except OSError:
                pass
            self._session = None

    def session(self, password: str | None = None) -> PrivilegedSession:
        if self._session is not None:
            out, close = self._session.run(["id", "-un"])
            if not close.get("problem") and out.strip() == "root":
                return self._session
            clt.log("   (bridge) the privileged session went away; opening another")
            self.close()

        password = password or os.environ.get("COCKPIT_PASSWORD") or ""
        if not password:
            raise BridgeError("no COCKPIT_PASSWORD to start the privileged bridge with")
        try:
            cookie, _manifests = clt.connect()
            ws = clt.ws_connect(cookie)
        except (OSError, RuntimeError) as exc:
            raise BridgeError(f"could not open a session on the host: {exc}") from exc
        first = ws.recv_control()
        if not first or first.get("command") != "init":
            ws.close()
            raise BridgeError(f"expected the bridge's init, got {first}")
        ws.send_control({"command": "init", "version": 1})
        session = PrivilegedSession(ws)
        try:
            session.start_superuser(password)
        except BridgeError:
            ws.close()
            raise
        self._session = session
        return session

    def perform(self, calls: list[Call], dry_run: bool = False,
                password: str | None = None) -> dict[str, Any]:
        """Run a plan. A dry run reports it and touches nothing."""
        if dry_run:
            for call in calls:
                call.status = "skipped"
            return {"performed": False, "backend": "bridge", "dry_run": True,
                    "steps": [c.as_json() for c in calls],
                    "note": "the bridge lists the calls it would make without "
                            "running any of them; backend=\"browser\" instead "
                            "walks the real dialog and presses Cancel"}
        session = self.session(password)
        result = session.perform(calls)
        if result.get("transport_lost"):
            self.close()
        return result


BRIDGE = Bridge()


# ==========================================================================
# Adapters: one catalog action → the calls its dialog makes
# ==========================================================================
#
# Each of these is a transcription of a call site in the page's own source,
# named in its comment. When one of them stops matching the page, the browser
# path still performs the action correctly — so the rule for anything
# uncertain is to raise Unsupported rather than to approximate.

@dataclass
class Request:
    """What cockpit_invoke was given, in the form an adapter wants it."""
    arguments: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    at: str | None = None

    def value(self, *names: str) -> Any:
        """A named input, from `arguments` or — for the fields the scan never
        recovered — from `extra`, whose keys are the dialog's visible labels.
        """
        pool = {_key(k): v for k, v in self.extra.items()}
        pool.update({_key(k): v for k, v in self.arguments.items()})
        for name in names:
            if pool.get(_key(name)) not in (None, ""):
                return pool[_key(name)]
        return None

    def need(self, *names: str, what: str = "") -> Any:
        value = self.value(*names)
        if value is None:
            raise Unsupported(f"the bridge needs {what or names[0]!r}: pass it as "
                              f"extra={{{names[0]!r}: …}}")
        return value

    def flag(self, *names: str) -> bool:
        value = self.value(*names)
        return bool(value) and str(value).lower() not in ("false", "0", "no")

    def target(self, what: str) -> str:
        if not self.at:
            raise Unsupported(f"this action works on one {what}: pass at=\"…\"")
        return str(self.at).lstrip("#/")


def _key(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(name).strip().lower())


@dataclass
class Adapter:
    build: Callable[[Request], list[Call]]
    summary: str                     # what it does, for cockpit_describe_tool


# ------------------------------------------------------------------- users

def _create_account(req: Request) -> list[Call]:
    # pkg/users/account-create-dialog.js create(): useradd, then passwd_change
    # (pkg/users/password-dialogs.js), then the two optional follow-ups.
    name = str(req.need("user_name", what="the account name"))
    password = str(req.need("password", "new_password", what="a password"))
    argv = ["useradd", "--create-home"]
    # The dialog always passes -s because it has read useradd's own default
    # into the field; leaving it off here lets useradd apply that same default.
    if req.value("user_shell", "shell"):
        argv += ["-s", str(req.value("user_shell", "shell"))]
    if req.value("real_name", "full_name"):
        argv += ["-c", str(req.value("real_name", "full_name"))]
    if req.value("user_uid", "uid"):
        argv += ["-u", str(req.value("user_uid", "uid"))]
    if req.value("user_home_dir", "home_dir"):
        argv += ["-d", str(req.value("user_home_dir", "home_dir"))]
    argv.append(name)

    calls = [spawn(argv, detail="create the account"),
             spawn(["chpasswd"], stdin=f"{name}:{password}\n",
                   detail="set its password, as passwd_change() does")]
    if req.flag("locked"):
        calls.append(spawn(["usermod", name, "--lock"], detail="lock it"))
    if req.flag("force_password_change", "force_change"):
        calls.append(spawn(["passwd", "-e", name],
                           detail="expire the password, forcing a change at login"))
    return calls


def _delete_account(req: Request) -> list[Call]:
    # pkg/users/delete-account-dialog.js — including the "Delete files" box,
    # without which the home directory is left behind with a dangling uid.
    name = req.target("account")
    argv = ["/usr/sbin/userdel"] + (["-r"] if req.flag("files", "delete_files") else []) + [name]
    return [spawn(argv, detail="delete the account" +
                  (" and its files" if "-r" in argv else ", keeping its files"))]


def _lock_account(req: Request) -> list[Call]:
    # pkg/users/lock-account-dialog.js
    return [spawn(["/usr/sbin/usermod", req.target("account"), "--lock"],
                  detail="lock the account")]


def _logout_account(req: Request) -> list[Call]:
    # pkg/users/logout-account-dialog.js
    return [spawn(["loginctl", "terminate-user", req.target("account")],
                  detail="end all of the account's sessions")]


def _set_password(req: Request) -> list[Call]:
    # pkg/users/password-dialogs.js passwd_change(). The dialog's own "old
    # password" box is for a user changing their *own* password through
    # passwd(1); an administrator setting someone else's goes through chpasswd
    # and is never asked for it.
    name = req.target("account")
    password = str(req.need("new_password", "password", what="the new password"))
    return [spawn(["chpasswd"], stdin=f"{name}:{password}\n",
                  detail=f"set {name}'s password")]


def _force_password_change(req: Request) -> list[Call]:
    # pkg/users/password-dialogs.js, the "Force change" control
    return [spawn(["passwd", "-e", req.target("account")],
                  detail="expire the password, forcing a change at the next login")]


def _change_shell(req: Request) -> list[Call]:
    # pkg/users/shell-dialog.js
    name = req.target("account")
    shell = str(req.need("shell", "user_shell", what="the login shell"))
    return [spawn(["usermod", "--shell", shell, name],
                  detail=f"give {name} the shell {shell}")]


def _create_group(req: Request) -> list[Call]:
    # pkg/users/group-create-dialog.js create(): groupadd name -g id, where
    # the dialog has already filled id with the next free one.
    name = str(req.need("name", "group_name", what="the group name"))
    argv = ["groupadd", name]
    if req.value("id", "gid"):
        argv += ["-g", str(req.value("id", "gid"))]
    return [spawn(argv, detail="create the group")]


def _delete_group(req: Request) -> list[Call]:
    # pkg/users/delete-group-dialog.js: -f when the group is somebody's
    # primary group, which is what its button says ("Force delete").
    name = req.target("group")
    argv = ["groupdel"] + (["-f"] if req.flag("force") else []) + [name]
    return [spawn(argv, detail="delete the group")]


def _rename_group(req: Request) -> list[Call]:
    # pkg/users/rename-group-dialog.jsx
    group = req.target("group")
    new = str(req.need("name", "new_name", what="the new group name"))
    return [spawn(["groupmod", group, "--new-name", new],
                  detail=f"rename {group} to {new}")]


# ----------------------------------------------------------------- systemd

S_SERVICE = "org.freedesktop.systemd1"
S_PATH = "/org/freedesktop/systemd1"
S_MANAGER = "org.freedesktop.systemd1.Manager"


def _unit_action(method: str, mode: list[Any], what: str) -> Callable[[Request], list[Call]]:
    # pkg/systemd/service-details.jsx unitAction(): the manager method, the
    # unit's name, and "fail" as the job mode — not "replace".
    def build(req: Request) -> list[Call]:
        unit = req.target("unit")
        if "." not in unit:
            unit += ".service"              # the services page's own default
        return [dbus(S_SERVICE, S_PATH, S_MANAGER, method, [unit, *mode],
                     detail=f"{what} {unit}")]
    return build


ADAPTERS: dict[str, Adapter] = {
    # users: the dialogs that perform the action …
    "users_accounts_create": Adapter(_create_account,
        "useradd --create-home …, then chpasswd (needs the password in `extra`)"),
    "users_account_confirm_delete": Adapter(_delete_account,
        "userdel [-r] <at>"),
    "users_account_confirm_lock": Adapter(_lock_account, "usermod <at> --lock"),
    "users_account_confirm_logout": Adapter(_logout_account,
        "loginctl terminate-user <at>"),
    "users_account_set_password": Adapter(_set_password,
        "chpasswd (needs the new password in `extra`)"),
    "users_shell": Adapter(_change_shell,
        "usermod --shell <shell> <at> (needs the shell in `extra`)"),
    "users_groups_create": Adapter(_create_group, "groupadd <name> [-g <id>]"),
    "users_group_confirm_rename": Adapter(_rename_group,
        "groupmod <at> --new-name <name> (needs the new name in `extra`)"),

    # … and the page controls that open them. The browser needs both halves;
    # over the bridge there is no dialog to open, so pressing "Delete" on an
    # account is the same request as confirming it, and a caller who found
    # the opener in the catalog means the action.
    "users_delete_account": Adapter(_delete_account, "userdel [-r] <at>"),
    "users_lock_account": Adapter(_lock_account, "usermod <at> --lock"),
    "users_log_user_out": Adapter(_logout_account, "loginctl terminate-user <at>"),
    "users_terminate_session": Adapter(_logout_account,
        "loginctl terminate-user <at>"),
    "users_force_change": Adapter(_force_password_change, "passwd -e <at>"),
    "users_delete_group": Adapter(_delete_group, "groupdel [-f] <at>"),
    "users_rename_group": Adapter(_rename_group,
        "groupmod <at> --new-name <name> (needs the new name in `extra`)"),

    # systemd units, addressed by at="sshd.service"
    "system_start": Adapter(_unit_action("StartUnit", ["fail"], "start"),
                            "systemd StartUnit(<at>, fail)"),
    "system_start_service": Adapter(_unit_action("StartUnit", ["fail"], "start"),
                                    "systemd StartUnit(<at>, fail)"),
    "system_stop": Adapter(_unit_action("StopUnit", ["fail"], "stop"),
                           "systemd StopUnit(<at>, fail)"),
    "system_restart": Adapter(_unit_action("RestartUnit", ["fail"], "restart"),
                              "systemd RestartUnit(<at>, fail)"),
    "system_reload": Adapter(_unit_action("ReloadUnit", ["fail"], "reload"),
                             "systemd ReloadUnit(<at>, fail)"),
    "system_clear_failed_to_start": Adapter(
        _unit_action("ResetFailedUnit", [], "clear the failed state of"),
        "systemd ResetFailedUnit(<at>)"),
}


def adapter_for(name: str) -> Adapter | None:
    return ADAPTERS.get(name)


def plan(name: str, arguments: dict[str, Any] | None = None,
         extra: dict[str, Any] | None = None, at: str | None = None) -> list[Call]:
    """The calls that perform ``name``. Raises Unsupported if it cannot say."""
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise Unsupported(f"no bridge adapter for {name!r}")
    return adapter.build(Request(dict(arguments or {}), dict(extra or {}), at))


# ==========================================================================
# Main: check the privileged path, and optionally exercise it end to end
# ==========================================================================

def _accounts(session: PrivilegedSession) -> list[str]:
    """The human accounts, read the way the users page reads them."""
    passwd = session.read_file("/etc/passwd") or ""
    return [line.split(":")[0] for line in passwd.splitlines()
            if line.count(":") >= 6 and (line.split(":")[2] or "0").isdigit()
            and int(line.split(":")[2]) >= 1000]


def _report(label: str, result: dict[str, Any]) -> bool:
    ok = result.get("performed")
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    for step in result["steps"]:
        out = step.get("output", "")
        print(f"          {step['status']:8} {step['run']}"
              + (f"   [{out[:60]}]" if out else "")
              + (f"   -> {step['detail']}" if step["status"] == "failed" else ""))
    return bool(ok)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Perform Cockpit actions over the bridge protocol, with no "
                    "browser: check that the privileged path works, or exercise "
                    "it on a throwaway account.",
        epilog="Environment: COCKPIT_USER, COCKPIT_PASSWORD")
    parser.add_argument("target", nargs="?", default=None,
                        metavar="[SCHEME://]HOST[:PORT]",
                        help=f"the Cockpit server (default: {clt.DEFAULT_TARGET})")
    parser.add_argument("--user", metavar="NAME", help="log in as this user")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the server's TLS certificate")
    parser.add_argument("--probe", metavar="NAME",
                        help="create an account of this name and delete it again, "
                             "which is the end-to-end check of the write path")
    parser.add_argument("--list", action="store_true",
                        help="list the catalog actions that have an adapter")
    args = parser.parse_args(argv)

    if args.list:
        for name, adapter in sorted(ADAPTERS.items()):
            print(f"  {name:34} {adapter.summary}")
        return 0

    clt.set_target(args.target or os.environ.get("COCKPIT_HOST") or clt.DEFAULT_TARGET)
    clt.INSECURE = args.insecure
    user = args.user or os.environ.get("COCKPIT_USER") or getpass.getuser()
    password = os.environ.get("COCKPIT_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            parser.error("no COCKPIT_PASSWORD in the environment and no tty to ask on")
        password = getpass.getpass(f"Cockpit password for {user}: ")
    os.environ["COCKPIT_USER"], os.environ["COCKPIT_PASSWORD"] = user, password

    started = time.time()
    session = BRIDGE.session(password)
    print(f"privileged bridge up ({session.how}), {time.time() - started:.1f}s from cold")
    whoami, _close = session.run(["id", "-un"])
    print(f"  channels run as {whoami.strip()!r}")

    if not args.probe:
        print(f"\n{len(ADAPTERS)} actions can be performed this way "
              f"(--list to see them).")
        BRIDGE.close()
        return 0

    name = args.probe
    if name in _accounts(session):
        print(f"\n{name!r} already exists — pick a name that does not, so that "
              f"this check cannot delete something real.")
        BRIDGE.close()
        return 1

    print(f"\ncreate {name}:")
    ok = _report("users_accounts_create", BRIDGE.perform(plan(
        "users_accounts_create",
        {"user_name": name, "real_name": "Bridge probe", "user_shell": "/bin/bash"},
        {"password": "Str0ng-MCP-pw!x"})))
    print(f"  accounts now: {_accounts(session)}")

    if ok:
        print(f"\ndelete {name} (files included, as the dialog's checkbox does):")
        ok = _report("users_account_confirm_delete", BRIDGE.perform(
            plan("users_account_confirm_delete", {"files": True}, at=name)))
        print(f"  accounts now: {_accounts(session)}")
        _out, close = session.run(["test", "-d", f"/home/{name}"])
        print(f"  home directory gone: {bool(close.get('exit-status'))}")

    print(f"\n{time.time() - started:.1f}s, {session.calls} channels")
    BRIDGE.close()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        BRIDGE.close()
        raise SystemExit(130) from None
    except (BridgeError, Unsupported) as exc:
        print(f"cockpit_bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
