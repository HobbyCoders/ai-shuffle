"""Harbor (Terminal-Bench 2.0) installed-agent adapter for fluid.

Lets ``harbor run`` benchmark fluid head-to-head against Claude Code on the
same tasks with the same model. The adapter uploads a locally built wheel into
the task container, installs it with uv (which provides the required
Python 3.12), and drives ``fluid run`` in one-shot mode with
``--approval-policy never_ask`` — the analog of
``claude -p --dangerously-skip-permissions``.

Usage (from the repo root; see benchmarks/harbor/README.md for the full
comparison protocol):

    uv build
    PYTHONPATH=. harbor run \
        -d terminal-bench/terminal-bench-2 \
        -a benchmarks.harbor.fluid_agent:FluidAgent \
        -m anthropic/claude-haiku-4-5 \
        -k 5

Agent kwargs (pass with ``--agent-kwarg key=value``):
    code_mode=true    run fluid with --code-mode (code-as-actions kernel)
    continual=true    run fluid with --continual (implies code-mode)

The wheel is found via $FLUID_WHEEL, else the newest ``dist/*.whl`` in the
repo root.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import override

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    with_prompt_template,
)
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# harbor model prefix -> (fluid --provider name, api key env var)
_PROVIDERS = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
}

_CONTAINER_LOG = "/logs/agent/fluid.txt"

# fluid's REPL renders one line per model call and one terminal line per turn:
#   [usage] in=123 out=45 cache_read=6789 cache_write=100 hit_rate=0.98
#   [done] stop=end_turn calls=<call-count> cost=<reported-cost>
# cache_write (cache-creation tokens) is optional so logs from older fluid
# builds that did not print it still parse.
_USAGE_RE = re.compile(
    r"\[usage\] in=(\d+) out=(\d+) cache_read=(\d+)"
    r"(?: cache_write=(\d+))? hit_rate=([\d.]+)"
)
_DONE_RE = re.compile(r"\[done\] stop=(\S+) calls=(\d+)(?: cost=\$([\d.]+))?")


class FluidAgent(BaseInstalledAgent):
    """Runs fluid's native API loop inside the Terminal-Bench task container."""

    # passthrough: the provider is inferred from the model prefix
    # (anthropic/..., openrouter/...) and the matching provider env var
    # (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, ...) is resolved and passed
    # through unchanged — which is exactly the name fluid reads.
    MODEL_CONNECTION = ModelConnectionSpec(passthrough=True)

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
                f"{extra_flags}"
                f"{shlex.quote(instruction)} "
                f"2>&1 | stdbuf -oL tee {_CONTAINER_LOG}"
            ),
            env=env,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        log = self._find_log()
        if log is None:
            self.logger.warning("fluid.txt log not found; no metrics reported")
            return
        text = log.read_text(errors="replace")

        input_total = cache_read_total = cache_write_total = output_total = 0
        for match in _USAGE_RE.finditer(text):
            input_total += int(match.group(1))
            output_total += int(match.group(2))
            cache_read_total += int(match.group(3))
            if match.group(4) is not None:
                cache_write_total += int(match.group(4))

        num_calls: int | None = None
        cost: float | None = None
        done = None
        for done in _DONE_RE.finditer(text):
            pass  # keep the last [done] line
        if done is not None:
            num_calls = int(done.group(2))
            if done.group(3) is not None:
                cost = float(done.group(3))

        prompt_total = input_total + cache_read_total + cache_write_total
        if prompt_total or output_total:
            # n_input_tokens is the full prompt total: uncached input plus
            # cache reads plus cache-creation (cache_write) tokens.
            context.n_input_tokens = prompt_total
            context.n_cache_tokens = cache_read_total
            context.n_output_tokens = output_total
        context.cost_usd = cost
        context.metadata = {
            "num_model_calls": num_calls,
            "cache_hit_rate": (
                cache_read_total / prompt_total if prompt_total else None
            ),
        }

    def _find_log(self) -> Path | None:
        candidate = self.logs_dir / "agent" / "fluid.txt"
        if candidate.is_file():
            return candidate
        return next(self.logs_dir.rglob("fluid.txt"), None)
