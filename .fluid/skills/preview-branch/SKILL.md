---
name: preview-branch
description: Verify a branch's UI in the agent browser against this worker — the LAN-host rule, the owner-auth guard and the session preview cookie, python -m app.preview stamp for a branch build, and when to snapshot versus screenshot. Use before claiming any user-visible change is verified.
---

# Preview a branch's UI in the agent browser

The agent browser (browser_* tools) may run on a different host from the
shell your commands run in (it does on the container and on shared runners).
Everything below works in both cases.

## 1. Where the browser can reach you

- Any server you start must bind `0.0.0.0`. Open it at
  `http://<browser host>:<port>` — the address in the "# Workspace facts"
  card ("Agent browser: ... open it at http://<host>:<port>"). That address
  works whether the browser is local or remote; `localhost` only works when
  it is local, and `browser_navigate` on a loopback URL that fails names the
  right address in its error ("Browse http://<host>:<port> instead").
- Vite's dev server: `npm run dev -- --host 0.0.0.0 --port 5173` in
  `frontend/` (run `npm ci` there first in a fresh worktree). It proxies
  `/api` to `AI_SHUFFLE_DEV_API` (default `http://localhost:8000`) and sets
  `x-forwarded-host`, which the worker's owner-auth guard rejects — that is
  the "only accepts direct connections" / `local_access: false` result. The
  session preview cookie from section 2 passes through this proxy, so: open
  the preview-access URL from the card first (with `next=/`), then open the
  Vite URL on the same hostname; cookies ignore the port, so it rides along
  and the proxy forwards it to the backend. If the cookie was set on a
  different hostname than the Vite one, set it through the proxy instead by
  opening `http://<vite host>:5173/api/v1/auth/preview-access?token=...&next=/`.

## 2. Opening THIS worker's UI (the running app)

The agent browser is refused the worker's own ports (`:8000` and the
protected one) on every internal address: a request from this host to them
is the owner, with no token asked. That is the "this is the AI Shuffle
worker's own listener ... may never open" error; no setting lifts it, and you
must not work around it with a relay or a second worker.

The worker has a third, preview-only listener for the browser (`:8002` by
default, `SHUFFLE_PREVIEW_PORT` to move it, `0` to switch it off). It never
treats a peer as the owner and serves nothing except to a session's preview
grant.

Do this, once per session:

1. Copy the preview-access URL from the "# Workspace facts" card
   (`http://<host>:<preview port>/api/v1/auth/preview-access?token=...&next=/`).
   No URL in the card means the listener is off or this session runs as an
   API key; say so instead of looking for another way in.
2. `browser_navigate` to it. The server validates the token (bound to this
   session, expires with it), sets an HttpOnly cookie and redirects to `next`.
3. Stay on that port. Every later navigate/act in this browser session
   carries the cookie; the UI behaves as the local owner for reads and for
   driving a chat session. Mutations of auth, pairing, credentials, settings,
   network and plugin routes are refused on that cookie by design; make those
   through the API from your shell when the task calls for them. The cookie
   is only honoured from a private-network peer; a public tunnel cannot use
   it.

To land on a specific session, pass `next=/?session=<session id>` on the first
navigate. If you get 403 from the redirect endpoint, the token expired or the
session row is gone — there is no retry; say so.

## 3. Previewing a BRANCH's build (not the running worker's)

`app/preview.py` serves a stamped branch build from the normal authenticated
listener of an isolated instance. It never starts services or proxies.

1. In the worktree: `cd frontend && npm ci && npm run build` (output in
   `frontend/build/`).
2. From the worktree root: `python -m app.preview stamp` — records branch,
   commit, source hash and build hash into `frontend/build/.shuffle-preview.json`.
   A stale stamp (source or build changed afterwards) makes startup fail
   closed with "rebuild and run python -m app.preview stamp".
3. Start the app with `AI_SHUFFLE_PREVIEW=1` **only on an isolated instance**
   (its own DATA_DIR and Postgres, e.g. a throwaway container) or after
   stopping this instance's worker. Never run a second worker against the
   running instance's data dir; never copy a `.env` into the worktree.
4. `GET /api/preview` on that instance returns the stamped identity so you
   can prove which commit the UI came from; every page carries
   `X-AI-Shuffle-Preview: <build sha>`.
5. The preview routes accept the local owner or the session preview cookie
   from section 2 (same LAN/listener rules).

When an isolated instance is not available, say the branch UI was checked
statically (vite dev) and list which API-backed screens were not exercised.

## 4. Look cheaply

- After navigate, one `browser_snapshot` per surface. On a large page take
  the first snapshot in full, then `filter="interactive"` or `scope_ref=eN`
  for re-reads. Refs die on navigation and after a major DOM change
  (`snapshot_stale: true`) — re-snapshot, do not retry the old ref.
- `browser_eval` for state (store values, element text, computed styles).
- `browser_screenshot` only when layout or rendering is the claim. A
  coordinate click needs a screenshot taken after the previous action; the
  server re-captures for you when the view has not moved.
- `browser_read_text` when you want prose, not controls.

## 5. Report

Name the page, the session and the exact interaction you exercised. If the
running app was never opened, write that in the summary and the PR body
instead of "verified".
