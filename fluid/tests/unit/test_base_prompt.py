"""The CLI's default base system prompt and its wiring (fluid.cli.prompt)."""

from __future__ import annotations

from fluid.cli.config import resolved_prompt, resolved_system
from fluid.cli.prompt import CODE_MODE_ADDENDUM, DEFAULT_BASE_PROMPT
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge, env_overrides, load_config
from fluid.config.model import HarnessConfig


def _config(tmp_path, **overrides) -> HarnessConfig:
    return HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), **overrides})
    )


def test_cli_prompt_starts_with_the_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    compiled = resolved_prompt(_config(tmp_path))
    assert compiled.sections[0].kind == "base"
    assert compiled.sections[0].text == DEFAULT_BASE_PROMPT.rstrip()
    assert (compiled.text or "").startswith("# fluid")
    # The base prompt points at the environment block, so it must be present.
    assert compiled.of_kind("environment")
    assert "# Environment" in (compiled.text or "")


def test_config_system_replaces_the_default_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    compiled = resolved_prompt(_config(tmp_path, system="You are MyBot."))
    assert compiled.sections[0].kind == "base"
    assert compiled.sections[0].text == "You are MyBot."
    assert "# fluid" not in (compiled.text or "")


def test_base_prompt_false_removes_the_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    compiled = resolved_prompt(_config(tmp_path, base_prompt=False))
    assert not compiled.of_kind("base")
    assert "# fluid" not in (compiled.text or "")
    # A user base still applies with the default switched off.
    compiled = resolved_prompt(_config(tmp_path, base_prompt=False, system="Mine."))
    assert compiled.sections[0].text == "Mine."


def test_code_mode_addendum_only_under_code_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    plain = resolved_system(_config(tmp_path)) or ""
    assert CODE_MODE_ADDENDUM.rstrip() not in plain
    coded = resolved_prompt(_config(tmp_path, code_mode=True))
    extras = [s.text for s in coded.of_kind("extra")]
    assert CODE_MODE_ADDENDUM.rstrip() in extras
    # Appended after the base, never in the cached prefix.
    assert (coded.text or "").index("# Code mode") > (coded.text or "").index("# fluid")


def test_extra_system_still_appends(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    compiled = resolved_prompt(_config(tmp_path), extra_system="Per-run note.")
    assert compiled.sections[-1].kind == "extra"
    assert compiled.sections[-1].text == "Per-run note."


def test_prompt_is_identical_across_compilations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    config = _config(tmp_path)
    first = resolved_system(config)
    second = resolved_system(config)
    assert first == second


def test_base_prompt_is_static_and_bounded() -> None:
    # Sprawl tripwire for the cached prefix, not a provider limit.
    assert len(DEFAULT_BASE_PROMPT) < 14_000
    # No volatile facts: those belong in the environment block.
    for volatile in ("202", "C:\\", "/home/", "{", "}"):
        assert volatile not in DEFAULT_BASE_PROMPT
    assert DEFAULT_BASE_PROMPT == DEFAULT_BASE_PROMPT.strip() + "\n"


def test_base_prompt_config_key_and_env(tmp_path) -> None:
    assert BUILTIN_DEFAULTS["base_prompt"] is True
    config = load_config(cwd=tmp_path, include_system=False, include_user=False, env={})
    assert config.base_prompt is True
    assert config.to_dict()["base_prompt"] is True  # shown by `fluid config`
    assert env_overrides({"FLUID_BASE_PROMPT": "false"}) == {"base_prompt": False}
    config = load_config(
        cwd=tmp_path, include_system=False, include_user=False,
        env={"FLUID_BASE_PROMPT": "0"},
    )
    assert config.base_prompt is False
    (tmp_path / "fluid.toml").write_text("base_prompt = false\n", encoding="utf-8")
    config = load_config(cwd=tmp_path, include_system=False, include_user=False, env={})
    assert config.base_prompt is False
