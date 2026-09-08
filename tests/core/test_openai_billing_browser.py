"""Safe, deterministic OpenAI Platform credit response capture."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.core import openai_billing_browser as mod
from app.core.browser_stream import translate_input


@pytest.fixture
def browser(monkeypatch):
    broker = Mock(side_effect=AssertionError("Billing must not allocate a workspace broker"))
    monkeypatch.setattr("app.core.browser_control.get", broker)
    yield mod.OpenAIBillingBrowser()
    broker.assert_not_called()


@pytest.fixture
def session(browser):
    session = mod._Session("provider", platform_page=True)
    browser._sessions["provider"] = session
    return session


def request(browser, session, request_id="req", org="org-test", url=mod.CREDIT_URL, method="GET"):
    headers = {"OpenAI-Organization": org, "Authorization": "test-secret"} if org else {}
    browser._event(session, "Network.requestWillBeSent", {
        "requestId": request_id, "request": {"url": url, "method": method, "headers": headers},
    })
    return session.requests.get(request_id)


@pytest.mark.parametrize("value", [None, True, "21.09", float("nan"), float("inf"), {}, []])
def test_invalid_balance_is_rejected(value):
    with pytest.raises(mod.BillingBrowserError, match="billing_invalid_response"):
        mod._observation({"total_available": value}, "org-test")


@pytest.mark.parametrize("value", [0, 21.09, -0.5])
def test_real_numeric_balance_and_only_allowlisted_data(value):
    observation = mod._observation({
        "total_available": value, "total_used": 4, "Authorization": "test-secret",
        "grants": [{"secret": "must-not-persist"}],
    }, "org-test")
    assert observation["credits"]["balance_remaining"] == value
    assert observation["raw"] == {
        "source": "browser_platform", "organization_id": "org-test", "total_available": value, "total_used": 4,
    }
    assert "test-secret" not in json.dumps(observation)


@pytest.mark.parametrize("url,method", [
    ("https://attacker.example/v1/dashboard/billing/credit_grants", "GET"),
    (mod.CREDIT_URL + "?query=1", "GET"), (mod.CREDIT_URL + "/", "GET"),
    (mod.CREDIT_URL, "POST"), (mod.CREDIT_URL.replace("https:", "http:"), "GET"),
])
def test_capture_is_exact_endpoint_only(browser, session, url, method):
    assert request(browser, session, url=url, method=method) is None


def test_only_platform_top_page_can_capture(browser, session):
    browser._event(session, "Page.frameNavigated", {"frame": {"url": "https://attacker.example"}})
    assert request(browser, session) is None


@pytest.mark.parametrize("headers,expected", [
    ({"openai-organization": "org-test"}, "org-test"),
    ({"OPENAI-ORGANIZATION": "org-test"}, "org-test"),
    ({"OpenAI-Organization": "invalid"}, None),
    ({"OpenAI-Organization": "org-test\nAuthorization: value"}, None),
])
def test_org_header_allowlist(headers, expected):
    assert mod._organization(headers) == expected


@pytest.mark.asyncio
async def test_capture_ready_and_confirm_never_exports_secrets(browser, session, monkeypatch):
    record = request(browser, session)
    assert "test-secret" not in json.dumps(record)
    record["status"] = 200
    monkeypatch.setattr(browser, "_call", AsyncMock(return_value={"body": json.dumps({"total_available": 21.09, "grants": ["secret"]})}))
    await browser._capture(session, "req", record)
    state = browser.status("provider")
    assert state["status"] == "ready"
    observation = browser.confirm("provider", state["flow_id"], state["observation_id"], "org-test")
    assert observation["credits"]["balance_remaining"] == 21.09
    assert session.expected_org == "org-test"
    assert browser.status("provider")["status"] == "connected"
    assert "grants" not in json.dumps(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize("org,status,kind", [
    (None, 200, "billing_organization_required"),
    ("org-other", 200, "billing_organization_mismatch"),
    ("org-test", 401, "billing_sign_in_required"),
    ("org-test", 500, "billing_invalid_response"),
])
async def test_bound_connection_fails_closed(browser, session, org, status, kind):
    session.expected_org = "org-test"
    previous = mod._observation({"total_available": 12}, "org-test")
    session.observation = previous
    record = request(browser, session, org=org)
    record["status"] = status
    await browser._capture(session, "req", record)
    assert session.error_kind == kind
    assert session.observation is previous
    assert session.status == "reconnect_required"


@pytest.mark.asyncio
async def test_old_generation_and_out_of_order_responses_are_ignored(browser, session, monkeypatch):
    record = request(browser, session)
    record["status"] = 200
    session.generation += 1
    call = AsyncMock()
    monkeypatch.setattr(browser, "_call", call)
    await browser._capture(session, "req", record)
    call.assert_not_called()
    record = request(browser, session)
    record["status"] = 200
    request(browser, session, "later")
    await browser._capture(session, "req", record)
    call.assert_not_called()


@pytest.mark.asyncio
async def test_generation_change_during_body_read_is_ignored(browser, session, monkeypatch):
    record = request(browser, session)
    record["status"] = 200

    async def body(*args):
        session.generation += 1
        return {"body": "{\"total_available\":21.09}"}

    monkeypatch.setattr(browser, "_call", body)
    await browser._capture(session, "req", record)
    assert session.observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'malformed'])
@pytest.mark.parametrize('advance', ['newer', 'generation'])
async def test_superseded_capture_failure_cannot_overwrite_current_observation(browser, session, monkeypatch, failure, advance):
    started, release = asyncio.Event(), asyncio.Event()
    async def body(_session, method, params):
        if params['requestId'] == 'old':
            started.set()
            await release.wait()
            if failure == 'timeout':
                raise mod.BillingBrowserError('billing_timeout')
            return {'body': 'not json'}
        return {'body': json.dumps({'total_available': 25})}
    monkeypatch.setattr(browser, '_call', body)
    def finish(request_id):
        request(browser, session, request_id)
        browser._event(session, 'Network.responseReceived', {
            'requestId': request_id, 'response': {'url': mod.CREDIT_URL, 'status': 200}})
        browser._event(session, 'Network.loadingFinished', {'requestId': request_id})
    finish('old')
    old = next(iter(session.body_tasks))
    await started.wait()
    if advance == 'generation':
        session.generation += 1
    finish('new')
    await asyncio.gather(*(task for task in session.body_tasks if task is not old))
    fresh_id = session.observation_id
    release.set()
    await old
    assert session.status == 'ready'
    assert session.error_kind is None
    assert session.observation_id == fresh_id
    assert browser.confirm('provider', session.flow_id, fresh_id, 'org-test')['credits']['balance_remaining'] == 25


def test_stale_network_loading_failure_does_not_downgrade_ready(browser, session):
    request(browser, session, 'old')
    request(browser, session, 'new')
    session.status = 'ready'
    browser._event(session, 'Network.loadingFailed', {'requestId': 'old'})
    assert session.status == 'ready'
    assert session.error_kind is None


@pytest.mark.asyncio
async def test_generation_only_advance_discards_failed_body_read(browser, session, monkeypatch):
    record = request(browser, session)
    record['status'] = 200
    async def body(*args):
        session.generation += 1
        raise mod.BillingBrowserError('billing_timeout')
    monkeypatch.setattr(browser, '_call', body)
    await browser._capture(session, 'req', record)
    assert session.error_kind is None


def test_stale_confirmation_cannot_bind(browser, session):
    session.status = "ready"
    session.observation_id = "observation"
    session.observation = mod._observation({"total_available": 21.09}, "org-test")
    session.observation["fetched_at"] -= mod.OBSERVATION_MAX_AGE + 1
    with pytest.raises(mod.BillingBrowserError, match="billing_stale_observation"):
        browser.confirm("provider", session.flow_id, "observation", "org-test")
    assert session.expected_org is None


@pytest.mark.asyncio
async def test_refresh_does_not_reload_unconfirmed_login(browser, session, monkeypatch):
    navigate = AsyncMock()
    monkeypatch.setattr(browser, "_navigate", navigate)
    with pytest.raises(mod.BillingBrowserError, match="billing_flow_changed"):
        await browser.refresh("provider", "org-test")
    navigate.assert_not_called()


@pytest.mark.asyncio
async def test_close_disposes_only_owned_page(browser, session, monkeypatch):
    session.target_id = "owned-tab"
    session.ws = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(mod.native_chrome, "cdp_get", close)
    await browser.close("provider")
    close.assert_awaited_once_with("/json/close/owned-tab")
    assert browser.status("provider")["status"] == "not_connected"


@pytest.mark.asyncio
async def test_restart_restores_owned_tab_and_expected_organization(browser, monkeypatch):
    async def open_session(session):
        assert session.expected_org == "org-test"
        session.observation = mod._observation({"total_available": 0}, "org-test")
        session.changed.set()

    monkeypatch.setattr(browser, "_open", open_session)
    observation = await browser.refresh("provider", "org-test")
    assert observation["credits"]["balance_remaining"] == 0


@pytest.mark.asyncio
async def test_refresh_timeout_is_bounded(browser, monkeypatch):
    monkeypatch.setattr(mod, "REFRESH_TIMEOUT", 0.01)

    async def hang(session):
        await asyncio.Event().wait()

    monkeypatch.setattr(browser, "_open", hang)
    with pytest.raises(mod.BillingBrowserError, match="billing_timeout"):
        await browser.refresh("provider", "org-test")


@pytest.mark.asyncio
async def test_expired_login_disposes_its_page(browser, session, monkeypatch):
    monkeypatch.setattr(mod, "LOGIN_TIMEOUT", 0)
    dispose = AsyncMock()
    monkeypatch.setattr(browser, "_dispose", dispose)
    await browser._expire(session)
    dispose.assert_awaited_once_with(session)
    assert session.status == "reconnect_required"


@pytest.mark.asyncio
async def test_reconcile_preserves_active_provider_tabs(browser, session, monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(browser, "close", close)
    await browser.reconcile({"provider"})
    close.assert_not_called()
    await browser.reconcile(set())
    close.assert_awaited_once_with("provider")


@pytest.mark.asyncio
async def test_auth_redirect_keeps_initial_login_interactive(browser, session):
    session.status = "ready"
    record = request(browser, session)
    record["status"] = 401
    await browser._capture(session, "req", record)
    assert browser.status("provider")["status"] == "pending"


@pytest.mark.asyncio
async def test_extra_info_org_header_is_used_without_retaining_other_headers(browser, session, monkeypatch):
    request(browser, session, org=None)
    browser._event(session, "Network.requestWillBeSentExtraInfo", {
        "requestId": "req", "headers": {"openai-organization": "org-test", "cookie": "test-secret"},
    })
    browser._event(session, "Network.responseReceived", {
        "requestId": "req", "response": {"url": mod.CREDIT_URL, "status": 200},
    })
    monkeypatch.setattr(browser, "_call", AsyncMock(return_value={"body": "{\"total_available\":0}"}))
    browser._event(session, "Network.loadingFinished", {"requestId": "req"})
    await asyncio.gather(*session.body_tasks)
    assert session.observation["organization_id"] == "org-test"
    assert "test-secret" not in json.dumps(session.extra_orgs)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "ready"])
@pytest.mark.parametrize("payload,method,params", [
    ({"type": "viewport", "width": 390, "height": 844},
     "Emulation.setDeviceMetricsOverride",
     {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False}),
    ({"type": "input", "kind": "text", "text": "login@example.test"},
     "Input.insertText", {"text": "login@example.test"}),
    ({"type": "input", "kind": "mouse", "event": "down", "x": 400, "y": 50, "clickCount": 1},
     "Input.dispatchMouseEvent",
     {"type": "mousePressed", "x": 390.0, "y": 50.0, "button": "left", "clickCount": 1, "modifiers": 0}),
    ({"type": "input", "kind": "key", "event": "down", "key": "Enter", "code": "Enter", "keyCode": 13},
     "Input.dispatchKeyEvent",
     {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0, "text": "\r"}),
    ({"type": "command", "command": "reload"}, "Page.reload", {"ignoreCache": False}),
    ({"type": "command", "command": "stop"}, "Page.stopLoading", {}),
])
async def test_login_input_uses_owned_cdp_under_operation_lock(
    browser, session, status, payload, method, params,
):
    session.status = status
    session.ws = AsyncMock()
    other = mod._Session("other-provider", ws=AsyncMock())
    browser._sessions[other.provider_id] = other

    async def reply(raw):
        assert browser.operation_lock(session.provider_id).locked()
        command = json.loads(raw)
        session.replies[command["id"]].set_result({})

    session.ws.send.side_effect = reply
    command = translate_input(payload, bounds=(390, 844))
    await browser.dispatch_input(session.provider_id, session.flow_id, command)

    session.ws.send.assert_awaited_once()
    assert json.loads(session.ws.send.await_args.args[0]) == {
        "id": 1, "method": method, "params": params,
    }
    other.ws.send.assert_not_awaited()
    assert not session.replies
    assert not browser.operation_lock(session.provider_id).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction,index,entry_id", [
    ("back", 1, 11), ("forward", 1, 33),
    ("back", 0, None), ("forward", 2, None),
    ("back", None, None), ("forward", None, None),
])
async def test_login_input_history_uses_owned_entries_and_respects_boundaries(
    browser, session, monkeypatch, direction, index, entry_id,
):
    session.ws = AsyncMock()

    async def history(owner, method, params):
        assert owner is session
        assert browser.operation_lock(session.provider_id).locked()
        return {
            "currentIndex": index or 0,
            "entries": [{"id": 11}, {"id": 22}, {"id": 33}] if index is not None else [],
        }

    send = AsyncMock(side_effect=history)
    monkeypatch.setattr(browser, "_call", send)
    command = translate_input({"type": "command", "command": direction})
    await browser.dispatch_input(session.provider_id, session.flow_id, command)

    expected = [call(session, "Page.getNavigationHistory", {})]
    if entry_id is not None:
        expected.append(call(session, "Page.navigateToHistoryEntry", {"entryId": entry_id}))
    assert send.await_args_list == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    "missing", "other-provider", "stale-flow", "closed", "no-socket",
    "connected", "refresh-pending", "refresh-ready", "unavailable", "reconnect_required",
])
async def test_login_input_rejects_noninteractive_or_unowned_flow(
    browser, session, monkeypatch, state,
):
    session.ws = AsyncMock()
    provider_id, flow_id = session.provider_id, session.flow_id
    if state == "missing":
        browser._sessions.clear()
    elif state == "other-provider":
        provider_id = "other-provider"
    elif state == "stale-flow":
        flow_id = "old-flow"
    elif state == "closed":
        session.closed = True
    elif state == "no-socket":
        session.ws = None
    elif state.startswith("refresh-"):
        session.status = state.removeprefix("refresh-")
        session.expected_org = "org-test"
    else:
        session.status = state
    send = AsyncMock()
    monkeypatch.setattr(browser, "_call", send)

    with pytest.raises(mod.BillingBrowserError, match="^billing_browser_unavailable$"):
        await browser.dispatch_input(provider_id, flow_id, {"method": "Page.reload"})

    send.assert_not_awaited()
    assert not browser.operation_lock(provider_id).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["replace", "close", "confirm", "refresh"])
async def test_login_input_cannot_survive_flow_lifecycle_change(
    browser, session, monkeypatch, transition,
):
    session.ws = AsyncMock()
    flow_id = session.flow_id

    async def open_session(owner):
        owner.ws = AsyncMock()
        if owner.expected_org:
            owner.observation = mod._observation({"total_available": 21.09}, "org-test")
            owner.changed.set()

    monkeypatch.setattr(browser, "_open", open_session)
    send = AsyncMock(return_value={"body": json.dumps({"total_available": 21.09})})
    monkeypatch.setattr(browser, "_call", send)
    try:
        if transition == "replace":
            replacement = await browser.start(session.provider_id)
            assert replacement["flow_id"] != flow_id
        elif transition == "close":
            await browser.close(session.provider_id)
        elif transition == "confirm":
            record = request(browser, session)
            record["status"] = 200
            await browser._capture(session, "req", record)
            browser.confirm(session.provider_id, flow_id, session.observation_id, "org-test")
        else:
            await browser.close(session.provider_id)
            await browser.refresh(session.provider_id, "org-test")
            assert browser._sessions[session.provider_id].expected_org == "org-test"

        send.reset_mock()
        with pytest.raises(mod.BillingBrowserError, match="^billing_browser_unavailable$"):
            await browser.dispatch_input(session.provider_id, flow_id, {"method": "Page.reload"})
        send.assert_not_awaited()
    finally:
        await browser.close(session.provider_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "Page.navigate", "Page.navigateToHistoryEntry", "Runtime.evaluate", "Browser.close",
])
async def test_login_input_rejects_unexpected_cdp_method(browser, session, monkeypatch, method):
    session.ws = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(browser, "_call", send)

    with pytest.raises(mod.BillingBrowserError, match="^billing_invalid_input$"):
        await browser.dispatch_input(session.provider_id, session.flow_id, {"method": method})

    send.assert_not_awaited()
