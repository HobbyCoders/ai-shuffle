"""Shared ``pathlib.Path`` test double for worktree-cwd tests.

``FakePath`` lets a test control ``is_dir()`` via a class-owned set of
"existing" absolute paths, with no real filesystem. Patch it into every module
namespace whose ``Path`` binding the code under test uses, e.g.::

    import app.core.project_utils as pu
    monkeypatch.setattr(pu, "Path", FakePath)
    FakePath.existing = {"/abs/.worktrees/feat/foo"}

Reset ``FakePath.existing`` between tests (fixtures in the consuming test
modules do this) — it is class-level state.
"""


class FakePath:
    """Minimal ``Path`` stand-in: ``is_dir()`` is True iff the (stringified)
    path is in the test-owned ``FakePath.existing`` set."""

    existing: set = set()

    def __init__(self, p):
        self._p = str(p)

    def is_dir(self) -> bool:
        return self._p in FakePath.existing

    def __str__(self) -> str:
        return self._p
