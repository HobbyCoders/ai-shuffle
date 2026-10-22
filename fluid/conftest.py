"""Use shared scratch even when Fluid's separate root hides the parent conftest."""
# Re-export the tryfirst hook rather than using addopts (local CI clears them)
# or pytest_plugins (invalid when this conftest is nested in a combined run).
from scripts.ci.test_scratch import pytest_configure  # noqa: F401
