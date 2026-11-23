"""Routes for PR watch: the Settings > Usage limits and start/list/stop."""

import pytest

SETTINGS = "/api/v1/settings/pr-watch"
WATCHES = "/api/v1/pr-watches"


def test_settings_defaults_and_bounds(authenticated_client):
    body = authenticated_client.get(SETTINGS).json()
    assert body["mode"] == "fix" and body["max_wakes_per_day"] == 5 and body["max_usd_per_day"] == 3.0
    assert body["defaults"]["idle_days"] == 7
    assert body["limits"]["max_wakes_per_day"] == [0, 50]


def test_settings_update_keeps_other_values(authenticated_client):
    r = authenticated_client.put(SETTINGS, json={"max_wakes_per_day": 2, "trusted_bots": ["coderabbitai[bot]"]})
    assert r.status_code == 200, r.text
    r = authenticated_client.put(SETTINGS, json={"max_usd_per_day": 0.75})
    body = r.json()
    assert body["max_wakes_per_day"] == 2 and body["max_usd_per_day"] == 0.75
    assert body["trusted_bots"] == ["coderabbitai"]


@pytest.mark.parametrize("body", [{}, {"mode": "auto"}, {"max_usd_per_day": 500}, {"idle_days": 1.5}])
def test_settings_refuse_bad_values(authenticated_client, body):
    assert authenticated_client.put(SETTINGS, json=body).status_code == 400


def test_start_list_and_stop_a_watch(authenticated_client):
    body = {"repo": "acme/app", "pr_number": 7, "pr_url": "https://github.com/acme/app/pull/7", "pr_title": "Add"}
    r = authenticated_client.post(WATCHES, json=body)
    assert r.status_code == 200, r.text
    watch = r.json()
    assert watch["status"] == "active" and watch["session_id"] is None and watch["wakes_today"] == 0
    assert authenticated_client.post(WATCHES, json=body).json()["id"] == watch["id"]

    listed = authenticated_client.get(WATCHES).json()["watches"]
    assert [w["id"] for w in listed] == [watch["id"]]

    stopped = authenticated_client.delete(f"{WATCHES}/{watch['id']}").json()
    assert stopped["status"] == "stopped" and stopped["reason"] == "Stopped by you"
    assert authenticated_client.delete(f"{WATCHES}/no-such").status_code == 404
    # Watching again after a stop starts a new watch.
    assert authenticated_client.post(WATCHES, json=body).json()["id"] != watch["id"]


@pytest.mark.parametrize("body,status", [
    ({"repo": "not a repo", "pr_number": 1}, 400),
    ({"repo": "a/b", "pr_number": 1, "pr_url": "javascript:alert(1)"}, 400),
    ({"repo": "a/b", "pr_number": 0}, 422),
    ({"repo": "a/b", "pr_number": 1, "session_id": "missing-chat"}, 409),
])
def test_start_refuses_bad_input(authenticated_client, body, status):
    assert authenticated_client.post(WATCHES, json=body).status_code == status
