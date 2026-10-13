"""
Regression tests: legacy tool-name aliases are resolved at config-read time.

The builtin ``PushNotification`` leaf tool was renamed to ``NotifyDevices`` to
break a collision with a bare stub of the same name (issue #610). Existing saved
profiles may still list the legacy names in ``allowed_tools`` /
``disallowed_tools``; those must resolve to ``mcp__builtins__NotifyDevices`` at
read time WITHOUT any DB migration.

``_apply_legacy_tool_aliases`` is the small mapping that does this; these tests
pin its behavior directly.
"""

import app.core.query_options as qo


def test_bare_legacy_name_maps_to_new_builtin():
    result = qo._apply_legacy_tool_aliases(["PushNotification"])
    assert result == ["mcp__builtins__NotifyDevices"]


def test_prefixed_legacy_name_maps_to_new_builtin():
    result = qo._apply_legacy_tool_aliases(["mcp__builtins__PushNotification"])
    assert result == ["mcp__builtins__NotifyDevices"]


def test_new_name_passes_through_unchanged():
    result = qo._apply_legacy_tool_aliases(["mcp__builtins__NotifyDevices"])
    assert result == ["mcp__builtins__NotifyDevices"]


def test_unrelated_names_untouched_and_order_preserved():
    names = ["Read", "PushNotification", "Bash"]
    result = qo._apply_legacy_tool_aliases(names)
    assert result == ["Read", "mcp__builtins__NotifyDevices", "Bash"]


def test_no_duplicate_when_both_legacy_and_new_present():
    names = ["mcp__builtins__NotifyDevices", "PushNotification"]
    result = qo._apply_legacy_tool_aliases(names)
    assert result == ["mcp__builtins__NotifyDevices"]


def test_empty_list_returns_empty():
    assert qo._apply_legacy_tool_aliases([]) == []
