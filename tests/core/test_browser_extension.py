"""The worker side of the AI Shuffle browser extension (app/core/browser_extension.py)."""

import asyncio
import json

import pytest

from app.core import browser_extension as ext


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.closed = code


class FakeBrowser(ext.ExtensionBrowser):
    """An extension that answers requests from a table instead of a socket."""

    def __init__(self, answers):
        super().__init__(FakeSocket(), {"browser_id": "browser-0001", "name": "Chrome on Windows",
                                        "product": "Chrome", "version": "131.0"})
        self.answers = answers
        self.calls = []

    async def request(self, method, params, timeout=ext.REQUEST_TIMEOUT):
        self.calls.append((method, params))
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer


PAGE = {"targetId": "T1", "type": "page", "title": "Example", "url": "https://example.com/"}


@pytest.fixture(autouse=True)
def clean_state():
    yield
    ext._browsers.clear()
    ext._targets.clear()
    ext._relay_tokens.clear()
    ext._page_tokens.clear()


def relay_with(answers):
    browser = FakeBrowser(answers)
    socket = FakeSocket()
    return ext.Relay(socket, "chat-1", browser, "Fix the login page"), browser, socket


def test_auto_attach_opens_the_chats_tab_group_and_announces_its_tabs():
    relay, browser, socket = relay_with({"group.attach": {"tabs": [{"tabId": 7, "targetInfo": PAGE}]}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}))
    assert browser.calls == [("group.attach", {"group": "chat-1", "title": "Fix the login page"})]
    attached, reply = socket.sent
    assert attached["method"] == "Target.attachedToTarget"
    assert attached["params"]["targetInfo"]["targetId"] == "T1" and attached["params"]["targetInfo"]["attached"]
    assert reply == {"id": 1, "result": {}}
    # Page commands go to that tab through chrome.debugger.
    sid = attached["params"]["sessionId"]
    asyncio.run(relay.handle({"id": 2, "sessionId": sid, "method": "Runtime.evaluate", "params": {"expression": "1"}}))
    assert browser.calls[-1] == ("cdp", {"tabId": 7, "method": "Runtime.evaluate", "params": {"expression": "1"}})
    assert socket.sent[-1]["sessionId"] == sid


def test_new_pages_are_tabs_in_the_group_and_only_group_tabs_can_be_attached():
    relay, browser, socket = relay_with({"tab.create": {"tabId": 9, "targetInfo": {**PAGE, "targetId": "T9"}}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
    assert browser.calls == [("tab.create", {"group": "chat-1", "title": "Fix the login page", "url": "about:blank"})]
    assert socket.sent[-1] == {"id": 1, "result": {"targetId": "T9"}}
    asyncio.run(relay.handle({"id": 2, "method": "Target.attachToTarget", "params": {"targetId": "other-tab"}}))
    assert socket.sent[-1]["error"]["message"] == "No such target in this chat's tab group"


def test_extra_cdp_sessions_open_through_a_virtual_browser_session():
    """Playwright's newCDPSession attaches to the browser target, then to the page from there."""
    relay, browser, socket = relay_with({"group.attach": {"tabs": [{"tabId": 7, "targetInfo": PAGE}]}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}))
    primary = socket.sent[0]["params"]["sessionId"]
    asyncio.run(relay.handle({"id": 2, "method": "Target.attachToBrowserTarget", "params": {}}))
    root = socket.sent[-1]["result"]["sessionId"]
    asyncio.run(relay.handle({"id": 3, "sessionId": root, "method": "Target.attachToTarget",
                              "params": {"targetId": "T1", "flatten": True}}))
    assert socket.sent[-1]["sessionId"] == root
    extra = socket.sent[-1]["result"]["sessionId"]
    assert extra not in (primary, root)
    asyncio.run(relay.handle({"id": 4, "sessionId": extra, "method": "Page.enable", "params": {}}))
    assert browser.calls[-1] == ("cdp", {"tabId": 7, "method": "Page.enable", "params": {}})
    # Tab events reach both page sessions; detaching the extra one keeps the primary.
    asyncio.run(relay.on_extension_event("cdp", {"tabId": 7, "method": "Page.loadEventFired", "params": {}}))
    assert {m["sessionId"] for m in socket.sent[-2:]} == {primary, extra}
    asyncio.run(relay.handle({"id": 5, "sessionId": root, "method": "Target.detachFromTarget",
                              "params": {"sessionId": extra}}))
    assert extra not in relay.sessions and primary in relay.sessions
    asyncio.run(relay.handle({"id": 6, "method": "Target.detachFromTarget", "params": {"sessionId": root}}))
    assert root not in relay.browser_sessions


def test_browser_level_commands_the_extension_cannot_do_are_refused():
    relay, browser, socket = relay_with({})
    asyncio.run(relay.handle({"id": 3, "method": "Browser.close", "params": {}}))
    assert "not available" in socket.sent[-1]["error"]["message"]
    assert browser.calls == []
    asyncio.run(relay.handle({"id": 4, "method": "Browser.getVersion", "params": {}}))
    assert socket.sent[-1]["result"]["product"] == "Chrome/131.0"


def test_events_reach_only_the_relay_that_owns_the_tab_and_a_closed_tab_detaches():
    relay, browser, socket = relay_with({"group.attach": {"tabs": [{"tabId": 7, "targetInfo": PAGE}]}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.setAutoAttach", "params": {}}))
    sid = socket.sent[0]["params"]["sessionId"]
    socket.sent.clear()
    asyncio.run(relay.on_extension_event("cdp", {"tabId": 8, "method": "Page.loadEventFired", "params": {}}))
    assert socket.sent == []
    asyncio.run(relay.on_extension_event("cdp", {"tabId": 7, "method": "Page.loadEventFired", "params": {}}))
    assert socket.sent == [{"method": "Page.loadEventFired", "params": {}, "sessionId": sid}]
    asyncio.run(relay.on_extension_event("detached", {"tabId": 7, "reason": "tab_closed"}))
    assert socket.sent[-1] == {"method": "Target.detachedFromTarget", "params": {"sessionId": sid, "targetId": "T1"}}
    assert relay.targets == {}


def test_tabs_a_group_tab_opens_join_only_that_chat():
    relay, browser, socket = relay_with({"group.attach": {"tabs": []}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.setAutoAttach", "params": {}}))
    asyncio.run(relay.on_extension_event("tab_created", {"group": "chat-2", "tabId": 11, "targetInfo": PAGE}))
    assert relay.targets == {}
    asyncio.run(relay.on_extension_event("tab_created", {"group": "chat-1", "tabId": 11, "targetInfo": PAGE}))
    assert 11 in relay.targets and socket.sent[-1]["method"] == "Target.attachedToTarget"


def test_runtime_env_needs_a_connected_browser_and_a_relay_token_is_single_target():
    from app.core.browser_runtime import RuntimeUnavailable

    browser = FakeBrowser({})
    assert ext.runtime_env("chat-1", None) == {}
    with pytest.raises(ext.ExtensionError):
        ext.set_session_target("chat-1", browser.id)
    ext._browsers[browser.id] = browser
    ext.set_session_target("chat-1", browser.id)
    env = ext.runtime_env("chat-1", "http://127.0.0.1:8123/api/v1/browser/panel/register")
    assert env["AIS_BROWSER_TARGET"] == "extension"
    prefix = "ws://127.0.0.1:8123/api/v1/browser-extension/cdp/"
    assert env["AIS_BROWSER_CDP_ENDPOINT"].startswith(prefix)
    token = env["AIS_BROWSER_CDP_ENDPOINT"][len(prefix):]
    assert ext.claim_relay(token) == ("chat-1", browser)
    # One use: a second relay cannot connect with the same token.
    assert ext.claim_relay(token) is None
    # Switching the chat back to the built-in browser voids an unused token.
    unused = ext.runtime_env("chat-1", None)["AIS_BROWSER_CDP_ENDPOINT"].rsplit("/", 1)[1]
    ext.set_session_target("chat-1", None)
    assert ext.session_assigned("chat-1") and ext.session_target("chat-1") is None
    assert ext.claim_relay(unused) is None
    ext.set_session_target("chat-1", browser.id)
    browser.closed = True
    with pytest.raises(RuntimeUnavailable):
        ext.runtime_env("chat-1", None)


def test_a_stopped_runtime_drops_its_unused_relay_token():
    browser = FakeBrowser({})
    ext._browsers[browser.id] = browser
    ext.set_session_target("chat-1", browser.id)
    token = ext.runtime_env("chat-1", None, "runtime-1")["AIS_BROWSER_CDP_ENDPOINT"].rsplit("/", 1)[1]
    ext.forget_runtime("runtime-2")
    ext.forget_runtime(None)
    assert token in ext._relay_tokens
    ext.forget_runtime("runtime-1")
    assert ext.claim_relay(token) is None


def test_only_browsers_the_caller_may_use_are_listed_or_selectable():
    mine, theirs = FakeBrowser({}), FakeBrowser({})
    mine.account_id, theirs.id, theirs.account_id = "acct-me", "browser-0002", "acct-other"
    ext._browsers.update({mine.id: mine, theirs.id: theirs})
    may_use = lambda browser: browser.account_id == "acct-me"  # noqa: E731
    assert [b["id"] for b in ext.browsers(may_use)] == [mine.id]
    with pytest.raises(ext.ExtensionError):
        ext.set_session_target("chat-1", theirs.id, may_use)
    assert not ext.session_assigned("chat-1")
    ext.set_session_target("chat-1", mine.id, may_use)
    assert ext.session_target("chat-1") == mine.id


class PanelSocket(FakeSocket):
    """The Browser panel's side of a page socket: frames queued for the worker."""

    def __init__(self):
        super().__init__()
        self.inbox = asyncio.Queue()
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return await self.inbox.get()


def test_the_browser_panel_streams_a_group_tab_over_a_page_socket(monkeypatch):
    monkeypatch.setenv("BROWSER_PANEL_REGISTER_URL", "http://127.0.0.1:8123/api/v1/browser/panel/register")
    relay, browser, _ = relay_with({"group.attach": {"tabs": [{"tabId": 7, "targetInfo": PAGE}]},
                                    "cdp": {"frameId": "F1"}})
    asyncio.run(relay.handle({"id": 1, "method": "Target.setAutoAttach", "params": {}}))
    ext._browsers[browser.id] = browser
    browser.relays[relay.key] = relay
    # Only a chat that uses this browser, and only a tab in its group, gets one.
    assert ext.page_endpoint("chat-1", "T1") is None
    ext.set_session_target("chat-1", browser.id)
    assert ext.page_endpoint("chat-1", "someone-elses-tab") is None
    prefix = "ws://127.0.0.1:8123/api/v1/browser-extension/page/"
    url = ext.page_endpoint("chat-1", "T1")
    assert url.startswith(prefix)

    async def scenario():
        panel = PanelSocket()
        served = asyncio.create_task(ext.serve_page(panel, url[len(prefix):]))
        await panel.inbox.put(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        await panel.inbox.put(json.dumps({"id": 2, "method": "Page.startScreencast", "params": {"format": "jpeg"}}))
        for _ in range(20):
            await asyncio.sleep(0)
        # bringToFront shows the tab in its window without raising the window.
        assert ("tab.activate", {"tabId": 7}) in browser.calls
        assert ("cdp", {"tabId": 7, "method": "Page.startScreencast", "params": {"format": "jpeg"}}) in browser.calls
        assert {"id": 2, "result": {"frameId": "F1"}} in panel.sent
        # The tab's own events reach the panel; another tab's, or a child session's, do not.
        await browser.dispatch({"event": "cdp", "tabId": 8, "method": "Page.screencastFrame", "params": {}})
        await browser.dispatch({"event": "cdp", "tabId": 7, "sessionId": "child", "method": "Page.screencastFrame", "params": {}})
        await browser.dispatch({"event": "cdp", "tabId": 7, "method": "Page.screencastFrame", "params": {"sessionId": 3}})
        assert [m for m in panel.sent if "method" in m] == [{"method": "Page.screencastFrame", "params": {"sessionId": 3}}]
        # Closing the tab ends the panel's socket and the tap goes away.
        await browser.dispatch({"event": "detached", "tabId": 7})
        await asyncio.wait_for(served, 1)
        assert panel.accepted and not any(k.startswith("page-") for k in browser.relays)

    asyncio.run(scenario())
    # A page token is single use.
    assert ext._page_tokens == {}


def test_a_page_socket_with_an_unknown_token_is_refused():
    panel = PanelSocket()
    asyncio.run(ext.serve_page(panel, "nope"))
    assert panel.closed == 4004 and not panel.accepted


def test_only_listed_extension_ids_may_frame_the_phone_ui(monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_BROWSER_EXTENSION_IDS", "abcdefghijklmnopabcdefghijklmnop, not-an-id;evil")
    ancestors = ext.extension_frame_ancestors().split()
    assert ancestors == ["chrome-extension://fnjpjlkegjhjepkdnnignfokaffgdmak",
                         "chrome-extension://abcdefghijklmnopabcdefghijklmnop"]
