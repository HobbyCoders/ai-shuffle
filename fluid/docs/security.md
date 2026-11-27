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
  `[[permission.rules]]` allow overrides them. Letter case is ignored, as in
  the Bash classifier below, so `.ENV` asks on every platform.
  `.env.example`-style templates are exempt: the rule steps aside and the
  call is decided as any other is, so reading a template runs and editing
  one asks under `on_write`. `permission.protect_sensitive_files = false`
  disables the set. The rules read the policy when a call is judged, so a
  session that switches into or out of `never_ask` loses or regains them at
  once; under `never_ask` the call is the policy's own allow, which the
  repeat guard still counts. A host that builds its own engine installs the
  same set with `fluid.config.loader.sensitive_file_rules(active=...)`; AI
  Shuffle's native leg does.
- **Accept Edits approves project files only.** The mode's allow rules for
  `Edit`, `Write`, `MultiEdit` and `NotebookEdit` carry a guard
  (`fluid.permissions.edit_guard.edit_stays_in_project`), so they hold
  wherever they sit in the rule list. An edit is approved when the file, as
  the tool opens it (symbolic links followed), is inside the session's
  working folder, a folder the host added or the session's own scratch
  folder. The scratch folder is the one the session's owner resolves for it
  (`PermissionEngine.edit_roots`, set by the loop), never one read off the
  path, so another session's scratch folder asks.
  Everything else falls through to the policy and asks: a path outside
  those folders, a link that leaves them, a hard-linked file (it may be a
  second name for a file elsewhere), a credential file, and a file that
  starts programs or configures a tool: anything under `.git`, `.fluid`,
  `.claude`, `.codex`, `.vscode`, `.idea`, `.husky` or `.devcontainer`,
  shell startup files, `.gitconfig`, `.gitattributes`, `.mcp.json`,
  `fluid.toml` and the like (the lists are `PROTECTED_DIR_NAMES` and
  `PROTECTED_FILE_GLOBS`). Both the name the call wrote and the file it
  reaches are judged, so a link named `.env` that points at an ordinary
  file asks. A session started inside one of those folders (`repo/.vscode`)
  gets no approval at all, whether the folder is named that way or reached
  through a link of that name. A folder that one of those names links to
  (`.vscode -> tooling/editor`) is treated as that folder under every name,
  including for a session started inside it: the protected names in the
  file's folder and in every folder above it are read, and the route through
  a link is judged like any other, so a worktree checkout reached through
  one stays project code. A link that sits elsewhere in the tree is not
  seen, since finding one would take a walk of the whole project. A request
  that lists several files (a
  delegated agent reporting one edit over more than one target,
  `file_paths`) is approved only when every file passes; the delegated-agent
  mapping lists the targets for every request that can stand for an edit,
  whatever name it arrives under.
  Names are compared the way Windows and macOS open them (letter
  case, trailing dots and spaces, `:stream` suffixes, 8.3 short names), on
  every platform. A checkout under `.fluid/worktrees/<name>/` or
  `.claude/worktrees/<name>/` is project code. A session whose working
  folder is the home folder or a filesystem root gets no approval for the
  dot-folders there. The check is a permission preflight, not a filesystem
  broker: the tool opens the file afterwards.
- The **Bash risk classifier** demotes an otherwise read-only command to
  `write` (→ prompts under `on_write`) when any argument names a credential
  file — `cat .env` does not sail under the read-only fast path. Letter case
  is ignored (`.ENV` opens `.env` on Windows and macOS), a relative operand
  is also read from the folder the shell starts in (the call's own `cwd`,
  else the session's working folder: `cat credentials` in `~/.aws`), and a
  `..` after a named folder asks, in an operand or in `cwd`, because that
  folder can be a link. The same holds for the process environment, which carries the
  provider keys and `GH_TOKEN` on purpose: bare `env` and `printenv`,
  `printenv GH_TOKEN`, `/proc/<pid>/environ` (by any spelling or `cwd`, and
  any glob that can select a process folder), awk's `ENVIRON` and jq's `env`
  all prompt, and so does a jq program the classifier cannot read (`-f`,
  `-L`, `import`, `include`, a `#` comment). jq words are read where jq
  reads them: `include"m";` and `"\(env)"` count, `.env` and `"env"` do not.
- The classifier rates a command read-only only when the shell runs the
  words as written. A `$` expansion outside single quotes (`-exe${x}c`,
  `$'-exec'`, `.e${x}nv`, `$GH_TOKEN`), a brace list, a quoted word made only
  of operator characters (`';'`), a `#` inside a word and any control
  character other than tab and newline (a carriage return included) make the
  command `write`. A glob expands to file names a clone can choose, so for a
  program with options that write or run something it passes only where no
  file name can change the answer: `sort src/*.txt` is read-only, `sort *.txt`
  asks (a file named `-oX.txt`), and so does `uniq out/*` (uniq writes its
  second operand). Programs with no such option (`ls`, `cat`, `grep`, `wc`)
  take any glob.
- **Approvals and expansion.** "Always allow" is recorded per program
  (`git`, `npm run`, `timeout git`). A command gets no label, so its approval
  covers that call only, when the shell can replace a word the label is read
  from: `$X status`, `timeout $T git status`, `npm $X`. With options before
  the subcommand (`npm --prefix web run build`) the label is the bare program
  and any expanding word after them makes the approval one-shot. An allow
  rule does not match such a command either. Deny and ask rules match on the
  widest reading of the text. Monitor's `command` is read by the same rules
  as Bash's, under any server prefix.
- **Monitor's script fallback.** Monitor runs a script that lost its exec bit
  through `sh`, but only when the command names it by path (`./launch.sh`).
  A bare name is the program on PATH, as the permission check read it; a
  file of that name in the working folder is never run in its place.
- **cmd.exe (Windows without Git Bash).** The classifier reads POSIX syntax
  and cmd.exe does not, and cmd.exe runs `git.cmd` from the working folder
  before the `git` on PATH. There nothing is rated read-only, every approval
  covers one call, and no allow rule matches a command. Deny and ask rules
  still apply. Install Git for Windows to get the same behaviour as every
  other platform.
- **Git settings for agent shells.** `git status` is rated read-only, and git
  runs programs a repository's config names. Every shell that runs
  model-chosen commands (Bash, Monitor, a slash command's inline shell, the
  delegated CLI's Bash, the app's background commands) gets
  `safe.bareRepository=explicit` and `core.fsmonitor=false` as command-scope
  config, which outranks a repository's own file on Linux, macOS and Windows.
  A folder of plain files shaped like a repository, which a clone can ship,
  is no longer a repository to `git -C`, and no config can name a program for
  a status read. A real bare repository still works with `--git-dir`. These
  settings need git 2.38; an older git ignores them without a word. So the
  worker's own git is asked its version when a session starts (a native
  session when it is built, a delegated one when its tools start being
  served), through the shell Bash uses and with none of a session's
  settings. The question runs in the shell's own folder, never the project's,
  and is not asked at all under cmd.exe, where nothing is rated read-only and
  the working folder's `git.cmd` would answer. Rating a tool call starts no process: it only looks that answer
  up, because it runs before a rule, a hook or a person has had a say on the
  call. When the git is older, missing or silent, or is a git that appeared
  on `PATH` after the answer was taken, no git command is rated read-only:
  each one prompts, until the next session start asks the new git. The
  worker's own `PATH` is trusted the way it is for `ls` and `cat`, and the
  host runs that same git for its own work: a program an approved step
  installed there is the worker's program from then on, on every platform.
  `--output`, `--ext-diff` and `--textconv` prompt in any
  shortened form (`--ext-d`). What these settings do not cover: a checkout's
  own `.git/config` can name a filter, a diff driver, a text converter or a
  signature program, and git runs them on `git status`, `git diff`,
  `git show`, `git log -p` and `git blame`; no git option turns all of that
  off. A clone does not carry `.git/config`, but an unpacked archive can. So
  a folder's `.git` counts as the project's own configuration: writes to
  `.git` stay behind a prompt, and in a project the `fluid` CLI's owner has
  not trusted (item 10) no git command is rated read-only and the harness
  starts no git of its own.
- **Settings that choose the programs.** The classifier knows what the
  worker's own `cat`, `ls` and `git` do. A session whose settings `env` sets
  `PATH`, `PATHEXT`, a shell startup file (`BASH_ENV`, `ENV`, `ZDOTDIR`),
  `HOME`, a loader variable (`LD_*`, `DYLD_*`) or any `GIT_*` name runs
  programs the classifier has not looked at, so none of that session's
  commands is rated read-only: each one prompts, and "allow always" still
  covers the program from then on. The rule is the same on Linux, macOS and
  Windows.
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
as `sudo` or `npm` ran, not the wrapper alone; a program named by path, such
as `/usr/bin/git` or `scripts/git`, is approved for that one call only), the
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

**10. Project trust** (`fluid/config/trust.py`). Every gate above is
configurable, and a project's own files are one of the places it is
configured from. A folder that was cloned or unpacked is someone else's
content, so the `fluid` CLI loads nothing from it until its owner says the
project is trusted: not its config files (which could set
`approval_policy = "never_ask"`, turn off `protect_sensitive_files` or add
allow rules), not its hooks and MCP servers (programs that start with the
session), and not its settings, instruction files, skills, agents, commands
or the plugins its settings enable. An untrusted project runs on the
built-in defaults plus the user's own config.

- Trust is the owner's answer and nothing else: a yes at the question
  `fluid chat` and `fluid run` ask on a terminal, `fluid trust`, or
  `--trust-project` for one run. No environment variable and no config value
  grants it, so nothing the project or a process that sets variables for the
  CLI controls can.
- The answer is kept in `~/.fluid/trusted_projects.json`, keyed by the
  project's real path (the git repository root, or the start directory
  outside a checkout). A project cannot carry its own trust, a checkout
  nested inside a trusted folder is asked about separately, and a link to an
  untrusted folder is judged by where it leads. A recorded path is compared
  as text and never resolved again, so a trusted folder that is moved away
  and replaced by a link to another folder is not trusted through the old
  name, and no case folding is done. The working folder's links are followed
  once, before the question: the question, the saved answer and the session
  all use that one folder, so a `--cwd` that is a link cannot be pointed at
  another project between them. Two `fluid` processes that answer at once
  both keep their answer (the update holds a lock file beside the record).
- The project's `.git` is part of it. git runs the programs `.git/config`
  names even on a status read, so the harness runs no git of its own, and
  rates no git command read-only, in any checkout but the trusted project's.
  This is judged per call, by the checkout git would open: the session's
  folder, the Bash call's `cwd`, or a `git -C` folder. In an untrusted
  project that is every git command; in a trusted one it is a command aimed
  at another checkout, including one nested inside the project (a vendored
  clone, a submodule) or behind a link. A `-C` folder the check cannot
  establish (an absolute path, a variable, `~`) asks. Without git the prompt
  has no git lines and file listing falls back to a directory walk.
- Nothing above the trusted folder is searched for instruction files,
  commands, skills or agents: outside a git checkout the project is the
  trusted folder alone, so a shared temp or download folder above an
  unpacked project has no say. Reading a file inside another checkout that
  sits in the project adds none of that checkout's instruction files to the
  result either.
- The Claude provider's child process reads settings and instruction files
  itself. It is started with the same scopes (`--setting-sources`), so it
  leaves an untrusted project's files out too, and with exclude patterns
  (`claudeMdExcludes`) for every folder above the project, so those add
  nothing. Both need Claude Code 2.1.239 or newer; with an older or unknown
  version the CLI's Claude provider does not start and says which version it
  needs.
- Without a terminal there is no question. The project's files are left out
  and stderr says so, which is the safe result for a script or CI run in a
  checkout nobody vouched for.
- A record that cannot be read trusts nothing.
- The rule reads paths only, so it is the same on Linux, macOS and Windows.

The AI Shuffle app does not load a project's fluid config files into its
sessions; it configures them itself, so this layer is the CLI's. The app has
no trust question: a project there is a folder its owner registered, and git
runs in it as in a trusted project.

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
- Project trust is for a folder, not for a version of its contents. Files
  that reach a trusted project later (a pull, a branch switch, an edit by the
  agent) load without a new question. Take the trust back with
  `fluid trust --remove` before working on content you have not read.
- Project trust does not defend against whoever starts the process. The
  home folder (`HOME`, `USERPROFILE`) decides where the trust record and the
  user config live, and a launcher that points it at a project makes that
  project's files the user's own. Nothing inside a project folder can set
  it.
- A trusted project's `.git/config`, and any project's in the AI Shuffle
  app, can name programs that `git status` and `git diff` run without a
  prompt. Register or trust a folder that came with its own `.git` (an
  unpacked archive, a copied checkout) only after reading that file.
- With the Claude provider, Claude Code decides which instruction files it
  loads below the project. It loads a subfolder's `CLAUDE.md` when it reads
  files there, and that includes a separate checkout placed inside a trusted
  project; fluid's own engines leave such a checkout's files out, Claude
  Code does not. The exclude patterns for the folders above the project were
  checked against Claude Code 2.1.332 on Linux only; path matching on
  Windows and macOS follows Claude Code's documentation and has not been
  exercised.
- A shell command that changes folder and then runs git (`cd other && git
  status`) was never rated read-only and still asks. The per-checkout rule
  covers the forms that were: a plain git command, the call's `cwd`, and
  `git -C`.

## Override reference

| Knob | Effect |
| --- | --- |
| `permission.protect_sensitive_files = false` | Drop the default credential-file ask-rules. |
| `[[permission.rules]]` allow for a path | Override a default rule for that path (user rules come after defaults; last match wins). |
| `permission.approval_policy = "never_ask"` | Opts out of all prompting, including the default sensitive-file rules. Fences, screening, defanging, and the memory secret guard still apply. |
| `tools.disallowed = ["WebFetch", "WebSearch"]` | Remove the web surface entirely. |
| `fluid trust` / `--trust-project` | Load a project's own config, hooks, MCP servers and other files (saved, or for one run). The knobs above count from a project's files only after this. |
| `identity_file = false` / editing a blocked file | Screening blocks are recoverable by editing the file; nothing is deleted. |
