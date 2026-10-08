<!--
title: Support Reports
description: Build, preview, diagnose and send error reports to HobbyCoders support
order: 15
-->

# Support Reports API

Endpoints behind **Send report to HobbyCoders support**. A report is built on this worker, shown to the owner, and sent to HobbyCoders only when the owner calls [send](#post-apiv1supportreportsreport_idsend). Nothing is sent automatically.

There are two kinds of report:

- **Basic report** (free, no AI): the error, up to 10 other errors from the last 24 hours, the last 60 warning and error log lines, the app version and the platform.
- **AI-diagnosed report**: a basic report plus a diagnosis written by a model the owner picks. It uses the owner's own AI usage. The diagnosis names the provider and model that wrote it, and the raw evidence stays attached, so a weak diagnosis still leaves a useful report.

Every endpoint on this page is **Owner only**. Agent previews (a preview cookie) can read drafts and check fingerprints, but get `403` when they try to start a diagnosis or send.

## Flow

1. `GET /api/v1/support/capabilities` to decide what to offer.
2. `POST /api/v1/support/reports` builds a basic draft from an error and returns its preview.
3. `POST /api/v1/support/check` asks HobbyCoders whether the error is already known. Do this before offering an AI diagnosis: if the answer is `known` or `fixed`, tell the user and let them skip the AI run. They can still send the basic report.
4. Optionally `POST /api/v1/support/diagnoses` (after telling the user it uses their AI usage), then poll `GET /api/v1/support/diagnoses/{run_id}` until it is `done` or `failed`. A finished run carries the AI report's preview.
5. Show the preview's `body`. `POST /api/v1/support/reports/{report_id}/send` sends exactly that text.

Drafts and diagnosis runs live in memory for one hour and are lost when the worker restarts; build the draft again in that case.

## Privacy

- Before the preview, recognised secrets are replaced with `***`: API keys of the common providers, GitHub tokens, `Bearer` and `Basic` credentials, private keys, `password=`/`token=` style assignments and passwords in URLs such as database DSNs. The same filter runs again right before sending. It recognises known credential formats only; it does not find arbitrary secrets, so the user should read the preview.
- The user's home directory is shown as `~`.
- The known-issue check sends only the 32-character fingerprint.
- A sent report reaches the HobbyCoders support inbox and a private GitHub issue tracker. The hub stores reports for up to a year, and deleting the Shuffle account deletes them.

## Fingerprint

A fingerprint is a 32-character id for a kind of error. The same bug on two installs or two versions has the same fingerprint, which is how the known-issue check finds it. `fingerprint_parts` in a preview shows what it was made from.

## Hub states

`/check` and `/send` answer `200` with a `state` whenever the request itself was valid, even when HobbyCoders could not be reached. When `state` is not `ok`, the response also has a `message` to show and, when the hub sent one, `retry_after` in seconds. The draft is kept, so the user can retry.

| `state` | Meaning |
|---------|---------|
| `ok` | Done. |
| `not_signed_in` | This worker is not signed in to a Shuffle account, or the account no longer accepts it. Reports need a signed-in worker. |
| `hub_unreachable` | No connection to hobbycoders.com. |
| `rate_limited` | Too many reports: the hub allows 5 an hour per worker and 20 a day per account, and 60 checks an hour per worker. |
| `unavailable` | The support service is paused, down, or older than this app. |
| `rejected` | The hub refused the report (for example, too large). |

## Endpoints

### GET /api/v1/support/capabilities

What this worker can offer.

**Auth:** Owner only

**Response:**
```json
{
  "basic_report": true,
  "ai_diagnosis": true,
  "signed_in": true,
  "default_model": "prov:3f0c...:gpt-6.1",
  "fix_with_ai": false,
  "fix_with_ai_reason": null,
  "diagnosis_caps": {"max_turns": 1, "timeout_seconds": 180, "prompt_max_chars": 60000, "tools": [], "cost_cap": null}
}
```

- `signed_in`: whether `/check` and `/send` can reach HobbyCoders. Building drafts and diagnoses works either way.
- `default_model`: the model a diagnosis uses when the request names none, in the same form a model picker sends (`prov:<provider id>:<model id>` or a Claude model id). It is the text-generation model from **Settings → Default models**.
- `fix_with_ai`: whether to offer the older **Fix with AI** action, which opens a chat whose agent edits code. It is `true` only when this install runs from a git checkout of the source (`fix_with_ai_reason: "source_checkout"`). Customer installs (Docker images and desktop installs) have no checkout and get `false`.
- `diagnosis_caps`: the diagnosis is one model turn with no tools, a prompt of at most `prompt_max_chars` characters and a `timeout_seconds` limit. There is no separate cost cap (`cost_cap: null`); the single turn and the prompt size bound the cost.

### POST /api/v1/support/reports

Build a basic report draft. No AI is used and nothing leaves the worker.

**Auth:** Owner only

Send exactly one of `error_id` or `error`.

| Field | Type | Notes |
|-------|------|-------|
| `error_id` | string | An `id` from `GET /api/v1/logs/errors`. |
| `error` | object | The error as the UI saw it: `message` (required), `source` (`frontend`, `backend`, `ws` or `api`), `detail` (stack trace), `url`, `status_code`. The same fields as a **Fix with AI** action payload. |
| `user_note` | string | Optional, up to 2,000 characters: what the user was doing. |

```bash
curl -X POST http://localhost:8000/api/v1/support/reports \
  -H "Authorization: Bearer $OWNER_TOKEN" -H "Content-Type: application/json" \
  -d '{"error_id": "4b9e...", "user_note": "I opened the session list."}'
```

**Response:** a report preview.
```json
{
  "report_id": "9f1c2e...",
  "kind": "basic",
  "status": "draft",
  "fingerprint": "5d41402abc4b2a76b9719d911017c592",
  "fingerprint_parts": {"type": "IndexError", "message": "list index out of range", "frames": ["app/api/sessions.py:list_sessions", "app/db/sessions.py:load"]},
  "title": "IndexError: list index out of range",
  "app_version": "0.9.1",
  "platform": "docker linux x86_64",
  "error": {"id": "4b9e...", "source": "backend", "level": "ERROR", "message": "IndexError: list index out of range", "detail": "Traceback ...", "occurrence_count": 3, "updated_at": "2026-10-03T10:00:00+00:00"},
  "recent_errors": [{"source": "frontend", "level": "ERROR", "message": "...", "occurrence_count": 1, "updated_at": "..."}],
  "log_excerpt": ["2026-10-03 10:00:00 - app.api.sessions - ERROR - ..."],
  "user_note": "I opened the session list.",
  "diagnosis": null,
  "body": "AI Shuffle support report (basic)\nApp version: 0.9.1\n...",
  "body_chars": 5120,
  "redactions": 2,
  "sent": null,
  "created_at": "2026-10-03T10:05:00+00:00",
  "expires_at": "2026-10-03T11:05:00+00:00"
}
```

- `body` is the exact text that will be sent. Show it before the user sends.
- `redactions` counts the `***` replacements in `body`.
- `status` is `draft` or `sent`; `sent` holds the send result once sent.
- An AI report also has `diagnosis`: `{"model": "OpenAI / gpt-6.1", "provider": "OpenAI", "model_id": "gpt-6.1", "text": "...", "workaround": "- Reload the page." | null, "started_at", "finished_at"}`. `workaround` is the model's "What you can try" section, written for the user.

**Errors:** `400` when neither or both of `error_id` and `error` are sent, `404` when `error_id` is no longer in the error log.

### GET /api/v1/support/reports/{report_id}

The preview of a draft or sent report (basic or AI). `404` once it has expired.

**Auth:** Owner only

### POST /api/v1/support/reports/{report_id}/note

Replace the user's note on a draft and get the updated preview. `{"user_note": "..."}`; `null` or an empty string removes it. `409` after the report was sent.

**Auth:** Owner only

### POST /api/v1/support/check

Known-issue check. Sends only the fingerprint to HobbyCoders. No AI is used.

**Auth:** Owner only

| Field | Type | Notes |
|-------|------|-------|
| `report_id` | string | A draft's id; its fingerprint is checked. |
| `fingerprint` | string | Or a fingerprint directly (32 hex characters). |

**Response:**
```json
{
  "state": "ok",
  "fingerprint": "5d41402abc4b2a76b9719d911017c592",
  "known_issue": {"status": "fixed", "fixed_in": "0.9.2", "note": "Update to 0.9.2.", "reported": true, "update_available": true}
}
```

- `status`: `new` (HobbyCoders has not marked it), `known` (known, not fixed yet) or `fixed` (fixed in `fixed_in`).
- `note`: a message from HobbyCoders for users with this error, or `null`.
- `reported`: whether anyone has sent a report of this error before.
- `update_available`: `fixed_in` is newer than this worker's version.

On another `state` (see [Hub states](#hub-states)), `known_issue` is `null`.

### POST /api/v1/support/diagnoses

Start an AI diagnosis of a basic draft. **This uses the owner's own AI usage.** Tell the user so before calling it.

**Auth:** Owner only

| Field | Type | Notes |
|-------|------|-------|
| `report_id` | string | A basic draft. |
| `model` | string or null | The model to use, as a model picker sends it. `null` uses `default_model`. Any model is accepted. |
| `acknowledge_ai_usage` | boolean | Required, and must be `true`: the user agreed to spend their AI usage. |

The model gets one turn and no tools: it cannot edit files, run commands or use the network. The worker gathers the evidence itself (the error, recent errors, the log excerpt and the installed app source around the error's app frames), filters secrets from it, and marks it as untrusted text in the prompt. The model only writes the diagnosis; it never sends anything.

**Response (`202`):** a run.
```json
{
  "run_id": "c3d4...",
  "status": "running",
  "base_report_id": "9f1c2e...",
  "report_id": null,
  "error": null,
  "model": "OpenAI / gpt-6.1",
  "started_at": "2026-10-03T10:06:00+00:00",
  "finished_at": null,
  "caps": {"max_turns": 1, "timeout_seconds": 180, "prompt_max_chars": 60000, "tools": []}
}
```

**Errors:** `400` without `acknowledge_ai_usage: true`, for a report that is already an AI report, or for a model that is not available; `404` for an expired draft; `409` while another diagnosis is running (one at a time); `403` from an agent preview.

### GET /api/v1/support/diagnoses/{run_id}

Poll a run. `status` is `running`, `done` or `failed`.

**Auth:** Owner only

- `done`: `report_id` is the new AI report's id and `report` is its preview (`kind: "ai"`). The basic draft is unchanged and can still be sent instead.
- `failed`: `error` says why (for example the provider refused the request, the model is not signed in, or the run took longer than 180 seconds). No report is made; the basic draft can still be sent.

### POST /api/v1/support/reports/{report_id}/send

Send a draft to HobbyCoders support. The only endpoint that sends a report. Call it only from an explicit user action.

**Auth:** Owner only

**Request:** `{"confirm": true}` (required).

The worker filters secrets once more and sends the preview's `body`, its title, fingerprint, kind, app version and platform, and for an AI report the diagnosis model. Nothing else is sent.

**Response:**
```json
{
  "state": "ok",
  "report_id": "9f1c2e...",
  "hub_report_id": "0e7c...",
  "sent_at": "2026-10-03T10:08:00+00:00",
  "delivered": {"email": "sent", "github": "created"},
  "known_issue": {"status": "new", "fixed_in": null, "note": null, "reported": true, "update_available": false}
}
```

`delivered` says where HobbyCoders received it: `email` is `sent`, `not_configured` or `failed`; `github` is `created`, `commented`, `not_configured`, `failed` or `deferred`. The report is stored by HobbyCoders whatever these say, so the user does not need to resend.

On another `state` the response is `{"state", "message", "retry_after"?, "report_id"}` and the draft stays a draft.

**Errors:** `400` without `confirm: true`; `404` for an expired draft; `409` when the report was already sent (or is being sent); `403` from an agent preview.
