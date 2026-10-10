"""Measure shipped owner/code prompt text without a model, database or MCP call.

Run: PYTHONPATH=fluid python scripts/measure_prompt_catalog.py [--json]
The fixed fixture uses actual host factories, registry bridging and compilation.
Installation-specific files/rosters and external browser handshakes are excluded.
Both eager code-mode text and the full discoverable catalog are reported; the
latter is a size ceiling, NOT a claim that deferred schemas are sent each call.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]


def compile_fixture(*, native=True):
    from app.core import agent_provider_models as agents
    from app.core.config import settings
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    from app.core.environment import DEFAULT_SECURITY_INSTRUCTIONS
    from app.mcp.app_config_sdk import APP_CONFIG_SYSTEM_PROMPT, create_app_config_server
    from app.mcp.builtin_tools import create_builtin_tools_server
    from app.mcp.chat_access_sdk import GUIDANCE, create_chat_access_server
    from app.mcp.ai_tools_sdk import create_ai_tools_server
    from fluid.codemode.projection import Projection

    # Freeze only installation inputs, never descriptions, schemas or compilers.
    with ExitStack() as stack:
        fixed = {
            "app.db.database.get_all_profiles": [],
            "app.core.agent_provider_models.available_subagents": [
                agents.SubagentTarget("helper", "Helper", "Implement and test code.", "sonnet")],
            "app.core.agent_provider_models.caller_subagents_disabled": False,
            "app.core.agent_provider_models.task_model_choices": ("Claude", []),
            "app.core.agent_provider_models.task_worker_property": None,
            "app.core.subagent_default.effective": SimpleNamespace(model="sonnet"),
            "app.mcp.builtin_tools._discover_agent_registry": {},
            "fluid.context.agents.discover_agents": [],
            "fluid.context.compiler.build_instruction_blocks": [],
            "fluid.context.compiler.discover_skills": [],
            "fluid.context.compiler.environment_block": "# Environment\nWorking directory: /fixture\nPlatform: linux",
            "app.core.provider.harness_adapter._agent_context": (None, {}),
            "app.core.provider.harness_adapter._learned_guidance_blocks": (),
            "app.core.provider.harness_adapter._recall_backend_available": True,
            "app.core.provider.harness_adapter._delegated_fluid_context": [],
            "app.core.provider.harness_adapter._cli_ships_search_tools": False,
            "app.core.fal_credential.is_enabled": False,
        }
        for name, value in fixed.items():
            stack.enter_context(patch(name, return_value=value))
        stack.enter_context(patch.object(type(settings), "managed_database_available", return_value=True))
        # Fail closed if a future factory starts consulting the live database.
        db_guard = stack.enter_context(patch("app.db.base.get_connection", side_effect=AssertionError("offline fixture opened DB")))
        for name, value in {"task_tool_max_workers": 64, "task_tool_max_depth": 5,
                            "subagent_max_concurrent_per_session": 0,
                            "task_worker_max_per_provider": 0}.items():
            stack.enter_context(patch.object(settings, name, value))
        servers = {
            "builtins": create_builtin_tools_server(is_admin=True, cwd="/fixture", workflow_guidance=True),
            "app-config": create_app_config_server(is_admin=True),
            "chat-access": create_chat_access_server("fixture"),
            "ai-tools": create_ai_tools_server(dict.fromkeys((
                "image_generation", "image_editing", "image_reference", "video_generation",
                "image_to_video", "video_with_audio", "text_to_3d", "image_to_3d",
                "audio_generation", "audio_to_audio"), True), "/fixture/tools"),
        }
        assert all(servers.values()), "A bundled tool factory returned no catalog"
        options = SessionOptions(model="gpt-5", cwd="/fixture", mcp_servers=servers,
            setting_sources=[], extra={"automatic_workflows": True, "github_connected": True})
        adapter = ha.HarnessAdapter()
        registry = asyncio.run(adapter._build_registry(options, native=native))
        specs = registry.list_tools(loaded_only=False)
        projection = Projection(specs, deferred=[s.name for s in registry.deferred_tools()])
        codemode = SimpleNamespace(_projection=lambda: projection)
        base = "\n\n".join(("You are a coding assistant.", DEFAULT_SECURITY_INSTRUCTIONS,
                               APP_CONFIG_SYSTEM_PROMPT, GUIDANCE))
        if native:
            compiled = ha.HarnessAdapter._compile_prompt_sections(
                base, None, options, code_mode=True, codemode=codemode, registry=registry)
        else:
            # The CLI owns its preset prompt. Exercise the actual host append path,
            # not a reconstruction of the proprietary CLI prompt or a model call.
            compiled = SimpleNamespace(text=ha._delegated_append_text(base, options, registry))
        assert compiled is not None and compiled.text, "Prompt compilation failed"
        db_guard.assert_not_called()
        full = ha._code_mode_tool_sections(codemode, deferred=frozenset(), registry=registry)
        return compiled, full, specs


def measure():
    import re
    compiled, full, specs = compile_fixture()
    # Split combined base/workflow blocks at their existing headings, preserving
    # order and every character. Separators count toward the aggregate totals.
    system = "\n\n".join(s.text for s in compiled.sections if s.kind != "tools")
    sections = {}
    for index, text in enumerate(re.split(r"(?m)(?=^#{1,2} )", system)):
        if text:
            sections[f"{index:02d} {text.splitlines()[0]}"] = len(text)
    tools = {name: len(text) for text, name in full}
    full_text = system + "\n\n" + "\n\n".join(text for text, _ in full)
    return {
        "sections": sections, "tools": tools,
        "totals": {"system": len(system), "full_catalog": sum(tools.values()) + 2 * (len(tools) - 1),
                   "combined_full": len(full_text), "compiled_eager": len(compiled.text)},
        "tool_count": len(specs),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = measure()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Fixture: owner code chat; bundled builtins/app-config/chat-access/ai-tools; no external MCPs")
        print("Characters / approximate tokens (chars/4; not provider billing):")
        for group in ("sections", "tools", "totals"):
            print(f"\n{group}")
            for name, count in result[group].items():
                print(f"{name}: {count:,} chars / {count / 4:,.1f} tokens")


if __name__ == "__main__":
    main()
