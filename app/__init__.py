"""
AI Shuffle backend.
"""

# Checked-in fallback version, used when no APP_VERSION is supplied (source
# runs, dev builds). Release builds take the version from the git tag instead.
# Keep it equal to the latest release tag; tests/core/test_version_source.py
# checks it against the frontend, desktop and Android fallbacks.
__version__ = "0.9.4"
