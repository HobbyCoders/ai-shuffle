"""Credentials stay out of provider errors, ``fluid config`` and learner evidence."""
import json
import time

import httpx
import pytest

from fluid.cli.__main__ import _redacted_config
from fluid.codemode.continual import Synthesizer
from fluid.codemode.journal import ToolJournal
from fluid.codemode.skills import SkillStore
from fluid.config.model import HarnessConfig
from fluid.context.screening import redact_secrets, redact_text
from fluid.providers._chat_completions import ChatCompletionsNormalizer, stream_chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers._transport import header_secrets
from fluid.providers.openai_responses import codex_oauth
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.base import ProviderRequest
from fluid.providers.gemini.client import GeminiClient
from fluid.providers.openai_responses.client import OpenAIResponsesClient

# No vendor prefix: only the exact-value pass can catch it.
KEY = "plainkey0123456789abcdefXYZ"
URL = "https://provider.example.com/v1/endpoint"
REQUEST = ProviderRequest(model="test-model", messages=[])
NO_RETRY = RetryPolicy(max_retries=0, base_delay=0)


def test_redact_text_known_values_shapes_and_clipped_prefix():
    text = (
        f"echo {KEY}; Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 "
        "sk-ant-api03-abcdefghijklmnop https://user:hunter2pass@db.example.com/x "
        "api_key=AB12CD34EF56GH78IJ90 ordinary words stay"
    )
    out = redact_text(text, [KEY])
    for secret in (KEY, "abcdefghijklmnopqrstuvwxyz012345", "sk-ant-api03", "hunter2pass",
                   "AB12CD34EF56GH78IJ90"):
        assert secret not in out
    assert "Bearer [redacted]" in out and "https://user:[redacted]@db.example.com" in out
    assert out.endswith("ordinary words stay")
    # A clip that cut the key in half leaves no prefix of it.
    assert redact_text("bad key " + KEY[:15] + "…", [KEY]) == "bad key [redacted]…"
    assert redact_text("bad key " + KEY[:12], [KEY]) == "bad key [redacted]"
    assert redact_text("local model loaded", []) == "local model loaded"


def _refusing(status, body):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body)))


def _sse(*events):
    data = b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=data,
                                       headers={"content-type": "text/event-stream"})))


def _stream(kind, http):
    if kind == "responses":
        return OpenAIResponsesClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    if kind == "anthropic":
        return AnthropicClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    if kind == "gemini":
        return GeminiClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    return stream_chat_completions(URL, {"model": "test-model", "messages": []},
                                   {"Authorization": f"Bearer {KEY}"}, http_client=http,
                                   timeout=5, retry=NO_RETRY)


def _errors_text(chunks):
    errors = [c.payload for c in chunks if c.kind == "error"]
    assert errors
    return json.dumps(errors)


@pytest.mark.parametrize("kind", ["responses", "anthropic", "gemini", "chat"])
async def test_http_refusal_that_echoes_the_key_is_redacted(kind):
    # A proxy quoting the rejected header back, once in full and once where
    # the 2000-character message clip cuts it in half.
    body = f"Unauthorized: header Authorization: Bearer {KEY} rejected. " + "x" * 1900 + KEY
    async with _refusing(401, body) as http:
        chunks = [c async for c in _stream(kind, http)]
    text = _errors_text(chunks)
    assert KEY[:10] not in text
    assert "[redacted]" in text


async def test_anthropic_stream_error_redacted_on_the_first_party_lane():
    """Only the compatible-provider lane used to redact in-stream errors."""
    async with _sse(("error", {"type": "error", "error": {
        "type": "invalid_request_error", "message": f"bad x-api-key {KEY}"}})) as http:
        chunks = [c async for c in _stream("anthropic", http)]
    assert KEY not in _errors_text(chunks)


async def test_responses_stream_error_redacts_a_fetched_oauth_token():
    token = "oauthtoken-ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    class Auth:
        async def get_access_token(self):
            return token

        async def on_401(self):
            pass

    async with _sse(("error", {"error": {"message": f"token {token} expired " + "y" * 600 + token}})) as http:
        client = OpenAIResponsesClient(auth=Auth(), base_url=URL, http_client=http, retry=NO_RETRY)
        chunks = [c async for c in client.stream(REQUEST)]
    text = _errors_text(chunks)
    assert token[:10] not in text


def test_fluid_config_view_masks_credentials_but_round_trip_keeps_them():
    config = HarnessConfig.from_dict({
        "providers": {"proxy": {"kind": "local",
                                "base_url": "https://user:hunter2pass@llm.example.com/v1",
                                "api_key_env": "PROXY_KEY"}},
        "mcp": {"tracker": {"url": "https://mcp.example.com",
                            "headers": {"Authorization": f"Bearer {KEY}"},
                            "env": {"TRACKER_TOKEN": KEY},
                            "args": ["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123"]}},
    })
    raw = config.to_dict()
    assert raw["mcp"]["tracker"]["env"]["TRACKER_TOKEN"] == KEY  # serializer unchanged
    shown = _redacted_config(raw)
    text = json.dumps(shown)
    assert KEY not in text and "hunter2pass" not in text and "ghp_abc" not in text
    assert shown["mcp"]["tracker"]["headers"] == {"Authorization": "[redacted]"}
    assert shown["mcp"]["tracker"]["env"] == {"TRACKER_TOKEN": "[redacted]"}
    assert shown["providers"]["proxy"]["api_key_env"] == "PROXY_KEY"  # a name, not a secret
    assert shown["mcp"]["tracker"]["url"] == "https://mcp.example.com"


def test_learner_evidence_redacted_on_write_and_when_rendered(tmp_path):
    store = SkillStore(tmp_path / "skills")
    # Assembled so the repository's secret scan does not flag a fake key.
    secret = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    journal = ToolJournal(store)
    journal.record_tool("Bash", {"command": f"curl -H 'Authorization: Bearer {secret}'"},
                        f"export OPENAI_API_KEY={secret}", False)
    journal.record_event("user", f"my key is {secret}")
    on_disk = store._journal_path.read_text(encoding="utf-8")
    assert secret[:12] not in on_disk and "[redacted]" in on_disk
    # A journal written before redaction still reaches the learner clean.
    legacy = {"kind": "tool", "name": "Bash", "input": f'{{"token": "{secret}"}}',
              "output": f"password={secret}"}
    rendered = "\n".join(Synthesizer._render_entry(legacy, 0))
    rendered += "\n".join(Synthesizer._render_entry(
        {"kind": "cell", "code": f"key = '{secret}'", "stdout": secret}, 1))
    assert secret[:12] not in rendered


# ── review round 1 ───────────────────────────────────────────────────────────

# Assembled so the repository's secret scan does not flag a fake key block.
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"
PEM_BODY = ("QUJD" * 16 + "\n") * 3 + "QUJD"
URL_SECRET = "hunter2pass"
URL_TAIL = f" https://user:{URL_SECRET}@db.example.com/x"


def test_assignment_formats_with_opaque_and_quoted_values():
    """S1: an env assignment with an opaque value, a quoted value with
    punctuation, and a JSON field all lose the value; the name stays."""
    cases = {
        "export APP_API_KEY=opaquevalueopaque": "export APP_API_KEY=[redacted]",
        "X_TOKEN=abc12345 && run": "X_TOKEN=[redacted] && run",
        'password: "hunter2!go"': 'password: "[redacted]"',
        "db_password='p@ss w!rd'": "db_password='[redacted]'",
        '{"user": "alice", "password": "pw!"}': '{"user": "alice", "password": "[redacted]"}',
        'X-Api-Key: opaque-value-123': "X-Api-Key: [redacted]",
        "GET /v1?api_key=AB12CD34EF56GH78IJ90&page=2": "GET /v1?api_key=[redacted]&page=2",
        # The JSON form of a tool's arguments, quotes escaped inside a value.
        '{"command": "export X_TOKEN=\\"abc!123\\""}': '{"command": "export X_TOKEN=\\"[redacted]\\""}',
    }
    for text, expected in cases.items():
        assert redact_text(text) == expected, text


def test_structured_credential_fields_masked_whatever_their_shape():
    """S1: a value under a credential-named key has no recognisable shape;
    the key alone decides. Other fields and the key names are kept."""
    data = {"user": "alice", "password": "short!", "apiKey": "v",
            "nested": [{"client_secret": "abc", "tokens": 12, "api_key_env": "PROXY_KEY"}]}
    assert redact_secrets(data) == {
        "user": "alice", "password": "[redacted]", "apiKey": "[redacted]",
        "nested": [{"client_secret": "[redacted]", "tokens": 12, "api_key_env": "PROXY_KEY"}],
    }


def test_journaled_tool_arguments_mask_credential_fields(tmp_path):
    store = SkillStore(tmp_path / "skills")
    ToolJournal(store).record_tool(
        "mcp__db__connect", {"host": "db.example.com", "password": "hunter2!"}, "ok", False)
    on_disk = store._journal_path.read_text(encoding="utf-8")
    assert "hunter2!" not in on_disk and "db.example.com" in on_disk


def test_legitimate_source_is_not_rewritten():
    """S4: expressions on the right of a credential name, references and a
    lone key-header constant stay exactly as written."""
    source = (
        f'HEADER = "{PEM_BEGIN}"\n'
        "client = make_client(api_key=settings.provider_api_key)\n"
        'token = os.environ["APP_TOKEN"]\n'
        "secret = load_secret()\n"
        "password = None\n"
        "def connect(api_key: str = \"\", token=self.token):\n"
        "    headers = {'Authorization': auth_header}\n"
        'TOKEN=$(cat token.txt) PASSWORD="$DB_PASSWORD"\n'
        "max_tokens=4096; sort_key=name\n"
        "the token: expired, the password: hidden\n"
    )
    assert redact_text(source) == source


def test_cell_source_journaled_verbatim(tmp_path):
    store = SkillStore(tmp_path / "skills")
    code = (f'HEADER = "{PEM_BEGIN}"\n'
            "client = make_client(api_key=settings.provider_api_key)\nprint(client)\n")
    store.journal_append({"kind": "cell", "code": code})
    line = json.loads(store._journal_path.read_text(encoding="utf-8").splitlines()[-1])
    assert line["code"] == code


def test_private_key_block_needs_key_material():
    """S4: a block with base64 lines goes, END line included; without an END
    line (a clipped block) only the key lines go, never what follows."""
    full = f"before\n{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}\nafter = 1"
    assert redact_text(full) == "before\n[redacted]\nafter = 1"
    clipped = f"{PEM_BEGIN}\n" + ("QUJD" * 16 + "\n") * 2 + "after = 1"
    assert redact_text(clipped) == "[redacted]\nafter = 1"
    escaped = json.dumps({"key": f"{PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}\n", "x": 1})
    assert "QUJD" not in redact_text(escaped) and '"x": 1' in redact_text(escaped)


def test_short_configured_key_redacted_but_the_clients_own_placeholder_ignored():
    """S6: an exact configured credential goes at any length, whatever its
    spelling; only the placeholder the client chose itself is exempt."""
    assert redact_text("gateway rejected sk-1234 here", ["sk-1234"]) == "gateway rejected [redacted] here"
    assert redact_text("blank keys garble nothing", ["", "  "]) == "blank keys garble nothing"
    assert header_secrets({"Authorization": "Bearer local"}, placeholder="local") == []
    assert header_secrets({"Authorization": "Bearer local"}) == ["Bearer local", "local"]
    assert header_secrets({"Authorization": "Bearer sk-1234"}) == ["Bearer sk-1234", "sk-1234"]


def test_short_known_key_masked_as_a_token_but_kept_inside_words():
    """A short configured key (``k``, ``local``) never rewrites the words
    that contain it; standing alone it still goes."""
    assert redact_text("check your plan: terminal usage chunk", ["k"]) == (
        "check your plan: terminal usage chunk")
    assert redact_text("open http://localhost:8080 or local.example.com", ["local"]) == (
        "open http://localhost:8080 or local.example.com")
    assert redact_text("invalid API key local.", ["local"]) == "invalid API key [redacted]."
    assert redact_text("Bearer local", ["Bearer local", "local"]) == "[redacted]"
    assert redact_text('{"apiKey": "k"} key=k', ["k"]) == '{"apiKey": "[redacted]"} key=[redacted]'


async def test_chat_refusal_echoing_a_short_key_is_redacted():
    async with _refusing(401, "invalid key sk-1234 for this gateway") as http:
        chunks = [c async for c in stream_chat_completions(
            URL, {"model": "test-model", "messages": []}, {"Authorization": "Bearer sk-1234"},
            http_client=http, timeout=5, retry=NO_RETRY)]
    assert "sk-1234" not in _errors_text(chunks)


def _straddling(cap, extra=0):
    """Pads that put a ``cap``-char clip at every point of URL_TAIL's
    password and just before its ``@host``."""
    lead = len(" https://user:") + extra
    return range(cap - lead - len(URL_SECRET) - 2, cap - lead + 2)


def test_sse_error_redacted_before_its_clip():
    """S2: the in-stream error message is clipped at 2000 chars; a cut just
    before ``@host`` must not leave the URL password behind."""
    for pad in _straddling(2000):
        payload = ChatCompletionsNormalizer()._error_payload({"message": "x" * pad + URL_TAIL})
        assert URL_SECRET[:4] not in json.dumps(payload), pad


async def test_sse_error_redacted_through_the_stream():
    message = "x" * (2000 - len(" https://user:") - len(URL_SECRET)) + URL_TAIL
    body = f"data: {json.dumps({'error': {'message': message, 'code': 500}})}\n\n".encode()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"})))
    async with http:
        chunks = [c async for c in _stream("chat", http)]
    assert URL_SECRET[:4] not in _errors_text(chunks)


def _friction_lines(store):
    lines = [json.loads(line) for line in
             store._journal_path.read_text(encoding="utf-8").splitlines()]
    friction = [line for line in lines if line["kind"] == "friction"]
    assert friction
    return json.dumps(friction)


def test_friction_evidence_redacted_before_its_clip(tmp_path):
    """S2: friction text (300 chars) and argument previews (200) are clipped;
    a URL password straddling either cut never reaches the journal."""
    for pad in _straddling(300, len("Bash({}) failed: File not found: ")):
        store = SkillStore(tmp_path / f"text{pad}")
        ToolJournal(store).record_tool(
            "Bash", {}, "File not found: " + "y" * pad + URL_TAIL, True)
        assert URL_SECRET[:4] not in _friction_lines(store), pad
    for pad in _straddling(200, len('{"command": "')):
        store = SkillStore(tmp_path / f"args{pad}")
        journal = ToolJournal(store)
        for _ in range(2):
            journal.record_tool("Bash", {"command": "z" * pad + URL_TAIL}, "(no matches)", False)
        assert URL_SECRET[:4] not in _friction_lines(store), pad


def test_cell_journal_redacts_before_the_kernel_and_journal_clips(tmp_path):
    """S2: a cell's stdout comes from the whole text, and an error summary
    the kernel clipped is replaced by the whole error's last line."""
    from types import SimpleNamespace

    from fluid.codemode.kernel import KernelResult
    from fluid.codemode.session import _JOURNAL_STDOUT_CAP, CodeModeSession

    store = SkillStore(tmp_path / "skills")
    full_out = "o" * (_JOURNAL_STDOUT_CAP - len(" https://user:") - len(URL_SECRET)) + URL_TAIL
    message = "e" * 400 + URL_TAIL
    result = KernelResult(
        # The kernel cut its stdout just before ``@host``.
        stdout=full_out[:_JOURNAL_STDOUT_CAP], stdout_full=full_out,
        error="ValueError: …", error_full=f"Traceback\nValueError: {message}",
        error_summary=f"ValueError: {'e' * 200} …[-500 chars]… {URL_TAIL[:20]}",
    )
    host = SimpleNamespace(_skills=store, _friction_pending=[], _synthesizer=None)
    CodeModeSession._record_cell(host, "print(1)", result, [])
    line = json.loads(store._journal_path.read_text(encoding="utf-8").splitlines()[-1])
    assert URL_SECRET[:4] not in json.dumps(line)
    assert line["error"].startswith("ValueError: eee")


def test_fluid_config_masks_split_and_joined_credential_flags():
    """S3: ``--api-key VALUE`` and ``--token=VALUE`` lose the value; other
    flags and the underlying config are unchanged."""
    config = HarnessConfig.from_dict({"mcp": {"tracker": {
        "command": "tracker-mcp",
        "args": ["--api-key", "opaquevalue1", "--token=abc", "--port", "8080", "--verbose"]}}})
    raw = config.to_dict()
    shown = _redacted_config(raw)
    assert shown["mcp"]["tracker"]["args"] == [
        "--api-key", "[redacted]", "--token=[redacted]", "--port", "8080", "--verbose"]
    assert raw["mcp"]["tracker"]["args"][1] == "opaquevalue1"


async def test_refresh_failure_does_not_echo_the_tokens():
    """S5: a refused refresh whose description quotes the tokens raises an
    error without them; the code that decides permanence is kept."""
    refresh, access = "rt-opaque-refresh-0001", "at-opaque-access-0001"

    def handler(request):
        return httpx.Response(400, json={
            "error": "refresh_token_reused",
            "error_description": f"refresh token {refresh} reused (access {access})"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(codex_oauth.CodexAuthError) as raised:
            await codex_oauth.refresh_tokens(refresh, client=http, access_token=access)
    text = f"{raised.value} {raised.value.code}"
    assert refresh not in text and access not in text and "[redacted]" in text
    assert raised.value.is_permanent


def test_redaction_stays_linear_on_large_adversarial_input():
    """No pattern backtracks: each 256 KiB probe finishes well inside a
    second on any runner (a quadratic pattern would take minutes)."""
    n = 256 * 1024
    probes = ["a-" * (n // 2), "a:" * (n // 2), "password=" * (n // 9),
              'token="' * (n // 7), "https://u:" + "a" * n, PEM_BEGIN + " " * n,
              f"{PEM_BEGIN}\n" + ("A" * 64 + "\n") * (n // 65)]
    for probe in probes:
        started = time.perf_counter()
        redact_text(probe, ["abcdefghZ" * 3])
        assert time.perf_counter() - started < 5, probe[:20]


# ── review round 2 ───────────────────────────────────────────────────────────

ADVERSARIAL_PEM = [
    # Header lines whose value spaces could also start the next header.
    PEM_BEGIN + " a: a" * 800,
    PEM_BEGIN + ("\nA: " + "b " * 20) * 40 + "!",
    PEM_BEGIN + "\nProc-Type: " + " " * 4000 + "!",
    PEM_BEGIN + "\\nProc-Type: 4 \\n " * 200,
    # Base64-looking runs that are not whole lines.
    PEM_BEGIN + ("\n" + "A" * 16 + " !") * 200,
    PEM_BEGIN + " " + "A " * 2000 + "!",
    PEM_BEGIN + " " + "A" * 4000 + " !",
]


@pytest.mark.parametrize("probe", ADVERSARIAL_PEM, ids=range(len(ADVERSARIAL_PEM)))
def test_malformed_private_key_headers_stay_fast(probe):
    """S7: a 4 KiB malformed block costs one pass, not a re-split of every
    space between header values and the next header."""
    redact_text(probe)  # warm the pattern cache
    started = time.perf_counter()
    assert redact_text(probe) == probe  # no key material: nothing to mask
    assert time.perf_counter() - started < 0.1


def test_private_key_with_headers_goes_and_the_line_after_a_clipped_block_stays():
    """S7: Proc-Type/DEK-Info headers belong to the block; a base64-looking
    identifier on the line after a clipped block is not key material."""
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    encrypted = (f"x\n{begin}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123ABCD\n\n"
                 f"{PEM_BODY}\n{end}\ny")
    assert redact_text(encrypted) == "x\n[redacted]\ny"
    lines = ("QUJD" * 16 + "\n") * 2
    for after in ("ABCDEFGHIJKLMNOPQRSTUVWX = 1", "ABCDEFGHIJKLMNOPQRSTUVWX=1",
                  "QUJDQUJDQUJDQUJDQUJD(x)", "done\nnext = 2"):
        assert redact_text(f"{PEM_BEGIN}\n{lines}{after}") == f"[redacted]\n{after}", after
    # A key pasted onto one line, closed or clipped.
    one_line = PEM_BODY.replace("\n", " ")
    assert redact_text(f"k={PEM_BEGIN} {one_line} {PEM_END}!") == "k=[redacted]!"
    assert redact_text(f"k {PEM_BEGIN} {one_line}") == "k [redacted]"


def test_escaped_quotes_and_query_parameters():
    """S1: a quote escaped inside a value is part of it; a URL query
    credential goes whatever its letters; code identifiers stay."""
    cases = {
        json.dumps({"password": 'pa"ss tail-of-secret'}): '{"password": "[redacted]"}',
        json.dumps({"command": 'export X_TOKEN="ab\\"cd tail-of-secret"'}):
            '{"command": "export X_TOKEN=\\"[redacted]\\""}',
        "password = 'it\\'s tail-of-secret'": "password = '[redacted]'",
        "GET /v1?api_key=ABCDEFGHIJKLMNOPQRST&page=2": "GET /v1?api_key=[redacted]&page=2",
        "https://cb.example.com/done?state=1&access_token=onlyletters&x=1":
            "https://cb.example.com/done?state=1&access_token=[redacted]&x=1",
        "/v1/items?token=abc": "/v1/items?token=[redacted]",
    }
    for text, expected in cases.items():
        assert redact_text(text) == expected, text
    code = "call(api_key=provider_key, token=auth_token)\nok && TOKEN=$TOKEN run\n"
    assert redact_text(code) == code


def test_large_token_is_redacted_before_the_preview_is_cut(tmp_path):
    """S2: a 6 KiB token keeps its closing quote because the whole text is
    redacted first; past the redaction limit an open value fails closed."""
    from fluid.context.screening import clip_redacted

    token = ("tok3nAbCdEfGh" * 600)[:6144]
    body = json.dumps({"access_token": token, "token_type": "bearer", "expires_in": 3600})
    assert token[:16] not in clip_redacted(body, 200)
    store = SkillStore(tmp_path / "skills")
    ToolJournal(store).record_tool("Bash", {"command": "curl https://auth.example.com"}, body, False)
    assert token[:16] not in store._journal_path.read_text(encoding="utf-8")
    huge = '{"access_token": "' + "Z" * (300 * 1024) + '"}'
    assert "ZZZZ" not in clip_redacted(huge, 200, mark="…")


def test_credential_flag_value_starting_with_a_dash_is_masked():
    """S3: ``--api-key -opaqueKey123`` passes the key to a parser that
    accepts it; the printed view must not."""
    shown = _redacted_config(HarnessConfig.from_dict({"mcp": {"tracker": {
        "command": "tracker-mcp", "args": ["--api-key", "-opaqueKey123", "--verbose"]}}}).to_dict())
    assert shown["mcp"]["tracker"]["args"] == ["--api-key", "[redacted]", "--verbose"]


def test_python_string_prefixes_masked_and_tokenizer_tokens_kept(tmp_path):
    """S4: a raw/bytes literal key goes from the journal write and the
    learner rendering; tokenizer special tokens and f-string expressions
    stay."""
    from types import SimpleNamespace

    from fluid.codemode.kernel import KernelResult
    from fluid.codemode.session import CodeModeSession

    raw = "rawliteralkey0123456789"
    code = (f'api_key = r"{raw}"\nTOKEN = Rb\'{raw}\'\nsecret = u"{raw}"\n'
            'tok = Tokenizer(token="</s>", pad_token="[PAD]", bos_token="<|endoftext|>",'
            ' cls_token="<s>")\n'
            'api_key = f"{prefix}-{suffix}"\npassword_field = "password"\n')
    store = SkillStore(tmp_path / "skills")
    host = SimpleNamespace(_skills=store, _friction_pending=[], _synthesizer=None)
    CodeModeSession._record_cell(host, code, KernelResult(stdout="ok"), [])
    written = json.loads(store._journal_path.read_text(encoding="utf-8").splitlines()[-1])["code"]
    rendered = "\n".join(Synthesizer._render_entry({"kind": "cell", "code": code}, 0))
    for text in (written, rendered):
        assert raw not in text
        for kept in ('token="</s>"', 'pad_token="[PAD]"', 'bos_token="<|endoftext|>"',
                     'cls_token="<s>"', 'api_key = f"{prefix}-{suffix}"',
                     'password_field = "password"'):
            assert kept in text, kept


async def test_configured_key_spelled_like_a_placeholder_is_redacted():
    """S6: a gateway whose real key is ``local`` echoes it; only the key the
    local client substitutes when none is configured is exempt."""
    from fluid.providers.local.client import LocalClient

    async def refusal(api_key):
        async with _refusing(401, "invalid API key local") as http:
            client = LocalClient(api_key, base_url=URL, http_client=http, retry=NO_RETRY)
            return _errors_text([c async for c in client.stream(REQUEST)])

    assert "invalid API key local" not in await refusal("local")
    assert "invalid API key local" in await refusal(None)
