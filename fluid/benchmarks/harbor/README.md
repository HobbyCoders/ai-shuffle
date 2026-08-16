# Benchmarking fluid vs Claude Code on Terminal-Bench 2.0

[Terminal-Bench 2.0](https://www.tbench.ai/docs/run-terminal-bench-2-0) scores
agent harnesses (model + scaffold as a pair) on 89 real terminal tasks, each in
its own Docker container. [Harbor](https://harborframework.com) is its official
runner and ships a first-party Claude Code agent — so the comparison is: same
dataset, same model, Harbor's `claude-code` agent vs this repo's
`FluidAgent` adapter ([fluid_agent.py](fluid_agent.py)).

Fairness notes:

- Both sides call the same Anthropic model with the same API key. fluid uses
  its **native** Anthropic backend here — never the `delegated_claude` backend,
  which would just be Claude Code wearing a fluid hat.
- `fluid run --approval-policy never_ask` mirrors
  `claude -p --dangerously-skip-permissions`: fully autonomous, no prompts.
- Resolve rate is only half the story. fluid's pitch is efficiency, so compare
  tokens, cost per solved task, and cache hit rate too — the adapter reports
  all of these into Harbor's results (see below).

## Prerequisites

- Docker running (Docker Desktop with WSL2 on Windows).
- [uv](https://docs.astral.sh/uv/) installed.
- Harbor: `uv tool install harbor`
- `ANTHROPIC_API_KEY` set in the environment.

## 1. Build the fluid wheel

The adapter uploads a locally built wheel into each task container (so runs
benchmark your working tree, not a published package):

```bash
uv build
```

This produces `dist/fluid-*.whl`; the adapter picks the newest automatically
(override with `FLUID_WHEEL=/path/to/wheel`).

## 2. Smoke-test the setup

Oracle run (no model, verifies Docker + dataset):

```bash
harbor run -d terminal-bench/terminal-bench-2 -a oracle -l 5
```

fluid on 5 tasks with a cheap model (run from the repo root — `PYTHONPATH=.`
lets Harbor import the adapter):

```bash
PYTHONPATH=. harbor run -d terminal-bench/terminal-bench-2 -a benchmarks.harbor.fluid_agent:FluidAgent -m anthropic/claude-haiku-4-5 -k 5
```

PowerShell equivalent: `$env:PYTHONPATH = "."` first, then the same
`harbor run` command.

## 3. The paired comparison

Same model, same task set, both agents. Terminal-Bench runs are noisy — do 3–5
trials per side and compare means; single-run deltas under ~5 points mean
nothing.

```bash
PYTHONPATH=. harbor run -d terminal-bench/terminal-bench-2 -a benchmarks.harbor.fluid_agent:FluidAgent -m anthropic/claude-sonnet-5
```

```bash
harbor run -d terminal-bench/terminal-bench-2 -a claude-code -m anthropic/claude-sonnet-5
```

Useful variants:

- Single task: `--include-task-name "<task-name>"`
- fluid with code-as-actions: add `--agent-kwarg code_mode=true`
  (and `--agent-kwarg continual=true` for the continual harness). Running
  fluid with and without `code_mode` is its own interesting ablation.
- Retry transient API failures: `--max-retries 3`

## 4. Reading results

Harbor prints a job directory at the end of each run. Per trial it stores the
task verdict plus the agent context the adapter fills in:
`n_input_tokens` / `n_cache_tokens` / `n_output_tokens`, `cost_usd`, and
`metadata.cache_hit_rate` / `metadata.num_model_calls` (parsed from fluid's
`[usage]` and `[done]` output lines, saved verbatim at
`<job>/…/logs/agent/fluid.txt`). Compare per side:

- **resolve rate** (Harbor's headline number)
- **cost per solved task** and total tokens
- **cache hit rate** (fluid's first-class metric)
- **model calls per task** (code-mode should drive this down)

fluid's `[usage]` line reports `cache_write` (cache-creation) tokens, and the
adapter includes them in `n_input_tokens`, so the prompt total is complete.
For cross-side cost comparisons still prefer `cost_usd`, which is
provider-reported.

## Cost warning

Full Terminal-Bench 2.0 with a Sonnet-class model costs roughly $50–150+ per
trial. Iterate on Haiku with `-k 5`/`-k 10` until everything is green, then
commit to full paired trials.
