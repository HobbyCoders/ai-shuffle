"""Settings importer edges: MCP server sanitizing (validation, malformed URLs,
embedded credentials), the digest helpers' rejections, review sidecars."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fluid.context import settings_importer as si
from fluid.context.settings_import import preview_settings_import


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("name, raw, note", [
    ("", {"command": "x"}, "must be non-empty"),
    ("s", "not-an-object", "must be non-empty"),
    ("s", {"args": []}, "requires a string command or url"),
    ("s", {"command": "x", "url": "https://h/"}, "cannot specify both"),
    ("s", {"command": "x", "args": "--flag"}, "args must be a list of strings"),
    ("s", {"command": "x", "args": [1]}, "args must be a list of strings"),
    ("s", {"command": "run --password=hunter2"}, "embeds a credential"),
    ("s", {"url": "https://h/p/token=abc"}, "malformed or carries a credential"),
    ("s", {"url": "http://[oops/mcp"}, "malformed or carries a credential"),
])
def test_sanitize_server_rejections(name, raw, note) -> None:
    clean, message = si._sanitize_server(name, raw)
    assert clean is None and note in message


def test_malformed_url_values_never_crash_and_are_not_copied() -> None:
    clean, note = si._sanitize_server("s", {
        "command": "x",
        "args": ["plain", "http://[oops", "https://u:p@example.test/"],
        "env": {"ENDPOINT": "http://[bad", "MODE": "fast"},
    })
    assert clean["args"] == ["plain", "${S_ARG1}", "${S_ARG2}"]
    assert clean["env"] == {"ENDPOINT": "${S_ENDPOINT}", "MODE": "fast"}
    assert "set before use" in note


def test_malformed_url_in_a_claude_json_does_not_abort_the_preview(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _write(source / ".claude.json", json.dumps({"mcpServers": {
        "broken": {"url": "http://[oops/mcp"},
        "fine": {"command": "srv", "args": ["http://[x"]},
    }}))
    preview = preview_settings_import(
        sources=("claude",), source_home=source, destination_home=tmp_path / "dst"
    )
    mcp = next(item for item in preview.items if item.kind == "global_mcp")
    payload = json.loads(mcp._content)["mcpServers"]
    assert payload == {"fine": {"command": "srv", "args": ["${FINE_ARG0}"]}}
    assert any("broken" in item.message for item in preview.items if item.status == "diagnostic")


def test_sanitize_server_passes_flags_and_notes_dropped_fields() -> None:
    clean, note = si._sanitize_server("srv", {
        "url": "https://example.test/mcp",
        "type": "sse",
        "cwd": "/work",
        "disabled": True,
        "enabled": "yes",           # not a bool: dropped silently
        "headers": ["not", "a", "map"],
        "clientSecret": "x",
        "timeout": 5,
    })
    assert clean == {
        "url": "https://example.test/mcp", "type": "sse", "cwd": "/work", "disabled": True,
    }
    assert "sse" in note and "headers must be an object" in note
    assert "credential field(s) omitted: clientSecret" in note
    assert "unsupported field(s) omitted: timeout" in note


def test_redact_args_flag_at_end_and_secret_word_keys() -> None:
    placeholders: list[str] = []
    out = si._redact_args("s", ["--api-key"], placeholders)
    assert out == ["--api-key"] and placeholders == []  # nothing followed the flag
    out = si._redact_args("s", ["--verbose", "a b=c", "AUTH_TOKEN=zzz"], placeholders)
    assert out == ["--verbose", "a b=c", "AUTH_TOKEN=${S_AUTH_TOKEN}"]


def test_redact_map_none_value_becomes_empty() -> None:
    assert si._redact_map("s", {"MODE": None}, []) == {"MODE": ""}


# ─── digest helpers ──────────────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_tree_digest_rejects_special_files_and_symlinks(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    _write(tree / "a.txt", "a")
    assert si._tree_digest(tree) is not None
    assert si._stat_signature_digest(tree) is not None
    os.mkfifo(tree / "pipe")
    assert si._tree_digest(tree) is None

    linked = tmp_path / "l"
    _write(linked / "a.txt", "a")
    (linked / "ln").symlink_to(linked / "a.txt")
    assert si._tree_digest(linked) is None
    assert si._stat_signature_digest(linked) is None
    assert si._tree_is_symlink_free(linked) is False
    assert si._tree_is_symlink_free(tmp_path / "t") is True


def test_digest_helpers_reject_non_directories_and_unreadable(tmp_path: Path, monkeypatch) -> None:
    f = _write(tmp_path / "f.txt", "x")
    assert si._tree_digest(f) is None
    assert si._stat_signature_digest(f) is None
    assert si._file_digest(tmp_path) is None
    assert si._normalized_text_digest(tmp_path / "missing.md") is None

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(si.os, "walk", boom)
    assert si._tree_digest(tmp_path) is None
    assert si._stat_signature_digest(tmp_path) is None
    assert si._tree_is_symlink_free(tmp_path) is False


def test_normalized_text_digest_ignores_line_endings_and_trailing_space(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.md", "x  \r\ny\r\n\n")
    b = _write(tmp_path / "b.md", "x\ny")
    assert si._normalized_text_digest(a) == si._normalized_text_digest(b)


def test_read_toml_and_json_errors(tmp_path: Path) -> None:
    bad = _write(tmp_path / "c.toml", "not = [valid")
    raw, error = si._read_toml(bad)
    assert raw is None and "could not parse TOML" in error
    lst = _write(tmp_path / "c.json", "[]")
    assert si._read_json(lst) == (None, "JSON root must be an object")


# ─── review sidecars ─────────────────────────────────────────────────────────


def _instruction_item(tmp_path: Path, text: str = "import me\n"):
    source = _write(tmp_path / "src" / "CLAUDE.md", text)
    return si._item(
        "claude", "global_instruction", (source,), tmp_path / "dst" / ".fluid" / "AGENTS.md",
        provenance={"normalized_text_digest": si._normalized_text_digest(source)},
    )


def test_review_sidecar_states(tmp_path: Path) -> None:
    item = _instruction_item(tmp_path)
    fluid_dest = tmp_path / "dst" / ".fluid" / "AGENTS.md"
    sidecar = si._instruction_sidecar(item, fluid_dest)
    assert sidecar.parent == tmp_path / "dst" / ".fluid" / "imports" / "claude"

    ready = si._as_review_sidecar(item, fluid_dest)
    assert ready.status == "ready" and ready.destination == str(sidecar)

    _write(sidecar, "import me\n")
    assert si._as_review_sidecar(item, fluid_dest).status == "duplicate"
    sidecar.write_text("something else\n")
    assert si._as_review_sidecar(item, fluid_dest).status == "conflict"

    agents_dest = tmp_path / "proj" / ".agents" / "AGENTS.md"
    assert si._instruction_sidecar(item, agents_dest).parent == tmp_path / "proj" / ".agents" / "imports" / "claude"
    root_dest = tmp_path / "proj" / "AGENTS.md"
    assert si._instruction_sidecar(item, root_dest).parent == tmp_path / "proj" / ".agents" / "imports" / "claude"


def test_review_sidecar_for_non_instruction_is_a_conflict(tmp_path: Path) -> None:
    source = _write(tmp_path / "agent.md", "a")
    item = si._item("claude", "global_agent", (source,), tmp_path / "dst" / "agent.md")
    outcome = si._as_review_sidecar(item, tmp_path / "dst" / "agent.md")
    assert outcome.status == "conflict" and "native destination already exists" in outcome.message


def test_review_sidecar_under_a_symlinked_parent_is_rejected(tmp_path: Path) -> None:
    item = _instruction_item(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    outcome = si._as_review_sidecar(item, tmp_path / "linked" / ".fluid" / "AGENTS.md")
    assert outcome.status == "skipped" and "symlink ancestor" in outcome.message


def test_import_settings_previews_by_default(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _write(source / ".claude" / "CLAUDE.md", "hi\n")
    preview = si.import_settings(sources=("claude",), source_home=source, destination_home=tmp_path / "d")
    assert isinstance(preview, si.SettingsImportPreview)
    result = si.import_settings(
        apply=True, selected_ids=(), sources=("claude",), source_home=source,
        destination_home=tmp_path / "d",
    )
    assert isinstance(result, si.SettingsImportResult)
    assert not (tmp_path / "d" / ".fluid" / "AGENTS.md").exists()
