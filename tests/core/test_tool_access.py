"""Tool-access resolution and the live surface (rulings T1, T2, T5, T8).

The table test below is the contract every runtime path now reads through:
``tool_selection_mode`` decides, and an empty list is never "unrestricted".
"""

import pytest

from app.core.tool_access import (
    DENY_ALL,
    ToolAccess,
    fold_legacy_tool_switches,
    infer_tool_selection_mode,
    intersect_tool_surfaces,
    matches_any,
    resolve_tool_access,
    tool_permitted,
)


# =============================================================================
# T2: the four modes
# =============================================================================

@pytest.mark.parametrize(
    "config,expected",
    [
        # mode "all" — nothing restricts, whatever the lists say.
        ({"tool_selection_mode": "all"}, ToolAccess(None, None)),
        (
            {"tool_selection_mode": "all", "allowed_tools": ["Read"],
             "disallowed_tools": ["Bash"]},
            ToolAccess(None, None),
        ),
        # mode "none" — a real, total deny (the bug T2 exists to fix: this used
        # to be saved as allowed_tools=[] and read as unrestricted).
        ({"tool_selection_mode": "none"}, ToolAccess(None, [DENY_ALL])),
        (
            {"tool_selection_mode": "none", "allowed_tools": ["Read"]},
            ToolAccess(None, [DENY_ALL]),
        ),
        # mode "allow" — the saved list IS the surface.
        (
            {"tool_selection_mode": "allow", "allowed_tools": ["Read", "Grep"]},
            ToolAccess(["Read", "Grep"], None),
        ),
        # ... and an empty allow list degrades to "none", not to "all".
        (
            {"tool_selection_mode": "allow", "allowed_tools": []},
            ToolAccess(None, [DENY_ALL]),
        ),
        # mode "disallow" — the saved list is the blocklist.
        (
            {"tool_selection_mode": "disallow", "disallowed_tools": ["Bash"]},
            ToolAccess(None, ["Bash"]),
        ),
        (
            {"tool_selection_mode": "disallow", "disallowed_tools": []},
            ToolAccess(None, None),
        ),
        # An unknown mode falls back to the legacy inference.
        (
            {"tool_selection_mode": "sideways", "allowed_tools": ["Read"]},
            ToolAccess(["Read"], None),
        ),
    ],
)
def test_resolve_tool_access_modes(config, expected):
    assert resolve_tool_access(config) == expected


@pytest.mark.parametrize(
    "config,mode",
    [
        ({}, "all"),
        ({"allowed_tools": []}, "none"),
        ({"allowed_tools": ["Read"]}, "allow"),
        ({"disallowed_tools": ["Bash"]}, "disallow"),
        ({"allowed_tools": None, "disallowed_tools": []}, "all"),
    ],
)
def test_legacy_inference_matches_the_old_editor(config, mode):
    assert infer_tool_selection_mode(config) == mode


def test_unstamped_config_resolves_by_inference():
    """A profile the v82 migration never saw still resolves the way its author
    saw it in the old editor."""
    assert resolve_tool_access({"allowed_tools": []}) == ToolAccess(None, [DENY_ALL])
    assert resolve_tool_access({}) == ToolAccess(None, None)


def test_legacy_aliases_still_apply():
    access = resolve_tool_access(
        {"tool_selection_mode": "allow", "allowed_tools": ["PushNotification", "Read"]}
    )
    assert access.allowed == ["mcp__builtins__NotifyDevices", "Read"]


# =============================================================================
# Pattern matching
# =============================================================================

@pytest.mark.parametrize(
    "name,patterns,expected",
    [
        ("Read", ["Read"], True),
        ("Read", ["Write"], False),
        ("mcp__browser__navigate", ["mcp__browser__*"], True),
        ("mcp__browser__navigate", ["mcp__other__*"], False),
        ("anything", [DENY_ALL], True),
    ],
)
def test_matches_any(name, patterns, expected):
    assert matches_any(name, patterns) is expected


def test_tool_permitted_deny_beats_allow():
    assert tool_permitted("Bash", ["Bash", "Read"], None) is True
    assert tool_permitted("Bash", ["Bash", "Read"], ["Bash"]) is False
    assert tool_permitted("Bash", None, [DENY_ALL]) is False
    assert tool_permitted("Bash", None, None) is True


# =============================================================================
# T3: surface composition
# =============================================================================

def test_intersect_surfaces_keeps_the_narrower_side():
    assert intersect_tool_surfaces(None, None) is None
    assert intersect_tool_surfaces(["Read"], None) == ["Read"]
    assert intersect_tool_surfaces(None, ["Read"]) == ["Read"]
    assert intersect_tool_surfaces(["Read", "Bash"], ["Read", "Grep"]) == ["Read"]
    # A pattern on one side and an exact name on the other intersect to the
    # exact name, never to nothing.
    assert intersect_tool_surfaces(
        ["mcp__x__*"], ["mcp__x__foo"]
    ) == ["mcp__x__foo"]
    # No overlap is a real answer, reported as [] (callers turn it into a deny).
    assert intersect_tool_surfaces(["Read"], ["Bash"]) == []


# =============================================================================
# T5: the ai_tools / app_config_tools fold (what the v82 migration applies)
# =============================================================================

def test_fold_turns_off_ai_categories_into_disallows():
    folded = fold_legacy_tool_switches(
        {
            "allowed_tools": [],
            "tool_selection_mode": "all",
            "ai_tools": {"image_generation": True},
            "app_config_tools": True,
        }
    )
    assert folded["tool_selection_mode"] == "disallow"
    # generateImage survives; a category the user left off is denied by name.
    assert "mcp__ai-tools__generateImage" not in folded["disallowed_tools"]
    assert "mcp__ai-tools__generateVideo" in folded["disallowed_tools"]
    # app_config_tools was on, so the group is not denied.
    assert "mcp__app-config__*" not in folded["disallowed_tools"]


def test_fold_denies_app_config_when_the_switch_was_off():
    folded = fold_legacy_tool_switches({"app_config_tools": False})
    assert folded["tool_selection_mode"] == "disallow"
    assert "mcp__app-config__*" in folded["disallowed_tools"]


def test_fold_in_allow_mode_drops_disabled_ai_tools_from_the_list():
    folded = fold_legacy_tool_switches(
        {
            "tool_selection_mode": "allow",
            "allowed_tools": [
                "Read",
                "mcp__ai-tools__generateImage",
                "mcp__ai-tools__generateVideo",
            ],
            "ai_tools": {"image_generation": True},
            "app_config_tools": False,
        }
    )
    assert folded["tool_selection_mode"] == "allow"
    assert folded["allowed_tools"] == ["Read", "mcp__ai-tools__generateImage"]
    # Nothing is written to the deny list in allow mode — not being in the
    # allow list is already the restriction.
    assert "mcp__app-config__*" not in (folded.get("disallowed_tools") or [])


def test_fold_is_a_no_op_shape_for_a_clean_config():
    folded = fold_legacy_tool_switches(
        {"tool_selection_mode": "all", "app_config_tools": True}
    )
    assert folded["tool_selection_mode"] == "all"
    assert not folded.get("disallowed_tools")
