"""Bounded updater identity, warm output and refresh regressions (no real database)."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from docker.application_updates import ContainerUpdates

A, B = "a" * 40, "b" * 40


@pytest.fixture
def updater(tmp_path, monkeypatch):
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    monkeypatch.delenv("SHUFFLE_SOURCE_SUPERVISOR", raising=False)
    monkeypatch.delenv("SHUFFLE_AGENT_ISOLATION", raising=False)
    return ContainerUpdates(tmp_path / "updates", {"revision": A, "sequence": 1, "runtime": "test"},
                            b"", lambda: True, lambda _: {"active_sessions": 0})


def test_resident_revision_is_not_app_revision(updater, monkeypatch):
    monkeypatch.setenv("GIT_SHA", B)
    updater.publish("idle")
    state = json.loads((updater.root / "status.json").read_text())
    assert state["updater"]["revision"] == A
    assert "stage-diagnostics-v1" in state["updater"]["capabilities"]


def test_warm_tools_output_skips_second_build(updater, tmp_path, monkeypatch):
    calls = []
    def build(command, cwd, env):
        calls.append(command)
        (cwd / "dist").mkdir(exist_ok=True)
        (cwd / "dist/index.js").write_text("compiled")
    monkeypatch.setattr(updater, "run_build", build)
    for revision in (A, B):
        source = tmp_path / revision
        package = source / "tools"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"scripts":{"build":"tsc"}}')
        (package / "package-lock.json").write_text("{}")
        (package / "index.ts").write_text("source")
        updater.node_output("tools", source, {}, {"node": "same"})
        assert (package / "dist/index.js").read_text() == "compiled"
    assert len(calls) == 1
    # Running revisions are copies: corrupting a warm cache cannot mutate one.
    cached = next((updater.root / "dependencies/builds/tools").iterdir())
    (cached / "dist/index.js").write_text("damaged")
    assert (tmp_path / A / "tools/dist/index.js").read_text() == "compiled"
    source = tmp_path / "third"
    import shutil
    shutil.copytree(tmp_path / A, source, ignore=shutil.ignore_patterns("dist"))
    updater.node_output("tools", source, {}, {"node": "same"})
    assert len(calls) == 2
    assert (source / "tools/dist/index.js").read_text() == "compiled"


@pytest.mark.parametrize("change", ["source", "lock", "script", "config", "identity", "disabled"])
def test_output_rebuilds_for_changed_inputs(updater, tmp_path, monkeypatch, change):
    import shutil
    first, second = tmp_path / "first", tmp_path / "second"
    (first / "browser-mcp").mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "input.ts"):
        (first / "browser-mcp" / name).write_text("original")
    def build(command, cwd, env):
        (cwd / "dist").mkdir()
        (cwd / "dist/index.js").write_text("compiled")
    run = Mock(side_effect=build)
    monkeypatch.setattr(updater, "run_build", run)
    updater.node_output("browser-mcp", first, {}, {"node": "one"})
    shutil.copytree(first, second, ignore=shutil.ignore_patterns("dist"))
    field = {"source": "browser-mcp/input.ts", "lock": "browser-mcp/package-lock.json",
             "script": "browser-mcp/package.json", "config": "config.json"}.get(change)
    if field:
        (second / field).write_text("changed")
    if change == "disabled":
        monkeypatch.setenv("AI_SHUFFLE_PERF_DOCKER_BUILD_REUSE", "off")
    updater.node_output("browser-mcp", second, {}, {"node": "two" if change == "identity" else "one"})
    assert run.call_count == 2


def test_command_timings_are_monotonic_and_do_not_publish_commands(updater, tmp_path, monkeypatch):
    from docker import application_updates as module
    updater.begin_diagnostics(B)
    # Epoch jumps must not affect the durations.
    monkeypatch.setattr(module.time, "time", lambda: -10**9)
    updater.run_build([sys.executable, "-c", "pass", "private-command"], tmp_path, {"PRIVATE_ENV": "secret"})
    with pytest.raises(module.UpdateError):
        updater.run_build([sys.executable, "-c", "raise SystemExit(2)", "private-error"], tmp_path, {})
    updater.publish("error")
    state = json.loads((updater.root / "status.json").read_text())
    assert [s["outcome"] for s in state["diagnostics"]["stages"]] == ["completed", "failed"]
    assert all(s["elapsed_ms"] >= 0 for s in state["diagnostics"]["stages"])
    assert not any(word in json.dumps(state) for word in ("private", "secret", "PRIVATE_ENV"))
    recovered = ContainerUpdates(updater.root, updater.image, b"", lambda: True, lambda _: {})
    assert recovered.diagnostics == updater.diagnostics


def test_bounded_diagnostics_and_failed_logging_cannot_break_update(updater, monkeypatch):
    from docker import application_updates as module
    monkeypatch.setattr(module, "_log", Mock(side_effect=OSError("private")))
    updater.begin_diagnostics(B)
    for _ in range(110):
        with updater.stage("command"):
            pass
    assert len(updater.diagnostics["stages"]) == 96


def test_signed_producing_path_reports_dependency_and_prebuilt_results(build_feed, feed):
    from tests.test_prebuilt_ui_bundle import bundle, serve, VERSION
    from tests.test_application_updates import B
    updater, stage, _ = build_feed
    data, ui = bundle()
    serve(feed, data, ui)
    stage(B, 2, ui=ui, version=VERSION)
    status = json.loads((updater.root / "status.json").read_text())
    stages = status["diagnostics"]["stages"]
    assert status["diagnostics"]["revision"] == B
    assert any(s["stage"] == "prebuilt_ui" and s["outcome"] == "hit" for s in stages)
    assert any(s["stage"] == "python_cache" and s["outcome"] == "miss" for s in stages)
    assert any(s["stage"] == "tools_cache" and s["outcome"] == "miss" for s in stages)


from tests.test_application_updates import feed
from tests.test_docker_dependency_reuse import build_feed
