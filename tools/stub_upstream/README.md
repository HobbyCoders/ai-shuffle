# Local stub upstream — zero-spend provider E2E

**Test / dev tooling. Not wired into any production path.** Nothing under
`app/` imports, registers, or reaches this package; it is started by hand by an
operator and binds loopback only.

## Why this exists

The standing bar is real-app E2E before merge. For provider work (translator
gateway, circuit breaker, failover, cross-provider subagent legs) the only
account that exercises those paths is a ChatGPT/Codex subscription that is at
100% weekly usage and hard-blocked — zero requests, not even a cheap probe. This
gives us a fake vendor instead: a loopback HTTP server that speaks the same
OpenAI-Responses SSE the gateway expects, so a real session, real gateway, real
worker, real events and a real browser can be driven with canned responses and
no vendor spend.

---

## What it serves

| Route | Purpose |
|---|---|
| `POST /v1/responses` | OpenAI platform Responses API (gateway `openai` mode) |
| `POST /backend-api/codex/responses` | ChatGPT/Codex backend (gateway `codex` mode) |
| `POST /v1/messages` | Anthropic passthrough (best effort — see fidelity notes) |
| `GET /v1/models`, `GET /backend-api/codex/models` | provider catalog fetch |
| `/__control__/*` | scenario selection + request log (see below) |

Default port **8799** (control) and **8798** (MITM proxy) — deliberately clear
of the app (8000), the primary gateway (8788) and the second-instance gateway
(8789). Binding to anything other than loopback is refused in code.

The canned SSE payloads are **not invented here**. They are reused from
`tests/core/translator_gateway/_fixtures.py`, which was built from the live
Codex SSE dumps recorded in `docs/CODEX_SPIKE_FINDINGS.md`.

---

## 1. Start it

From the repo root (the repo root must be on `sys.path`, which `-m` handles):

```bash
python -m tools.stub_upstream \
  --port 8799 \
  --scenario text \
  --log-file ./stub-requests.jsonl
```

Add `--proxy` when you need a **running app instance** to reach it (see §3b):

```bash
python -m tools.stub_upstream --port 8799 --proxy --proxy-port 8798 \
  --ca-dir ./.stub-upstream-ca --log-file ./stub-requests.jsonl
```

Everything is also settable by env: `STUB_UPSTREAM_PORT`,
`STUB_UPSTREAM_SCENARIO`, `STUB_UPSTREAM_SCENARIO_JSON`, `STUB_UPSTREAM_LOG`,
`STUB_UPSTREAM_PROXY_PORT`, `STUB_UPSTREAM_CA_DIR`, `STUB_UPSTREAM_MAX_HANG`,
`STUB_UPSTREAM_VERBOSE`.

Smoke check:

```bash
curl -s http://127.0.0.1:8799/__control__/state
curl -sN -X POST http://127.0.0.1:8799/v1/responses -d '{"model":"gpt-5.6-sol"}'
```

---

## 2. Create a provider row pointing at it

`provider_type: "openai"` is the easiest target — the gateway takes its key
straight off the provider row (`auth_token`), so no Codex OAuth rows are needed.
Use `"chatgpt"` only when you specifically want to exercise the codex
auth/refresh branch (that needs a `codex_tokens` bundle seeded separately).

Provider rows are admin-only and the API is CSRF-guarded, so you need **both** a
valid admin session cookie **and** an `Origin` header that matches the host:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/model-providers \
  -H 'Content-Type: application/json' \
  -H 'Origin: http://127.0.0.1:8000' \
  -b "$COOKIE_JAR" \
  -d '{
        "name": "STUB (local, no spend)",
        "provider_type": "openai",
        "base_url": "http://127.0.0.1:8799",
        "auth_token": "sk-stub-not-a-real-key",
        "default_model": "gpt-5.6-sol",
        "models": [{"id": "gpt-5.6-sol", "label": "stub gpt-5.6-sol"}],
        "context_window": 400000,
        "enabled": true
      }'
```

Notes:

* **`auth_token` is stored encrypted.** The DB must be unlocked (the encryption
  key set) or the gateway refuses the provider at spawn time with a "encryption
  key" error. A dummy value is fine — the stub never checks it, and the request
  log only ever shows a redacted fingerprint.
* **Curate `models_json` yourself.** Passing `models` on create/update persists
  the curated list. Do *not* rely on the "fetch models" button for a `chatgpt`/
  `openai` row: those types are served from the built-in preset roster
  (`app/core/model_catalog_presets.py`), not from `base_url`. The stub does
  serve `GET {base_url}/v1/models` (OpenAI `{"data":[...]}` shape) for the
  provider types that *do* fetch upstream (`openrouter` / `custom`).
* **`context_window`** is worth setting: the gateway's oversize guard only runs
  when the provider row declares one, so a stub row without it silently skips
  that path.

### The important caveat — `base_url` does **not** steer chat traffic

`app/core/translator_gateway/upstream.py` hardcodes its endpoints:

```
CODEX_RESPONSES_URL    = "https://chatgpt.com/backend-api/codex/responses"
OPENAI_RESPONSES_URL   = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
```

A provider row's `base_url` is consumed only by the `GET {base_url}/v1/models`
catalog fetch in `app/api/model_providers.py`. There is **no in-app knob** that
points a running instance's chat traffic at a loopback stub. That is why §3b
exists. (Adding an env-var override for those three constants would make this
unnecessary — but `app/core/translator_gateway/` is owned by another branch, so
nothing there was touched.)

---

## 3. Get traffic to land on the stub

### 3a. In tests — the `client=` seam (preferred)

Every gateway network entry point accepts an optional `client:
httpx.AsyncClient`. `tools/stub_upstream/redirect.py` supplies one whose
transport rewrites the vendor hosts onto the stub, so the **real** `upstream.py`
runs over a **real** TCP socket:

```python
from tools.stub_upstream.redirect import stub_client

async with stub_client(port) as client:
    async for name, payload in upstream.stream_responses_events(
        provider_id=..., mode="openai", responses_body=..., client=client
    ):
        ...
```

The `Host` header is deliberately left as the vendor host, so the stub's request
log shows which vendor the gateway *believed* it was calling.

### 3b. Against a running app instance — the MITM proxy

The gateway's outbound client is `httpx.AsyncClient(...)` with `trust_env` left
at its default `True` (`app/core/http_client.py`), and httpx 0.27.2 honors both
the proxy env vars and `SSL_CERT_FILE` (`httpx/_config.py ::
load_ssl_context_verify` → `get_ca_bundle_from_env`). So start the stub with
`--proxy` and launch the E2E app instance with:

```
HTTPS_PROXY=http://127.0.0.1:8798
SSL_CERT_FILE=<repo>/.stub-upstream-ca/stub-upstream-ca.pem
NO_PROXY=127.0.0.1,localhost
```

The proxy answers `CONNECT`, terminates TLS with a leaf cert signed by a
throwaway local CA, and serves the ordinary stub routes. **It never dials out** —
an unrecognized host still gets a canned response, which is what makes the whole
harness zero-spend by construction.

* The CA lives only in `--ca-dir` and is trusted only by processes you point at
  it with `SSL_CERT_FILE`. **Do not install it in a system trust store.**
* `HTTPS_PROXY` is process-wide: *all* outbound HTTPS from that instance
  (Anthropic, GitHub, Firebase, …) is intercepted too. For an E2E instance that
  is a feature, not a bug. Keep `NO_PROXY` covering loopback so the CLI ↔
  gateway hop is untouched.
* This is verified by `tests/tools/test_stub_upstream.py::TestMitmProxyRoute`,
  which drives `stream_responses_events` with **no** `client=` argument (the
  production path) and asserts the request arrived at the stub via the proxy.

---

## 4. Bind a session to it

A session inherits its provider from its **profile** (`ProfileConfig` in
`app/core/models.py`): set `provider` to the stub provider row's id and `model`
to a model id the row lists. `subagent_provider` / `subagent_model` bind the
cross-provider subagent leg if you are exercising that.

The profile update route is `PUT /api/v1/profiles/{id}` and it takes a **whole**
`ProfileConfig` — so GET the profile first, merge your two fields into its
existing `config`, and PUT the merged object back. Sending only
`{"provider": ..., "model": ...}` resets every other config field to its default.

```bash
curl -s -X PUT http://127.0.0.1:8000/api/v1/profiles/<profile-id> \
  -H 'Content-Type: application/json' \
  -H 'Origin: http://127.0.0.1:8000' \
  -b "$COOKIE_JAR" \
  -d '{"config": { ...existing config..., "provider": "<stub-provider-id>", "model": "gpt-5.6-sol"}}'
```

(The Providers UI does the same thing safely — using it is fine and usually
faster than hand-merging JSON.)

Then start a session on that profile. At spawn, `environment.apply_provider_env`
mints an opaque routing token and points the CLI at
`ANTHROPIC_BASE_URL=http://127.0.0.1:<gateway port>`; the gateway resolves the
token → provider → stub.

### Environment gotchas for the E2E instance (verified against this tree)

* **Gateway port.** The gateway binds `TRANSLATOR_GATEWAY_PORT` (default 8788,
  `app/core/config.py`). The primary instance holds 8788, so a second instance
  must run `TRANSLATOR_GATEWAY_PORT=8789` or it will fail to bind.
* **`INSTANCE_ID`.** New sessions are pinned to
  `placement = {"where": "runner:<instance_id>"}` (`app/db/sessions.py`), and
  `placement.py` honors that pin. `INSTANCE_ID` (env) overrides the persisted
  id (`config.py :: effective_instance_id`). If the E2E instance runs a
  *different* `INSTANCE_ID` from the one that owns the session, chat fails with
  *"No capable device online for this session"* (`app/api/chat_ws_handler.py`).
* **Frontend bundle.** `app/main.py` serves `Path(__file__).parent / "static"`,
  i.e. **`app/static`** — in a worktree that is `<worktree>/app/static`, not
  `app/app/static`. `frontend/scripts/stamp-sw-version.mjs` (npm `postbuild`)
  stamps the service worker's `CACHE_VERSION` to `<pkg version>-<git short
  sha>` in `build/sw.js` only; the tracked `frontend/static/sw.js` stays at
  `'dev'`. So: rebuild the frontend and re-copy `frontend/build/*` →
  `app/static` (`scripts/deploy.ps1` does both) or you will be testing a stale
  bundle against fresh backend behavior.

---

## 5. Select a scenario

Precedence per request: **`X-Stub-Scenario` header / `?scenario=` query** →
**one-shot queue** → **current default**.

| Scenario | Behavior |
|---|---|
| `text` | normal streaming assistant reply (`text`, `n_deltas`) |
| `tool_call` | streaming function call (`tool_name`, `tool_arguments`, `tool_call_id`) |
| `reasoning` | reasoning summary + `encrypted_content` + answer |
| `slow` | `text` plus `delay_before_headers` / `delay_before_first_event` / `delay_between_events` / `hang_seconds` |
| `stream_error` | `fail_after_events` good frames, then `response.failed` + `error`, clean close |
| `truncate` | `fail_after_events` good frames, then the connection drops mid-stream |
| `rate_limit` | **HTTP 429 + `Retry-After`** (configurable) |
| `auth_error` | HTTP 401 (drives the codex refresh-once-then-retry branch) |
| `http_status` | any `status` you want |

Any field of `Scenario` (`tools/stub_upstream/scenarios.py`) can be overridden,
including `headers` — handy for feeding the `x-codex-*` quota headers that
`provider_usage.capture_codex_headers` reads.

**Set the default:**

```bash
curl -s -X POST http://127.0.0.1:8799/__control__/scenario \
  -d '{"name":"text","text":"hello from the stub","n_deltas":8}'
```

**Set a one-shot queue** (this is how a circuit-breaker run is driven — three
429s with `Retry-After: 1`, then a healthy reply):

```bash
curl -s -X POST http://127.0.0.1:8799/__control__/queue \
  -d '{"queue":[{"name":"rate_limit","retry_after":"1","repeat":3}]}'
```

**Boot default via env / flags:**

```bash
STUB_UPSTREAM_SCENARIO=rate_limit \
STUB_UPSTREAM_SCENARIO_JSON='{"retry_after":"90"}' \
python -m tools.stub_upstream
```

**One-off override without touching state:**

```bash
curl -s -X POST 'http://127.0.0.1:8799/v1/responses?scenario=rate_limit' -d '{}'
curl -s -X POST http://127.0.0.1:8799/v1/responses \
  -H 'X-Stub-Scenario: {"name":"tool_call","tool_name":"Task"}' -d '{}'
```

---

## 6. Read the request log

Every request is recorded with timestamp, method, path, the `Host` the caller
used, whether it arrived direct or via the proxy, the parsed JSON body, the
served scenario, the response status and the event names emitted.

```bash
curl -s 'http://127.0.0.1:8799/__control__/log?limit=5' | python -m json.tool
curl -s 'http://127.0.0.1:8799/__control__/count?path=/v1/responses'   # breaker counting
tail -f ./stub-requests.jsonl                                          # if --log-file was set
```

**Credential headers are never stored verbatim.** `Authorization`,
`proxy-authorization`, `x-api-key`, `api-key`, `chatgpt-account-id` and cookies
are replaced with `<redacted len=N fp=xxxxxxxx>` — the `fp` is an 8-hex SHA-256
prefix, so you can tell *whether* a credential changed (token rotation, account
failover) without ever seeing it.

---

## 7. Teardown

```bash
curl -s -X POST http://127.0.0.1:8799/__control__/reset   # clear log + queue, restore boot default
```

Then `Ctrl-C` the stub. Afterwards:

* unset `HTTPS_PROXY` / `SSL_CERT_FILE` / `NO_PROXY` on the E2E instance and
  restart it, or it will keep trying to reach a dead proxy;
* delete the CA directory (`.stub-upstream-ca/`) — it is regenerated on demand;
* delete or disable the stub provider row so a later session cannot accidentally
  bind to a dead upstream;
* delete `stub-requests.jsonl` if you set `--log-file`.

The CA directory and log file are already covered by `.gitignore`.

---

## Where the stub is **NOT** faithful

Stated plainly, because a stub you wrongly trust is worse than no stub.

1. **The 429 body is a reconstruction.** `docs/CODEX_SPIKE_FINDINGS.md` never
   captured a live rate-limit response from the Codex backend, so the JSON error
   envelope follows OpenAI's documented platform shape. The parts the gateway
   actually keys on — the HTTP status and the `Retry-After` header — are exact
   and are asserted in the tests.
2. **`response.failed` / `error` frames were never observed live.** The spike
   doc explicitly lists them as *not observed*. The `stream_error` scenario uses
   the public OpenAI Responses event schema for those frames; treat it as "a
   plausible mid-stream failure", not "the exact bytes ChatGPT sends".
3. **No `[DONE]` sentinel by default.** That matches the live Codex behavior
   recorded in the spike doc (the stream just ends after `response.completed`).
   `emit_done_sentinel: true` opts into the api.openai.com-style sentinel.
4. **Anthropic passthrough is best effort.** The gateway forwards passthrough
   bytes verbatim without parsing them, so the stub's Anthropic SSE has been
   validated only for well-formedness and reassembly — **not** against the
   bundled Claude Code CLI's parser.
5. **No prompt-cache, no entitlement gating, no real usage accounting.** The
   `usage` block comes from the shared fixtures and is static; `cached_tokens`
   is always 0. `x-codex-*` quota headers are emitted only if you set them via
   `Scenario.headers`.
6. **No credential validation whatsoever.** Any `Authorization` value is
   accepted. The stub cannot prove the gateway sent a *correct* token — only
   *which* token (by fingerprint) and *that* it sent one.
7. **Timing is coarse.** `slow` / `hang_seconds` are bounded by
   `--max-hang-seconds` (default 120s) so a stub can never wedge a test run;
   real upstream latency jitter is not modeled.
8. **HTTP/1.1 only, no HTTP/2.** Real vendors negotiate h2. httpx falls back to
   HTTP/1.1 cleanly, and the gateway does not depend on h2 framing, but any
   h2-specific behavior is out of reach here.
