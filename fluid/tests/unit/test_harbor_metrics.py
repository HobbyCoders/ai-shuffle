"""Benchmark contracts without installing Harbor or making provider calls."""
import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from fluid.cli import __main__ as cli
from fluid.codemode.continual import MIN_NEW_ENTRIES, Synthesizer
from fluid.codemode.skills import SkillStore
from fluid.config import loader
from fluid.config.model import HarnessConfig, ProviderConfig
from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM
from fluid.events import TurnComplete, Usage
from fluid.providers._chat_completions import ChatCompletionsNormalizer
from fluid.providers._transport import truncated_stream_payload
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore

from tests.unit.test_delegated_cost_usage import FAKE, Scripted, _turn


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


def _run_metrics(adapter, tmp_path, monkeypatch, provider, *, configure=None):
    make_engine = cli.make_engine

    def build(*args, **kw):
        engine = make_engine(*args, **{**kw, "provider": provider})
        if configure is not None:
            configure(engine)
        return engine

    monkeypatch.setattr(cli, "make_engine", build)
    adapter.logs_dir = tmp_path
    log_dir = tmp_path / "agent"
    log_dir.mkdir()
    metrics = log_dir / "fluid-usage.jsonl"
    rc = cli.main([
        "run", "--cwd", str(tmp_path), "--store", str(tmp_path / "sessions"),
        "--usage-jsonl", str(metrics), "answer",
    ])
    context = SimpleNamespace(n_input_tokens=None, n_cache_tokens=None, n_output_tokens=None)
    adapter.populate_context_post_run(context)
    records = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    return rc, context, records


def test_terminal_counts_a_truncated_call_without_a_per_call_record(adapter, tmp_path, monkeypatch):
    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield StreamChunk("message_start", {"usage": {
                    "input_tokens": 10, "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5, "cost_usd": 0.125,
                }})
                yield StreamChunk("block_start", {
                    "index": 0, "block_type": "tool_use", "tool_name": "Read", "tool_use_id": "cut",
                })
                yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": '{"file_path":'})
                yield StreamChunk("error", truncated_stream_payload("message_stop"))
                return
            yield StreamChunk("message_start", {"usage": {
                "input_tokens": 7, "cache_read_input_tokens": 11,
            }})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "second try"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {
                "usage": {"output_tokens": 3, "cost_usd": 0.25}, "stop_reason": "end_turn",
            })
            yield StreamChunk("message_stop", {})

    rc, context, records = _run_metrics(adapter, tmp_path, monkeypatch, Provider())
    assert rc == 0
    assert [record["type"] for record in records] == ["usage", "status"]
    assert context.metadata["num_model_calls"] == 2
    assert context.cost_usd == 0.375
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (53, 31, 3)
    assert records[0]["turn_id"] == records[1]["turn_id"]


@pytest.mark.parametrize("reported", [False, True], ids=["absent", "explicit-zero"])
def test_provider_error_preserves_whether_counts_were_reported(adapter, tmp_path, monkeypatch, reported):
    class Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):
            if reported:
                yield StreamChunk("message_start", {"usage": {
                    "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                }})
            yield StreamChunk("error", {"message": "Unauthorized", "status": 401})

    rc, context, records = _run_metrics(adapter, tmp_path, monkeypatch, Provider())
    assert rc == 1
    assert context.metadata["num_model_calls"] == 1
    assert context.metadata["stop_reason"] == "error"
    expected = 0 if reported else None
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (expected,) * 3
    assert [record["type"] for record in records] == (["usage", "status"] if reported else ["status"])


def test_cli_captures_checkpoint_usage_emitted_during_close(adapter, tmp_path, monkeypatch, capsys):
    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        checkpoint_started = None
        checkpoint_cancelled = False

        async def stream(self, request):
            if request.system == CHECKPOINT_SYSTEM:
                try:
                    yield StreamChunk("message_start", {"usage": {"input_tokens": 4, "cost_usd": 0.02}})
                    self.checkpoint_started.set()
                    await asyncio.Future()  # Real engine close cancels this stream.
                finally:
                    self.checkpoint_cancelled = True
                return

            self.checkpoint_started = asyncio.Event()
            self.engine._schedule_checkpoint(self.engine.session_id, self.engine._active_turn_id)
            await asyncio.wait_for(self.checkpoint_started.wait(), timeout=5)
            yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 2, "cost_usd": 0.10},
                                        "stop_reason": "end_turn"})
            yield StreamChunk("message_stop", {})

    provider = Provider()

    def configure(engine):
        provider.engine = engine
        # Only the region/trigger are fixtures; scheduling, provider streaming,
        # cancellation, accounting and the CLI run/close are real.
        monkeypatch.setattr(engine._compactor.config, "summary_enabled", True)
        monkeypatch.setattr(engine._compactor, "summary_region", lambda *a, **kw: ("a", "b", "summary input"))

    rc, context, records = _run_metrics(adapter, tmp_path, monkeypatch, provider, configure=configure)
    assert rc == 0
    assert provider.checkpoint_cancelled
    [late] = [event for event in provider.engine.bus.log(provider.engine.session_id)
              if isinstance(event, Usage) and event.raw.get("auxiliary_background")]
    assert late.input_tokens == 4
    assert [row["type"] for row in records] == ["usage", "status", "usage"]
    assert records[0]["input_tokens"] == 10
    assert records[2]["outside_turn_totals"] and records[2]["input_tokens"] == 4
    assert (context.n_input_tokens, context.n_output_tokens) == (14, 2)
    assert context.cost_usd == pytest.approx(0.12)
    assert context.metadata["num_model_calls"] == 2
    assert capsys.readouterr().out.count("[usage]") == 1


def test_openrouter_normalizer_exports_normalized_zero_for_missing_counters(adapter, tmp_path, monkeypatch):
    class Provider:
        name = "openrouter"
        caps = ProviderCaps()

        async def stream(self, request):
            normalizer = ChatCompletionsNormalizer()
            for packet in (
                {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10}},
            ):
                for chunk in normalizer.consume("message", packet):
                    yield chunk

    rc, context, records = _run_metrics(adapter, tmp_path, monkeypatch, Provider())
    assert rc == 0
    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (10, 0, 0)
    assert records[0]["output_tokens"] == records[1]["usage"]["output_tokens"] == 0


def test_export_preserves_estimate_flags_carried_by_usage_event(tmp_path):
    metrics = tmp_path / "usage.jsonl"
    with metrics.open("w", encoding="utf-8") as stream:
        cli._write_usage_event(stream, Usage(session_id="s", turn_id="first", input_tokens=10,
            output_tokens=2, raw={"input_tokens": 10, "output_tokens": 2, "input_tokens_estimated": True,
                                  "output_tokens_estimated": True, "usage_estimated": True}))
    record = json.loads(metrics.read_text(encoding="utf-8"))
    assert record["input_tokens_estimated"] is True
    assert record["output_tokens_estimated"] is True
    assert record["usage_estimated"] is True


async def test_export_preserves_delegated_resume_cost_provenance(tmp_path):
    run = Scripted(tmp_path, {"turns": [_turn(1.25)]}, cli_session_id="cli-prev")
    try:
        terminal = await run.turn("one")
    finally:
        await run.aclose()
    assert terminal.usage["cost_from_table"] is True
    out = StringIO()
    cli._write_usage_event(out, terminal)
    record = json.loads(out.getvalue())
    assert record["cost_usd"] == terminal.cost_usd
    assert record["cost_from_table"] is True


async def test_delegated_continual_transcript_is_identical_with_metrics(tmp_path, monkeypatch):
    fact = "The user prefers to run tests from the repository root."

    class LearnerProvider:
        name = "anthropic"

        async def stream(self, request):
            yield StreamChunk(kind="text_delta", payload={"text": f"<memory>{fact}</memory>"})
            yield StreamChunk(kind="message_stop")

    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [_turn(0.10)]}), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    monkeypatch.setattr(loader, "_build_provider_client", lambda config, provider: LearnerProvider())
    # Defer the real synthesis pass to engine close, after stream_turn returns.
    monkeypatch.setattr(Synthesizer, "_batch_ready", lambda self: False)
    make_engine = cli.make_engine
    monkeypatch.setattr(cli, "make_engine", lambda *args, **kw: make_engine(
        *args, claude_cmd=[sys.executable, str(FAKE)], **kw,
    ))

    transcripts = []
    for with_metrics in (False, True):
        skills_dir = tmp_path / f"skills-{with_metrics}"
        skills = SkillStore(skills_dir)
        for i in range(MIN_NEW_ENTRIES):
            skills.journal_append({"kind": "user", "text": f"Preference {i}: {fact}"})
        config = HarnessConfig(
            cwd=str(tmp_path), setting_sources=(), skills_dir=str(skills_dir),
            default_provider="claude", providers={"claude": ProviderConfig(kind="delegated_claude")},
            continual=True, continual_provider="anthropic",
        )
        out = StringIO()
        metrics = StringIO() if with_metrics else None
        assert await cli.run_once(config, MemoryStore(), "answer", out=out, usage_out=metrics) == 0
        assert any(memory.text == fact for memory in skills.memories())
        assert "[learned] memory:" in out.getvalue()
        if metrics is not None:
            assert any(json.loads(line)["type"] == "status" for line in metrics.getvalue().splitlines())
        transcripts.append(out.getvalue())
    assert transcripts[0] == transcripts[1]


def test_turn_totals_keep_background_usage_and_unfinished_turns(adapter, tmp_path):
    adapter.logs_dir = tmp_path
    log_dir = tmp_path / "agent"
    log_dir.mkdir()
    with (log_dir / "fluid-usage.jsonl").open("w", encoding="utf-8") as stream:
        for event in (
            Usage(session_id="s", turn_id="first", input_tokens=10, raw={"input_tokens": 10}),
            Usage(session_id="s", turn_id="first", input_tokens=2, raw={
                "input_tokens": 2, "auxiliary_source": "checkpoint_summary",
            }),
            TurnComplete(session_id="s", turn_id="first", stop_reason="end_turn",
                         num_model_calls=2, usage={"input_tokens": 22, "output_tokens": 5}, cost_usd=0.10),
            Usage(session_id="s", turn_id="first", input_tokens=3, raw={
                "input_tokens": 3, "auxiliary_source": "checkpoint_summary",
                "auxiliary_background": True, "cost_usd": 0.02,
            }),
            # The next turn was killed after one usage record; no terminal status.
            Usage(session_id="s", turn_id="wake", input_tokens=4, raw={"input_tokens": 4}),
        ):
            cli._write_usage_event(stream, event)
    context = SimpleNamespace(n_output_tokens=None)
    adapter.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_output_tokens) == (29, 5)
    assert context.cost_usd == pytest.approx(0.12)
    assert context.metadata["num_model_calls"] == 3


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
