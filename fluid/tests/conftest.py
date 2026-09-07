"""Shared test setup.

Every test runs with HOME (and USERPROFILE, which is what ``expanduser`` reads
on Windows) pointed at an empty tmp directory. Discovery is filesystem-native
by design — a NativeLoop built with no explicit cwd reads ``~/.claude`` for
settings (CE4) and enabled plugins (CE5), and a plugin's MCP servers would then
really be spawned from a unit test. Tests that need a Claude setup build one
under their own tmp home and re-point HOME themselves; their ``monkeypatch``
runs after this fixture, so it wins.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home
