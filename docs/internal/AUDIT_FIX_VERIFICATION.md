# Audit-fix verification with glm-5.3-flash

How to prove the 2026-08-26 session-audit fixes work, using the model that showed the
worst efficiency profile in the audit. Nothing here runs a model on its own — every
step is manual and observable, so you can watch for anything going wrong.

Two layers of proof:

1. **Unit proof (no model, no tokens)** — already automated; run any time.
2. **Live proof (glm-5.3-flash)** — one scripted session per behavior, each with a
   concrete pass/fail check you can read off the transcript or the database.

## Layer 1 — unit proof, no model

```bash
# fluid (run in the omni-harness checkout, branch claude/session-audit-fixes)
uv run pytest tests/unit -q
```

```bash
# ai-shuffle (this branch; vendor/fluid already mirrors the fluid branch)
PYTHONPATH=vendor/fluid python -m pytest tests/api tests/core tests/db tests/middleware tests/mcp tests/test_harness_adapter.py -o addopts="" -q -p no:cacheprovider
```

New audit-specific test files, if you want to see just the fixes:
`vendor/fluid/tests/unit/test_audit_*.py` (fluid) and, app-side,
`tests/db/test_sessions_cumulative_usage.py`, `tests/db/test_usage_accounting_migration.py`,
`tests/db/test_backfill_usage_from_events.py`, `tests/mcp/test_task_polling_nudge.py`,
`tests/api/test_chat_ws_plan_gate.py`, `tests/core/test_harness_adapter_session_ctx.py`.

## Layer 2 — live proof with glm-5.3-flash

Prereqs: rebuild the worker image first (`docker compose build`) — it now installs
ripgrep and dev requirements, adds `~/.local/bin` to PATH, and seeds git identity.
Create a fresh session on a glm-5.3-flash profile (native leg, codemode on) for each
numbered check unless noted. Where a check reads the DB, the queries are inline.

### V1 — model guidance blocks reach glm (P9)

Start a session, send any trivial prompt, then inspect the compiled prompt (the
`fluid_session_nodes` system/prompt record, or debug-log the compiled sections).

- **Pass:** the prompt contains the headings `Act, don't narrate` and
  `Execution discipline`. A Claude-model session contains neither.

### V2 — 16KB stdout cap + fair-share truncation (P1/P2)

Prompt: *"Read app/core/turn_scheduler.py and app/core/session_runtime.py fully and
print both files completely in one python cell."*

- **Pass:** the cell output is capped near 16,384 chars; the truncation notice reads
  `…[stdout truncated: kept N of M chars across K prints; heaviest drops: …]` and
  BOTH files contribute surviving text (fair share), not just the first.

### V3 — ResultRef recovery via `_refs` and `_` (P1)

Prompt: *"In one cell call Read on app/core/query_engine.py (a large file) without
assigning the result. In the NEXT cell, without re-reading the file, print the first
500 characters of that earlier result."*

- **Pass:** the second cell recovers the payload through `_refs[<id>]` or `_` with
  no second Read call in the transcript.

### V4 — sync Task report arrives as a file, not a flood (P2/P5)

Prompt: *"Spawn a sync General Purpose subagent to map every breakpoint in frontend/ CSS,
then summarize its report."*

- **Pass:** the Task tool result JSON carries `report_path`, `report_chars`, and a
  ~2,000-char `report_head` — no inline `report` field; the file exists under
  `<settings.get_fluid_projects_dir>/<native-memory-project-slug>/task-reports/`,
  outside the checkout; the absolute receipt is read with `Read` (offset/limit
  visible in the transcript) instead of printing 60KB. Also exercise Ledger and
  Note in a fresh git repo: no `.fluid/` or untracked harness files may appear;
  restart/resume must recite their global state. A legacy cwd ledger/note must
  remain untouched and be copied only if canonical state does not exist. Empty
  canonical state must not resurrect legacy entries. Existing configured Read
  denials and chat attachment confinement must remain enforced.

### V5 — TaskList refuses to be a polling loop (P5)

Prompt: *"Start a background Monitor on `sleep 60 && echo done`, then immediately
call TaskList twice."*

- **Pass:** each TaskList result begins with
  `all tasks still running — park; you will be woken on completion.` and the model
  parks (turn ends) instead of looping.

### V6 — Grep negated globs and Bash exit codes (P4)

Prompt: *"Using the Grep tool with glob `!{.fluid/**}`, count files mentioning
`stdout_cap`. Then run a shell command that succeeds silently, e.g.
`git config user.name x-check && git config --unset user.name`."*

- **Pass:** Grep returns real matches (worker image now has rg; if you force the
  Python fallback by renaming rg, the negation still works or hard-errors — never a
  clean `(no matches)`); the silent shell command's result reads
  `(no output) [exit code 0]`.

### V7 — plan approval from chat (P10)

Start a session in **plan mode**. Let the model draft a plan and call StopPlanning.
When the purple Plan Approval panel appears, do not click it — type `Proceed.` in
the chat composer.

- **Pass:** the panel clears, the tool result says the plan was approved and plan
  mode is off, the model starts implementing, and no duplicate "Proceed." turn runs
  afterwards. Repeat with `make it smaller first` — the gate stays in plan mode and
  the words reach the model.

### V8 — token accounting tells the truth (P8)

After any session above finishes:

```sql
SELECT total_tokens_in, total_tokens_out, total_cache_read_tokens,
       total_cache_creation_tokens, total_model_calls, turn_count
FROM sessions WHERE id = '<session-id>';
```

- **Pass:** cache columns are non-zero on any multi-turn session; `turn_count`
  equals the number of user turns (not model calls); and
  `python -m scripts.backfill_usage_from_events --dry-run --session-id <id>`
  reports no drift (the live accumulation matches the event stream).

### V9 — one notification, no duplicated payload (P3)

Run V4's prompt but with `mode="background"`, and let the session park until the
child completes.

- **Pass:** exactly one `<task-notification>` block arrives for the task; it does
  not contain the same text twice (summary duplicated into result); a re-opened /
  resumed session does not re-deliver it.

### V10 — worker environment (P13)

In any session: *"Run `rg --version`, `pytest --version`, and `git config
user.email`, and write a note then remove it by its exact text."*

- **Pass:** all three commands succeed inside the worker; Note removal reports
  exactly what was removed; a brand-new project session shows the memory directory
  already exists (no failed Glob working around a missing dir).

## Reading the efficiency delta (optional but the point of it all)

After running V2-V5 with glm-5.3-flash, compare against the audit's baselines for
the same model (`ses_2976e0dd`, `ses_83a88dd4`): calls-per-cell during
verification, bytes printed per cell, and the absence of TaskList polling runs are
the three numbers the audit flagged. The `fluid_session_events` stream for the new
sessions is the source of truth — the same queries from the audit apply unchanged.
