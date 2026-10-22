"""Use shared scratch even when Fluid's separate root hides the parent conftest."""
# Re-export the tryfirst hook rather than using addopts (local CI clears them)
# or pytest_plugins (invalid when this conftest is nested in a combined run).
try:
    from scripts.ci.test_scratch import pytest_configure  # noqa: F401
except ModuleNotFoundError as exc:
    # Fluid can be tested on its own, without the surrounding app repository.
    # Do not hide broken dependencies inside an available shared hook.
    if exc.name not in {"scripts", "scripts.ci", "scripts.ci.test_scratch"}:
        raise
