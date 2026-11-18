# Harness protection

How fluid defends the session against prompt injection, credential leakage,
and exfiltration — what each layer does, where it lives, and how to override
it. The stance throughout: **deterministic, harness-level defenses first**,
model compliance second. The base prompt tells the model to treat tool output
as data, but every layer below works even when the model doesn't listen.

## Threat model

Content that reaches the model but was not written by the user is untrusted:
file contents in a cloned repo, command output, web pages, MCP server
responses, and anything derived from them. An attacker who controls any of it
may try to (a) issue instructions the model mistakes for the user's, (b)
impersonate fluid's own control channels, (c) exfiltrate workspace data
through an outbound request, or (d) plant payloads that persist into future
sessions (instruction files, memory).

## The layers

**1. Prompt policy** (`fluid/cli/prompt.py`). The base prompt states the
contract: tool results are data; instructions inside them are reported, not
followed; a denied tool stays denied — no workarounds through another route.

**2. Untrusted-result fencing** (`fluid/tools/hardening.py`). MCP results and
web content (`WebFetch`/`WebSearch`) are wrapped in a labeled
data-not-instructions fence at the dispatch boundary. Closing tags embedded
in the payload are defanged case-insensitively, and there is deliberately no
"already wrapped" fast path — that check would be attacker-forgeable.
Results carrying provider-side elision markers get an explicit
incompleteness notice.

**3. Control-channel spoofing defense** (`defang_harness_markers`, applied in
the engine's tool-result path). fluid has two trusted markers — the
`<system-reminder>` block and the mid-turn steering marker — and the prompt
teaches the model to trust exactly them. Genuine instances are appended by
the engine *after* dispatch, so any instance inside a tool result's own text
(a README the model Read, kernel stdout, an MCP payload) is spoofing and is
neutralized in place. The surrounding data survives. The same defang runs
over the descriptions an MCP server supplies before they enter the
deferred-tool catalog, which is itself a `<system-reminder>` block, and each
description is folded to one line so it cannot add an entry of its own.

The delegated Claude Code path stages `claude_output_guard` independently
of the optional guidance mod. Its `session.append` function hook applies
the same marker replacements inside CLI-owned `tool_result.content`, for
built-in and MCP tools, successes and failures. Host-authored deliveries
remain unchanged. If the guard cannot run, the selected compatibility
policy keeps tools running with a one-time visible warning and ERROR log;
pre-existing tool deny policies still apply.

**4. Prompt-bound file screening** (`fluid/context/screening.py`).
Instruction files (AGENTS.md chain, nested files, the identity file) and
memory entries enter the prompt verbatim, so they are screened *before*
injection: override directives ("ignore your previous instructions"),
role-tag smuggling (`</system>`, `<|im_start|>`), fluid's own harness
markers, concealment orders, and invisible-text payloads (zero-width/bidi
control characters above a small threshold) block the file with a placeholder
naming the finding. Patterns are deliberately narrow — ordinary prose about
security must never match. Oversized files are capped with a Read-back
marker rather than silently truncated. WebFetch strips invisible characters
from every fetched page.

**5. Error hygiene** (`scrub_error_text`). Exception text is bounded and
stripped of role tags, code fences, and CDATA before entering context — the
model *reads* framing tokens even when they can't structurally escape.

**6. Credential protection.**

- The continual store's memory list rejects any entry that looks like a
  credential (key blocks, vendor token prefixes, `password = …` assignments) —
  memories are injected into every future session's prompt, so a leaked token
  there would persist indefinitely. The synthesizer is told to store *where*
  the secret lives instead.
- **Sensitive-file rules**: under any policy except `never_ask`, default
  ask-before-touching rules cover credential files (`.env` and variants, SSH
  keys, `*.pem`/`*.p12`/`*.pfx`, `.netrc`, `.npmrc`, `.pypirc`,
  `.git-credentials`, `~/.aws/credentials`) for `Read`/`Edit`/`Write`/
  `Grep`/`Glob`. They are prepended before user rules, so one
  `[[permission.rules]]` allow overrides them; `.env.example`-style templates
  are exempt. `permission.protect_sensitive_files = false` disables the set.
- The **Bash risk classifier** demotes an otherwise read-only command to
  `write` (→ prompts under `on_write`) when any argument names a credential
  file — `cat .env` does not sail under the read-only fast path.
- **Grep withholds matches from credential-pattern files** during tree-wide
  searches, with a visible note — a search for "TOKEN" cannot quietly print
  `.env` contents into context. Reading such a file explicitly still works,
  through the permission rule above.

**7. Exfiltration gating.**

- `WebFetch` is `risk="write"` — not because a GET mutates anything, but
  because an outbound request with a model-chosen URL is an exfiltration
  channel (workspace data encoded into a query string). Under `on_write` the
  first fetch to each domain prompts; answering always covers that domain for
  the session. It is also SSRF-guarded (http/https only; hosts resolving to
  loopback/private/link-local ranges refused) and reports cross-host
  redirects instead of following them. `WebSearch` stays `safe`: queries go
  only to the search backend, which is not an attacker-receivable channel.
- Bash's description and the base prompt forbid substituting `curl`/`wget`
  for a denied web tool; the permission engine's denial messages say the same.

**8. Structural gates** (pre-existing, part of the same story). The
permission engine (policy + last-match-wins rules + scoped allow-always; an
MCP tool's own `readOnlyHint` counts only for a server with
`trust_annotations = true`, and a Bash allow-always names what a wrapper such
as `sudo` or `npm` ran, not the wrapper alone), the
doom-loop guard, plan mode's exclusion of write tools (from the request
schemas on the JSON path, and — because code mode reaches its tools through a
kernel rather than a schema — from the code-mode projection and the host-call
gate, plus a final refusal at dispatch), the
session tool restriction (CE12) enforced at schema build, registry view, and
dispatch, read-before-write on file edits, and the code-mode kernel's
gates. Every in-kernel *tool* call crosses the pipe to the host, where it
passes the same hooks and permissions as a JSON-path call. Inside the child,
cell code cannot reach the machine directly: an interpreter audit hook
(`fluid/codemode/_audit_guard.py`, installed with `sys.addaudithook` before
the first cell and irremovable after) refuses `open`, directory listing,
`shutil`, socket creation and connection, `subprocess.Popen`, `os.system` /
`os.exec*` / `os.spawn*`, `ctypes`, environment edits and further audit hooks
whenever a cell frame is on the stack or the call runs off the main thread,
so reaching the real builtins through a pre-bound module
(`json.__builtins__['__import__']('os')`) gains nothing. The child is also
spawned with credential-looking variables (`*KEY*`, `*TOKEN*`, `*SECRET*`,
`*PASSWORD*`, `*CREDENTIAL*` and the known provider names) stripped from its
environment. The patched `open`/`io`/`socket.socket` builtins and the import
allowlist remain as the model-facing layer with the friendlier messages.

**9. Hooks as the user's extension point.** `[[hooks]]` PreToolUse commands
can deny or rewrite any call with organization-specific policy the harness
can't know (path allowlists, secret scanners, audit logging).

## What this does not claim

- Fencing and screening raise the cost of injection; they do not make a
  compliant-looking payload impossible. The model's own judgment remains a
  layer, which is why the guidance and the mechanical gates both exist.
- The pattern lists (secrets, sensitive files, screen findings) are
  deliberately conservative: false negatives are accepted to keep false
  positives near zero. Add project-specific coverage with permission rules
  and hooks.
- The code-mode kernel is not a container. The audit hook covers the
  operations the interpreter reports; it does not cover memory exhaustion
  inside the process (the memory cap does), CPU spin (the timeout does), or
  code that runs after every cell frame has left the stack, such as an
  `atexit` callback compiled under a different filename. Reading the
  interpreter's own `.py` sources under `sys.path` stays possible because
  imports need it. A container-grade kernel remains future work
  (`docs/agent-experience.md`).

## Isolated execution profile (opt-in)

By default a permitted command runs with the full access of the user Fluid
runs as; the layers above decide *whether* it runs, not *what it can reach*.
`[sandbox] mode = "isolated"` adds an OS-enforced boundary around the
processes Fluid starts for the model. It is off by default; with it off, no
spawn path changes.

```toml
# ~/.fluid/config.toml (user or system layer)
[sandbox]
mode = "isolated"          # "off" (default) | "isolated"
network = false            # default false
writable_roots = []        # extra writable directories
readable_roots = []        # extra read-only directories
env_allow = []             # extra inherited variable names, "PREFIX_*" ok
protect = []               # extra credential paths no root may contain
```

A project `.fluid/config.toml` can turn the profile on but cannot change
anything else in `[sandbox]`, and while the profile is on it cannot move the
session's working directory either (a project `cwd` is ignored with a
warning): confined commands can write the workspace, where that file lives.
The workspace is the directory Fluid was launched in, or the `cwd` a user,
system or environment layer or the host sets.

**Mechanism (Linux).** Each confined process starts through a small stdlib
launcher run as `python -I -S`. The launcher applies a
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) ruleset and a
seccomp filter, then `exec`s the command. Both come from the kernel, are
inherited by every descendant, and cannot be lifted from inside. It needs no
binary, no root, no capability and no user namespace, so it works in an
unprivileged Docker container whose seccomp profile allows the Landlock
system calls (Docker's default profile does from Docker 24).

Everything that runs before the launcher confines itself is kept out of the
model's reach. The launcher script is never run from where Fluid is installed
(a source checkout or editable install may be the workspace): Fluid reads its
source once at import and runs a read-only copy from a private temporary
directory outside every granted root. It runs under the real interpreter
binary, not a virtualenv link. A configuration that would make that
interpreter, its standard library, the loader's directories (`/etc`, `/lib*`,
`/usr/lib*`) or the launcher copy writable is refused.

**What it covers.**

| Spawn site | Under the profile |
|---|---|
| Bash tool | confined |
| Monitor (CLI host spawner) | confined |
| Monitor (a host spawner that does not declare `applies_isolation`) | refused |
| Code-mode Python kernel | confined, with its own sources read-only |
| `[[hooks]]` commands | confined; a PreToolUse hook that cannot run confined denies the call |
| Task and Swarm workers (CLI host) | inherit the parent's profile, including one a host put on the parent's context, each with its own private home and temp directory |
| Slash-command `` !`cmd` `` expansion | not run (the text is left as written) |
| `delegated_claude` provider | refused when the session is built: the delegated CLI runs its own tools |
| The ACP leg (`fluid.engine.acp`, used for Antigravity) | **not covered**: the external agent runs its own tools in its own process, and the app builds that loop outside `build_engine` |
| MCP stdio servers, Grep's ripgrep, git probes for the prompt, provider sign-in helpers | **not covered**: these run commands chosen by the owner's config or by Fluid, not the model, and many need the network or the home directory |

When confinement fails after the startup probe passed (for example a
container that allows the probe but denies `landlock_restrict_self`), the
launcher exits 126 with a `fluid isolation refused:` message and the command
never runs. Bash, hooks and the code-mode kernel report that as "not run",
not as the command's exit status, and a PreToolUse hook denies the call. A
Monitor command shows the same message and exit status in its output.

**Guaranteed on Linux with Landlock (kernel 5.13 or newer, ABI up to 12):**

- File contents: data is written, created, removed, renamed or truncated only
  under the session's working directory, `writable_roots` and a private
  per-session home and temp directory (`HOME` and `TMPDIR` point there).
- Device nodes: only `/dev/null`, `/dev/full` and `/dev/tty` (read and write)
  and `/dev/zero`, `/dev/random` and `/dev/urandom` (read) can be opened.
  `/dev/shm` and every other node are not reachable, and device ioctls are
  denied from ABI 5.
- Reads and execution are limited to the system directories (`/usr`, `/bin`,
  `/lib*`, `/etc`, `/opt`, `/proc`, `/sys`), the Python installation,
  `readable_roots` and the writable roots. The rest of the home directory,
  other projects and `/tmp` cannot be opened.
- Ownership and extended attributes cannot be changed anywhere, the
  workspace included: a seccomp filter makes `chown`, `fchown`, `lchown`,
  `fchownat` and every `setxattr`/`removexattr` form fail with `EPERM`.
- Credential locations: a root that is, contains or is inside a credential
  location is refused before anything runs. These are `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.docker`,
  `~/.netrc`, `~/.git-credentials`, `~/.pgpass`, `~/.npmrc`, `~/.pypirc`,
  `~/.config/gcloud`, `~/.config/gh`, Fluid's own `~/.fluid` and
  `~/.config/fluid`, `~/.claude`, `~/.claude.json`, `~/.codex`, and anything in
  `protect`. A session started in the home directory itself is refused for
  this reason. Fluid checks the path names first. The launcher then repeats
  the check on the object it actually opened, before granting it: a root
  that resolves anywhere other than its own name (a link, or a name swapped
  for a link after Fluid's check) is refused. So is one that is a protected
  directory, a directory above one, or a directory beneath one, compared by
  device and inode. That inode comparison also catches a bind mount of a
  protected directory.
- A confined process cannot ptrace, or read `/proc/<pid>/environ`, `mem` or
  `fd/` of, any process outside its sandbox. From Landlock ABI 6 (kernel 6.12)
  it cannot signal those processes or connect to their abstract Unix sockets.
- The environment is an allowlist: `PATH`, locale, terminal and user-name
  variables, `env_allow`, and the session's settings `env` (a deliberate
  hand-over). API keys, tokens and connection strings in the host environment
  are not passed on. Loader variables (`LD_PRELOAD`, `GCONV_PATH`, ...) from the
  settings `env` apply only to the confined command, never to the launcher.
- `network = false`: the seccomp filter makes `socket(2)` fail with `EACCES`
  for every address family, Unix sockets included, and blocks io_uring. On
  Landlock ABI 4 (kernel 6.7) TCP bind and connect are also denied by
  Landlock, and from ABI 10 UDP bind, connect and send. Existing sockets are
  not inherited (the launcher closes every descriptor above stderr).
  `socketpair(2)` is deliberately left open: it only makes a connected pair
  inside the process (tools use it for their own pipes) and reaches nothing
  outside.
- Fail closed: when the profile is on and cannot be enforced (not Linux, a
  kernel without Landlock or with it disabled at boot, a Landlock ABI newer
  than 12 that this backend has not been reviewed against, a container
  seccomp profile that blocks it, an architecture without a seccomp table, a
  refused root), Bash, Monitor, code mode and hooks report the reason and run
  nothing. The session logs the same reason when it is built. There is no
  unconfined fallback.

**Not guaranteed:**

- Other platforms. macOS and Windows have no backend yet; with the profile on,
  every command is refused. The backend interface (`unavailable_reason`,
  `prefix`) is built so that a Seatbelt backend can be added.
- Permission bits and timestamps outside the writable roots. Landlock does
  not mediate `chmod` or `utimensat`, and blocking them with seccomp would
  also break `chmod +x` in the workspace. So a confined command can still
  change the mode or the timestamps of any file the user owns that it can
  reach by path, including in read-only roots. It cannot change the file's
  contents, owner or extended attributes.
- Hard links and bind mounts made *before* the session. Landlock grants
  follow the file system object, not its name: a hard link inside the
  workspace to a file elsewhere, made earlier by something unconfined, is
  writable through that link. The check on opened roots catches a root that
  is, or is a bind mount of, a protected directory. It does not catch a
  single protected file hard-linked into the workspace, or a bind mount of
  one mounted beneath a root. Keep such links out of the workspace.
- `network = true` is not a per-host allowlist; it leaves the network fully
  open. On kernels before Landlock ABI 9 it also leaves pathname Unix sockets
  open: a confined command can connect to any socket file it can resolve and
  the user may write (for example a container runtime's control socket). From
  ABI 9 Landlock allows that only for sockets under the writable roots.
- Reading world-readable system files (`/etc`, `/proc/<pid>/cmdline` of other
  processes, `/sys`) remains possible, as do CPU, memory and disk exhaustion.
  Kernel bugs are out of scope.
- Files the confined command writes in the workspace can be acted on later by
  something unconfined: git hooks in `.git/hooks`, build scripts, project
  MCP server entries, or a project's settings files read by the next session.
  Review workspace changes before running them outside the profile.
- On Landlock ABI 1 (kernels 5.13–5.18), renaming or linking a file into a
  different directory always fails, even inside the workspace. `truncate(2)`
  by path is blocked by seccomp on ABI 1 and 2, since Landlock does not handle
  it there.
- The app's own host-side spawners (its Monitor spawner, terminals, its own
  hook runner, its MCP Bash) are outside this harness. A host opts in by
  putting the policy on `ToolCtx.extra["isolation"]` (or passing `[sandbox]`
  in the config it builds) and wrapping its spawns with
  `IsolationPolicy.wrap`. A Monitor spawner declares that by setting
  `applies_isolation = True` on itself; until then the profile refuses Monitor
  rather than trusting it.

**Checking a machine.** `python -c "from fluid.tools import isolation;
print(isolation.landlock_abi())"` prints the ABI, or `0` and the reason. The
enforcement tests in `tests/unit/test_isolation.py` attack the policy from
inside a confined child: writing outside the workspace, reading a planted
secret, reading the exec-time environment of a separate process, and
connecting to a local listener. They skip, naming the reason, where Landlock
is unavailable, and fail instead of skipping on GitHub Actions Linux.

## Override reference

| Knob | Effect |
| --- | --- |
| `permission.protect_sensitive_files = false` | Drop the default credential-file ask-rules. |
| `[[permission.rules]]` allow for a path | Override a default rule for that path (user rules come after defaults; last match wins). |
| `permission.approval_policy = "never_ask"` | Opts out of all prompting, including the default sensitive-file rules. Fences, screening, defanging, and the memory secret guard still apply. |
| `tools.disallowed = ["WebFetch", "WebSearch"]` | Remove the web surface entirely. |
| `identity_file = false` / editing a blocked file | Screening blocks are recoverable by editing the file; nothing is deleted. |
