"""CLI regressions: JSON-stream input, output flags, exit codes and the
delegation of unsupported programs/options to a real jq."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.jq_shim.__main__ import find_real_jq

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
SHIM_BIN = PROJECT_ROOT / "app" / "core" / "jq_shim" / "bin"


def run(*args, input_data="", path=None, fallback=False):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("JQ_SHIM_NO_FALLBACK", None)
    if not fallback:
        env["JQ_SHIM_NO_FALLBACK"] = "1"
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, "-m", "app.core.jq_shim", *args],
        input=input_data, capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), env=env, timeout=60,
    )


class TestInputAndOutput:
    def test_multiple_json_values_on_stdin(self):
        # jq runs the filter once per input value (JSON Lines); this used to fail
        result = run("-c", ".a", input_data='{"a":1}\n{"a":2} {"a":3}')
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["1", "2", "3"]

    def test_slurp_multiple_values(self):
        result = run("-c", "-s", "map(.a)", input_data='{"a":1}\n{"a":2}')
        assert result.stdout.strip() == "[1,2]"

    def test_slurp_empty_input(self):
        assert run("-c", "-s", ".", input_data="").stdout.strip() == "[]"

    def test_multiple_files(self, tmp_path):
        (tmp_path / "a.json").write_text('{"v":1}')
        (tmp_path / "b.json").write_text('{"v":2}\n{"v":3}')
        result = run(".v", str(tmp_path / "a.json"), str(tmp_path / "b.json"))
        assert result.stdout.split() == ["1", "2", "3"]

    def test_invalid_json_input(self):
        result = run(".", input_data="{nope")
        assert result.returncode == 2
        assert "error" in result.stderr

    def test_join_output_implies_raw(self):
        result = run("-j", ".[]", input_data='["a","b",1]')
        assert result.stdout == "ab1"

    def test_tab_and_sort_keys(self):
        result = run("--tab", "-S", ".", input_data='{"b":1,"a":[2]}')
        assert result.stdout == '{\n\t"a": [\n\t\t2\n\t],\n\t"b": 1\n}\n'

    def test_stream_output_one_per_line(self):
        result = run("-c", ".[] | {n: .}", input_data="[1,2]")
        assert result.stdout.splitlines() == ['{"n":1}', '{"n":2}']

    def test_error_after_outputs_exits_5(self):
        result = run(".[] | .a", input_data='[{"a":1}, "s"]')
        assert result.returncode == 5
        assert "Cannot index string" in result.stderr


class TestExitStatus:
    def test_exit_status_false(self):
        assert run("-e", ".a", input_data='{"a":false}').returncode == 1

    def test_exit_status_null(self):
        assert run("-e", ".a", input_data="{}").returncode == 1

    def test_exit_status_truthy(self):
        assert run("-e", ".a", input_data='{"a":0}').returncode == 0

    def test_exit_status_no_output(self):
        assert run("-e", "empty", input_data="{}").returncode == 4


class TestUnsupportedWithoutFallback:
    @pytest.mark.parametrize(
        "program,message",
        [
            ("if . then 1 else 2 end", "unsupported jq syntax: 'if'"),
            (".a + 1", "arithmetic operator"),
            ("@base64", "@format"),
            ("[range(3)]", "range/1 is not defined"),
            ("{x: .[]}", "generators"),
        ],
    )
    def test_compile_errors_exit_3(self, program, message):
        result = run(program, input_data="[1]")
        assert result.returncode == 3
        assert message in result.stderr

    def test_unknown_option_exits_2(self):
        result = run("-R", ".", input_data="x")
        assert result.returncode == 2


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the fake jq")
class TestFallbackToRealJq:
    @pytest.fixture
    def fake_jq_dir(self, tmp_path):
        fake = tmp_path / "jq"
        fake.write_text('#!/bin/sh\necho "REAL-JQ $*"\ncat\n')
        fake.chmod(0o755)
        return tmp_path

    def _path(self, *dirs):
        return os.pathsep.join([str(d) for d in dirs] + [os.environ.get("PATH", "")])

    def test_unsupported_program_is_delegated_with_stdin_intact(self, fake_jq_dir):
        result = run(".a + 1", input_data='{"a":1}', path=self._path(SHIM_BIN, fake_jq_dir), fallback=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["REAL-JQ .a + 1", '{"a":1}']

    def test_unknown_option_is_delegated(self, fake_jq_dir):
        result = run("-R", ".", input_data="raw", path=self._path(fake_jq_dir), fallback=True)
        assert result.stdout.splitlines() == ["REAL-JQ -R .", "raw"]

    def test_supported_program_is_not_delegated(self, fake_jq_dir):
        result = run("-c", "[.[] | .a]", input_data='[{"a":1}]', path=self._path(fake_jq_dir), fallback=True)
        assert result.stdout.strip() == "[1]"

    def test_no_fallback_env_disables_delegation(self, fake_jq_dir):
        env_path = self._path(fake_jq_dir)
        result = run(".a + 1", input_data="{}", path=env_path)  # JQ_SHIM_NO_FALLBACK=1
        assert result.returncode == 3
        assert "REAL-JQ" not in result.stdout

    def test_find_real_jq_skips_shim_copies(self, fake_jq_dir, monkeypatch):
        monkeypatch.delenv("JQ_SHIM_NO_FALLBACK", raising=False)
        monkeypatch.setenv("PATH", os.pathsep.join([str(SHIM_BIN), str(fake_jq_dir)]))
        assert find_real_jq() == str(fake_jq_dir / "jq")
        monkeypatch.setenv("PATH", str(SHIM_BIN))
        assert find_real_jq() is None


class TestMainInProcess:
    """The same entry point, called in-process (fast, and covers __main__)."""

    @pytest.fixture
    def cli(self, monkeypatch, capsys):
        import io

        from app.core.jq_shim import __main__ as cli_module

        monkeypatch.setenv("JQ_SHIM_NO_FALLBACK", "1")

        def invoke(*args, stdin=""):
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
            code = cli_module.main(list(args))
            out, err = capsys.readouterr()
            return code, out, err

        invoke.module = cli_module
        return invoke

    def test_variables(self, cli, tmp_path):
        (tmp_path / "s.json").write_text("1 [2]")
        (tmp_path / "r.txt").write_text("raw\n")
        code, out, _ = cli(
            "-c", "-n", "{a: $a, b: $b, s: $s, r: $r}",
            "--arg", "a", "x", "--argjson", "b", '{"k":1}',
            "--slurpfile", "s", str(tmp_path / "s.json"),
            "--rawfile", "r", str(tmp_path / "r.txt"),
        )
        assert code == 0
        assert json.loads(out) == {"a": "x", "b": {"k": 1}, "s": [1, [2]], "r": "raw\n"}

    @pytest.mark.parametrize(
        "args",
        [
            ("--argjson", "b", "{bad", "."),
            ("--slurpfile", "s", "/nonexistent/file.json", "."),
            ("--rawfile", "r", "/nonexistent/file.txt", "."),
            (".", "/nonexistent/input.json"),
        ],
    )
    def test_bad_variable_or_file_sources_exit_2(self, cli, args):
        code, _, err = cli(*args)
        assert code == 2
        assert "jq: error" in err

    def test_invalid_json_file_exits_2(self, cli, tmp_path):
        (tmp_path / "bad.json").write_text("{nope")
        code, _, err = cli(".", str(tmp_path / "bad.json"))
        assert code == 2
        assert "error parsing" in err

    def test_version(self, cli):
        code, out, _ = cli("--version")
        assert code == 0 and "jq-shim" in out

    def test_empty_stdin_is_null_input(self, cli):
        assert cli(".", stdin="")[1].strip() == "null"

    def test_raw_and_compact(self, cli):
        assert cli("-r", ".[]", stdin='["a",{"b":1}]')[1] == 'a\n{\n  "b": 1\n}\n'
        assert cli("-c", "-S", ".", stdin='{"b":1,"a":2}')[1] == '{"a":2,"b":1}\n'

    def test_evaluation_error_exits_5(self, cli):
        code, _, err = cli('error("boom")', stdin="null")
        assert code == 5 and "boom" in err

    def test_compile_error_exits_3(self, cli):
        code, _, err = cli(".a +", stdin="{}")
        assert code == 3 and "arithmetic" in err

    def test_unknown_option_exits_2(self, cli):
        code, _, err = cli("--indent", "4", ".")
        assert code == 2 and "jq: error" in err

    def test_delegation_uses_real_jq_with_original_argv(self, cli, monkeypatch):
        calls = []
        monkeypatch.setattr(cli.module, "find_real_jq", lambda: "/opt/jq")
        def fake_call(argv, env):
            assert env["JQ_SHIM_NO_FALLBACK"] == "1"  # no delegation loops
            calls.append(argv)
            return 7

        monkeypatch.setattr(cli.module.subprocess, "call", fake_call)
        assert cli("-c", "if . then 1 else 2 end")[0] == 7
        assert cli("-R", ".")[0] == 7
        assert calls == [["/opt/jq", "-c", "if . then 1 else 2 end"], ["/opt/jq", "-R", "."]]

    def test_find_real_jq_honours_opt_out(self, cli, monkeypatch):
        monkeypatch.setenv("JQ_SHIM_NO_FALLBACK", "1")
        assert cli.module.find_real_jq() is None

    def test_find_real_jq_skips_unreadable_and_empty_entries(self, cli, monkeypatch, tmp_path):
        monkeypatch.delenv("JQ_SHIM_NO_FALLBACK")
        monkeypatch.setenv("PATH", os.pathsep.join(["", str(tmp_path)]))
        assert cli.module.find_real_jq() is None

    def test_output_too_deep_is_a_clean_error(self, cli, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RecursionError()

        monkeypatch.setattr(cli.module, "output_result", boom)
        code, _, err = cli(".", stdin="1")
        assert code == 5 and "nested too deeply" in err
