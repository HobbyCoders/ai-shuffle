"""Harbor (Terminal-Bench 2.0) installed-agent adapter for fluid.

Lets ``harbor run`` benchmark fluid head-to-head against Claude Code on the
same tasks with the same model. The adapter uploads a locally built wheel into
the task container, installs it with uv (which provides the required
Python 3.12), and drives ``fluid run`` in one-shot mode with
``--approval-policy never_ask`` — the analog of
``claude -p --dangerously-skip-permissions``.

The default Harbor setup does not protect the metrics file from tool writes.
The file is separate from the transcript, but Fluid and its Bash tool run
with the same user identity by default. It contains normalized engine
counters, not raw provider measurements. OpenRouter Chat Completions and
OpenAI Responses can turn a missing provider counter into zero. The JSONL
keeps estimate flags when the engine event supplies them.

Usage (from the repo root; see benchmarks/harbor/README.md for the full
comparison protocol):

    uv build
    PYTHONPATH=. harbor run \
        -d terminal-bench/terminal-bench-2 \
        -a benchmarks.harbor.fluid_agent:FluidAgent \
        -m anthropic/claude-haiku-4-5 \
        -l 5

Agent kwargs (pass with ``--agent-kwarg key=value``):
    code_mode=true    run fluid with --code-mode (code-as-actions kernel)
    continual=true    run fluid with --continual (implies code-mode)

The wheel is found via $FLUID_WHEEL, else the newest ``dist/*.whl`` in the
repo root.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import os
import shlex
from pathlib import Path
from typing import override

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    with_prompt_template,
)
from harbor.agents.agent_auth_resolution import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# harbor model prefix -> (fluid --provider name, api key env var)
_PROVIDERS = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
}

_CONTAINER_LOG = "/logs/agent/fluid.txt"
_METRICS_FILE = "fluid-usage.jsonl"
_CONTAINER_METRICS = f"/logs/agent/{_METRICS_FILE}"


class FluidAgent(BaseInstalledAgent):
    """Runs fluid's native API loop inside the Terminal-Bench task container."""

    # The default spec infers the provider from the model prefix
    # (anthropic/..., openrouter/...) and resolves the matching provider env
    # var (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, ...) under its own name,
    # which is exactly the name fluid reads. Harbor 0.25 moved this class and
    # dropped the old ``passthrough`` option; this needs Harbor 0.25 or newer.
    MODEL_CONNECTION = ModelConnectionSpec()

    CLI_FLAGS = [
        CliFlag("code_mode", cli="--code-mode", type="bool",
                env_fallback="FLUID_CODE_MODE"),
        CliFlag("continual", cli="--continual", type="bool",
                env_fallback="FLUID_CONTINUAL"),
    ]

    @staticmethod
    @override
    def name() -> str:
        return "fluid"

    @override
    def get_version_command(self) -> str | None:
        return '. "$HOME/.local/bin/env"; uv tool list | grep -m1 fluid'

    @override
    def parse_version(self, stdout: str) -> str:
        # "fluid v0.1.0" -> "0.1.0"
        return stdout.strip().removeprefix("fluid").strip().lstrip("v")

    @staticmethod
    def _wheel_path() -> Path:
        override_path = os.environ.get("FLUID_WHEEL")
        if override_path:
            wheel = Path(override_path)
            if not wheel.is_file():
                raise FileNotFoundError(f"$FLUID_WHEEL not found: {wheel}")
            return wheel
        repo_root = Path(__file__).resolve().parents[2]
        wheels = sorted(
            repo_root.glob("dist/fluid-*.whl"),
            key=lambda p: p.stat().st_mtime,
        )
        if not wheels:
            raise FileNotFoundError(
                f"No fluid wheel in {repo_root / 'dist'} — run `uv build` "
                "first, or point $FLUID_WHEEL at one."
            )
        return wheels[-1]

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # stdbuf (coreutils) is needed by the tee pipeline in run().
        await self.ensure_system_dependencies(
            environment, ("curl", "ca_certificates", "coreutils")
        )
        wheel = self._wheel_path()
        remote_wheel = f"/tmp/{wheel.name}"
        await environment.upload_file(wheel, remote_wheel)
        # uv brings its own Python (fluid needs >=3.12, task images vary).
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                '. "$HOME/.local/bin/env" && '
                f"uv tool install --python 3.12 {shlex.quote(remote_wheel)} && "
                "fluid --help >/dev/null"
            ),
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        provider_prefix, model = self.model_name.split("/", 1)

        access = self.model_connection
        provider_prefix = access.provider or provider_prefix
        if provider_prefix not in _PROVIDERS:
            raise ValueError(
                f"Unsupported provider: {provider_prefix} "
                f"(supported: {', '.join(sorted(_PROVIDERS))})"
            )
        fluid_provider, api_key_env = _PROVIDERS[provider_prefix]
        if not access.api_key:
            raise ValueError(f"No API key found for provider: {provider_prefix}")
        env = {**access.env, api_key_env: access.api_key}
        # Output budget per model call. Claude Code's default is 32k; pass
        # FLUID_MAX_TOKENS through so both legs can run with the same cap.
        max_tokens = os.environ.get("FLUID_MAX_TOKENS")
        if max_tokens:
            env["FLUID_MAX_TOKENS"] = max_tokens
        # Per-turn model-call cap. Claude Code runs unbounded under Harbor, so
        # set FLUID_MAX_CALLS high (or to the same --max-turns) for parity.
        max_calls = os.environ.get("FLUID_MAX_CALLS")
        if max_calls:
            env["FLUID_MAX_CALLS"] = max_calls

        cli_flags = self.build_cli_flags()
        extra_flags = f"{cli_flags} " if cli_flags else ""

        await self.exec_as_agent(
            environment,
            command=(
                '. "$HOME/.local/bin/env"; '
                "fluid run "
                f"--provider {fluid_provider} "
                f"--model {shlex.quote(model)} "
                "--approval-policy never_ask "
                f"--usage-jsonl {_CONTAINER_METRICS} "
                f"{extra_flags}"
                f"{shlex.quote(instruction)} "
                f"2>&1 | stdbuf -oL tee {_CONTAINER_LOG}"
            ),
            env=env,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        metrics = self.logs_dir / "agent" / _METRICS_FILE
        if not metrics.is_file():
            # Some Harbor integrations pass the agent log directory itself.
            metrics = self.logs_dir / _METRICS_FILE
        if not metrics.is_file():
            self.logger.warning("fluid-usage.jsonl not found; no metrics reported")
            return

        token_fields = (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        )

        def read_counts(data: object) -> dict[str, int]:
            if not isinstance(data, dict):
                raise ValueError("expected token counters")
            counts = {key: data[key] for key in token_fields if key in data}
            if any(type(n) is not int or n < 0 for n in counts.values()):
                raise ValueError("invalid token count")
            return counts

        def read_charge(n: object, usd: object) -> tuple[int | None, float | None]:
            if n is not None and (type(n) is not int or n < 0):
                raise ValueError("invalid model call count")
            if usd is not None and (
                type(usd) not in (int, float) or not math.isfinite(usd) or usd < 0
            ):
                raise ValueError("invalid cost")
            return n, usd

        per_call: dict[str | int, Counter[str]] = defaultdict(Counter)
        outside_turn_totals: Counter[str] = Counter()
        outside_turn_charges: list[tuple[int | None, float | None]] = []
        terminal: dict[str | int, dict[str, int]] = {}
        by_turn: dict[str | int, tuple[int | None, float | None]] = {}
        stop_reason: str | None = None
        with metrics.open(encoding="utf-8", errors="replace") as stream:
            for i, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("expected a record")
                    turn_id = record.get("turn_id")
                    turn = turn_id if isinstance(turn_id, str) else i
                    if record.get("type") == "usage":
                        counts = read_counts(record)
                        if record.get("outside_turn_totals") is True:
                            outside_turn_charges.append(read_charge(
                                record.get("num_model_calls"), record.get("cost_usd"),
                            ))
                            outside_turn_totals.update(counts)
                        else:
                            per_call[turn].update(counts)
                    elif record.get("type") == "status":
                        reason = record["stop_reason"]
                        if not isinstance(reason, str):
                            raise ValueError("invalid stop reason")
                        charge = read_charge(record["num_model_calls"], record["cost_usd"])
                        counts = read_counts(record.get("usage", {}))
                        stop_reason = reason
                        # Some failures emit both ErrorEvent and TurnComplete.
                        # Keep a single terminal snapshot for each turn.
                        terminal[turn] = counts
                        by_turn[turn] = charge
                except (ValueError, KeyError, OverflowError):
                    # A killed process can leave a partial final line.
                    self.logger.warning(f"Ignoring invalid fluid usage record on line {i}")

        totals = outside_turn_totals.copy()
        for turn in per_call.keys() | terminal.keys():
            # Terminal counters include calls that may not have emitted a
            # record (e.g. truncated and retried). Use per-call sums only for
            # counters the terminal did not report, or for unfinished turns.
            totals.update({**per_call.get(turn, {}), **terminal.get(turn, {})})
        cache_read = totals.get("cache_read_tokens")
        prompt_counts = [
            totals[key] for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            if key in totals
        ]
        prompt_total = sum(prompt_counts) if prompt_counts else None
        if prompt_total is not None:
            context.n_input_tokens = prompt_total
        if cache_read is not None:
            context.n_cache_tokens = cache_read
        if "output_tokens" in totals:
            context.n_output_tokens = totals["output_tokens"]
        charges = [*by_turn.values(), *outside_turn_charges]
        calls = [n for n, _ in charges if n is not None]
        costs = [usd for _, usd in charges if usd is not None]
        context.cost_usd = sum(costs) if costs else None
        context.metadata = {
            "num_model_calls": sum(calls) if calls else None,
            "cache_hit_rate": cache_read / prompt_total if prompt_total and cache_read is not None else None,
            "stop_reason": stop_reason,
        }
