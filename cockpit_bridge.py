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
    ./cockpit_bridge.py --insecure https://myhost:9090 \
        --probe-reboot                          # schedule a reboot, call it off

Environment: COCKPIT_USER, COCKPIT_PASSWORD — as for the other two scripts.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
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
    # "shutdown --reboot +0" is answered by the host going away, which looks
    # exactly like a failure from here: the channel closes with a problem, or
    # the socket dies mid-call. Only the call itself knows that this is the
    # expected answer, so it says so.
    takes_host_down: bool = False

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


def spawn(argv: list[str], stdin: str | None = None, detail: str = "",
          takes_host_down: bool = False) -> Call:
    return Call("spawn", argv=[str(a) for a in argv], stdin=stdin, detail=detail,
                takes_host_down=takes_host_down)


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
        going_down = False
        for call in calls:
            try:
                if call.op == "spawn":
                    out, close = self.run(call.argv, call.stdin)
                else:
                    reply, close = self.call(call.service, call.path,
                                             call.interface, call.method, call.args)
                    out = "" if reply in (None, []) else json.dumps(reply)
            except (OSError, ConnectionError, struct.error) as exc:
                if call.takes_host_down:
                    call.status, call.detail = "done", f"{GOING_DOWN} ({exc})"
                    going_down = True
                    break
                call.status = "failed"
                call.detail = f"the transport died: {exc}"
                result["error"] = call.detail
                result["transport_lost"] = True
                break
            call.output = (out or "").strip()
            problem = close.get("problem")
            status = close.get("exit-status")
            if problem and call.takes_host_down and _lost(problem):
                call.status, call.detail = "done", f"{GOING_DOWN} ({problem})"
                going_down = True
                break
            if problem or status:
                call.status = "failed"
                # "message" is the command's own stderr, which err:"message"
                # put there — it is the best explanation of a failure there is.
                why = close.get("message") or problem or f"exit status {status}"
                call.detail = str(why).strip()
                result["error"] = f"{call.render()}: {call.detail}"
                break
            # A command that succeeded and still said something said the one
            # thing worth reading: shutdown(1) writes "Reboot scheduled for …"
            # to stderr, which err:"message" put in the close message.
            if not call.output and close.get("message"):
                call.output = str(close["message"]).strip()
            call.status = "done"
        else:
            result["performed"] = bool(calls)
        if going_down:
            # The command ran; what did not come back is the host. Calls after
            # it are not attempted — there is nothing left to attempt them on.
            result["performed"] = True
            result["transport_lost"] = True
        # Usually it does not come to that: "shutdown --reboot +0" answers
        # before logind gets round to killing the session, and the call looks
        # like any other success. The host is on its way down either way, and
        # a caller told otherwise would read a four-day uptime off a machine
        # that is rebooting.
        if any(call.takes_host_down and call.status == "done" for call in calls):
            result["host_going_down"] = True
        result["steps"] = [c.as_json() for c in calls]
        return result


GOING_DOWN = ("the host stopped answering — which, for this call, is it doing "
              "what it was told")

# A host that went down under us is reported in too many ways to enumerate —
# "disconnected" and "terminated" from the bridge, "timeout" from _collect(),
# and whatever the socket raised when it was torn down mid-call. So the test
# runs the other way: these are the problems that mean the bridge was there
# and said no, and everything else, for a call that was going to take the
# host down, means it did.
_REFUSALS = ("access-denied", "authentication-failed", "not-found",
             "not-supported", "too-large")


def _lost(problem: str) -> bool:
    return not any(token in str(problem) for token in _REFUSALS)


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


def _unlock_account(req: Request) -> list[Call]:
    # pkg/users/account-details.js change_locked(true): usermod --unlock. The
    # "Disallow interactive password" checkbox flips lock and unlock with the
    # same control, but the catalog can only name the "Lock account" button the
    # scan finds, so the other side of that checkbox is named by hand.
    return [spawn(["/usr/sbin/usermod", req.target("account"), "--unlock"],
                  detail="allow interactive password login again")]


def _set_real_name(req: Request) -> list[Call]:
    # pkg/users/account-details.js changeRealName(): usermod --comment. The
    # control is an editable text box that commits on blur, so the scan never
    # sees a "Save" button for it. The dialog refuses a name containing ':' and
    # the dialog's own check is mirrored here.
    name = req.target("account")
    comment = str(req.need("real_name", "full_name", "name", "comment",
                           what="the full name"))
    if ":" in comment:
        raise Unsupported("the full name must not contain a ':'")
    return [spawn(["/usr/sbin/usermod", name, "--comment", comment],
                  detail=f"set {name}'s full name")]


def _set_password_expiration(req: Request) -> list[Call]:
    # pkg/users/expiration-dialogs.js: passwd -x <days>. -1 lifts the expiry
    # (the "Never expire" radio); a positive number is days from now.
    name = req.target("account")
    raw = req.value("days", "days_until_expiration",
                    "number_of_days", "expiration_days")
    if raw in (None, ""):
        mode = req.value("mode")
        if mode in (None, "") or str(mode).lower() in ("never", "no", "unlimited",
                                                       "disabled", "off"):
            days = "-1"
        else:
            raise Unsupported('the bridge needs the mode ("never" / number of '
                              'days): pass extra={"days": 90} or '
                              'extra={"mode": "never"}')
    else:
        text = str(raw).strip()
        if text.lstrip("-").isdigit():
            days = text
        else:
            raise Unsupported(f"days={raw!r} is not an integer or \"never\"")
    how = "lift the expiry" if days == "-1" else f"expire it after {days} day(s)"
    return [spawn(["passwd", "-x", days, name],
                  detail=f"make {name}'s password {how}")]


def _add_account_to_group(req: Request) -> list[Call]:
    # pkg/users/account-details.js addGroup(): gpasswd -a <user> <group>. The
    # account's groups are edited as chips; adding one is a click on a chip,
    # which the scan cannot recover as an action.
    user = req.target("account")
    group = str(req.need("group", "group_name", what="the group"))
    return [spawn(["gpasswd", "-a", user, group],
                  detail=f"add {user} to the group {group}")]


def _remove_account_from_group(req: Request) -> list[Call]:
    # pkg/users/account-details.js removeGroup(): gpasswd -d <user> <group>.
    user = req.target("account")
    group = str(req.need("group", "group_name", what="the group"))
    return [spawn(["gpasswd", "-d", user, group],
                  detail=f"remove {user} from the group {group}")]


# --------------------------------------------------- the host itself: reboot

# pkg/lib/cockpit-components-shutdown.jsx onSubmit():
#
#     const arg = this.props.shutdown ? "--poweroff" : "--reboot";
#     cockpit.spawn(["shutdown", arg, this.state.when, this.state.message],
#                   { superuser: "require", err: "message" })
#
# `when` is the dialog's Delay select, as shutdown(1) spells it: "+0" for "No
# delay" through "+60", or "+N" minutes worked out from a date and a time. It
# starts at "+1" — a Cockpit reboot is a reboot in one minute unless the user
# says otherwise, and that default is worth keeping, because it is the only
# thing standing between a mistaken call and a host that is already gone.
# The message is passed whether or not it is empty.

_NOW = ("0", "+0", "now", "immediately", "immediate", "none", "no delay", "no_delay")


def when(req: Request) -> tuple[str, bool]:
    """The Delay as shutdown(1) wants it, and whether it means "right now"."""
    raw = req.value("delay", "when", "minutes", "time")
    if raw in (None, ""):
        return "+1", False                       # the dialog's own default
    text = str(raw).strip()
    if text.lower() in _NOW:
        return "+0", True
    if text.lstrip("+").isdigit():
        return "+" + text.lstrip("+"), False
    if re.fullmatch(r"\d{1,2}:\d{2}", text):     # "23:30", which shutdown takes
        return text, False
    raise Unsupported(f"delay={raw!r} is not a number of minutes, \"now\" or HH:MM")


def _shutdown(reboot: bool) -> Callable[[Request], list[Call]]:
    def build(req: Request) -> list[Call]:
        at, now = when(req)
        message = str(req.value("message", "wall",
                                "message_to_logged_in_users") or "")
        what = "reboot" if reboot else "power off"
        return [spawn(["shutdown", "--reboot" if reboot else "--poweroff", at, message],
                      detail=(f"{what} the host now" if now else
                              f"schedule a {what} for {at}, which "
                              f"system_cancel_reboot calls off"),
                      takes_host_down=now)]
    return build


def _cancel_shutdown(_req: Request) -> list[Call]:
    # pkg/systemd/overview-cards/shutdownStatus.tsx cancelShutdownAction() —
    # logind, not "shutdown -c", because that is what the card's button calls.
    return [dbus("org.freedesktop.login1", "/org/freedesktop/login1",
                 "org.freedesktop.login1.Manager", "CancelScheduledShutdown", [],
                 detail="call off the scheduled reboot or poweroff")]


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


# ---------------------------------------------------------------- storaged

# The storage package talks to UDisks2, and every object it names the caller
# must address by that object's D-Bus path — the page's own "location"
# (block_location, a name like "vda") is not enough to reach it. So these
# builders want the path the way the dialogs have it (client.blocks[…].path),
# passed in at=/extra. What is not here on purpose is the rest of the page:
# formatting, mounting and creating a subvolume all write /etc files
# (fstab, crypttab) on top of their one D-Bus or spawn call, which a recipe
# cannot express until Call gains a "file" op (the doc's "cannot do yet");
# and the btrfs subvolume create/delete go through a python btrfs_tool
# wrapper, not a single call.

U_SERVICE = "org.freedesktop.UDisks2"
U_ROOT = "/org/freedesktop/UDisks2"


def uiface(name: str) -> str:
    return "org.freedesktop.UDisks2." + name


def _as_list(value: Any) -> list[str]:
    """A UDisks2 member list: the dialog passes as, but one path is enough."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _uobject_path(req: Request, what: str) -> str:
    """The UDisks2 object path of the object addressed by at=/extra.

    The page reaches the same object by name (`block_location`, "vda"): that
    is not a call target, so a bare name is refused rather than guessed at —
    the caller has to hand the path, as the dialog has it (client.blocks[…].path).
    """
    value = req.value("path", "device_path", "object_path")
    if value in (None, "") and req.at:
        value = str(req.at)
    value = str(value or "").strip()
    if not value.startswith("/org/freedesktop/UDisks2/"):
        why = (f"{value!r} is the page's name, which is not a UDisks2 path — "
               f"pass the path instead" if value and not value.startswith("/")
               else f"pass the /org/freedesktop/UDisks2/… object path")
        raise Unsupported(f"this action works on one {what}: {why} "
                          f"(at=, or extra={{\"path\": \"…\"}})")
    return value


def _dev_path(req: Request, what: str) -> str:
    """The /dev device node a cryptsetup call wants, by name or at= — a bare
    name is turned into /dev/<name>, which is exactly what cryptsetup takes."""
    value = str(req.value("device", "device_path", "dev") or req.at or "").strip()
    if not value:
        raise Unsupported(f"this action works on one {what}: pass its "
                          f"/dev device node (e.g. extra={{\"device\": \"/dev/sda\"}})")
    if not value.startswith("/dev/"):
        value = "/dev/" + value.lstrip("/")
    return value


def _unlock_encrypted(req: Request) -> list[Call]:
    # pkg/storaged/crypto/keyslots.jsx unlock_with_type(): the Encrypted
    # object's Unlock(<passphrase>), with "read-only" set when the crypttab
    # entry is readonly. The clevis/stored variants end at the same call
    # (they only differ in where the passphrase comes from).
    path = _uobject_path(req, "encrypted drive")
    passphrase = str(req.need("passphrase", "key", what="the passphrase"))
    options = {}
    if req.flag("read_only", "readonly"):
        options["read-only"] = {"t": "b", "v": True}
    return [dbus(U_SERVICE, path, uiface("Encrypted"), "Unlock",
                 [passphrase, options],
                 detail=f"unlock {path}")]


def _change_luks_passphrase(req: Request) -> list[Call]:
    # pkg/storaged/crypto/keyslots.jsx passphrase_change(): cryptsetup
    # luksChangeKey <dev> [--key-slot N], the old passphrase on stdin, then
    # the new one. The "key" the caller has is the slot being re-armed.
    dev = _dev_path(req, "encrypted drive")
    old = str(req.need("old_passphrase", what="the current passphrase"))
    new = str(req.need("new_passphrase", "new_password", what="the new passphrase"))
    argv = ["cryptsetup", "luksChangeKey", dev]
    if (slot := req.value("key_slot", "slot")) is not None:
        argv += ["--key-slot", str(slot)]
    return [spawn(argv, stdin=f"{old}\n{new}\n",
                  detail=f"change {dev}'s passphrase")]


def _add_luks_passphrase(req: Request) -> list[Call]:
    # pkg/storaged/crypto/keyslots.jsx passphrase_add(): cryptsetup
    # luksAddKey <dev>, the current passphrase on stdin, then the new one —
    # a key is added, the old one stays.
    dev = _dev_path(req, "encrypted drive")
    old = str(req.need("old_passphrase", what="a passphrase that opens the drive"))
    new = str(req.need("new_passphrase", "new_password", what="the passphrase to add"))
    return [spawn(["cryptsetup", "luksAddKey", dev],
                  stdin=f"{old}\n{new}",
                  detail=f"add a passphrase to {dev}, keeping the old one")]


def _create_volume_group(req: Request) -> list[Call]:
    # pkg/storaged/lvm2/create-dialog.jsx: the LVM2 manager's
    # VolumeGroupCreate(<name>, <block paths>), after prepare_available_spaces
    # has turned any free space into a real partition. Whole disks are passed
    # straight through; the member list is what the dialog's "Disks" field held.
    name = str(req.need("name", what="the volume group name"))
    members = _as_list(req.need("disks", "members", "pvs",
                                what="the UDisks2 path (or /dev path) of each member"))
    return [dbus(U_SERVICE, U_ROOT, uiface("Manager.LVM2"), "VolumeGroupCreate",
                 [name, members, {}],
                 detail=f"create the volume group {name} over {len(members)} member(s)")]


def _create_raid_device(req: Request) -> list[Call]:
    # pkg/storaged/mdraid/create-dialog.jsx: Manager.MDRaidCreate(<paths>,
    # <level>, <name>, <chunk bytes>). The dialog's chunk field is KiB; 0
    # means "no striping", and it lands in the call verbatim.
    members = _as_list(req.need("disks", "members", what="the UDisks2 path (or /dev path) of each member"))
    level = str(req.need("level", what="the RAID level, e.g. raid1"))
    name = str(req.need("name", what="the RAID device name"))
    chunk = req.value("chunk")
    chunk_bytes = int(chunk) * 1024 if chunk not in (None, "") else 0
    return [dbus(U_SERVICE, U_ROOT, uiface("Manager"), "MDRaidCreate",
                 [members, level, name, chunk_bytes, {}],
                 detail=f"create the RAID {level} device {name} over {len(members)} member(s)")]


def _create_logical_volume_snapshot(req: Request) -> list[Call]:
    # pkg/storaged/lvm2/volume-group.jsx create_snapshot(): the logical
    # volume's CreateSnapshot(<name>, <size>); 0 for "the size it was asked
    # for" is what the dialog passes when it has no size.
    path = _uobject_path(req, "logical volume")
    name = str(req.need("name", what="the snapshot name"))
    size = req.value("size")
    size_bytes = int(size) if size not in (None, "") else 0
    return [dbus(U_SERVICE, path, uiface("LogicalVolume"), "CreateSnapshot",
                 [name, size_bytes, {}],
                 detail=f"create the snapshot {name}")]


def _start_multipath(_req: Request) -> list[Call]:
    # pkg/storaged/multipath.jsx activate(): enable multipath and start its
    # service, which is what the alert's "Start multipath" link does.
    return [spawn(["mpathconf", "--enable", "--with_multipathd", "y"],
                   detail="enable multipath and start multipathd")]


_FILESYSTEM_TYPES = ("btrfs", "ext4", "xfs", "vfat", "ntfs", "swap",
                     "ext2", "ext3", "f2fs", "exfat", "minix",
                     "reiser4", "nilfs2")


def _create_filesystem_and_mount(req: Request) -> list[Call]:
    # The "Format and mount" action of pkg/storaged/block/format-dialog.jsx,
    # which the dialogs run as
    #     block.Format(type, options)        # UDisks2
    # then client.mount_at():
    #     cockpit.script('set -e; mkdir -p "$2"; mount "$1" "$2" -o "$3"', …)  # client.js:1097
    #
    # options carries 'tear-down' and the fstab entry under 'config-items'
    # (the single Format() call also writes /etc/fstab). That one-call shape
    # is what a recipe *should* be, but it needs the fstab write (the "file"
    # Call op the doc lists as missing), and the ay/config-items variant form
    # does not decode the way udisks2 2.10.1 expects (it reports "opts must
    # not be blank"). So instead of one D-Bus call that a destructive action
    # would then fail on, the recipe performs the same effect as a sequence
    # of the tools the dialog's own call ends up driving — same order, same
    # result, and it is the sequence that is known to work end to end:
    #
    #     wipefs -a <dev>                          # its 'tear-down'
    #     mkfs.<type> [-L <label>] <dev>           # its Format()
    #     <fstab install> ; mkdir -p <mp> ; mount  # Format()'s config-items + client.mount_at()
    ftype = str(req.need("type", "filesystem", "fs_type",
                          what="the filesystem type (btrfs, ext4, xfs, …)")).lower()
    if ftype not in _FILESYSTEM_TYPES:
        raise Unsupported(f"filesystem type {ftype!r} is not one the dialog offers "
                          f"({', '.join(_FILESYSTEM_TYPES)})")
    if ftype == "swap":
        raise Unsupported('the "swap" type has no mount point; the dialog '
                          'starts it with block_swap.Start() — not covered by this recipe')
    dev = _dev_path(req, "block device")
    mount_point = str(req.value("mount_point", "mountpoint", "dir") or "").strip()
    label = str(req.value("name", "label") or "").strip()

    calls = [spawn(["wipefs", "-a", dev],
                   detail="remove any existing signatures on the device (the dialog's tear-down)")]
    mkfs = ["mkfs." + ftype] + (["-L", label] if label else []) + [dev]
    calls.append(spawn(mkfs, detail=f"format {dev} as {ftype}"))

    if mount_point:
        if mount_point[0] != "/":
            mount_point = "/" + mount_point
        fstab_opts = "nofail" + (",subvol=/" if ftype == "btrfs" else "")
        # The dialog installs the fstab entry from its own config-items, then
        # mounts with the dialog's options (nofail by default, +subvol=/ btrfs).
        # A UUID-keyed entry, added only if absent, so a re-run does not stack
        # two lines for the same device.
        script = (f'set -e; uuid="$(blkid -o value -s UUID {dev})"; '
                  f'grep -qF "UUID=$uuid" /etc/fstab '
                  f'|| echo "UUID=$uuid {mount_point} auto {fstab_opts} 0 0" >> /etc/fstab; '
                  f'mkdir -p {mount_point}; mount {dev} {mount_point} -o {fstab_opts}')
        calls.append(spawn(["sh", "-c", script],
                           detail=f"write the fstab entry for {mount_point}, "
                                 f"create it, and mount {dev} there"))
    return calls


ADAPTERS: dict[str, Adapter] = {
    # users: the dialogs that perform the action …
    "users_accounts_create": Adapter(_create_account,
        "useradd --create-home …, then chpasswd (needs the password in `extra`)"),
    "users_account_confirm_delete": Adapter(_delete_account,
        "userdel [-r] <at>"),
    "users_account_confirm_lock": Adapter(_lock_account, "usermod <at> --lock"),
    "users_account_confirm_unlock": Adapter(_unlock_account, "usermod <at> --unlock"),
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
    "user_set_real_name": Adapter(_set_real_name,
        "usermod <at> --comment <name> (needs the full name in `extra`)"),
    "user_set_password_expiry": Adapter(_set_password_expiration,
        "passwd -x <days> <at> (needs days or mode in `extra`; \"never\" is -1)"),
    "user_add_to_group": Adapter(_add_account_to_group,
        "gpasswd -a <at> <group> (needs the group in `extra`)"),
    "user_remove_from_group": Adapter(_remove_account_from_group,
        "gpasswd -d <at> <group> (needs the group in `extra`)"),
    "users_delete_group": Adapter(_delete_group, "groupdel [-f] <at>"),
    "users_rename_group": Adapter(_rename_group,
        "groupmod <at> --new-name <name> (needs the new name in `extra`)"),

    # the host itself. The overview's split button and its menu only *open*
    # the dialog in cockpit-components-shutdown.jsx — the scan cannot see that
    # and lists them as actions, so both names perform the thing they promise.
    "system_reboot": Adapter(_shutdown(True),
        "shutdown --reboot <delay> <message>; delay defaults to the dialog's "
        "+1 minute, extra={\"delay\": \"now\"} goes down immediately"),
    "system_shutdown": Adapter(_shutdown(False),
        "shutdown --poweroff <delay> <message> (delay defaults to +1 minute)"),
    "system_shut_down": Adapter(_shutdown(False),
        "shutdown --poweroff <delay> <message> (delay defaults to +1 minute)"),
    # The status card's link, which the scan mislabels and drops — so the
    # catalog entry for it is hand-written too (cockpit_mcp_server.MISSED).
    # It is how a reboot scheduled a minute out is called off in time.
    "system_cancel_reboot": Adapter(_cancel_shutdown,
                                    "logind CancelScheduledShutdown()"),

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

    # storaged. These address their object by D-Bus path (at= or extra=), the
    # way client.blocks[…].path does — the page's "vda" name is not enough.
    # What is not here is what a recipe cannot say: formatting and mounting
    # write fstab/crypttab on top of one call (needs a "file" Call op), and
    # the btrfs subvolume ops wrap their create/delete in a python tool.
    "storage_unlock": Adapter(_unlock_encrypted,
        "UDisks2 Encrypted.Unlock(<passphrase>) on <at> (needs the UDisks2 path "
        "of an encrypted drive)"),
    "storage_change_passphrase": Adapter(_change_luks_passphrase,
        "cryptsetup luksChangeKey <dev> (needs the drive, old and new passphrase in `extra`)"),
    "storage_add_passphrase": Adapter(_add_luks_passphrase,
        "cryptsetup luksAddKey <dev> (needs the drive, an existing and a new passphrase in `extra`)"),
    "storage_create_volume_group": Adapter(_create_volume_group,
        "UDisks2 LVM2 VolumeGroupCreate(<name>, <members>) (needs name and member paths)"),
    "storage_create_raid_device": Adapter(_create_raid_device,
        "UDisks2 Manager MDRaidCreate(<members>, <level>, <name>, <chunk>)"),
    "storage_create_snapshot": Adapter(_create_logical_volume_snapshot,
        "UDisks2 LogicalVolume.CreateSnapshot(<name>, <size>) on <at>"),
    "storage_start_multipath": Adapter(_start_multipath,
        "mpathconf --enable --with_multipathd y"),

    # Format <device> as <type>, and optionally install the fstab entry and
    # mount it — the effect of pkg/storaged/block/format-dialog.jsx
    # "Format and mount", performed as the tools that call ends up driving
    # (wipefs, mkfs.<type>, then fstab install + mkdir + mount). `type` and
    # `mount_point` go in `extra` (the schema the scan recovers does not
    # name them); `at=` is the /dev device (or extra={"device": "/dev/…"}).
    "storage_create_filesystem": Adapter(_create_filesystem_and_mount,
        "wipefs → mkfs.<type> <dev>, then (when mount_point is given) the "
        "fstab entry + mkdir -p + mount "
        "(needs the type in `extra` and, to mount, the mount_point)"),
    "storage_format_and_mount": Adapter(_create_filesystem_and_mount,
        "wipefs → mkfs.<type> <dev>, then (when mount_point is given) the "
        "fstab entry + mkdir -p + mount "
        "(needs the type in `extra` and, to mount, the mount_point)"),
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


def _scheduled(session: PrivilegedSession) -> str:
    """What logind has scheduled — what the overview's status card reads."""
    reply, _close = session.call(
        "org.freedesktop.login1", "/org/freedesktop/login1",
        "org.freedesktop.DBus.Properties", "Get",
        ["org.freedesktop.login1.Manager", "ScheduledShutdown"], superuser=False)
    # Get() answers with one out argument, and that argument is a variant
    kind, usec = ((reply or [{}])[0] or {}).get("v") or ("", 0)
    if not kind:
        return "nothing scheduled"
    return f"{kind} at {time.strftime('%H:%M:%S', time.localtime(usec / 1e6))}"


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
    parser.add_argument("--probe-reboot", action="store_true",
                        help="schedule a reboot far enough out to call it off "
                             "again, which is the end-to-end check of the reboot "
                             "path without rebooting anything")
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

    reboot_ok = True
    if args.probe_reboot:
        # 99 minutes out, so that a cancel that does not work is still an hour
        # and a half of warning rather than a surprise.
        print(f"\nschedule a reboot, then call it off ({_scheduled(session)}):")
        reboot_ok = _report("system_reboot", BRIDGE.perform(
            plan("system_reboot", extra={"delay": 99, "message": "cockpit_bridge probe"})))
        print(f"  logind: {_scheduled(session)}")
        if reboot_ok:
            reboot_ok = _report("system_cancel_reboot",
                                BRIDGE.perform(plan("system_cancel_reboot")))
            print(f"  logind: {_scheduled(session)}")
        if not reboot_ok:
            print("\n  a scheduled reboot may still be standing — call it off "
                  "with 'shutdown -c' on the host.")

    if not args.probe:
        if not args.probe_reboot:
            print(f"\n{len(ADAPTERS)} actions can be performed this way "
                  f"(--list to see them).")
        BRIDGE.close()
        return 0 if reboot_ok else 1

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
    return 0 if (ok and reboot_ok) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        BRIDGE.close()
        raise SystemExit(130) from None
    except (BridgeError, Unsupported) as exc:
        print(f"cockpit_bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
