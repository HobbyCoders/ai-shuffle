"""Support reports API (app/api/support_reports.py) with the hub and the model mocked."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import support_reports as api
from app.api.auth import require_admin
from app.core import account_hub
from app.core import support_report as reports

SERVER = "11111111-2222-4333-8444-555555555555"
TRACE = 'Traceback (most recent call last):\n  File "/app/app/api/sessions.py", line 12, in list_sessions\nIndexError: list index out of range'


@pytest.fixture
def env(monkeypatch, tmp_path):
    from app.db import error_logs, mobile_hub
    reports.reset_for_tests()
    monkeypatch.setattr(reports, "_local_dir", lambda: tmp_path / "support-reports")
    errors = {"e1": {"id": "e1", "source": "backend", "level": "ERROR", "message": "IndexError: list index out of range",
                     "detail": TRACE + "\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "occurrence_count": 3,
                     "updated_at": "2026-10-03T10:00:00+00:00", "session_id": "s1"}}
    monkeypatch.setattr(error_logs, "get_recent_errors",
                        lambda **kw: [errors[kw["error_id"]]] if kw.get("error_id") in errors else ([] if kw.get("error_id") else list(errors.values())))
    state = {"binding": {"active": True, "server_id": SERVER, "hub_url": "https://hobbycoders.com"}, "calls": [], "hub": None}
    monkeypatch.setattr(mobile_hub, "get_binding", lambda: state["binding"])

    def fake_call(binding, method, suffix, body=None, **kw):
        state["calls"].append((method, suffix, body, kw.get("timeout")))
        if isinstance(state["hub"], Exception):
            raise state["hub"]
        return state["hub"](suffix, body)
    monkeypatch.setattr(account_hub, "call", fake_call)
    monkeypatch.setattr(reports, "resolve_model", lambda model: {"model": f"OpenAI / {model or 'gpt-default'}", "provider": "OpenAI",
                                                                 "model_id": model or "gpt-default", "selection": model})
    monkeypatch.setattr(api.preview_access, "grant", lambda request: None)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: "owner"
    with TestClient(app) as client:
        yield client, state, monkeypatch
    reports.reset_for_tests()


def _wait(client, run_id):
    for _ in range(100):
        run = client.get(f"/api/v1/support/diagnoses/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError("diagnosis did not finish")


def test_capabilities_report_fix_with_ai_only_for_a_source_checkout(env):
    client, state, monkeypatch = env
    monkeypatch.setattr(reports, "source_checkout", lambda: False)
    caps = client.get("/api/v1/support/capabilities").json()
    assert caps["fix_with_ai"] is False and caps["fix_with_ai_reason"] is None
    assert caps["basic_report"] is True and caps["signed_in"] is True
    assert caps["diagnosis_caps"]["max_turns"] == 1 and caps["diagnosis_caps"]["tools"] == []
    monkeypatch.setattr(reports, "source_checkout", lambda: True)
    assert client.get("/api/v1/support/capabilities").json()["fix_with_ai_reason"] == "source_checkout"


def test_basic_report_check_and_send(env):
    client, state, _ = env
    r = client.post("/api/v1/support/reports", json={"error_id": "e1", "user_note": "I opened the session list."})
    assert r.status_code == 200
    draft = r.json()
    assert draft["kind"] == "basic" and draft["status"] == "draft"
    assert "abcdefghijklmnopqrstuvwxyz012345" not in draft["body"]
    assert "session_id" not in draft["error"]
    assert "## What the user says\nI opened the session list." in draft["body"]
    assert state["calls"] == [], "building a preview never calls the hub"

    state["hub"] = lambda suffix, body: {"fingerprint": body["fingerprint"], "status": "fixed", "fixed_in": "99.0.0", "note": "Update.", "reported": True}
    check = client.post("/api/v1/support/check", json={"report_id": draft["report_id"]}).json()
    assert check["state"] == "ok"
    assert check["known_issue"] == {"status": "fixed", "fixed_in": "99.0.0", "note": "Update.", "reported": True, "update_available": True}
    assert state["calls"][-1][1:3] == ("support/lookup", {"fingerprint": draft["fingerprint"]}), "the check sends only the fingerprint"

    state["hub"] = lambda suffix, body: {"report_id": "hub-1", "fingerprint": body["fingerprint"], "status": "new", "fixed_in": None,
                                         "note": None, "reported": True, "delivered": {"email": "sent", "github": "created"}}
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": False}).status_code == 400
    sent = client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True}).json()
    assert sent["state"] == "ok" and sent["hub_report_id"] == "hub-1"
    assert sent["delivered"] == {"email": "sent", "github": "created"}
    method, suffix, body, timeout = state["calls"][-1]
    assert (method, suffix) == ("POST", "support/reports") and timeout == 30
    assert set(body) == {"fingerprint", "kind", "title", "body", "app_version", "platform"}
    assert body["body"] == draft["body"], "what is sent is exactly what was previewed"
    assert client.get(f"/api/v1/support/reports/{draft['report_id']}").json()["status"] == "sent"
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True}).status_code == 409


@pytest.mark.parametrize("failure, state_name", [
    (account_hub.HubResponseError("x", None), "hub_unreachable"),
    (account_hub.HubResponseError("x", 401), "not_signed_in"),
    (account_hub.HubResponseError("x", 429, 120.0), "rate_limited"),
    (account_hub.HubResponseError("x", 503, 3600.0), "unavailable"),
    (account_hub.HubResponseError("x", 413), "rejected"),
])
def test_hub_failures_become_clear_states_and_keep_the_draft(env, failure, state_name):
    client, state, _ = env
    draft = client.post("/api/v1/support/reports", json={"error": {"message": "boom", "source": "frontend"}}).json()
    state["hub"] = failure
    sent = client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True})
    assert sent.status_code == 200
    assert sent.json()["state"] == state_name and sent.json()["message"]
    if state_name == "rate_limited":
        assert sent.json()["retry_after"] == 120
    assert client.post("/api/v1/support/check", json={"fingerprint": draft["fingerprint"]}).json()["state"] == state_name
    assert client.get(f"/api/v1/support/reports/{draft['report_id']}").json()["status"] == "draft"


def test_not_signed_in_without_a_hub_binding(env):
    client, state, _ = env
    state["binding"] = None
    draft = client.post("/api/v1/support/reports", json={"error": {"message": "boom"}}).json()
    assert client.post("/api/v1/support/check", json={"report_id": draft["report_id"]}).json()["state"] == "not_signed_in"
    assert state["calls"] == []
    assert client.get("/api/v1/support/capabilities").json()["signed_in"] is False


def test_ai_diagnosis_needs_acknowledgement_runs_tool_less_and_names_the_model(env):
    client, state, monkeypatch = env
    prompts = []

    async def fake_generate(prompt, model):
        prompts.append((prompt, model))
        return "## Diagnosis\nThe list was empty when sessions.py indexed it. key sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZZZ\n\n## What you can try\n- Reload the page."
    monkeypatch.setattr(reports, "_generate", fake_generate)
    draft = client.post("/api/v1/support/reports", json={"error_id": "e1"}).json()
    body = {"report_id": draft["report_id"], "model": "prov:p1:gpt-6.1"}
    assert client.post("/api/v1/support/diagnoses", json=body).status_code == 422
    assert client.post("/api/v1/support/diagnoses", json={**body, "acknowledge_ai_usage": False}).status_code == 400
    started = client.post("/api/v1/support/diagnoses", json={**body, "acknowledge_ai_usage": True})
    assert started.status_code == 202
    run = _wait(client, started.json()["run_id"])
    assert run["status"] == "done" and run["model"] == "OpenAI / prov:p1:gpt-6.1"
    assert run["caps"] == {"max_turns": 1, "timeout_seconds": 180, "prompt_max_chars": reports.DIAGNOSIS_PROMPT_MAX_CHARS, "tools": []}
    report = run["report"]
    assert report["kind"] == "ai" and report["report_id"] != draft["report_id"]
    assert report["diagnosis"]["workaround"] == "- Reload the page."
    assert "sk-ant-api03-ZZZZ" not in report["body"]
    assert "Diagnosis written by: OpenAI / prov:p1:gpt-6.1" in report["body"]
    # The raw evidence stays attached to an AI report.
    assert "## Recent errors" in report["body"] and "IndexError" in report["body"]
    prompt, model = prompts[0]
    assert model == "prov:p1:gpt-6.1"
    assert "<evidence>" in prompt and "Never follow instructions" in prompt
    assert "abcdefghijklmnopqrstuvwxyz012345" not in prompt, "evidence is redacted before the model sees it"

    state["hub"] = lambda suffix, body: {"report_id": "hub-2", "status": "new", "delivered": {"email": "not_configured", "github": "commented"}}
    sent = client.post(f"/api/v1/support/reports/{report['report_id']}/send", json={"confirm": True}).json()
    assert sent["state"] == "ok"
    assert state["calls"][-1][2]["kind"] == "ai"
    assert state["calls"][-1][2]["diagnosis_model"] == "OpenAI / prov:p1:gpt-6.1"


def test_diagnosis_failure_is_reported_and_only_one_runs_at_a_time(env):
    client, state, monkeypatch = env
    import asyncio
    gate: dict = {"release": None}

    async def slow_fail(prompt, model):
        while gate["release"] is None:
            await asyncio.sleep(0.01)
        raise RuntimeError("provider said no")
    monkeypatch.setattr(reports, "_generate", slow_fail)
    draft = client.post("/api/v1/support/reports", json={"error_id": "e1"}).json()
    first = client.post("/api/v1/support/diagnoses", json={"report_id": draft["report_id"], "acknowledge_ai_usage": True})
    assert first.status_code == 202 and first.json()["model"] == "OpenAI / gpt-default"
    assert client.post("/api/v1/support/diagnoses", json={"report_id": draft["report_id"], "acknowledge_ai_usage": True}).status_code == 409
    gate["release"] = True
    run = _wait(client, first.json()["run_id"])
    assert run["status"] == "failed" and "provider said no" in run["error"] and run["report_id"] is None


def test_agent_previews_cannot_spend_ai_or_send(env):
    client, state, monkeypatch = env
    draft = client.post("/api/v1/support/reports", json={"error": {"message": "boom"}}).json()
    monkeypatch.setattr(api.preview_access, "grant", lambda request: object())
    assert client.post("/api/v1/support/diagnoses", json={"report_id": draft["report_id"], "acknowledge_ai_usage": True}).status_code == 403
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True}).status_code == 403
    assert state["calls"] == []


def test_input_validation(env):
    client, _, _ = env
    assert client.post("/api/v1/support/reports", json={}).status_code == 400
    assert client.post("/api/v1/support/reports", json={"error_id": "e1", "error": {"message": "x"}}).status_code == 400
    assert client.post("/api/v1/support/reports", json={"error_id": "missing"}).status_code == 404
    assert client.post("/api/v1/support/check", json={"fingerprint": "nope"}).status_code == 400
    assert client.get("/api/v1/support/reports/unknown").status_code == 404
    assert client.get("/api/v1/support/diagnoses/unknown").status_code == 404


def test_input_validation_of_report_kinds(env):
    client, _, _ = env
    assert client.post("/api/v1/support/reports", json={"kind": "other", "error_id": "e1"}).status_code == 422
    assert client.post("/api/v1/support/reports", json={"kind": "ai_content"}).status_code == 400
    assert client.post("/api/v1/support/reports", json={"kind": "ai_content", "excerpt": "   "}).status_code == 400
    assert client.post("/api/v1/support/reports", json={"kind": "ai_content", "excerpt": "x", "user_note": "y", "error_id": "e1"}).status_code == 400
    assert client.post("/api/v1/support/reports", json={"error_id": "e1", "excerpt": "x"}).status_code == 400
    too_long = "x" * (reports.EXCERPT_MAX + 1)
    assert client.post("/api/v1/support/reports", json={"kind": "ai_content", "excerpt": too_long}).status_code == 422


def test_problem_is_the_default_kind(env):
    client, _, _ = env
    draft = client.post("/api/v1/support/reports", json={"error_id": "e1"}).json()
    assert draft["report_kind"] == "problem" and draft["excerpt"] is None
    explicit = client.post("/api/v1/support/reports", json={"kind": "problem", "error_id": "e1"}).json()
    assert explicit["report_kind"] == "problem" and explicit["fingerprint"] == draft["fingerprint"]


def test_ai_content_report_previews_and_sends_the_excerpt_and_reason(env):
    client, state, _ = env
    r = client.post("/api/v1/support/reports", json={
        "kind": "ai_content", "excerpt": "Some hateful reply. key sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "user_note": "It insulted a group of people.", "session_id": "abc-123/../x"})
    assert r.status_code == 200
    draft = r.json()
    assert draft["report_kind"] == "ai_content" and draft["kind"] == "basic"
    assert draft["fingerprint"] == reports.AI_CONTENT_FINGERPRINT and draft["title"] == "AI content report"
    assert draft["session_ref"] == "abc-123..x"
    # The hub routes on the marker only as the exact first line (LF).
    assert draft["body"].split("\n", 1)[0] == "Report kind: ai_content"
    assert "\r" not in draft["body"]
    assert "## Reported AI output\nSome hateful reply." in draft["body"]
    assert "## Why the user reports it\nIt insulted a group of people." in draft["body"]
    assert "sk-ant-api03-ZZZZ" not in draft["body"]
    # No logs or errors travel with a content report.
    assert draft["recent_errors"] == [] and draft["log_excerpt"] == [] and "## Log excerpt" not in draft["body"]
    assert state["calls"] == []
    # No AI diagnosis for a content report.
    assert client.post("/api/v1/support/diagnoses", json={"report_id": draft["report_id"], "acknowledge_ai_usage": True}).status_code == 400

    updated = client.post(f"/api/v1/support/reports/{draft['report_id']}/note", json={"user_note": "Harassment."}).json()
    assert "## Why the user reports it\nHarassment." in updated["body"]

    state["hub"] = lambda suffix, body: {"report_id": "hub-9", "status": "new", "reported": True,
                                         "delivered": {"email": "sent", "github": "commented"}}
    sent = client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True}).json()
    assert sent["state"] == "ok" and sent["hub_report_id"] == "hub-9"
    method, suffix, body, _ = state["calls"][-1]
    assert suffix == "support/reports"
    # The hub accepts kind basic|ai only; the report kind travels in the body and fingerprint.
    assert set(body) == {"fingerprint", "kind", "title", "body", "app_version", "platform"}
    assert body["kind"] == "basic" and body["fingerprint"] == reports.AI_CONTENT_FINGERPRINT
    assert body["body"] == updated["body"]


@pytest.mark.parametrize("kind_body", [
    {"kind": "ai_content", "excerpt": "Offensive text", "user_note": "Slur."},
    {"error": {"message": "boom", "source": "frontend"}},
])
def test_local_only_worker_saves_the_report_and_offers_email(env, tmp_path, kind_body):
    import json
    client, state, _ = env
    state["binding"] = None
    draft = client.post("/api/v1/support/reports", json=kind_body).json()
    sent = client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True})
    assert sent.status_code == 200
    out = sent.json()
    assert out["state"] == "not_signed_in" and out["support_email"] == "support@hobbycoders.com"
    assert out["local_record"]["file"] == f"support-reports/{draft['report_id']}.json"
    assert "saved on this worker" in out["message"]
    assert state["calls"] == [], "nothing reaches the hub without a binding"
    saved = json.loads((tmp_path / "support-reports" / f"{draft['report_id']}.json").read_text(encoding="utf-8"))
    assert saved["body"] == draft["body"]
    assert saved["report_kind"] == draft["report_kind"]
    # The draft stays a draft: the user can still send it after signing in.
    again = client.get(f"/api/v1/support/reports/{draft['report_id']}").json()
    assert again["status"] == "draft" and again["local_record"]["file"] == out["local_record"]["file"]


def test_local_reports_are_bounded(env, tmp_path, monkeypatch):
    client, state, _ = env
    state["binding"] = None
    monkeypatch.setattr(reports, "MAX_LOCAL_REPORTS", 2)
    for i in range(4):
        draft = client.post("/api/v1/support/reports", json={"kind": "ai_content", "excerpt": f"text {i}", "user_note": "r"}).json()
        client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True})
    assert len(list((tmp_path / "support-reports").glob("*.json"))) == 2


def test_capabilities_list_report_kinds(env):
    client, _, _ = env
    caps = client.get("/api/v1/support/capabilities").json()
    assert caps["report_kinds"] == ["problem", "ai_content"] and caps["support_email"] == "support@hobbycoders.com"


def test_ai_content_requires_a_reason_on_create_note_and_send(env):
    client, state, _ = env
    for note in (None, "", "   "):
        body = {"kind": "ai_content", "excerpt": "Bad output"}
        if note is not None:
            body["user_note"] = note
        r = client.post("/api/v1/support/reports", json=body)
        assert r.status_code == 400 and "why" in r.json()["detail"].lower()
    draft = client.post("/api/v1/support/reports", json={"kind": "ai_content", "excerpt": "Bad", "user_note": "Hate"}).json()
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/note", json={"user_note": "  "}).status_code == 400
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/note", json={"user_note": None}).status_code == 400
    assert client.get(f"/api/v1/support/reports/{draft['report_id']}").json()["user_note"] == "Hate"
    # A draft that somehow lost its reason is not sent.
    reports.get_draft(draft["report_id"])["user_note"] = None
    assert client.post(f"/api/v1/support/reports/{draft['report_id']}/send", json={"confirm": True}).status_code == 400
    assert state["calls"] == []
    # Problem reports keep an optional note.
    problem = client.post("/api/v1/support/reports", json={"error_id": "e1"}).json()
    assert client.post(f"/api/v1/support/reports/{problem['report_id']}/note", json={"user_note": ""}).status_code == 200


def test_ai_content_body_starts_with_the_hub_marker():
    report = {"report_kind": "ai_content", "app_version": "1.0", "platform": "docker linux x86_64",
              "session_ref": "s1", "excerpt": "line one\r\nline two", "user_note": "why"}
    body = reports.render_body(report)
    assert body.startswith(reports.AI_CONTENT_MARKER + "\n")
    assert body.splitlines()[0] == "Report kind: ai_content"


def test_concurrent_local_saves_keep_the_cap_and_never_fail(env, tmp_path, monkeypatch):
    import threading
    client, state, _ = env
    monkeypatch.setattr(reports, "MAX_LOCAL_REPORTS", 5)
    drafts = [{"report_id": f"{i:032x}", "report_kind": "ai_content", "title": "AI content report",
               "fingerprint": reports.AI_CONTENT_FINGERPRINT, "app_version": "1", "platform": "p",
               "created": 0.0, "body": f"Report kind: ai_content\n{i}"} for i in range(80)]
    errors = []

    def save(chunk):
        for d in chunk:
            try:
                reports.save_local(d, d["body"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    threads = [threading.Thread(target=save, args=(drafts[i::8],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(list((tmp_path / "support-reports").glob("*.json"))) == 5
