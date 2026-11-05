"""Bound the requested tool-contract growth without making deferred tools eager."""
import json
from pathlib import Path

import pytest

from scripts.measure_prompt_catalog import compile_fixture, measure

# Baselines move only by a measured reason.
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
    for limit in ("1-80 chars", "<=600 chars", "<=1000 chars", "<=200 chars",
                  "<=10 ID strings", "<=3 strings", "<=5 strings", "<=100 chars each"):
        assert limit in docs[0]
