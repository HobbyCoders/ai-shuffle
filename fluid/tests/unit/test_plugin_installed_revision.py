"""The runtime must use the installed revision, not hash/name ordering."""

import json
from pathlib import Path

import pytest

from fluid.context.plugins import resolve_plugin_root


@pytest.mark.parametrize("legacy", [False, True])
def test_installed_record_selects_current_hash_not_lexicographic_max(tmp_path, legacy):
    home = tmp_path / "plugins"
    cache = home / "cache/source/sample"
    old, current = cache / "fffffff", cache / "0000000"
    old.mkdir(parents=True)
    current.mkdir()
    record = {"installPath": str(current), "version": "0000000"}
    (home / "installed_plugins.json").write_text(json.dumps({
        "version": 1 if legacy else 2,
        "plugins": {"sample@source": record if legacy else [record]},
    }))
    assert resolve_plugin_root("sample", "source", home=home) == (current, "0000000")


@pytest.mark.parametrize("bad_path", ["outside", "missing", "nested"])
def test_installed_path_cannot_escape_cache_or_load_an_old_revision(tmp_path, bad_path):
    home = tmp_path / "plugins"
    cache = home / "cache/source/sample"
    (cache / "old").mkdir(parents=True)
    path = {"outside": tmp_path / "outside", "missing": cache / "missing", "nested": cache / "old/nested"}[bad_path]
    if bad_path != "missing":
        path.mkdir(parents=True)
    (home / "installed_plugins.json").write_text(json.dumps({
        "plugins": {"sample@source": [{"installPath": str(path), "version": "current"}]},
    }))
    assert resolve_plugin_root("sample", "source", home=home) is None
