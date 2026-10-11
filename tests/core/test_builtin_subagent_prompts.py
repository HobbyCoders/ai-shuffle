"""What the built-in worker prompts have to say after the round-7 audit.

A worker in e8ece32a returned the summary "Findings" (the registry takes the
report's first line, ``provider_agent._completion_summary``); and
``frontend-designer`` was handed a pure logic helper twice because it lives
under ``components/``.
"""

from __future__ import annotations

import pytest

from app.core.builtin_subagents import (
    BUILTIN_SUBAGENTS,
    get_builtin_subagent_definition,
)


from app.core.agent_templates import STARTER_TEAM_AGENTS, FRONTEND_DESIGNER_PROMPT, GENERAL_PURPOSE_PROMPT


def _definition(agent_id):
    definition = next((agent for agent in STARTER_TEAM_AGENTS if agent["id"] == agent_id), None)
    assert definition is not None
    return definition


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
def test_retired_agents_are_not_builtins(retired_id):
    assert get_builtin_subagent_definition(retired_id) is None
    assert BUILTIN_SUBAGENTS == []


# ─── every worker: the first line IS the summary ─────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param(GENERAL_PURPOSE_PROMPT, id="general-purpose"),
        pytest.param(FRONTEND_DESIGNER_PROMPT, id="frontend-designer"),
    ],
)
def test_the_first_line_must_be_a_sentence_not_a_heading(prompt):
    lowered = prompt.lower()
    assert "never a heading" in lowered
    assert "first line" in lowered or "open with" in lowered
    # and it says WHY: the caller only ever sees that line.
    assert "summary" in lowered


# ─── general purpose: report what was actually exercised ─────────────────────


@pytest.mark.parametrize(
    "clause",
    [
        "not the same as the change working in the running app",
        "seeds the value your change reads",
        "rather than calling it verified",
    ],
)
def test_general_purpose_reports_verification_honestly(clause):
    assert clause in GENERAL_PURPOSE_PROMPT


def test_general_purpose_completes_assigned_investigation_and_review():
    assert "Implementation is your main job, but do the assigned work fully" in GENERAL_PURPOSE_PROMPT
    for work in ("investigation", "root-cause diagnosis", "code or PR review", "audits", "reproductions"):
        assert work in GENERAL_PURPOSE_PROMPT
    assert "and report your findings" in GENERAL_PURPOSE_PROMPT
    assert "Never refuse an assignment merely to tell the caller to use another agent" in GENERAL_PURPOSE_PROMPT
    assert "after doing the work" in GENERAL_PURPOSE_PROMPT
    for refusal in (
        "Your job is to change code, not to answer questions",
        "instead of doing that work here",
        "belong to the Explore agent",
    ):
        assert refusal not in GENERAL_PURPOSE_PROMPT


def test_general_purpose_honours_report_only_assignments():
    assert "A report-only assignment with no edits or commits is valid; honour it as given" in GENERAL_PURPOSE_PROMPT
    assert "Do not commit, push, or open pull requests unless the task explicitly asks for it" in GENERAL_PURPOSE_PROMPT


def test_general_purpose_reports_missing_ui_render_verification():
    assert "For rendered UI work, render-verify it" in GENERAL_PURPOSE_PROMPT
    assert "if you cannot, state in your report that the UI was not render-verified" in GENERAL_PURPOSE_PROMPT
    assert "sole agent responsible for all frontend" not in FRONTEND_DESIGNER_PROMPT


def test_general_purpose_description_advises_callers_without_forbidding_work():
    description = _definition("general-purpose")["description"]
    for work in ("investigations", "root-cause diagnosis", "code or PR reviews", "audits", "reproductions"):
        assert work in description
    assert "report-only work without edits" in description
    assert "Searches and investigations can be done by the calling agent or assigned here" in description
    assert "Explore" not in description
    assert "Plan" not in description
    assert "rendered UI work fits Frontend Designer" in description
    assert "routing advice, not a restriction on assigned work" in description
    assert "Do NOT use it" not in description
    assert "Give it a concrete change to make" not in description


# ─── frontend designer: routed on what the file produces ─────────────────────


def test_frontend_designer_prompt_disclaims_logic_helpers():
    assert "## Scope — what is NOT yours" in FRONTEND_DESIGNER_PROMPT
    assert "no markup and no styling" in FRONTEND_DESIGNER_PROMPT
    assert "belongs to the General Purpose agent" in FRONTEND_DESIGNER_PROMPT
    assert "The directory is not the test" in FRONTEND_DESIGNER_PROMPT


def test_frontend_designer_description_routes_on_output_not_directory():
    """The description is what the dispatching model reads, so the carve-out
    has to be there and not only in the prompt."""
    description = _definition("frontend-designer")["description"]
    assert "not where it lives" in description
    assert "components/" in description
    assert "General Purpose work" in description


def test_the_visual_default_survives():
    description = _definition("frontend-designer")["description"]
    assert "default agent for anything visual" in description


# ─── the definitions still load ──────────────────────────────────────────────


def test_every_starter_template_has_a_prompt_and_a_description():
    assert len(STARTER_TEAM_AGENTS) == 2
    for definition in STARTER_TEAM_AGENTS:
        assert definition["prompt"].strip()
        assert definition["description"].strip()
