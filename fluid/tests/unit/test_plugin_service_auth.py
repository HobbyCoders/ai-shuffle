"""A plugin service's declared OAuth sign-in (``services[].auth``)."""

import copy
import json
from pathlib import Path

import pytest
from fluid.context.plugin_authoring import (
    PluginDraftError,
    build_files,
    check_draft_files,
    check_service_auth,
    check_services,
    describe_service_auth,
    read_draft_files,
    registrable_domain,
    save_authored_plugin,
)
from fluid.context.plugin_check import check_plugin_folder

AUTH = {
    "type": "oauth2",
    "authorize_url": "https://dash.example.com/oauth2/auth",
    "token_url": "https://dash.example.com/oauth2/token",
    "scopes": ["zone:read", "dns.records:read"],
    "pkce": True,
}


def _service(**auth_changes) -> dict:
    auth = {**AUTH, **auth_changes}
    auth = {k: v for k, v in auth.items() if v is not None}
    return {"name": "example", "provider": "custom", "url": "https://api.example.com/v4",
            "access": "read", "purpose": "Read DNS records", "auth": auth}


def _refusal(entry: dict) -> str:
    with pytest.raises(PluginDraftError) as caught:
        check_services([entry])
    return str(caught.value)


def test_a_declared_sign_in_is_kept_in_a_stable_shape() -> None:
    entry = _service(revoke_url="https://api.example.com/oauth2/revoke")
    [checked] = check_services([entry])
    assert checked["auth"] == {**AUTH, "revoke_url": "https://api.example.com/oauth2/revoke"}
    # The normalised result checks to itself, so a draft copied between
    # devices (check_draft_files compares exactly) keeps passing.
    assert check_services([checked]) == [checked]
    assert describe_service_auth(checked["auth"]) == (
        "signs in with OAuth at dash.example.com; scopes: zone:read, dns.records:read"
    )


@pytest.mark.parametrize(("entry", "expected"), [
    ({**_service(), "auth": "oauth2"}, "auth must be an object"),
    (_service(client_secret="s3cr3t"), "never carries an OAuth client secret"),
    (_service(clientSecret="s3cr3t"), "never carries an OAuth client secret"),
    ({**_service(), "client_secret": "s3cr3t"}, "never hold a secret such as 'client_secret'"),
    (_service(client_id="has space"), "auth client_id must be the public client ID"),
    (_service(client_id=42), "auth client_id must be the public client ID"),
    (_service(client_id="ghp_" + "a" * 36), "client_id looks like a credential"),
    (_service(client_id="abc", pkce=False), "a shipped client_id is a public client and needs \"pkce\": true"),
    (_service(client_id="abc", pkce=None), "a shipped client_id is a public client and needs \"pkce\": true"),
    (_service(client_id="abc", client_secret="s3cr3t"), "never carries an OAuth client secret"),
    (_service(registration_url="http://dash.example.com/register"), "registration_url must be an https:// address"),
    (_service(registration_url="https://dash.example.com/register?x=1"),
     "registration_url must be an https:// address"),
    (_service(registration_url="https://register.other.net/x"), "registration_url must be on example.com"),
    (_service(registration_token="x"), "remove ['registration_token']"),
    (_service(type="oauth1"), "auth type must be 'oauth2'"),
    (_service(pkce=False), '"pkce": true'),
    (_service(pkce=None), '"pkce": true'),
    ({**_service(), "provider": "cloudflare", "url": None}, "needs its https:// API url"),
    (_service(authorize_url="http://dash.example.com/auth"), "authorize_url must be an https:// address"),
    (_service(token_url="https://dash.example.com/token?x=1"), "token_url must be an https:// address"),
    (_service(token_url="https://dash.example.com/token#frag"), "token_url must be an https:// address"),
    (_service(token_url="https://user:pw@dash.example.com/token"), "token_url must be an https:// address"),
    (_service(token_url="https://dash.example.com:notaport/token"), "token_url must be an https:// address"),
    (_service(token_url=None), "auth token_url is required"),
    (_service(authorize_url="https://example.com.evil.net/auth"),
     "authorize_url must be on example.com, the same domain as the service url"),
    (_service(token_url="https://login.other.org/token"), "token_url must be on example.com"),
    (_service(revoke_url="https://revoke.example.org/x"), "revoke_url must be on example.com"),
    (_service(scopes="read write"), "auth scopes must be a list of strings"),
    (_service(scopes=[f"s{i}" for i in range(21)]), "at most 20 auth scopes"),
    (_service(scopes=["read write"]), "each auth scope is one word"),
    (_service(scopes=['read"']), "each auth scope is one word"),
    (_service(scopes=[""]), "each auth scope is one word"),
    (_service(scopes=["read", "read"]), "auth scopes must be unique"),
], ids=[
    "not-object", "client-secret", "camel-secret", "secret-on-service", "client-id-space", "client-id-number",
    "client-id-token", "client-id-pkce-false", "client-id-pkce-missing", "client-id-with-secret",
    "registration-http", "registration-query", "registration-domain", "unknown-key", "type", "pkce-false",
    "pkce-missing", "no-service-url", "http", "query", "fragment", "credentials", "bad-port",
    "missing-token-url", "lookalike-domain", "other-domain", "revoke-domain", "scopes-string",
    "too-many-scopes", "scope-space", "scope-quote", "scope-empty", "duplicate-scopes",
])
def test_each_bad_declaration_is_refused_in_plain_words(entry, expected) -> None:
    if entry.get("url") is None:
        entry.pop("url", None)
    assert expected in _refusal(entry)


def test_a_shipped_public_client_and_a_registration_url_are_kept_and_shown() -> None:
    entry = _service(client_id="public-client.apps.example",
                     registration_url="https://dash.example.com/oauth2/register")
    [checked] = check_services([entry])
    assert checked["auth"] == {**AUTH, "client_id": "public-client.apps.example",
                               "registration_url": "https://dash.example.com/oauth2/register"}
    assert check_services([checked]) == [checked]
    assert describe_service_auth(checked["auth"]) == (
        "signs in with OAuth at dash.example.com; scopes: zone:read, dns.records:read; "
        "uses the plugin's public client ID public-client.apps.example; "
        "registers its own client at https://dash.example.com/oauth2/register"
    )


def test_the_refusal_names_a_secret_field_but_never_its_value() -> None:
    message = _refusal(_service(client_secret="do-not-echo-this-value"))
    assert "client_secret" in message
    assert "do-not-echo-this-value" not in message


@pytest.mark.parametrize(("host", "domain"), [
    ("api.example.com", "example.com"),
    ("example.com", "example.com"),
    ("a.b.example.co.uk", "example.co.uk"),
    ("api.example.de", "example.de"),
    ("10.0.0.5", "10.0.0.5"),
    ("localhost", "localhost"),
    ("API.Example.COM.", "example.com"),
])
def test_registrable_domain_is_simple_and_strict(host, domain) -> None:
    assert registrable_domain(host) == domain


def test_an_ip_service_only_signs_in_on_the_same_address() -> None:
    url = "https://10.0.0.5/api"
    ok = {**AUTH, "authorize_url": "https://10.0.0.5/auth", "token_url": "https://10.0.0.5/token"}
    assert check_service_auth("home", url, ok)["token_url"] == "https://10.0.0.5/token"
    with pytest.raises(PluginDraftError):
        check_service_auth("home", url, {**ok, "token_url": "https://10.0.0.6/token"})


def _spec(auth: dict) -> dict:
    return {
        "name": "dns-reader", "version": "0.1.0", "description": "Reads DNS",
        "skills": [{"name": "dns", "description": "List DNS", "body": "Call the API."}],
        "services": [{**_service(), "auth": auth}],
    }


def test_a_draft_with_a_sign_in_saves_and_copies_between_devices(tmp_path: Path) -> None:
    saved = save_authored_plugin(_spec(copy.deepcopy(AUTH)), home=tmp_path / "plugins")
    manifest = json.loads((saved.path / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["extensions"]["ai.shuffle"]["services"][0]["auth"] == AUTH
    files = read_draft_files(saved.path)
    assert files is not None
    check_draft_files("dns-reader", "0.1.0", files)


def test_a_draft_with_a_client_secret_is_refused() -> None:
    with pytest.raises(PluginDraftError, match="never carries an OAuth client secret"):
        build_files(_spec({**AUTH, "client_secret": "x"}))


def test_the_folder_check_reports_a_bad_sign_in(tmp_path: Path) -> None:
    root = tmp_path / "dns-reader"
    (root / "skills" / "dns").mkdir(parents=True)
    (root / "skills" / "dns" / "SKILL.md").write_text(
        "---\nname: dns\ndescription: List DNS\n---\nCall the API.\n", encoding="utf-8")
    manifest = {
        "name": "dns-reader", "version": "0.1.0", "description": "Reads DNS", "skills": "./skills/",
        "extensions": {"ai.shuffle": {"services": [_service(token_url="https://evil.net/token")]}},
    }
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_plugin_folder(root)
    assert not result.ok
    assert "token_url must be on example.com" in result.render()

    manifest["extensions"]["ai.shuffle"]["services"] = [_service()]
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_plugin_folder(root)
    assert result.ok, result.render()
    assert result.spec["services"][0]["auth"]["scopes"] == AUTH["scopes"]
