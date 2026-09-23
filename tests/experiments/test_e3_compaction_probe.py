"""E3 native compaction probe: every branch against fake transports, no network."""

from __future__ import annotations

import json

import httpx

from scripts.experiments import e3_compaction_probe as e3
from scripts.experiments._common import responses_sse, sse


def test_dry_run_exercises_every_probe_and_writes_json(tmp_path):
    out = tmp_path / "e3.json"
    printed = []
    assert e3.main(["--dry-run", "--out", str(out)], emit=printed.append) == 0
    results = {r["probe"]: r for r in json.loads(out.read_text())}
    assert results["openai_public"]["outcome"] == "accepted"
    assert results["openai_public"]["evidence"]["output_item_types"] == ["compaction"]
    assert results["openai_codex"]["outcome"] == "rejected"
    assert results["openai_codex"]["status"] == 400
    # The fake refuses compaction, so the combined request fails and each
    # identifier is isolated.
    assert results["anthropic_combined"]["outcome"] == "rejected"
    assert results["anthropic_ttl_1h_no_beta"]["outcome"] == "accepted"
    assert results["anthropic_clear_tool_uses_20250919"]["outcome"] == "accepted"
    assert results["anthropic_clear_thinking_20251015"]["outcome"] == "accepted"
    assert results["anthropic_compact_20260112"]["outcome"] == "rejected"
    assert "anthropic_ttl_1h_with_beta" not in results
    assert "openai_public" in printed[0]


async def test_requests_carry_the_identifiers_under_test():
    fake = e3.FakeProviders()
    cfg = e3.ProbeConfig(openai_api_key="k", anthropic_api_key="k", codex_token="t",
                         codex_account_id="acct")
    await e3.run_probes(cfg, transport=httpx.MockTransport(fake.handle))
    public, codex, combined = fake.requests[:3]
    assert json.loads(public.content)["input"][-1] == {"type": "compaction_trigger"}
    assert public.url == httpx.URL(e3.API_URL)
    assert codex.url == httpx.URL(e3.CODEX_RESPONSES_URL)
    assert codex.headers["chatgpt-account-id"] == "acct"
    assert codex.headers["originator"] == "codex_cli_rs"
    body = json.loads(combined.content)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [e["type"] for e in body["context_management"]["edits"]] == [
        "clear_thinking_20251015", "clear_tool_uses_20250919", "compact_20260112"]
    assert set(combined.headers["anthropic-beta"].split(",")) == {
        "context-management-2025-06-27", "compact-2026-01-12"}
    assert body["thinking"] == {"type": "adaptive"}


async def test_an_accepted_combined_request_needs_no_isolation():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}], "usage": {
            "input_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": 5000}},
            "context_management": {"applied_edits": []}})

    cfg = e3.ProbeConfig(anthropic_api_key="k", only=("anthropic",))
    results = await e3.run_probes(cfg, transport=httpx.MockTransport(handler))
    assert [(r.probe, r.outcome) for r in results] == [("anthropic_combined", "accepted")]
    assert len(calls) == 1


async def test_a_refused_ttl_is_retried_with_the_extended_ttl_beta():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        betas = request.headers.get("anthropic-beta", "")
        seen.append(betas)
        ttl = any((b.get("cache_control") or {}).get("ttl") == "1h" for b in body["system"])
        if ttl and e3.BETA_EXTENDED_TTL not in betas:
            return httpx.Response(400, json={"error": {"message": "ttl requires a beta header"}})
        return httpx.Response(200, json={"content": [], "usage": {
            "cache_creation": {"ephemeral_1h_input_tokens": 5000 if ttl else 0}}})

    cfg = e3.ProbeConfig(anthropic_api_key="k", only=("anthropic",))
    results = {r.probe: r for r in await e3.run_probes(cfg, transport=httpx.MockTransport(handler))}
    assert results["anthropic_ttl_1h_no_beta"].outcome == "rejected"
    assert results["anthropic_ttl_1h_with_beta"].outcome == "accepted"
    assert results["anthropic_ttl_1h_no_beta"].detail == "ttl requires a beta header"


def test_ttl_acceptance_needs_a_reported_1h_write():
    """Only a 1h cache write proves the TTL took effect. A 5m write means it
    was ignored, and a bare cache read does not say which TTL wrote the entry."""
    def result(usage):
        return e3._anthropic_result("p", 200, {"content": [], "usage": usage}, want_ttl=True)

    assert result({"cache_creation": {"ephemeral_1h_input_tokens": 5000}}).outcome == "accepted"
    downgraded = result({"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 5000}})
    assert downgraded.outcome == "accepted_no_effect" and "5m" in downgraded.detail
    read_only = result({"cache_read_input_tokens": 5000, "cache_creation": {}})
    assert read_only.outcome == "accepted_no_effect" and "re-run" in read_only.detail
    assert result({}).outcome == "accepted_no_effect"
    # Without the TTL under test, HTTP 200 is acceptance of the identifier.
    assert e3._anthropic_result("p", 200, {"content": [], "usage": {}}, want_ttl=False).outcome == "accepted"


async def test_openai_outcomes_without_a_compaction_item_or_with_a_stream_error():
    def quiet(request):
        return httpx.Response(200, content=responses_sse(response_id="r", model="m", usage={}))

    def failing(request):
        return httpx.Response(200, content=sse([("response.failed", {"type": "response.failed",
                              "response": {"error": {"message": "unknown item compaction_trigger"}}})]))

    for handler, outcome in ((quiet, "accepted_no_effect"), (failing, "rejected")):
        cfg = e3.ProbeConfig(openai_api_key="k", only=("openai_public",))
        [result] = await e3.run_probes(cfg, transport=httpx.MockTransport(handler))
        assert result.outcome == outcome


async def test_missing_credentials_skip_a_probe_without_a_request():
    calls = []
    cfg = e3.ProbeConfig()
    results = await e3.run_probes(cfg, transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert [r.outcome for r in results] == ["skipped", "skipped", "skipped"]
    assert calls == []
