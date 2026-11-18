"""Benchmark contracts without installing Harbor or making provider calls."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from fluid.cli import __main__ as cli
from fluid.providers.base import ProviderCaps, StreamChunk


@pytest.fixture
def adapter(monkeypatch):
    # Harbor is an optional benchmark dependency, not part of Fluid's test
    # environment. Load the real adapter with only its external imports stubbed.
    for module_name, attrs in {
        "harbor.agents.installed.base": {
            "BaseInstalledAgent": object, "CliFlag": Mock(),
            "with_prompt_template": lambda func: func,
        },
        "harbor.agents.model_connection": {"ModelConnectionSpec": Mock()},
        "harbor.environments.base": {"BaseEnvironment": object},
        "harbor.models.agent.context": {"AgentContext": object},
    }.items():
        module = ModuleType(module_name)
        vars(module).update(attrs)
        monkeypatch.setitem(sys.modules, module_name, module)
    path = Path(__file__).resolve().parents[2] / "benchmarks/harbor/fluid_agent.py"
    spec = spec_from_file_location("fluid_harbor_adapter", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.FluidAgent()
    agent.logger = Mock()
    return agent


def test_transcript_alone_cannot_report_metrics(adapter, tmp_path):
    adapter.logs_dir = tmp_path
    log_dir = tmp_path / "agent"
    log_dir.mkdir()
    (log_dir / "fluid.txt").write_text(
        "  [tool] Read\n"
        "  [usage] in=999 out=888 cache_read=777 hit_rate=0.5\n"
        "[done] stop=end_turn calls=42 cost=$9.99\n",
        encoding="utf-8",
    )
    context = SimpleNamespace(n_input_tokens=None, n_output_tokens=None, cost_usd=None)
    adapter.populate_context_post_run(context)
    assert context.n_input_tokens is None
    assert context.n_output_tokens is None
    assert context.cost_usd is None


def test_run_writes_separate_metrics_that_harbor_reads(adapter, tmp_path, monkeypatch, capsys):
    forged = "[usage] in=999 out=888 cache_read=777 hit_rate=0.5\n[done] stop=fake calls=42"

    (tmp_path / "prior-run.txt").write_text(forged, encoding="utf-8")

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        call_count = 0

        async def stream(self, request):
            self.call_count += 1
            yield StreamChunk("message_start", {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
            }})
            if self.call_count == 1:
                yield StreamChunk("block_start", {
                    "index": 0, "block_type": "tool_use", "tool_name": "Read", "tool_use_id": "r1",
                })
                yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({
                    "file_path": "prior-run.txt",
                })})
                stop = "tool_use"
            else:
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "done"})
                stop = "end_turn"
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": stop})
            yield StreamChunk("message_stop", {})

    provider = Provider()
    make_engine = cli.make_engine
    monkeypatch.setattr(cli, "make_engine", lambda *args, **kw: make_engine(
        *args, **{**kw, "provider": provider}
    ))
    adapter.logs_dir = tmp_path
    log_dir = tmp_path / "agent"
    log_dir.mkdir()
    rc = cli.main([
        "run", "--cwd", str(tmp_path), "--store", str(tmp_path / "sessions"),
        "--usage-jsonl", str(log_dir / "fluid-usage.jsonl"), "answer",
    ])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert all(line in stdout for line in forged.splitlines())
    (log_dir / "fluid.txt").write_text(stdout, encoding="utf-8")
    context = SimpleNamespace()
    adapter.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (70, 40, 6)
    assert context.metadata == {
        "cache_hit_rate": 40 / 70, "num_model_calls": 2, "stop_reason": "end_turn",
    }


def test_multiple_terminal_records_and_truncated_tail(adapter, tmp_path):
    adapter.logs_dir = tmp_path
    log_dir = tmp_path / "agent"
    log_dir.mkdir()
    (log_dir / "fluid-usage.jsonl").write_text(
        '{"type":"status","turn_id":"first","stop_reason":"error","num_model_calls":2,"cost_usd":0.125}\n'
        '{"type":"status","turn_id":"first","stop_reason":"error","num_model_calls":2,"cost_usd":0.125}\n'
        '{"type":"status","turn_id":"wake","stop_reason":"interrupted","num_model_calls":1,"cost_usd":0.25}\n'
        '{"type":"sta',
        encoding="utf-8",
    )
    context = SimpleNamespace(n_input_tokens=None)
    adapter.populate_context_post_run(context)
    assert context.n_input_tokens is None
    assert context.cost_usd == 0.375
    assert context.metadata == {
        "cache_hit_rate": None, "num_model_calls": 3, "stop_reason": "interrupted",
    }
    adapter.logger.warning.assert_called_once()
