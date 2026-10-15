"""Budget the real host-compiled prefix, not hand-maintained prompt copies."""
import pytest

from scripts.measure_prompt_catalog import compile_fixture, measure


# Measured with scripts/measure_prompt_catalog.py; chars/4 is only an estimate.
# Canonical CI recipe: native full=129070, eager=94923; delegated append=16307.
MEASURED_FULL = 129_070
MEASURED_EAGER = 94_923
MEASURED_DELEGATED_APPEND = 16_307


def test_prompt_catalog_budget():
    totals = measure()["totals"]
    for name, baseline in (("combined_full", MEASURED_FULL), ("compiled_eager", MEASURED_EAGER)):
        ceiling = baseline * 101 // 100
        assert totals[name] <= ceiling, (
            f"{name}: {totals[name]:,} chars exceeds {ceiling:,} (measured size + 1%). "
            "Run PYTHONPATH=fluid python scripts/measure_prompt_catalog.py, measure "
            "and justify the growth before updating this budget; do not drop rules."
        )


def test_delegated_append_budget():
    size = measure(native=False)["totals"]["compiled_eager"]
    ceiling = MEASURED_DELEGATED_APPEND * 101 // 100
    assert size <= ceiling, (
        f"Delegated append: {size:,} chars exceeds {ceiling:,}. "
        "Run PYTHONPATH=fluid python scripts/measure_prompt_catalog.py --delegated; "
        "tighten wording without dropping rules or raising the budget."
    )


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_private_access_guidance_has_one_owner(native):
    from app.mcp.chat_access_sdk import GUIDANCE

    compiled, catalog, _ = compile_fixture(native=native)
    system = ("\n\n".join(s.text for s in compiled.sections if s.kind != "tools")
              if native else compiled.text)
    combined = system + "\n\n" + "\n\n".join(text for text, _ in catalog)
    assert system.count(GUIDANCE.strip()) == 1
    assert combined.count("# Private resource access") == 1
    # Saved-entry discovery must not crowd out privacy or per-session approval.
    for rule in (
        "saved IDs, kinds and owner-chosen labels, never addresses or",
        "Reuse via saved_id; each session needs its own grant",
        "Never ask for addresses, passwords, private keys, API keys, tokens",
        "Never reveal private inputs or put secrets in tool arguments",
        "Initial access requires card approval",
        "Grants expire and are lost on restart",
        "scope, expiry and revocation apply in every permission mode",
        "skip confirmation only in bypass",
        "otherwise each needs a one-shot card decision",
    ):
        assert rule in system


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


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_ci_recipe_has_one_owner(native):
    from fluid.tools.fragments import CI_WAIT_GUIDANCE
    compiled, catalog, _ = compile_fixture(native=native)
    system = ("\n\n".join(s.text for s in compiled.sections if s.kind != "tools")
              if native else compiled.text)
    combined = system + "\n\n" + "\n\n".join(text for text, _ in catalog)
    assert combined.count(CI_WAIT_GUIDANCE) == 1
    assert combined.count("gh pr checks <PR> --watch --fail-fast") == 1
    assert "echo checks:" not in combined
    assert "gh pr checks" not in system
    assert "failed or cancelled checks must return non-zero" in combined


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
