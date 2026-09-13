/**
 * Server-level MCP `instructions`. Loaded once at initialize, so this is where
 * the operating discipline lives (keeping per-tool schemas/descriptions lean).
 * Written plainly so non-Claude models can follow it too.
 */

export const SERVER_INSTRUCTIONS = `You control one web page through this browser server. Work in a loop: look, act, then look again only when something changed.

TWO RULES BEFORE ANYTHING ELSE
- Refs (e12) die on every navigation and after a major DOM change. browser_act tells you when (snapshot_stale: true); a stale_ref error means the same thing. Re-snapshot, then act — never retry the old ref.
- Coordinates need a browser_screenshot taken AFTER the previous action or navigation, in the same batch of work. When the view has not moved the server re-captures for you; when it has, you get stale_screenshot with a fresh screenshot attached. Prefer refs; coordinates are the fallback.
- browser_act scroll takes exactly one of ref or coordinate like every other kind. browser_wait conditions are exactly url, text, element (no selector condition; use element + selector).
- After the first full browser_snapshot of a page, re-read with filter=interactive or scope_ref=eN. A Play-Console-sized page costs ~10k chars per full snapshot; 184 of them in one audited session was 1.8 MB of context.

SHARED WORKSPACE
- browser_status is always available, including during human control, and reports tabs, viewport, busy and pending takeover. Other tools reject with human_control while a viewer owns or requests control. Do not fight the viewer or spin retries; continue other work or ask for control to be returned.
- browser_wait waits for condition=url with url, condition=text with text, or condition=element with ref or selector. Specify exactly one target. URL/text match=contains (default) or exact; text/element state=visible (default), hidden, attached, or detached. timeout_ms defaults to 5000 and is capped at 15000. A timed-out result is not success. Pending takeover interrupts waits.
- Human input, navigation, tab changes and viewport changes invalidate screenshots and refs. A stale_screenshot response requires a new browser_screenshot before using coordinates again.
- To finish a sign-in handoff, ask the user to choose Return control in the Browser panel and wait for Agent control before resuming. A chat reply alone does not release browser ownership. Never override genuine human control.
- Microphone capture is unsupported in the built-in Browser panel: it has no site-permission approval flow. Ask the user to open the page in their normal browser and approve microphone access there. The agent browser runs on its browser-host worker, not necessarily the user's device; it does not forward the viewer's microphone. Desktop app microphone permissions are separate. Do not auto-grant permissions or use blanket permission flags.

REFS AND SNAPSHOTS
- browser_snapshot returns the page as a YAML accessibility tree. Actionable elements carry a ref like [ref=e12].
- To act on an element, pass its ref to browser_act. Refs are the primary way to target things — prefer them over coordinates.
- Refs stay valid for the whole time you are on one page. A committed navigation (new URL) invalidates every ref; take a fresh snapshot afterward.
- Every snapshot response includes a snapshot_id. If you act on a ref that no longer exists you get a stale_ref error — the fix is always: take a new browser_snapshot and use the new refs.
- Snapshots can be large. Use filter=interactive to see only controls plus headings/landmarks, and scope_ref=eN to re-snapshot just one subtree. Long lists of similar rows are collapsed with a "... and N more (scope_ref=eX to expand)" marker. Output is truncated at max_chars (default 40000) with a note telling you how to see more.

ACTING
- browser_act takes one kind: click, dblclick, type, fill, key, hover, scroll, select, drag. Target with exactly one of ref or coordinate. For key, omit both to press on the active page/focused element without a screenshot. Explicit coordinates still require a fresh screenshot.
- fill sets an input's value directly; type sends real keystrokes; select picks an option by value; key presses a named key with optional modifiers; scroll takes direction and amount.
- The result is deliberately terse: { ok, url_changed, dom_mutated: none|minor|major, snapshot_stale }. A fresh interactive snapshot is attached only when the page changed a lot or navigated. If snapshot_stale is false, your existing refs still work — do not re-snapshot needlessly.

FINDING ELEMENTS
- browser_find takes a plain-language query ("the checkout button", "the search box") and returns up to 20 matching refs — one line each as "ref role \\"name\\"" plus a snapshot_id — WITHOUT putting the whole accessibility tree in your context. On a large or unfamiliar page, prefer browser_find over a full browser_snapshot to locate a specific control. The refs it returns are act-able directly: pass one straight to browser_act.
- It runs a small model over an internal snapshot. If that model is unavailable (no credentials, a timeout, or an error) it automatically falls back to deterministic text matching and tells you so on a "semantic search unavailable" line; the refs are still valid to act on.

SNAPSHOT VS SCREENSHOT
- Default to snapshots: they are cheap, structured, and give you refs. Reach for browser_screenshot only when you need to see layout, images, or canvas/visual state that the accessibility tree cannot express.
- browser_screenshot returns a downscaled JPEG and a line like "1280x800". Coordinates for coordinate-based browser_act must be in that pixel space; the server maps them back to the page for you. Prefer refs; use coordinates only when there is no usable ref.

READING AND SCRIPTING
- browser_read_text gives you the page's prose in reader mode (main article/content region, else the body) as { title, url, text }, capped ~30k chars. Reach for it when you want the content, not the controls — it is far cheaper than a snapshot or screenshot.
- browser_eval runs JavaScript in the page. It is a REPL: top-level await is allowed. A single bare expression returns its value automatically; multi-statement code must \`return\` the value explicitly, otherwise nothing comes back. Results are JSON-ish and size-capped (~10k chars, plus an internal total-work budget on huge/deep objects). Non-serializable values are described by type. On a thrown error you get the message, the top stack frame, and recent_requests — the last few responses with their status and content-type. Read those before blaming your code: a fetch that "returns invalid JSON" is usually an HTML sign-in page with a 200, or a 401/403.

SIGN-IN AND COOKIES
- This browser starts with NO session cookies for the user's own apps, and it cannot log itself in. A navigate that lands on another origin reports redirected_to_origin — that is a sign-in redirect, not a page, even though ok stays true because the navigation did happen.
- When you hit one, stop and say so: ask the user to sign in through the live browser panel (they can take control of this same page), or use an endpoint that does not require a session. Do not attempt to enter credentials.

CONSOLE AND NETWORK
- The server records console messages and network responses per tab from the moment the tab is acquired — you do not start a capture, you query the buffer.
- browser_console REQUIRES a pattern (substring or regex) and returns only matching messages ({level, text, location}); default limit 20, newest last. only_errors keeps errors; clear empties the buffer.
- browser_network is two-phase. Call it with no request_id to LIST requests ({id, method, url, status, resourceType}, filter with url_pattern, newest 20). Call it again with a request_id from that list to get that response's text body (capped ~20k; non-text bodies come back as a note with mimeType/size). Bodies are fetched on demand and may be gone after a navigation.

TABS
- One session, several tabs. browser_tabs op=list|create|select|close. create opens and switches to a new tab; select changes which tab is current; close removes one (you cannot close the last tab). EVERY other tool always operates on the current tab — there is no per-call tab argument. Switching or closing the current tab invalidates refs, so take a fresh snapshot afterward.

RESIZE AND UPLOAD
- browser_resize sets the viewport: preset=mobile|tablet|desktop, or an explicit width+height (which overrides a preset). Provide exactly one of those forms.
- browser_upload sets files on a file input you target by ref; pass absolute paths. Only files under the server's configured upload directory are accepted, 10MB per file.

BATCHING
- browser_batch runs several of the tools above in order and stops at the first error. Because the schema stays flat, each action is a JSON STRING: {"tool":"browser_act","input":{"kind":"click","ref":"e5"}}. Pass them as the actions array (max 20). A batch cannot contain another batch. You get one result per attempted action ({tool, ok, summary|error}).

NETWORK POLICY
- Some addresses are refused by egress policy: navigate returns "blocked by SSRF policy: <reason>". Cloud metadata is always refused and cannot be re-enabled. Loopback (127.0.0.1 / localhost / ::1) is refused only on installs that have not opted in — many have, so try it before assuming it is off.
- The reason string names the host, why it was refused, and what would allow it (BROWSER_SSRF_ALLOW / BROWSER_SSRF_ALLOW_LOOPBACK). Read it. Retrying the same URL unchanged will not help — this is configuration, not a flaky network — but the situation is fixable: tell the user which setting to flip, or, for a server you control, bind it to 0.0.0.0 and browse its LAN IP instead.

If the browser is momentarily reconnecting you may get error "browser_restarting" — wait briefly and retry.`;
