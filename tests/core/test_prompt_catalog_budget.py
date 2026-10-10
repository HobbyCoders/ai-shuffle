"""Budget the real host-compiled prefix, not hand-maintained prompt copies."""
import pytest

from scripts.measure_prompt_catalog import compile_fixture, measure


# Measured with scripts/measure_prompt_catalog.py; chars/4 is only an estimate.
# origin/main da6b00497: full=141166, eager=107085 characters.
MEASURED_FULL = 128_752
MEASURED_EAGER = 94_671


def test_prompt_catalog_budget():
    totals = measure()["totals"]
    for name, baseline in (("combined_full", MEASURED_FULL), ("compiled_eager", MEASURED_EAGER)):
        ceiling = baseline * 110 // 100
        assert totals[name] <= ceiling, (
            f"{name}: {totals[name]:,} chars exceeds {ceiling:,} (measured size + 10%). "
            "Run PYTHONPATH=fluid python scripts/measure_prompt_catalog.py, measure "
            "and justify the growth before updating this budget; do not drop rules."
        )


@pytest.mark.parametrize("native", [True, False], ids=["native-code", "delegated-cli"])
def test_workflow_duplicates_have_one_owner(native):
    from fluid.tools.fragments import SWARM_ROUTING, DELEGATION_SIZING, SUBAGENT_WORKTREE_GUIDANCE
    from fluid.tools.builtins.session_orchestration import _COMMON

    compiled, catalog, specs = compile_fixture(native=native)
    # Native compiled text already includes its eager tool catalog.
    system = ("\n\n".join(s.text for s in compiled.sections if s.kind != "tools")
              if native else compiled.text)
    combined = system + "\n\n" + "\n\n".join(text for text, _ in catalog)
    routing = SWARM_ROUTING if native else DELEGATION_SIZING
    for paragraph in (routing, SUBAGENT_WORKTREE_GUIDANCE, _COMMON.strip()):
        assert combined.count(paragraph) == 1, paragraph
    assert combined.count("Host safety ceilings (from configuration):") == 1
    assert system.count(routing) == 1
    if not native:
        assert "Swarm" not in system
    assert system.count(SUBAGENT_WORKTREE_GUIDANCE) == 1
    assert "never override a restriction on delegation" in combined
    assert "Never run `git worktree remove`" in combined
    by_leaf = {s.name.rsplit("__", 1)[-1]: s for s in specs}
    assert "Follow the system prompt's delegation and checkout rules" in by_leaf["Task"].description
    assert "Follow MessageSession's peer-coordination rules" in by_leaf["StartSession"].description
    assert "Follow MessageSession's peer-coordination rules" in by_leaf["ListSessions"].description
    for phrase in ("24 hours", "cancelled/expired cards", "Docker: container path"):
        assert phrase in by_leaf["RequestChatAccess"].description
    assert "detached processes may survive" in by_leaf["ChatRunCommand"].description


def test_measurement_is_repeatable_and_deferred_catalog_is_not_sent():
    first = measure()
    assert measure() == first
    compiled, catalog, specs = compile_fixture()
    assert len(specs) == 103
    assert first["totals"]["compiled_eager"] < first["totals"]["combined_full"]
    assert "# Deferred tools" in compiled.text
    # Discovery remains available; full schemas remain measurable on demand.
    assert any(s.name == "ToolSearch" for s in specs)
    assert any(name == "mcp__app-config__createAutomation" for _, name in catalog)
