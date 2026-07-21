/**
 * Server-level MCP `instructions`. Loaded once at initialize, so this is where
 * the operating discipline lives (keeping per-tool schemas/descriptions lean).
 * Written plainly so non-Claude models can follow it too.
 */

export const SERVER_INSTRUCTIONS = `You control one web page through this browser server. Work in a loop: look, act, then look again only when something changed.

REFS AND SNAPSHOTS
- browser_snapshot returns the page as a YAML accessibility tree. Actionable elements carry a ref like [ref=e12].
- To act on an element, pass its ref to browser_act. Refs are the primary way to target things — prefer them over coordinates.
- Refs stay valid for the whole time you are on one page. A committed navigation (new URL) invalidates every ref; take a fresh snapshot afterward.
- Every snapshot response includes a snapshot_id. If you act on a ref that no longer exists you get a stale_ref error — the fix is always: take a new browser_snapshot and use the new refs.
- Snapshots can be large. Use filter=interactive to see only controls plus headings/landmarks, and scope_ref=eN to re-snapshot just one subtree. Long lists of similar rows are collapsed with a "... and N more (scope_ref=eX to expand)" marker. Output is truncated at max_chars (default 40000) with a note telling you how to see more.

ACTING
- browser_act takes one kind: click, dblclick, type, fill, key, hover, scroll, select, drag. Target with exactly one of ref or coordinate.
- fill sets an input's value directly; type sends real keystrokes; select picks an option by value; key presses a named key with optional modifiers; scroll takes direction and amount.
- The result is deliberately terse: { ok, url_changed, dom_mutated: none|minor|major, snapshot_stale }. A fresh interactive snapshot is attached only when the page changed a lot or navigated. If snapshot_stale is false, your existing refs still work — do not re-snapshot needlessly.

SNAPSHOT VS SCREENSHOT
- Default to snapshots: they are cheap, structured, and give you refs. Reach for browser_screenshot only when you need to see layout, images, or canvas/visual state that the accessibility tree cannot express.
- browser_screenshot returns a downscaled JPEG and a line like "1280x800". Coordinates for coordinate-based browser_act must be in that pixel space; the server maps them back to the page for you. Prefer refs; use coordinates only when there is no usable ref.

If the browser is momentarily reconnecting you may get error "browser_restarting" — wait briefly and retry.`;
