"""Bound the requested tool-contract growth without making deferred tools eager."""
import json
from pathlib import Path

import pytest

from scripts.measure_prompt_catalog import compile_fixture, measure

# Baselines move only by a measured reason.
# Nov 2026: Remember and Forget gain the learner-proposal parameter (deferred
# on the delegated leg): delegated combined_full +371 (103,336 -> 103,707).
# Nov 2026: finer agent edits in the deferred app-config profile/subagent docs
# (see test_prompt_catalog_budget.py): combined_full +2,867 on both legs
# (native 133,602 -> 136,469; delegated 104,716 -> 107,583). Eager unchanged.
# Nov 2026: the claude CLI leg runs the compiled prompt (uniform app blocks,
# owner decision) instead of appending to Claude Code's: delegated
# compiled_eager 14,825 -> 21,324 and combined_full 107,583 -> 113,872 by the
# same growth (see test_prompt_catalog_budget.py). Native unchanged.
# Nov 2026: the delegated file-tool rules gain the pipe rule (net +68 after
# shortening their override clause): delegated compiled_eager 21,324 -> 21,392
# and combined_full 113,872 -> 113,940. Native unchanged.
# Nov 2026: the Vault secret hand-off adds the deferred ChatCopySecret tool and
# ChatHttpRequest's capture field (see test_prompt_catalog_budget.py):
# combined_full +1,042 on both legs (native 136,469 -> 137,511; delegated
# 113,940 -> 114,982). Native compiled_eager +16, still under its baseline;
# delegated eager unchanged.
# Nov 2026: Plan's deferred help says to send only changed tasks, so updates
# stop resending the whole plan; per-field limits shrink: Plan 2,005 -> 2,028.
FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "agent_tool_prefix_budget.json").read_text())


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_agent_tool_prefix_size_and_deferred_session_view(native):
    result = measure(native=native)
    mode = "native" if native else "delegated"
    for field, baseline in FIXTURE[mode].items():
        size = result["totals"][field]
        if field == "combined_full":
            # New capability, not an eager-prefix exemption: account for only
            # Plan's deferred entry (+ its join separator), bounded below.
            size -= result["tools"]["Plan"] + 2
        assert size <= baseline * (100 + FIXTURE["allowance_percent"]) // 100, (field, size)
    name = "SessionView" if native else "mcp__builtins__SessionView"
    assert result["tools"][name] <= FIXTURE["SessionView"] * 101 // 100
    compiled, catalog, specs = compile_fixture(native=native)
    spec = next(spec for spec in specs if spec.name == name)
    # The new paging instructions belong to the deferred tool, not every call.
    assert "text_offset" not in compiled.text
    assert "text_offset" in spec.input_schema["properties"]
    paragraphs = [paragraph.strip() for paragraph in spec.description.split("\n\n") if paragraph.strip()]
    assert len(paragraphs) == len(set(paragraphs))
    assert spec.description.count("Text previews") == 1
    assert sum(tool_name == name for _, tool_name in catalog) == 1


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_plan_help_is_bounded_and_deferred(native):
    from fluid.tools.builtins.plan import GUIDANCE

    compiled, catalog, specs = compile_fixture(native=native)
    docs = [text for text, name in catalog if name == "Plan"]
    assert len(docs) == 1
    assert len(docs[0]) <= FIXTURE["Plan"] * 101 // 100
    assert docs[0] not in compiled.text
    # When-to-plan is eager exactly once; all mechanics stay discoverable.
    assert len(GUIDANCE) <= 53
    expected = GUIDANCE if native else GUIDANCE.replace("use Plan", "use mcp__fluid_bundled__Plan")
    assert compiled.text.count(expected) == 1
    spec = next(s for s in specs if s.name == "Plan")
    assert spec.input_schema["properties"]["tasks"]["items"]["required"] == ["id", "title", "status"]
    paragraphs = [p.strip() for p in spec.description.split("\n\n") if p.strip()]
    assert len(paragraphs) == len(set(paragraphs))
    for contract in ("updates need expected_revision and unique request_id", "rollover=true",
                     "blocked needs blocked_reason", "cancelled is not success", "Never starts workers"):
        assert contract in docs[0] and contract not in compiled.text
    for field in ("depends_on", "blocked_reason", "evidence", "references"):
        assert field in docs[0]
    for limit in ("1-80 chars", "<=300 chars", "<=400 chars", "<=120 chars", "<=200 chars",
                  "<=10 ID strings", "<=2 strings", "<=3 strings", "<=100 chars each"):
        assert limit in docs[0]
