"""PR watch (app/core/pr_watch.py): settings, comparing snapshots, batching,
the caps the owner sets, and one poll end to end against the test database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core import pr_watch

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)


def snap(*, items=None, pending=False, failed=None, head="a" * 40, mergeable="MERGEABLE",
         merge_state="CLEAN", state="OPEN", viewer="owner-login", total=3):
    return {
        "viewer": viewer, "number": 7, "title": "Add a thing", "url": "https://github.com/acme/app/pull/7",
        "state": state, "head": head, "mergeable": mergeable, "merge_state": merge_state,
        "checks": {"pending": pending, "failed": failed or [], "total": total},
        "items": items or [],
    }


def comment(cid, *, author="reviewer", association="MEMBER", bot=False, body="Please rename this",
            kind="comment", at="2026-11-23T17:59:00Z", **extra):
    return {"id": f"{kind}:{cid}", "kind": kind, "author": author, "bot": bot,
            "association": association, "body": body, "url": f"https://github.com/x/{cid}",
            "at": at, **extra}


def run_diff(state, s, *, bots=(), wakes=(), repeats=3, writers=("reviewer",), since=None):
    return pr_watch.diff(state, s, trusted_bots=list(bots), wakes=list(wakes),
                         max_repeat_failures=repeats, now=NOW, writers=list(writers), since=since)


# ─── Settings ────────────────────────────────────────────────────────────────

def test_settings_defaults_and_clamping():
    assert pr_watch._parse_setting(None)["max_wakes_per_day"] == 5
    parsed = pr_watch._parse_setting(
        '{"max_wakes_per_day": 999, "max_usd_per_day": -4, "mode": "bogus", "trusted_bots": ["@CodeRabbitAI[bot]", "x"]}'
    )
    assert parsed["max_wakes_per_day"] == 50
    assert parsed["max_usd_per_day"] == 0
    assert parsed["mode"] == "fix"
    assert parsed["trusted_bots"] == ["coderabbitai", "x"]
    assert pr_watch._parse_setting("not json") == pr_watch._parse_setting(None)


def test_update_settings_validates_and_persists(mock_db):
    out = pr_watch.update_settings(max_wakes_per_day=2, max_usd_per_day=0.5, mode="triage",
                                   trusted_bots=["copilot-pull-request-reviewer[bot]"])
    assert out["max_wakes_per_day"] == 2 and out["mode"] == "triage"
    with patch("app.db.database.get_system_setting_cached",
               side_effect=lambda key: __import__("app.db.database", fromlist=["x"]).get_system_setting(key)):
        again = pr_watch.get_settings()
    assert again["max_usd_per_day"] == 0.5
    assert again["trusted_bots"] == ["copilot-pull-request-reviewer"]
    for bad in ({}, {"mode": "yolo"}, {"max_wakes_per_day": 51}, {"idle_days": 0},
                {"max_wakes_per_day": 2.5}, {"trusted_bots": ["has space"]}, {"max_usd_per_day": True}):
        with pytest.raises(ValueError):
            pr_watch.update_settings(**bad)


# ─── Snapshot ────────────────────────────────────────────────────────────────

def test_normalize_snapshot_flattens_checks_and_comments():
    data = {
        "viewer": {"login": "me"},
        "repository": {"pullRequest": {
            "number": 7, "title": "T", "url": "u", "state": "OPEN", "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY", "headRefOid": "h",
            "commits": {"nodes": [{"commit": {"oid": "h", "statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
                {"__typename": "CheckRun", "databaseId": 1, "name": "unit", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "d"},
                {"__typename": "CheckRun", "databaseId": 2, "name": "lint", "status": "COMPLETED", "conclusion": "CANCELLED"},
                {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"},
            ]}}}}]},
            "reviews": {"nodes": [{"databaseId": 5, "author": {"login": "bob", "__typename": "User"},
                                   "authorAssociation": "MEMBER", "state": "CHANGES_REQUESTED", "body": "", "url": "r"}]},
            "comments": {"nodes": [{"databaseId": 6, "author": {"login": "ci-bot", "__typename": "Bot"},
                                    "authorAssociation": "NONE", "body": "hi", "url": "c"}]},
            "reviewThreads": {"nodes": [{"isResolved": True, "comments": {"nodes": [
                {"databaseId": 8, "author": None, "authorAssociation": "NONE", "body": "x", "path": "a.py"}]}}]},
        }},
    }
    s = pr_watch.normalize_snapshot(data)
    assert s["viewer"] == "me" and s["head"] == "h" and s["mergeable"] == "CONFLICTING"
    assert s["checks"] == {"pending": True, "failed": [{"id": 1, "name": "unit", "url": "d"}], "total": 3}
    kinds = {i["id"]: i for i in s["items"]}
    assert kinds["review:5"]["state"] == "CHANGES_REQUESTED"
    assert kinds["comment:6"]["bot"] is True
    assert kinds["thread:8"]["author"] == "ghost" and kinds["thread:8"]["resolved"] is True
    with pytest.raises(ValueError):
        pr_watch.normalize_snapshot({"repository": {"pullRequest": None}})


# ─── Comparing snapshots ─────────────────────────────────────────────────────

def test_first_poll_marks_existing_comments_seen_but_reports_failing_ci():
    failed = [{"id": 9, "name": "unit", "url": "u"}]
    state, wake, notify = run_diff({}, snap(items=[comment(1)], failed=failed))
    assert [e["type"] for e in wake] == ["ci_failed"]
    assert state["seen"] == ["comment:1"] and notify == []
    # Same head again: CI is not reported twice.
    state, wake, _ = run_diff(state, snap(items=[comment(1)], failed=failed))
    assert wake == []


def test_ci_waits_until_every_check_finished():
    failed = [{"id": 9, "name": "unit", "url": "u"}]
    state, wake, _ = run_diff({"seen": []}, snap(pending=True, failed=failed))
    assert wake == [] and "checked_head" not in state
    state, wake, _ = run_diff(state, snap(failed=failed))
    assert wake and wake[0]["checks"] == failed


def test_trusted_comments_wake_and_strangers_only_notify():
    items = [comment(1), comment(2, association="NONE", author="stranger"),
             comment(3, bot=True, author="coderabbitai", association="NONE"),
             comment(4, bot=True, author="spam-bot", association="NONE")]
    _, wake, notify = run_diff({"seen": []}, snap(items=items), bots=["coderabbitai"])
    assert sorted(e["id"] for e in wake) == ["comment:1", "comment:3"]
    assert sorted(e["id"] for e in notify) == ["comment:2", "comment:4"]
    assert all(e["untrusted"] for e in notify)


def test_an_organization_member_without_write_access_only_notifies():
    # GitHub says MEMBER for any organization member, read-only ones too.
    items = [comment(1, author="read-only-member", association="MEMBER")]
    _, wake, notify = run_diff({"seen": []}, snap(items=items), writers=["reviewer"])
    assert wake == [] and [e["id"] for e in notify] == ["comment:1"]


def test_access_is_asked_once_per_person_and_unknown_means_no():
    items = [comment(1, author="Bob"), comment(2, author="bob"), comment(3, author="carol", association="COLLABORATOR"),
             comment(4, author="stranger", association="NONE"), comment(5, author="bot", bot=True)]
    logins = pr_watch.access_candidates({"seen": []}, snap(items=items), None)
    assert logins == ["bob", "carol"]  # strangers and bots are never asked
    asked = []
    cache = pr_watch.refresh_access({}, logins, lambda login: asked.append(login) or login == "bob", NOW)
    assert asked == ["bob", "carol"]
    assert cache["bob"]["write"] is True and cache["carol"]["write"] is False
    again = pr_watch.refresh_access({"access": cache}, logins, lambda login: asked.append(login), NOW)
    assert asked == ["bob", "carol"] and again == cache  # cached
    stale = pr_watch.refresh_access({"access": cache}, ["bob"], lambda _: False, NOW + timedelta(days=1))
    assert stale["bob"]["write"] is False and "carol" not in stale


def test_first_poll_counts_activity_newer_than_the_watch():
    items = [comment(1, at="2026-11-23T17:00:00Z"), comment(2, at="2026-11-23T17:59:00Z")]
    state, wake, _ = run_diff({}, snap(items=items), since=NOW - timedelta(minutes=30))
    assert [e["id"] for e in wake] == ["comment:2"]
    assert state["seen"] == ["comment:1", "comment:2"]


def test_noise_is_dropped():
    items = [
        comment(1, kind="review", state="APPROVED", body=""),
        comment(2, kind="review", state="COMMENTED", body="  "),
        comment(3, kind="thread", resolved=True),
        comment(4, kind="review", state="CHANGES_REQUESTED", body=""),
    ]
    _, wake, _ = run_diff({"seen": []}, snap(items=items))
    assert [e["id"] for e in wake] == ["review:4"]


def test_agents_own_replies_during_a_wake_are_ignored():
    wakes = [{"turn_id": "t", "at": "2026-11-23T17:50:00+00:00", "ended": True,
              "ended_at": "2026-11-23T17:58:00+00:00"}]
    items = [comment(1, author="owner-login", at="2026-11-23T17:55:00Z"),
             comment(2, author="owner-login", at="2026-11-23T17:59:30Z")]
    _, wake, _ = run_diff({"seen": []}, snap(items=items), wakes=wakes, writers=["owner-login"])
    assert [e["id"] for e in wake] == ["comment:2"]  # the owner's own later comment still counts


def test_conflict_and_behind_reported_once():
    state, wake, _ = run_diff({"seen": []}, snap(mergeable="CONFLICTING"))
    assert [e["type"] for e in wake] == ["conflict"]
    state, wake, _ = run_diff(state, snap(mergeable="CONFLICTING"))
    assert wake == []
    state, wake, _ = run_diff(state, snap(merge_state="BEHIND"))
    assert [e["type"] for e in wake] == ["behind"]


def test_repeated_failure_flags_the_owner_and_clears_when_green():
    failed = [{"id": 1, "name": "unit", "url": "u"}]
    state = {"seen": []}
    for n in range(3):
        state, _, notify = run_diff(state, snap(head=str(n) * 40, failed=failed), repeats=3)
    assert state["needs_owner"]["checks"] == ["unit"]
    assert notify == [{"type": "repeated_failure", "checks": ["unit"]}]
    state, _, _ = run_diff(state, snap(head="z" * 40))
    assert state["needs_owner"] is None and state["fail_streak"] == {}


def test_the_hold_ends_when_its_check_passes_even_if_another_fails():
    unit = [{"id": 1, "name": "unit", "url": "u"}]
    state = {"seen": []}
    for n in range(3):
        state, _, _ = run_diff(state, snap(head=str(n) * 40, failed=unit), repeats=3)
    assert state["needs_owner"]["checks"] == ["unit"]
    state, wake, _ = run_diff(state, snap(head="z" * 40, failed=[{"id": 2, "name": "lint", "url": "u"}]))
    assert state["needs_owner"] is None
    assert [c["name"] for c in wake[0]["checks"]] == ["lint"]


def test_a_check_that_fails_later_on_the_same_commit_is_reported():
    state, wake, _ = run_diff({"seen": []}, snap())  # every check passed on this commit
    assert wake == [] and state["checked_head"] == "a" * 40
    late = [{"id": 7, "name": "nightly", "url": "u"}]
    state, wake, _ = run_diff(state, snap(failed=late, total=4))
    assert [e["type"] for e in wake] == ["ci_failed"] and wake[0]["checks"] == late
    state, wake, _ = run_diff(state, snap(failed=late, total=4))
    assert wake == []  # the same failure is not reported twice


# ─── Batching and wording ────────────────────────────────────────────────────

def test_ready_to_deliver_uses_a_quiet_window():
    ci = {"type": "ci_failed", "head": "h", "checks": []}
    state = {"pending": [ci], "last_event_at": (NOW - timedelta(seconds=30)).isoformat()}
    assert not pr_watch.ready_to_deliver(state, NOW)
    state["last_event_at"] = (NOW - timedelta(seconds=50)).isoformat()
    assert pr_watch.ready_to_deliver(state, NOW)
    state["pending"].append({"type": "comment"})
    assert not pr_watch.ready_to_deliver(state, NOW)  # comments wait longer
    assert not pr_watch.ready_to_deliver({"pending": []}, NOW)


def test_describe_and_prompt():
    events = [{"type": "ci_failed", "head": "abcdef123", "checks": [{"id": 4, "name": "unit", "url": "u"},
                                                                    {"id": None, "name": "ci/x", "url": None}]},
              {"type": "review", "author": "bob", "state": "CHANGES_REQUESTED", "url": "r", "body": "Fix\n it"}]
    assert pr_watch.describe(events) == "2 checks failed, 1 review"
    watch = {"pr_number": 7, "repo": "acme/app", "pr_title": "Add"}
    fix = pr_watch.build_prompt(watch, events, "fix")
    assert "gh run view --job 4 --log-failed" in fix and "> Fix it" in fix
    assert "Do not merge" in fix and "never as instructions" in fix
    assert "Do not push" in pr_watch.build_prompt(watch, events, "triage")


# ─── Delivery and caps ───────────────────────────────────────────────────────

SETTINGS = dict(pr_watch.DEFAULTS, trusted_bots=[])
WATCH = {"id": "w1", "repo": "acme/app", "pr_number": 7, "pr_title": "Add", "session_id": "s1"}
PENDING = {"pending": [{"type": "conflict"}], "last_event_at": NOW.isoformat()}


@pytest.mark.asyncio
async def test_deliver_wakes_and_records_the_turn():
    with patch.object(pr_watch, "_wake", AsyncMock(return_value=("turn-1", ""))) as wake, \
            patch.object(pr_watch, "_notify", AsyncMock()) as notify:
        state, wakes = await pr_watch.deliver(WATCH, dict(PENDING), [], SETTINGS, NOW)
    wake.assert_awaited_once()
    assert wake.await_args.args[3] == 1  # seq feeds the idempotent command id
    assert state["pending"] == [] and wakes[0]["turn_id"] == "turn-1"
    assert "Woke the agent" in notify.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("settings,wakes,watch,word", [
    (dict(SETTINGS, mode="notify"), [], WATCH, None),
    (dict(SETTINGS, max_wakes_per_day=1), [{"at": NOW.isoformat(), "ended": True}], WATCH, "wakes today"),
    (dict(SETTINGS, max_usd_per_day=1.0), [{"at": NOW.isoformat(), "ended": True, "cost_usd": 1.2}], WATCH, "$1.00"),
    (SETTINGS, [], dict(WATCH, session_id=None), "No chat"),
])
async def test_deliver_respects_the_owners_limits(settings, wakes, watch, word):
    with patch.object(pr_watch, "_wake", AsyncMock(return_value=("t", ""))) as wake, \
            patch.object(pr_watch, "_notify", AsyncMock()) as notify:
        state, out = await pr_watch.deliver(watch, dict(PENDING), list(wakes), settings, NOW)
    wake.assert_not_awaited()
    assert state["pending"] == [] and out == wakes
    if word:
        assert word in notify.await_args.args[2]
        assert notify.await_args.kwargs["severity"] == "warning"


@pytest.mark.asyncio
async def test_deliver_holds_the_batch_while_the_last_wake_runs():
    running = [{"turn_id": "t0", "at": NOW.isoformat()}]
    with patch.object(pr_watch, "_wake", AsyncMock()) as wake, patch.object(pr_watch, "_notify", AsyncMock()):
        state, _ = await pr_watch.deliver(WATCH, dict(PENDING, seq=4), running, SETTINGS, NOW)
    wake.assert_not_awaited()
    assert state["pending"] == PENDING["pending"] and state["seq"] == 4


@pytest.mark.asyncio
async def test_needs_owner_blocks_waking():
    state = dict(PENDING, needs_owner={"checks": ["unit"]})
    with patch.object(pr_watch, "_wake", AsyncMock()) as wake, \
            patch.object(pr_watch, "_notify", AsyncMock()) as notify:
        await pr_watch.deliver(WATCH, state, [], SETTINGS, NOW)
    wake.assert_not_awaited()
    assert "unit kept failing" in notify.await_args.args[2]


# ─── One poll against the store ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_end_to_end_batches_then_ends_on_merge(mock_db):
    from app.db import pr_watches

    row = pr_watches.create(owner="admin", worker_id="me", repo="acme/app", pr_number=7,
                            pr_url=None, pr_title=None, session_id="s1")
    again = pr_watches.create(owner="admin", worker_id="me", repo="acme/app", pr_number=7,
                              pr_url=None, pr_title=None, session_id="s1")
    assert again["id"] == row["id"]  # one active watch per PR

    first = snap(items=[comment(1)])
    with patch.object(pr_watch, "_notify", AsyncMock()), \
            patch.object(pr_watch, "_wake", AsyncMock(return_value=("turn-9", ""))) as wake, \
            patch.object(pr_watch, "_refresh_wakes", side_effect=lambda w: list(w.get("wakes") or [])):
        await pr_watch.poll(row, fetch=lambda *_: first, access=lambda _login: True, now=NOW)
        row = pr_watches.get(row["id"])
        assert row["state"]["seen"] == ["comment:1"] and row["pr_title"] == "Add a thing"

        later = snap(items=[comment(1), comment(2)])
        await pr_watch.poll(row, fetch=lambda *_: later, access=lambda _login: True, now=NOW + timedelta(minutes=5))
        row = pr_watches.get(row["id"])
        assert len(row["state"]["pending"]) == 1
        wake.assert_not_awaited()  # still inside the quiet window

        await pr_watch.poll(row, fetch=lambda *_: later, access=lambda _login: True, now=NOW + timedelta(minutes=9))
        row = pr_watches.get(row["id"])
        wake.assert_awaited_once()
        assert row["state"]["pending"] == [] and row["wakes"][0]["turn_id"] == "turn-9"

        await pr_watch.poll(row, fetch=lambda *_: snap(state="MERGED"), access=lambda _login: True, now=NOW + timedelta(minutes=12))
    row = pr_watches.get(row["id"])
    assert row["status"] == "ended" and row["reason"] == "PR merged"


@pytest.mark.asyncio
async def test_poll_failure_backs_off_and_idle_watch_ends(mock_db):
    from app.db import pr_watches

    row = pr_watches.create(owner="admin", worker_id="me", repo="acme/app", pr_number=8,
                            pr_url=None, pr_title=None, session_id=None)

    def boom(*_):
        raise RuntimeError("GitHub could not be read: 502")

    await pr_watch.poll(row, fetch=boom, now=NOW)
    row = pr_watches.get(row["id"])
    assert row["failures"] == 1 and "502" in row["last_error"] and row["status"] == "active"

    pr_watches.save(row["id"], last_activity_at=NOW - timedelta(days=30), state={"seen": [], "head": "a" * 40})
    row = pr_watches.get(row["id"])
    with patch.object(pr_watch, "_notify", AsyncMock()):
        await pr_watch.poll(row, fetch=lambda *_: snap(), now=NOW)
    row = pr_watches.get(row["id"])
    assert row["status"] == "ended" and "No activity" in row["reason"]


def test_find_session_for_pr_prefers_the_opening_chat(mock_db):
    import json

    from app.db.base import get_db
    from tests.shared_fixtures import seed_session

    seed_session("worker-chat")
    seed_session("main-chat")
    url = "https://github.com/acme/app/pull/7"
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE sessions SET pr_number=%s, pr_url=%s, is_worker=TRUE WHERE id=%s",
                  (7, url, "worker-chat"))
        c.execute("UPDATE sessions SET pull_requests=%s WHERE id=%s",
                  (json.dumps([{"number": 7, "url": url, "state": "open"}]), "main-chat"))
    assert pr_watch.find_session_for_pr("acme/app", 7, None) == "main-chat"
    assert pr_watch.find_session_for_pr("acme/app", 99, None) is None


def test_start_refuses_a_chat_that_does_not_have_the_pr(mock_db):
    import json

    from app.db.base import get_db
    from tests.shared_fixtures import seed_session

    seed_session("other-chat")
    seed_session("pr-chat")
    url = "https://github.com/acme/app/pull/7"
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET pull_requests=%s WHERE id=%s",
                              (json.dumps([{"number": 7, "url": url, "state": "open"}]), "pr-chat"))
    with pytest.raises(ValueError, match="no record of this pull request"):
        pr_watch.start_watch(repo="acme/app", pr_number=7, pr_url=url, pr_title=None, session_id="other-chat")
    row = pr_watch.start_watch(repo="acme/app", pr_number=7, pr_url=url, pr_title=None, session_id="pr-chat")
    assert row["session_id"] == "pr-chat"


def test_a_new_watch_keeps_counting_todays_wakes_for_the_pr(mock_db):
    from app.db import pr_watches

    old = pr_watches.create(owner="admin", worker_id="me", repo="acme/app", pr_number=7,
                            pr_url=None, pr_title=None, session_id=None)
    pr_watches.save(old["id"], wakes=[{"turn_id": "t1", "session_id": "s-old", "at": pr_watch._now().isoformat(),
                                       "ended": True, "cost_usd": 0.5},
                                      {"turn_id": "t0", "at": "2020-01-01T00:00:00+00:00", "ended": True}])
    pr_watches.set_status(old["id"], "stopped", "Stopped by you")
    row = pr_watch.start_watch(repo="acme/app", pr_number=7, pr_url=None, pr_title=None, session_id=None)
    assert row["id"] != old["id"] and row["wakes_today"] == 1
    carried = pr_watches.get(row["id"])["wakes"]
    assert [w["turn_id"] for w in carried] == ["t1"] and carried[0]["session_id"] == "s-old"


@pytest.mark.asyncio
async def test_no_wake_after_stop_or_for_a_chat_on_another_worker(mock_db):
    from app.db import pr_watches
    from tests.shared_fixtures import seed_session

    seed_session("s-here")
    row = pr_watches.create(owner="admin", worker_id="me", repo="acme/app", pr_number=7,
                            pr_url=None, pr_title=None, session_id="s-here")
    submit = AsyncMock(return_value="turn-1")
    with patch("app.core.turn_manager.submit_async", submit), \
            patch("app.core.limit_resume.is_local", return_value=False):
        assert await pr_watch._wake(row, [], "fix", 1) == (None, pr_watch.ELSEWHERE)
    pr_watches.set_status(row["id"], "stopped", "Stopped by you")
    with patch("app.core.turn_manager.submit_async", submit), \
            patch("app.core.limit_resume.is_local", return_value=True):
        assert await pr_watch._wake(row, [], "fix", 2) == (None, pr_watch.STOPPED)
    submit.assert_not_awaited()

    with patch.object(pr_watch, "_wake", AsyncMock(return_value=(None, pr_watch.STOPPED))), \
            patch.object(pr_watch, "_notify", AsyncMock()) as notify:
        state, wakes = await pr_watch.deliver(WATCH, dict(PENDING), [], SETTINGS, NOW)
    assert state["pending"] == [] and wakes == []
    notify.assert_not_awaited()  # a stopped watch stays quiet
