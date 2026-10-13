"""Orphan task managers on the delegated surface — round-6 audit.

TaskGet/TaskList/TaskOutput/TaskStop can only report on, read or stop work that
Task or Monitor started; none of them starts anything. An audited delegated
session ran with Task disabled and still carried all four, so the surface
advertised four tools whose only possible answer was "no tasks". They stay
whenever EITHER producer is present — they are the Monitor's companions as much
as Task's — and drop only when there is nothing left to manage.
"""

from app.core.query_options import (
    _BACKGROUND_PRODUCERS,
    _TASK_MANAGER_TOOLS,
    _drop_orphan_task_managers,
)

_NAMES = [
    "mcp__builtins__Task",
    "mcp__builtins__Monitor",
    "mcp__builtins__TaskGet",
    "mcp__builtins__TaskList",
    "mcp__builtins__TaskOutput",
    "mcp__builtins__TaskStop",
    "mcp__builtins__ScheduleWakeup",
]


def _names(allowed=None, disallowed=None):
    return _drop_orphan_task_managers(list(_NAMES), allowed, disallowed)


def test_an_unrestricted_surface_is_untouched():
    assert _names() == _NAMES


def test_monitor_alone_keeps_its_companions():
    # The audited configuration: Task off, Monitor on.
    kept = _names(disallowed=["mcp__builtins__Task", "Task"])

    for name in _TASK_MANAGER_TOOLS:
        assert name in kept


def test_task_alone_keeps_them_too():
    kept = _names(disallowed=["mcp__builtins__Monitor", "Monitor"])

    for name in _TASK_MANAGER_TOOLS:
        assert name in kept


def test_no_producer_drops_every_manager():
    kept = _names(disallowed=list(_BACKGROUND_PRODUCERS))

    assert not [name for name in kept if name in _TASK_MANAGER_TOOLS]
    # Nothing else is touched.
    assert "mcp__builtins__ScheduleWakeup" in kept


def test_a_native_leg_counts_the_bare_names():
    # A native session runs fluid's bare Monitor while keeping these MCP
    # copies; the bridged Monitor name is shadowed, not permitted.
    kept = _drop_orphan_task_managers(
        list(_NAMES),
        ["Monitor", "mcp__builtins__TaskOutput", "mcp__builtins__TaskStop"],
        None,
    )

    assert "mcp__builtins__TaskOutput" in kept
    assert "mcp__builtins__TaskStop" in kept


def test_a_deny_all_surface_drops_them():
    assert not [
        name for name in _names(disallowed=["*"]) if name in _TASK_MANAGER_TOOLS
    ]
