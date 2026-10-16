"""Restrict directory collection to one full-gate batch without long CLI args.

The small allowlist fits Windows' command/environment limits. Returning None
for selected files preserves every other plugin/conftest's collection rules.
Paths are relative to the directory pytest was started in (the checkout root),
not its rootdir: fluid/tests/unit has its own rootdir (fluid/pyproject.toml).
"""
import json
import os


def pytest_configure(config):
    config._local_ci_backend_batch = set(json.loads(os.environ["LOCAL_CI_BACKEND_BATCH"]))


def pytest_ignore_collect(collection_path, config):
    if collection_path.name.startswith("test_") and collection_path.suffix == ".py":
        relative = collection_path.relative_to(config.invocation_params.dir).as_posix()
        if relative not in config._local_ci_backend_batch:
            return True
    return None
