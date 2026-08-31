"""The anchor-replay runner — headless, isolated, bounded (Phase 2.1/2.2).

Replays run **in-process** through the same recipe the subagent host uses
(:meth:`fluid.cli.host.CliHost._run_child`): build an engine, submit one
``UserTurn``, drive the bus to completion, read the final assistant text off
the store. The seam that matters is the injected provider — the CLI passes
none (the engine builds the real one from config), unit tests pass a scripted
one, so the gate is testable offline.

Isolation per replay: a throwaway workspace as ``cwd`` (seeded from the
anchor's ``setup`` files), an in-memory session store, and a temp *copy* of
the skills root — the prompt's memory snapshot and mounted skills stay
faithful while Recall counters, journal writes, and any synthesis hit the
copy. The tool surface is the anchor's list (default: files/search/memory,
no shell, no web) enforced by the ordinary CE12 config gate; permission
requests are auto-allowed inside that sandbox.

Candidate overlay: a guidance :class:`~fluid.codemode.evolution.Proposal`
replays with a staged guidance home — the real ``~/.fluid/guidance`` copied
aside with the proposed edit applied — threaded into prompt composition via
``load_guidance_files(model, home=…)``. Before spending a model call the
runner compiles the prompt with and without the candidate; byte-identical
prompts return ``unchanged`` (a family-scoped edit pays live replays only
for anchors of that family).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from fluid.codemode.anchors import Anchor, ReplayOutcome, evaluate_checks
from fluid.codemode.skills import DEFAULT_SKILLS_ROOT
from fluid.config.model import HarnessConfig, ToolsConfig
from fluid.events.model import (
    ErrorEvent,
    Interrupted,
    PermissionRequest,
    ToolUse,
    TurnComplete,
)
from fluid.events.ops import RespondPermission, UserTurn

# No shell, no web, no subagents: a replay proves a harness flow, not an
# arbitrary task. Anchors opt into more via their ``tools`` list.
DEFAULT_REPLAY_TOOLS = (
    "Read", "Write", "Edit", "Grep", "Glob", "Ledger", "Note",
    "Remember", "Recall", "SessionSearch", "SessionView",
)
DEFAULT_REPLAY_TIMEOUT = 300.0


class AnchorRunner:
    """Replay anchors against the current — or a candidate — harness state."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        provider: Any | None = None,
        provider_name: str | None = None,
        timeout: float = DEFAULT_REPLAY_TIMEOUT,
        guidance_source: Path | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._provider_name = provider_name
        self._timeout = timeout
        # Where the *current* guidance files live (staging copies from here).
        self._guidance_source = (
            guidance_source
            if guidance_source is not None
            else Path.home() / ".fluid" / "guidance"
        )

    async def replay(
        self, anchor: Anchor, proposal: Any | None = None
    ) -> ReplayOutcome:
        """One bounded replay; check verdicts land on ``outcome.failures``."""
        from fluid.cli.config import resolved_prompt

        stage = Path(tempfile.mkdtemp(prefix="fluid-anchor-"))
        try:
            workspace = stage / "workspace"
            workspace.mkdir()
            for rel, content in anchor.setup.items():
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            skills_copy = stage / "skills"
            source_root = Path(self._config.skills_dir or DEFAULT_SKILLS_ROOT)
            if source_root.is_dir():
                shutil.copytree(source_root, skills_copy)
            else:
                skills_copy.mkdir()

            config = replace(
                self._config,
                cwd=str(workspace),
                skills_dir=str(skills_copy),
                tools=ToolsConfig(
                    allowed=tuple(anchor.tools or DEFAULT_REPLAY_TOOLS),
                    disallowed=(),
                ),
                model_override=anchor.model or self._config.model_override,
                max_model_calls=max(1, anchor.max_model_calls),
            )

            guidance_home: Path | None = None
            if proposal is not None:
                guidance_home = self._stage_guidance(stage, proposal)
                # Compile-only short-circuit: an unaffected prompt cannot
                # regress, and costs no model call to prove it.
                plain = resolved_prompt(
                    config, provider_name=self._provider_name
                ).text
                candidate = resolved_prompt(
                    config,
                    provider_name=self._provider_name,
                    guidance_home=guidance_home,
                ).text
                if plain == candidate:
                    return ReplayOutcome(status="unchanged")

            outcome = await asyncio.wait_for(
                self._drive(config, anchor, guidance_home),
                timeout=self._timeout,
            )
            failures = evaluate_checks(
                anchor.checks,
                replace(outcome, workspace=workspace),
            )
            return replace(outcome, workspace=None, failures=tuple(failures))
        except TimeoutError:
            timed_out = ReplayOutcome(
                status="timeout",
                error=f"replay exceeded {self._timeout:.0f}s",
            )
            return replace(
                timed_out,
                failures=tuple(evaluate_checks(anchor.checks, timed_out)),
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    # ─── internals ───────────────────────────────────────────────────────
    def _stage_guidance(self, stage: Path, proposal: Any) -> Path | None:
        """A temp home with current guidance plus the proposal applied."""
        if getattr(proposal, "kind", None) != "guidance":
            return None
        home = stage / "home"
        guidance_dir = home / ".fluid" / "guidance"
        guidance_dir.mkdir(parents=True)
        if self._guidance_source.is_dir():
            for path in self._guidance_source.glob("*.md"):
                shutil.copy(path, guidance_dir / path.name)
        (guidance_dir / f"{proposal.target}.md").write_text(
            proposal.content, encoding="utf-8"
        )
        return home

    async def _drive(
        self,
        config: HarnessConfig,
        anchor: Anchor,
        guidance_home: Path | None,
    ) -> ReplayOutcome:
        from fluid.cli.config import resolved_prompt
        from fluid.config.loader import build_engine
        from fluid.store import MemoryStore

        compiled = resolved_prompt(
            config,
            provider_name=self._provider_name,
            guidance_home=guidance_home,
        )
        store = MemoryStore()
        meta = await store.create_session(cwd=config.cwd)
        engine = build_engine(
            config,
            store,
            provider=self._provider,
            provider_name=self._provider_name,
            session_id=meta.session_id,
            system=compiled.text,
            system_sections=compiled,
        )
        status = "ok"
        error = ""
        tool_calls: list[str] = []
        try:
            await engine.submit(UserTurn(content=anchor.prompt))
            async for event in engine.bus.subscribe(meta.session_id):
                if isinstance(event, ToolUse):
                    tool_calls.append(event.name)
                elif isinstance(event, PermissionRequest):
                    await engine.submit(
                        RespondPermission(
                            request_id=event.request_id, decision="allow",
                            message=None,
                        )
                    )
                elif isinstance(event, TurnComplete):
                    break
                elif isinstance(event, Interrupted):
                    status, error = "error", event.message
                    break
                elif isinstance(event, ErrorEvent):
                    status, error = "error", event.message
                    break
            await engine.wait_turn()
            final_text = ""
            try:
                nodes = await store.load_path(meta.session_id)
            except Exception:  # noqa: BLE001
                nodes = []
            for node in reversed(nodes):
                if node.message.role == "assistant":
                    text = node.message.text().strip()
                    if text:
                        final_text = text
                        break
            return ReplayOutcome(
                status=status,
                final_text=final_text,
                tool_calls=tuple(tool_calls),
                error=error,
            )
        finally:
            try:
                await engine.aclose()
            except Exception:  # noqa: BLE001
                pass
