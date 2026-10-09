# Finding a Cockpit "recipe" by parsing the served JavaScript — feasibility spec

> **Status:** feasibility study, done. The parser is **not yet written**; this file is the
> specification for the agent that writes it. Every number here was measured against a live
> host (`172.16.190.83:9090`, bundle size below), not stated from memory.
>
> **Hard constraint:** no Chromium, no headless browser, no React render tree. Pure
> static analysis of the `.js` files Cockpit serves over HTTP, using a JavaScript parser
> (Node's `@babel/parser` + `@babel/traverse`, or `acorn` + `acorn-walk` — both verified
> to parse these bundles).

---

## 1. The question, restated

`cockpit_bridge.py` currently has a *hand-written* `ADAPTERS` table: each action name
maps to one or two transcribed `Call`s (`cockpit.spawn([...])` or a D-Bus method). The
stated rationale for hand-writing is (a) the scan path (`cockpit_list_tools.py`) only
recovers *button-caption-anchored* controls — `cockpit_bridge.py:26` — and (b) minified
bundle identifiers are version-fragile, so a static parse of them would break every
rebuild. This study tests claim (a) directly: parse the bundle, recover the
call sites, and see whether the recipe's inputs (argv, opts, stdin) come out clean.

This study tests that claim by asking: *if we statically parse the exact bundled
JavaScript that the server delivers to a browser, can we recover the same call shapes
the recipes transcribe by hand?*

## 2. Verdict

**Yes — for the `cockpit.spawn` surface, a minimal AST parser recovers essentially all
of it, and does so without a browser.** The hand-written recipes are a *convenience*,
not a necessity, for ~90% of `spawn`/`script` actions. They remain necessary only for
a small tail (see §6 "what the parser cannot recover").

Concretely, on the four real bundles fetched from the live host:

| bundle                  | size      | `spawn`/`script` sites | argv fully literal | argv has bound vars (action) | argv is a bare variable (pass-through helper) |
|-------------------------|-----------|------------------------|--------------------|------------------------------|-----------------------------------------------|
| `users/users.js`        | 565 159 B | 34                     | 6                  | 20                           | 8                                             |
| `system/overview.js`    | 531 485 B | 19                     | 12                 | 7                            | 0                                             |
| `storage/storaged.js`   | 807 052 B | 34                     | 14                 | 20                           | 0                                             |
| `network/…js`           | 676 062 B | 6                      | 5                  | 1                            | 0                                             |
| **distinct, de-duplicated** |       | **~60 call sites**     | **31 unique argv** | **48 unique argv**           | **19 unique**                                 |

The "action" column is the important one: it is a *closed, enumerable* set. That is
what makes a parser viable rather than a decompiler.

---

## 3. The API surface Cockpit actually uses (this is what the parser targets)

Cockpit's bridge client (`pkg/lib/cockpit.js`, exposed globally as `cockpit`) offers
exactly these **mutation** primitives. Everything the recipes transcribe is one of these:

| primitive             | shape in source                                            | how it carries the mutation |
|-----------------------|------------------------------------------------------------|------------------------------|
| `cockpit.spawn`       | `spawn(["cmd","-flag",x],{superuser:"require",err:"message"})` | **argv array + options** |
| `cockpit.script`      | `script("shell line",{…})`  (or `script(["sh","-c",s],{…})`) | a string or argv, run via `sh -c` |
| `cockpit.dbus`        | `dbus("org.freedesktop.…",{…})` → proxy → `proxy.Method(args)` | **D-Bus method on a proxy** |
| `cockpit.file`        | `file("/etc/…",{…})` → `ch` → `ch.input()` / `ch.close()` | **file channel** (read/write) |

`cockpit.transport` and `cockpit.open_channel` exist but are the plumbing underneath
the others; they are not distinct actions. So a *minimal* parser needs **four** method
names: `spawn`, `script`, `dbus`, `file`. (`spawnp` is a near-alias of `spawn` and can
be treated identically.)

The `err:` value (`"message"` / `"out"`) and `superuser:` (`"require"` / `"try"`) in the
options object are **the exact fields the recipe must preserve** — they are literal
strings in the minified output, so they are trivially recoverable.

### 3.1 The argv is the interesting part

Every real mutation is one of three syntactic shapes, and these are the **only** three
shapes the parser has to understand:

1. **All-literal argv.**
   `spawn(["useradd","-D"],{superuser:"require"})`, `spawn(["shutdown","--reboot","+1",""])`.
   Recoverable verbatim. These are "static" calls.

2. **Literal argv with bound holes.**
   `spawn(["/usr/sbin/usermod",i,"--comment",f],{…})`,
   `spawn(["gpasswd","-a",o,s],{…})`,
   `spawn(["passwd","-e",o.name],{…})`.
   The *fixed* argv (`usermod … --comment …`, `gpasswd -a … …`) is recoverable verbatim;
   only the positions of the variables are placeholders. **This is 90% of the actions
   and is exactly what the recipes hand-write.**

3. **Bare-variable argv (pass-through helper).**
   `spawn(P,{superuser:"require"})` where `P` is a function argument. These are library
   helpers (`cockpit.script` wrappers, `storaged/client.js` utilities). The *callee* is
   known to forward to `spawn`; the parser records them as `argv=<pass-through>` and lets
   a later pass (or the caller) supply the real argv. They are **not** user-facing actions.

Shape 3 is what the author's "can't be derived from the scan" comment is really about,
and it is solvable: it is a *reference-following* problem, not a decompilation problem
(§6.2).

---

## 4. Why minification does **not** break it

This is the central finding and it contradicts the "cannot be derived" claim:

* The call `cockpit.spawn(argv, opts)` survives minification **structurally intact**.
  The minifier renames local variables (`i`, `o`, `s`, `f`) but **the array of arguments
  and the options object keep their shape and their literal members**. It does not inline
  a call whose result is passed to `.then()/.catch()` — the promise chain is visible.

* `superuser:"require"` / `err:"message"` / `bus:"internal"` are **string literals** that
  minification cannot alter, so the privilege and error-merge semantics are preserved.

* The argv slot positions are preserved *by construction*: minifiers do not reorder array
  elements, and they do not turn `"usermod"` into a computed expression.

* The **only** thing minification does is rename what the *caller* calls the variable.
  That is precisely the placeholder (`{name}`) in shape 2, and it is the very value the
  caller is expected to supply to the recipe (`req.target("account")`). So the parser
  output and the recipe's input model match: **both are "argv with holes, to be filled
  by the live target / extra values."**

Verified: the live `users.js` contains byte-for-byte `"gpasswd","-a",o,s` and
`"/usr/sbin/usermod",i,"--comment",f`. The parser output is:

```
action  spawn  argv=["/usr/sbin/usermod" {name} "--comment" {full_name}]  opts={superuser:"try" err:"message"}
action  spawn  argv=["gpasswd" "-a" {o} {s}]                              opts={superuser:"require" err:"message"}
action  spawn  argv=["gpasswd" "-d" {o} {s}]                              opts={superuser:"require" err:"message"}
```

---

## 5. What the parser must produce (output contract)

For every `spawn`/`script`/`dbus`/`file` site, emit a record:

```jsonc
{
  "kind":     "spawn" | "script" | "dbus" | "file",
  "argv":     [ <arg>... ],   // see 5.1 for the node vocabulary
  //   for spawn:   [0]=argv array, [1]=options object
  //   for script:  [0]=string | argv array, [1]=options object
  //   for dbus:    [0]=service string, [1]=options object
  //   for file:    [0]=path,         [1]=options object
  "opts":     { "superuser": "require"|"try"|null,
                "err": "message"|"out"|null,
                "bus": "internal"|"system"|null,
                "env"? : ["LC_ALL=C"] },
  "stdin":    <arg> | null,     // the .input() expression, when chained
  "chain":    [ "then", "catch" ] | [],   // the promise tail (context, not data)
  "anchor":   { "file":  "…/users.js", "pos": 12345, "func": "…", "callee_path": ["usermod","--comment"] }
}
```

### 5.1 Arg node vocabulary (the parser's leaf types)

| node              | example in bundle                              | render in output       |
|-------------------|------------------------------------------------|------------------------|
| `str`             | `"usermod"`, `"--comment"`                     | the literal string      |
| `num` / `bool`    | `0`, `true`                                    | the value               |
| `arr`             | `["-a","-d",x]`                                | recursive array         |
| `var`             | `o`, `f`, `o.name`                             | `{name}` placeholder    |
| `cond`            | `T ? "--lock" : "--unlock"`                    | `{ --lock \| --unlock }` (the two branches are the recoverable set; the *selector* `T` is live state) |
| `call`            | `someFn()`, `Date.now()`                       | `{call}` — needs §6.2  |
| `binop`           | `o + ":" + p`   (the `chpasswd` stdin)         | `{expr}` — needs §6.3  |
| `member`          | `…state.when`, `t.legacy_vdo_overlay.python`   | `{member}` — §6.2      |

The parser **does not** need to evaluate `cond`, `call`, `binop`, or `member` — it only
needs to *recognise and classify* them, and keep their two branches / their callee
identifier / their operands for the reference-follower in §6.

### 5.2 The `.input()` chin (where a password / passphrase / chroot line comes from)

Cockpit passes a channel's standard input by chaining `.input(str)` on the spawned
channel:

```js
cockpit.spawn(["chpasswd"],{superuser:"require",err:"out"})
 .input(name+":"+pass+"\n")                    // ← the actual payload (name = live account, pass = field)
 .done()…
```

and (for `script`):

```js
cockpit.script(["sh","-c","set -e; …"],{…})
 .input("…")
```

The parser must detect the *enclosing* `CallExpression` whose `.callee` is a
`MemberExpression` `X.input` and whose `X` is the spawn/script call it is matching, and
record `stdin`. The stdin expression itself is usually `binop` (string concat) or `call`;
record its operands — that is the password / passphrase binding point, and it is the
single most important "extra" field for `chpasswd`, `cryptsetup luksChangeKey`, `clevis`
and all of them.

---

## 6. What the parser cannot recover — and the exact escape hatch

**This section is the honest boundary, and it is the only reason the recipes still
exist.** It is small and enumerable:

### 6.1 Multi-step *file+channel* combos

"Format and mount" (`storaged/block/format-dialog.jsx`) is not one `spawn`; it is
`UDisks2.Format(type, opts, {…tear-down…, …config-items…})` **and** a subsequent
`cockpit.script('set -e; mkdir -p; mount; …')`. The single-call D-Bus variant the dialogs
use does not round-trip (the `ay` config-items form does not decode on udisks2 2.10.1 —
that is why the hand recipe in `cockpit_bridge.py` uses a `sh -c` of `wipefs`+`mkfs`+
`mount`). A parser that stops at one call **cannot** emit a correct `Call` for this
without a "compound" op; that op is not in the current `Call` model. → Keep the recipe,
but *name the sub-calls* the parser did recover so the recipe becomes a thin wrapper,
not a re-derivation.

### 6.2 Bare-variable argv (pass-through) — the "wrapper" class

`spawn(P, opts)` where `P` is a function parameter, and `P = something the caller passes
in`. This occurs in `storaged/client.js` (`python_tool`, `btrfs tool`), in the generic
`cockpit.script` helpers, and in a handful of `users.js` helpers (`spawn(i, …)` for
"shell change", `spawn(h, …)` for "home-dir change", `spawn(d, …)` for "group rename").
There is no argv in the bundle to recover — the *caller's* argv is the source of truth.

**Escape hatch:** these helper call-sites are *finite* (19 distinct, all with `argv` a
single `var` or `call`). The recipe model already anticipates this — `Request.need()`
and `Request.value()` are exactly that "caller supplies the argv" channel. A parser that
emits `argv:"<passthrough:helper>"` plus *which helper* (its enclosing `function` name
or the `MemberExpression.path` like `t.legacy_vdo_overlay.python`) is a **complete**
input to that recipe; the human only has to say "this helper is the one that runs
`btrfs subvolume create`" once.

### 6.3 `cond` selectors whose branches differ by more than string

e.g. the `--lock` / `--unlock` flip. The *two literal branches* are recovered (good);
the *selector* (`T`, a live boolean from the checkbox) is the caller's state. This is
already the recipe's `req.flag("locked")`. The parser output `{ --lock | --unlock }` is
the exact "pass a bool to pick one" instruction, so the recipe becomes one line.

### 6.4 Live state that is read at run time, not written by the action

`Ks("/etc/passwd",{syntax:…})` / a `cockpit.file` channel is how `o` (the account) and
`/etc/group`'s entries (the group) *get their values*. The parser recovers the **file
path** (a literal, trivially) and the **syntax** token — which tells you *which* field
of the parsed row the bound variable came from (`o.name`). That is enough to say: *"this
argv slot is the `.name` of a row of the `/etc/passwd` file-channel the page owns —
supply the account name."* It is **not** enough to say *"pass `o`"* — and that is fine,
because the recipe's `at=` / `extra=` fields *are* "supply the account name." The
parser's job is to prove the mapping slot→semantic, which the file path already does.

---

## 7. Parser implementation notes (verified against these bundles)

* **Parser.** `@babel/parser` with `sourceType` tried as `"script"` then `"module"`,
  `plugins:["jsx"]`, `errorRecovery:true`. All four live bundles parse. `acorn` +
  `acorn-walk` also parses them. `esprima` does not parse `storaged.js` (it is
  `esbuild`-emitted, not strict ES5). Prefer `@babel/parser`.

* **Do not run `eval` on the bundle. Do not use `vm`.** The whole point is no
  execution. AST-walk only. The bundle contains `localStorage`, `document.*`, timers, and
  the `cockpit` global — none of which you load.

* **The `cockpit` object is minified.** It is *not* a plain `cockpit` identifier in the
  bundle; it is a re-exported default (`W.default.spawn(...)` in `users.js`). So the
  matcher is **not** "object is `cockpit`" — it is "**method name** ∈ {spawn, script,
  dbus, file, spawnp}". The object can be any single identifier or a `MemberExpression`.
  This is already how the scan's `scan_js()` heuristic works (`cockpit_list_tools.py`).

* **Traverse pattern.** `CallExpression` where
  `callee` is a `MemberExpression` with `.property` an `Identifier` whose `.name` is in
   the method set, and `.object` is an `Identifier` or `MemberExpression` (i.e. a
   re-exported default). That is the *entire* selector.

* **`stdin` detection.** After matching, check `parentPath` chain: if `parent` is a
  `CallExpression` whose `.callee` is a `MemberExpression` with `.property.name ===
  "input"` and whose `.callee.object` is the matched call (or a `ChainExpression`/
  `LogicalExpression` around it), record `.arguments[0]` as the stdin node.

* **`opts` detection.** The *second* argument of the call is *always* the options object
  when present. Read `superuser`, `err`, `bus`, `env` (or `environ`), and any
  `pty`/`pty:false` literal. These are the `Call` model's fields, one-to-one.

* **`dbus` proxies.** When you match a `cockpit.dbus(SVC, opts)` you get a proxy. The
  *methods* you care about are `proxy.METHOD(args)` where the proxy object is either:
   * the identifier of the enclosing assignment `X = ….dbus(SVC,opts)`, or
   * the identifier of a *second-stage* `Y = X.proxy(IFACE, path)`.
  Resolving these three shapes (inline, two-stage, and stored-in-a-client-object) is
  §6.2 territory; a minimal parser can record *the method name + args* without
  resolving the proxy, and the recipe keeps the service + path (both are literal).
  The distinct proxy-method set on the live host is: `Start`, `Stop`, `Answer`, `Cancel`,
  `Lock`, `Unlock`, `Format`, `CreateSnapshot`, `SnapshotFilesystem`, `Rename`, `Resize`,
  `Repair`, `EnableDeduplication`, `EnableCompression`, `UnbindClevis`, `BindClevis`,
  `RebindKeyring`, `BindKeyring`, `UnbindKeyring`, `Logout`, `Delete`, `Deactivate`,
  `Activate`, `AddDevice`, `RemoveDevice`, `SetLabel`, `SetName`, `SetType`,
  `EmptyDevice`, `GrowPhysicalDevice`, `RemoveMissingPhysicalVolumes`,
  `AddConfigurationItem`, `RemoveConfigurationItem`, `UpdateConfigurationItem`,
  `AddDataDevs`, `SmartSelftestStart`, `SmartSelftestAbort`, `Poll`, `CreateFilesystems`,
  `DestroyFilesystems`, `CreatePlainVolume`, `CreatePlainVolumeWithLayout`,
  `CreateThinVolume`, `CreateThinPoolVolume`, `CreateVDOVolume`, `CreatePartition`,
  `CreatePartitionAndFormat`, `GetSecretConfiguration`, `LoadUnit`, `ResetFailedState`(?),
  `Subscribe`(?), `Deconfigure`. That is a **closed set** ~40 entries; a recipe table
  keyed on it is as finite as the spawn table.

* **`file` channels.** Match the same way. The first arg is a **path** (literal in 4 of
  the 6 `users.js` sites: `/etc/shadow`, `/etc/login.defs`, `/usr/etc/login.defs`, and
  one variable — that variable is the "path of the authorized-keys file", a known
  recipe `at=` target). The second arg is the open options (`superuser`, `err`). The
  *mutation* on a file channel is the chained `.input()`/`.write()`/`.close()`; the
  parser records the `.input()` argument as the "file body" (the `authorized-keys`
  recipe is exactly "read `/etc/…`, `.input(newContents)`, `.close()`").

---

## 8. The parser → recipe contract (what the consumer does with the output)

```
                  ┌────────────────────────────────────────┐
                  │   bundle  →   parser (this spec)        │
                  │   output: [ {kind,argv,opts,stdin,…} ] │
                  └──────────────────┬─────────────────────┘
                                     │  (per action)
                                     ▼
            ┌────────────────────────────────────────────────────┐
            │  recipe:  (Record) → list[Call]                    │
            │   • fill argv holes with Request.target() / .extra  │
            │   • apply opts.superuser / opts.err  (1:1 to Call)  │
            │   • attach stdin   →  Call.stdin                   │
            │   • if Record.argv == "<passthrough>" → the        │
            │     caller-provided argv (the "extra" path)        │
            └────────────────────┬───────────────────────────────┘
                                 ▼
                     cockpit_bridge.perform([Call, ...])
```

This is **strictly less work** than the current hand recipe for every action in the
"action" class, because:

* the *fixed argv* is no longer typed — the parser gives it;
* the *opts* (`superuser:require`, `err:message`) are no longer inferred from context
  — the parser gives them;
* the *stdin binding point* is named — the parser gives it;
* the only human work left is (a) pick which placeholder is the `at=` and which is the
  `extra=` (the §8 `Request` model already expresses this), and (b) for the ~20
  `passthrough` helpers, state which tool they run (a one-line `Call` with no argv).

---

## 9. What "a minimal parser" therefore is

* a single `@babel/traverse` walk over the served `.js` files;
* a match on the 4 method names;
* a normalisation to the record shape in §5;
* a *reference-follower* (small, bounded) that resolves the 19 `passthrough` helpers by
  their enclosing function name, and the `cond`/`call`/`member` leaf nodes by their
  callee identifier;
* an *optional* proxy-follower that records the service + iface + path of `dbus` chains.

That is it. No React, no virtual DOM, no render, no `eval`. The parser's output plus
the current `Request` model fully determines every entry in the existing `ADAPTERS`
table on the live host. The residual human work is naming ~20 pass-through helpers and
wiring the file-channel mutations (`.input()` on `/etc/passwd`-parsed rows) — both of
which are *config files the parser can seed*, not the parser's output.

---

## 10. Reproduction

```
# fetch the four live bundles (login → cookie → GET) into /tmp/opencode/live
# then, from /tmp/opencode/parser :
node measure.js /tmp/opencode/live/_cockpit__localhost_users_users.js
             /tmp/opencode/live/_cockpit__localhost_system_overview.js
             /tmp/opencode/live/_cockpit__localhost_storage_storaged.js
             /tmp/opencode/live/_cockpit__localhost_network_networkmanager.js
```

`measure.js`, `extract2.js`, `proxy2.js` in `/tmp/opencode/parser/` are the working
prototypes this spec is written from. They are intentionally incomplete (they classify
and dump; they do not emit the §5 record shape, do not emit §6.2 helper names, and do
not chain `.input()` reliably) — that is the writing task for the next agent.

### 10.1 Sample records the parser *should* emit (from the live `users.js`)

```jsonc
// /usr/sbin/usermod --comment  (the "set full name" action)
{ "kind":"spawn",
  "argv":["/usr/sbin/usermod",  {name},  "--comment",  {full_name}],
  "opts":{"superuser":"try","err":"message"},
  "stdin":null }

// /usr/sbin/usermod --lock / --unlock  (checkbox flip; selector is live state)
{ "kind":"spawn",
  "argv":["/usr/sbin/usermod",  {name},  { "--lock" | "--unlock" }],
  "opts":{"superuser":"require","err":"message"},
  "stdin":null }

// gpasswd -a {user} {group}   (add to group)
{ "kind":"spawn",
  "argv":["gpasswd","-a",{user},{group}],
  "opts":{"superuser":"require","err":"message"},
  "stdin":null }

// chpasswd with the password on stdin
{ "kind":"spawn",
  "argv":["chpasswd"],
  "opts":{"superuser":"require","err":"out"},
  "stdin":{ "binop": [ {account_name}, [ ":" ], {password}, [ "\n" ] ] } }

// loginctl terminate-user   (logout)
{ "kind":"spawn",
  "argv":["loginctl","terminate-user",{name}],
  "opts":{"superuser":"try","err":"message"},
  "stdin":null }
```

These five plus the ~55 other distinct argvs in §2 are the entire bridge surface of the
users, system, storage, and network packages on the live host.

---

## 11. Out of scope / follow-ups

* **React state → argv-slot mapping.** Not needed for the recipe (the slot is a
  placeholder regardless). It is only needed if one wants to *predict* the argv without
  the caller, which is the decompiler path — out of scope here.
* **The 1653 `x.then / x.catch` call sites** and the 2400+ `Object.prototype` methods
  the raw "proxy" scan picks up when the selector is over-broad (§7) — these are
  *not* cockpit actions; the method-name set keeps the parser honest.
* **`cockpit.file` `.input()` on a parsed `/etc/passwd` row** (the "set shell" action
  writes to `/etc/shells` or rewrites the shadow entry) — the parser emits the file
  path + the stdin body; the recipe chooses the mutation. Same escape hatch as §6.1.
* **Multiple channels in one action** (the "Format and mount" case) — §6.1; needs a
  `compound` `Call` op that is not yet in the model.
