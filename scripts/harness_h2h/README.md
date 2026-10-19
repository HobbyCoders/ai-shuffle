# Harness head-to-head: Claude Code vs Fluid

Real tasks from this repository's history, run by three agents on the same
model, effort and ChatGPT subscription, graded by hidden tests. It answers
"is Fluid slower or worse than Claude Code, and where does the time go?"
with numbers instead of impressions.

| Arm | What runs | Model access |
|---|---|---|
| `cc` | stock `claude -p` (Claude Code CLI on PATH) | `anthropic_proxy.py`: an Anthropic Messages endpoint backed by fluid's Codex client |
| `fluid` | fluid's own one-shot `run_once` (what `fluid run` calls), no app | fluid's Codex client, injected |
| `app` | a real AI Shuffle session on this worker, over loopback | the app's own ChatGPT provider row |

The `cc` and `fluid` arms send every model call through one wrapper
(`metered.MeteredClient`), so per-call latency, time to first token and
tokens are measured by the same code on the same transport, auth, model,
effort and prompt-cache routing. The `app` arm is the product as you use it;
its calls are rebuilt from the session's event log.

## Fairness rules

- One model and effort for every arm (default `gpt-6.1-sol`, `high`). Claude
  Code's small/fast model (haiku requests) runs at `low` effort in its own
  cache partition. The proxy forces effort, so neither harness picks its own.
- No history leak: each run gets the task's base commit through `git archive`
  into a fresh one-commit repo. The hidden tests come from the fix commit and
  are copied in only after the agent finishes, overwriting whatever it left.
- Same directory for every arm (`/workspace/harness-h2h-ws`, also the app
  project's path), refilled before each run. Runs are strictly sequential.
  The bench refuses a workspace that is, contains or sits inside this
  checkout, a task's source repo, the results dir or the home directory, and
  only empties a non-empty directory that carries its `.harness-h2h-workspace`
  marker.
- `cc` and `fluid` run with an empty `HOME`, no GitHub credentials and no user
  config, skills or memory. The `app` arm cannot be isolated that way, so every
  run's tool calls are scanned for routes to the real fix (`gh`, github.com,
  the source checkout) and flagged as contamination in the report.
- The `app` arm uses the neutral `harness-h2h-bench` profile (stock preset, no
  extra instructions), fast mode off, `bypassPermissions`. The profile's
  `disallowed_tools` blocks `Task`, `Swarm` and `StartSession`: app subagents
  run on the owner's subagent default model, not the model under test. A run
  that still delegates, or that the app aborts on an error (for example a
  signed-out provider), is recorded as a harness error and redone on the next
  `run`. While the app's ChatGPT provider is not usable the app arm is skipped.
- Arm order rotates per task and trial.
- Cost is an estimate: the subscription bills nothing per token, so tokens are
  priced at one list-price row for every arm (see `report.PRICE`).

## Tasks

`tasks.json`: 8 merged bug fixes (each prompt follows its original issue,
plus the exact names and messages the hidden tests check) and 2 codebase
questions graded by regexes over `ANSWER.md`. `validate` proves every bug-fix
task's hidden tests fail on the base commit and pass on the fix.

The base and fix SHAs come from the pre-release development history, which is
not part of this public repository. The tasks run from a checkout that still
has those commits (`repo` in each task); to bench elsewhere, point the tasks at
commits from public history.

## Running

From the repo root, with the Python that has pytest (the one `local_ci.py`
records in git config `local-ci.python`) and `PYTHONPATH=fluid:.`:

```sh
python -m scripts.harness_h2h.login device          # once: the bench's own ChatGPT login
python -m scripts.harness_h2h.login probe           # one tiny call on gpt-6.1-sol
python -m scripts.harness_h2h validate              # hidden tests fail on base, pass on fix
python -m scripts.harness_h2h run --out RUN_DIR --trials 2
python -m scripts.harness_h2h report --out RUN_DIR  # RUN_DIR/report.html + summary.json
```

`run` skips trials that already have a `result.json`, so an interrupted run
resumes; `--redo` re-runs them. `--arms cc,fluid` or `--task ID` narrow it.
A full matrix is long (10 tasks × 3 arms × N trials, a few minutes each):
start it under the Monitor tool.

The bench login lives in `~/.config/fluid/codex_auth_h2h.json`, apart from
the token the app keeps; the refresh token rotates, so the two must never
share a bundle.

## Reading the report

- **Wall time** splits into model time (union of call intervals), tool time
  (tool intervals not inside a call) and **harness overhead** (neither).
  Overhead is the harness's own cost: startup, prompt assembly, persistence,
  scheduling.
- **First-call prompt** is the fixed cost of the harness's system prompt and
  tool catalog before any work.
- Per-task medians over trials, and a paired wall-time ratio against Claude
  Code per task (geometric mean), because tasks differ by an order of
  magnitude in length.

Per run, `runs/<task>/<arm>/t<N>/` keeps the prompt, the raw stream or event
log, the metered call log, the agent's patch and `result.json`.
