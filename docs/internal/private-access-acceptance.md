# Private SSH access acceptance

## Folder request feedback (2026-10-02)

During #1598 acceptance on ServerWorker, a folder approval failed with
`FileNotFoundError`. The user saw a generic connection/SSH-credentials error.
This confirms misleading folder feedback, not a failure of the sandbox itself;
the container's code sandbox and folder-shell self-test had passed.

`RequestChatAccess` now accepts an optional `suggested_path` for folder requests.
The model should propose a known absolute path on the owning worker. The card
prefills it for review and allows edits. A suggestion does not open or create a
folder, grant access, or bypass approval. Only the privately submitted path is
used to open the folder; that path is not returned to the model. Older requests
without a suggestion still work. Docker users must select a path mounted
inside the container, not an unmounted host path.

Missing folders, non-directory/link components and unreadable folders receive
fixed, folder-specific messages without private exception text. Failed approval
leaves the same request available for correction until its original deadline.

The updated card has not been verified in the running app: this session's agent
browser is blocked from the worker listener and desktop control remains off.
The ServerWorker private-folder approval/command acceptance is still open.

Investigation of #1421 on 2026-09-21. The report describes SSH access to an
ServerWorker server, sometimes rejected inputs and then an immediate expiry message;
the original desktop/Android client and exact error are unknown. This acceptance
does not reproduce that incident or establish its cause.

## Running application check

A separate Windows worker used an isolated HOME, data directory and PostgreSQL
database. Its SSH target was an AsyncSSH server bound to loopback with a generated
host key and disposable password. The target returned fixed probe output and
never executed host shell commands. No production connection, saved credential,
model request or user ChatGPT quota was used.

A local driver invoked the registered `RequestChatAccess` and `ChatRunCommand`
SDK handlers directly. The normal broker, owner API, global invalidation and
built application UI handled each request. This verifies the tool-to-broker-to-UI
path without a model or delegated vendor CLI. A synthetic user event seeded the
conversation through the normal event store so the transcript view was mounted.

Observed before the feedback change:

- A wrong password failed without creating a grant; correcting it fulfilled the
  same pending request and returned one opaque handle.
- A command waited for its separate UI approval, then returned the fake target's
  successful result exactly once. A second command was denied and never reached
  the target.
- UI revocation removed the grant. An unsubmitted request reached the real
  four-minute timeout, returned the tool's expiry error and disappeared from the
  UI. No test clock was advanced.

An initial fixture fault is excluded from these results: the stripped Windows
environment omitted `USERNAME`, causing AsyncSSH's local-user lookup to import
the Unix-only `pwd` module. The form misleadingly described this worker failure
as bad connection details. Supplying a non-secret fixture username fixed the
test launcher; it is not evidence of an installed-worker defect or the ServerWorker
failure's root cause.

## Feedback change and verification

The owner API now distinguishes rejected SSH sign-in, a host-key mismatch and a
connection timeout. Unexpected worker exceptions produce a worker-error response
instead of asking the user to correct credentials. These new error responses use
fixed text; worker-failure logs record only the exception class, keeping private
inputs out of the feedback. Existing authorization,
fingerprint verification, request deadlines, grant duration and per-action
approval are unchanged.

The private form displays the server-provided submission deadline and names the
separate grant duration explicitly. The display uses local time; the server still
enforces expiry. There is no client-clock-based authorization or automatic retry.

On the rebuilt application, the browser showed each distinct wrong-password and
host-key error. Correcting both inputs fulfilled the same request. The deadline
and access-duration labels rendered correctly, and the final test grant was
revoked through the UI.

Validation: 40 backend tests passed, with two existing Linux-folder cases skipped
on Windows; 15 ChatAccess UI tests passed; Svelte check reported zero errors and
warnings; the production frontend build, changed-Python Ruff checks and blocking
DB scan passed. New real-loopback-SSH API regressions verify that failed sign-in
and host-key submissions can be corrected without replacing the request or
creating extra grants. Injected timeout and worker faults verify status/message
classification and that secret-bearing exception text is not echoed.

Remaining acceptance includes the reported ServerWorker/client failure, remote-owner
management and delegated-runtime cancellation behavior. Keep #1421 open.

## Session Bypass confirmations (2026-09-29)

Initial private access still requires the user's approval, including saved
connections. After that grant, writes and SSH commands skip the per-action card
only while the session's effective permission mode is Bypass. Other modes retain
one-shot approval; Chat sessions retain their default-permission ceiling. Scope,
read-only folders, ownership, expiry and revocation are unchanged.

The broker reads the connected adapter's live mode, falling back to the persisted
session mode when no adapter is connected (including Voice). Changing to Bypass
through the permission-mode API also releases pending valid action confirmations,
not pending initial access requests. Changing back restores confirmation for the
next action without rebuilding the tools. A denied, expired or revoked action is
not revived. Newly queued actions recheck the mode so an in-flight persisted-mode
read cannot miss the owner's Bypass sweep. Execution revalidates the grant after
the confirmation decision, including when Bypass skipped the card.

Private access is available from the shield/Access control in the shared desktop
and mobile header actions. Clicking it opens a native modal; pending-request
badges do not open it automatically. The grants disclosure retains the user's
open/closed choice rather than reopening whenever grants are present.

Backend regressions exercise registered SDK/Voice tools, initial request
fulfillment, real pinned loopback SSH, folder writes, native/delegated adapter
mode setters and the owner mode-change API. These tests do not constitute a
running-browser check or reproduce the original ServerWorker report.

A separate Linux preview with its own PostgreSQL/data directory exercised the
normal broker, owner API, event store/SSE and built UI at 430×932 and 1280×800.
The browser granted a disposable writable folder through the initial request
form. Collapsing its disclosure and closing the modal persisted while real
`assistant.text_delta` events and broker invalidations continued. The header
control and modal fit both viewports without horizontal overflow or adding to
the bottom task stack. A broker file write completed without a pending card in
Bypass; changing to Default produced a pending one-shot card and held the write;
changing back to Bypass released that pending write. No model/provider request,
production connection, credential or Android device was used in this check.

## HTTPS service credentials (#1267, 2026-10-01)

A third resource kind, `service`, gives the model a typed consumer for one API
key or token without ever seeing it. The private card collects an `https://`
address (optionally with a path prefix), the credential header (default
`Authorization`), an optional scheme (default `Bearer`) and the secret. Read-only
grants allow GET and HEAD; "Allow changes" adds POST, PUT, PATCH and DELETE,
which follow the session's permission mode like writes and SSH commands.

Boundaries (`app/core/chat_services.py`):

- HTTPS only. By default, verify against the worker's trust store for the granted
  host. An owner may instead select "Trust a specific certificate" in the private
  card and enter its SHA-256 fingerprint (64 hex digits, optionally colon-separated).
  Verify that fingerprint on the server or another trusted channel first; the app
  never discovers and silently trusts an unverified certificate. This exact,
  currently valid certificate becomes the trust anchor for that service grant.
  Granting completes a TLS handshake and sends no HTTP request or credential.
  Every request verifies the pin before application data or credentials are sent.
  Certificate replacement fails closed and needs a fresh owner-approved grant.
  Saving a connection encrypts and remembers its pin along with its credential.
  Unpinned services still require normal CA and hostname verification.
- `ChatHttpRequest` takes a handle, method, path, body and content type. The
  path must stay under the granted prefix: dot segments (also percent-encoded),
  encoded slashes/backslashes, empty segments, fragments and spaces are refused,
  and the final URL is re-parsed to confirm scheme, host, port and prefix.
- The credential header cannot replace framing/routing headers. Redirects are
  returned, never followed. No proxy, netrc or other worker environment is
  inherited. Only content type/length, location, retry-after, etag and
  last-modified response headers are returned (never Set-Cookie).
- Bodies are bounded to 64 KiB. Known encodings of the secret (raw, header
  value, URL/JSON-escaped, Base64, decoded Basic credentials) are redacted from
  the body and returned headers. Positions are counted in the received bytes so
  a truncated occurrence cannot slide into view. This is defence in depth: a
  service which transforms and echoes the credential differently could still
  disclose it, so the card says the model can use its full authority there.
- Grants, approvals, expiry, revocation (which cancels in-flight requests),
  session scoping and Bypass behave exactly as for SSH and folders. Service
  credentials can be saved to the encrypted vault; the catalogue carries the
  kind and address, never the secret. Folder grants are never saved.
- GET requests do not need approval. A service that changes state on GET is
  not protected by the read-only setting.

The header fix: since #1518 the private-access dialog of a Chat-lane or
projectless session had no opener, because the git cluster that holds the
"Private access" row is hidden there. Such sessions now render ChatAccess's own
Access button (`privateAccessTrigger`), which appears only while there is a
request, approval, grant or saved connection to show.

### Running application check

A disposable Linux worker (its own PostgreSQL, data directory and HOME, and a
trust store containing only a throwaway CA) ran the built application with a
loopback HTTPS fake API using that CA. A loopback-only harness route invoked
the registered `RequestChatAccess`, `ChatResources` and `ChatHttpRequest` SDK
handlers in the server process; no model, provider or real credential was used.
Playwright drove Chromium at 1280×800 and 430×932, and every check passed at
both sizes:

- A fresh Chat session had no grants; a fabricated handle and another session's
  handle were refused.
- The Chat header showed "Access · 1 pending"; Decline returned a refusal.
- Granting through the card returned only `id`, `kind`, `expires_at` and
  `writable`, and the fake API received no request at grant time.
- A GET reached the API with the credential header; the echoed credential came
  back as `[redacted]`. A POST on the read-only grant and a `/../admin` path were
  refused without reaching the API.
- The panel listed the grant's address and "GET and HEAD only". UI revocation
  stopped the next request.
- On a writable grant a PUT showed an "Approve this API request?" card with the
  exact method, path, content type and body. Deny was refused without reaching
  the API; Approve once delivered exactly one request.
- The secret never appeared in tool results, the page DOM or approval card, and
  there was no horizontal overflow.

### Owner-approved certificate pinning acceptance (2026-10-01)

Owner policy: GET/HEAD keep running without individual approval after the initial
private grant. Writes remain approval-gated outside Bypass. This does not make
poorly designed GET endpoints safe; redaction is still only defence in depth.

Exercised the built branch's real app and private-access card in a disposable
instance with its own PostgreSQL, data directory, owner cookie, and self-signed
HTTPS test service. No production credential or home-server setting was used.
The browser needed an exception for the disposable app listener's own self-signed
certificate; that browser exception is separate from service-grant pinning.

- Desktop 1440x900: unpinned self-signed grant rejected, zero HTTP requests;
  explicit fingerprint and private test credential accepted through the real
  card, with explicit encrypted save. A subsequent GET returned HTTP 200 with
  no approval card; the secret input was cleared and no secret appeared in text.
- Mobile-width web UI 390x844: the new certificate field rendered within the
  scrollable private panel; the saved connection displayed the correct
  certificate-fingerprint label and identity. A new owner-approved grant reused
  it without re-entering the credential; GET again returned HTTP 200 and zero
  pending approvals. No page-level horizontal overflow at either viewport.
- Real TLS regression tests exercise wrong pins, certificate replacement even
  with the same key, invalid fingerprint syntax, expired/not-yet-valid pinned
  certificates, and unchanged default CA/hostname verification. The service
  sees no authenticated request on pin mismatch. Vault tests cover encrypted
  save/load and the owner's public certificate metadata.

This is disposable-service acceptance, not a claim that the owner's unRAID API,
Android/native app, or production certificate rotation has been exercised.

## Isolated folder shell (#1267)

`ChatRunCommand` on a folder grant runs one command through
`app/core/chat_shell.py`: bubblewrap with new user, pid, network, ipc, uts and
cgroup namespaces, no nested user namespaces, the grant's open descriptor bound
at `/work` (read-only for a read-only grant), read-only `/usr` and Python prefix,
a private `/proc`, `/dev` and a 64 MiB `/tmp`. Limits: 60 s wall and CPU, 1 GiB
address space, 64 MiB per file, 256 open files, 64 processes. A seccomp filter
allows only IPv4/IPv6 sockets and stream socket pairs, and refuses io_uring,
the keyring calls and other ABIs: the network namespace does not close
Unix-domain sockets, and a listening one inside the folder (a `/tmp` grant can
hold the worker's own trust-authenticated PostgreSQL socket) would be a route
out. Approval works exactly as for SSH commands. It refuses, before any
approval card, on a non-Linux or root worker, on an architecture other than
x86_64 or aarch64, without bubblewrap 0.8+ options (`--bind-fd`,
`--ro-bind-fd`, `--size`, `--disable-userns`, `--seccomp`), when the Python
prefix contains the data directory, when the self-test fails, or when another
mount sits inside the folder.

Before each command, before the approval card and again after it, a bounded
walk of the folder (100,000 entries, 10 s, 256 levels) refuses the command if a
regular file has more links than the folder holds: a hard link to a file
elsewhere on the same filesystem would let the sandbox read and write it. Going
past a bound, an unreadable directory or another device refuses too. After a
command in a writable folder, setuid and setgid bits it set are cleared, so the
host never honours them; files that had them before keep them.

CI runs `tests/core/test_chat_shell.py` against real bubblewrap 0.9.0 on Ubuntu
24.04 with the sandbox required: writes outside `/work`, symlinks to host paths,
`/proc/1/root`, leaked descriptors, loopback and external network, a host
Unix socket listening inside the folder, hard links to host files, setuid
bits, nested user namespaces, surviving background processes, the limits, the timeout and the
approval path. The Docker image already ships bubblewrap; the container needs
the compose file's `security_opt` lines, as for the code-mode sandbox. The
image path has not been exercised on the owner's ServerWorker host.

Still outstanding for #1267: Windows/macOS folder brokers, owner-device routing
for mirrored sessions, and a supported remote HTTPS deployment path. Certificate
pinning now has an owner-entered SHA-256 option; production LAN-service
acceptance remains to be exercised with the owner.
