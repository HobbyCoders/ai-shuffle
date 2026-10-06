"""A test that installs the turn scheduler does not leak its seams (#1892).

The two tests run in file order on one worker: the first installs the
scheduler and deliberately does not clean up; the second must still start
with no kick, no settlement hook and no interrupt executor.
"""

from app.core import session_runtime, turn_manager, turn_scheduler


def test_install_without_cleanup():
    turn_scheduler.install()
    turn_manager.register_interrupt_executor(lambda *a, **k: None)
    assert turn_manager._kick_hook is turn_scheduler.kick


def test_next_test_starts_without_scheduler_seams():
    assert turn_manager._kick_hook is None
    assert turn_manager._interrupt_executor is None
    assert session_runtime._on_turn_settled is None
