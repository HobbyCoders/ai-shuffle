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
neutralized in place. The surrounding data survives.

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
permission engine (policy + last-match-wins rules + scoped allow-always), the
doom-loop guard, plan mode's exclusion of write tools (from the request
schemas on the JSON path, and — because code mode reaches its tools through a
kernel rather than a schema — from the code-mode projection and the host-call
gate, plus a final refusal at dispatch), the
session tool restriction (CE12) enforced at schema build, registry view, and
dispatch, read-before-write on file edits, and the code-mode kernel's
host-side gates (`open()` and network disabled; every in-kernel tool call
passes the same hooks and permissions).

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
- The code-mode sandbox is a soft boundary by design; the hard gates are
  host-side. A container-grade kernel remains future work
  (`docs/agent-experience.md`).

## Override reference

| Knob | Effect |
| --- | --- |
| `permission.protect_sensitive_files = false` | Drop the default credential-file ask-rules. |
| `[[permission.rules]]` allow for a path | Override a default rule for that path (user rules come after defaults; last match wins). |
| `approval_policy = "never_ask"` | Opts out of all prompting, including the default sensitive-file rules. Fences, screening, defanging, and the memory secret guard still apply. |
| `tools.disallowed = ["WebFetch", "WebSearch"]` | Remove the web surface entirely. |
| `identity_file = false` / editing a blocked file | Screening blocks are recoverable by editing the file; nothing is deleted. |
