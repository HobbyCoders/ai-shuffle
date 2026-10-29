/** Metadata-only browser catalog. No Playwright, handlers or workspace startup. */
import {
  statusSchema,
  waitSchema,
  navigateSchema,
  snapshotSchema,
  actSchema,
  screenshotSchema,
  findSchema,
  readTextSchema,
  evalSchema,
  consoleSchema,
  networkSchema,
  tabsSchema,
  resizeSchema,
  uploadSchema,
  batchSchema,
  recordSchema,
} from "./schemas.js";

export const TOOL_CATALOG = [
  { name: "browser_status", description: "Shared browser state without touching the page: control (agent or human), busy, tabs, viewport, pending dialog, downloads, upload root. Works during human control. upload_paths checks files without uploading.", inputSchema: statusSchema },
  { name: "browser_wait", description: "Wait for condition url, text or element (element takes ref or selector; there is no selector condition). state: visible (default), hidden, attached, detached. timeout_ms default 5000, max 15000. Text/selector waits search every attached same/cross-origin frame and open shadow roots, using Playwright visibility on both matching elements and every ancestor iframe, without reader injection filters; scope_ref limits the subtree. Negative states require known absence in every included frame; failed or timed-out checks are unknown, not absent. Invalid selectors return invalid_input. A timeout returns ok:false.", inputSchema: waitSchema },
  { name: "browser_navigate", description: "Open a URL, or \"back\" / \"forward\". Returns url, title, dispatch/settle/snapshot timings and an interactive snapshot; rendered pages without controls get a short DOM-quiet wait without a pre-action sample; a fresh control-free document with continuous text churn conservatively reaches the cap. Loading shells get a bounded hydration wait. text_churn_ignored:true means pre-observation text churn substituted for real quiet. redirected_to_origin means a sign-in wall this browser cannot pass itself.", inputSchema: navigateSchema },
  { name: "browser_snapshot", description: "Accessibility tree of the current tab with refs to act on. Refs expire on navigation and may after a major DOM change. After one full snapshot of a page, use filter=interactive or scope_ref.", inputSchema: snapshotSchema },
  { name: "browser_act", description: "One action on exactly one target: a ref, or a coordinate from the latest browser_screenshot (key may omit both; dialog answers take none). type sends keystrokes; fill reports value_matches for native fields and a capped actual_value only on mismatch (try type for masks). select accepts values[] for native multi-select or an exact label for ARIA widgets. drag accepts to_ref or screenshot path/to_coordinate plus hold_ms/drop_delay_ms. scroll returns position and at_end. Passive ticker history includes attached top-document shadow roots; detached observers are pruned and native attachShadow is restored while idle. Late-attached roots sample on discovery. Frame ticker evidence requires at least two recent pre-action mutations and expires after each action: ticking frames sample again, static frames sample once. Whitespace-only frames skip sampling but sample when text arrives. All document/target history is weakly keyed. A late-starting frame clock can cost one capped action before recovery. Returns url_changed, dom_mutated, snapshot_stale, settle_capped, observation_failed, pending_requests, pending_timers, text_churn_ignored (pre-action text churn ended observation, not real quiet; reusing a ticker for untracked progress can return early), timings and events (new_tab, download, dialog).", inputSchema: actSchema },
  { name: "browser_screenshot", description: "JPEG of the viewport, a region or the full page, with screenshot_id and size. Its pixels are the coordinate space for browser_act. For layout, images and canvas; snapshots are cheaper.", inputSchema: screenshotSchema },
  { name: "browser_find", description: "Find by exact role+name, label, placeholder, test_id or text; query still accepts labels or descriptions. Exact lookups search the main document and open shadow roots. Ambiguity returns ok:true, ambiguous:true and candidate refs; do not act until chosen: snapshot the parent row or container, then pass index. Full local search includes frame refs beyond snapshot output limits; the model receives a capped interactive tree only when description search or an unmatched query needs it. Returns up to 20 refs.", inputSchema: findSchema },
  { name: "browser_read_text", description: "Readable text (main content, else body), including same/cross-origin iframes and open shadow roots. Frames use document geometry, not viewport visibility or hit testing: below-fold, overlaid and pointer-events:none frames are read. Hidden/clipped/off-document frames are excluded and counted as frames_hidden; prose is labelled by origin. Reads at most 48 children with bounded concurrency; frames_skipped counts cap/timeout/busy skips. Main-frame failures are errors, not blank content. Optional scope_ref limits the subtree. 30000 chars per call; continue with offset.", inputSchema: readTextSchema },
  { name: "browser_eval", description: "Run JavaScript in the page (size-capped result, 5000 ms deadline). Timeout does not cancel page-side work: its outcome is unknown; inspect state before retrying mutations. Use browser_wait for long waits. Page-side errors include recent network responses.", inputSchema: evalSchema },
  { name: "browser_console", description: "Buffered console messages matching pattern, newest last.", inputSchema: consoleSchema },
  { name: "browser_network", description: "Without request_id: list recorded requests. With request_id: that response's text body, 20000 chars per call; continue with offset.", inputSchema: networkSchema },
  { name: "browser_tabs", description: "op=list, create, select or close. Every other tool acts on the current tab only.", inputSchema: tabsSchema },
  { name: "browser_resize", description: "Set the viewport by preset or width+height. Returns the applied size.", inputSchema: resizeSchema },
  { name: "browser_upload", description: "Set files on a file input ref. Paths must be under the runtime upload root; check them with browser_status upload_paths.", inputSchema: uploadSchema },
  { name: "browser_batch", description: "Run up to 20 tool calls in order. Stops at the first failed step; after a step that navigates or opens a tab or dialog, only observation steps still run. Keeps final observation output and fresh trees within an 80000-character shared text budget; earlier steps are compact. Screenshots stay beside their metadata.", inputSchema: batchSchema },
  { name: "browser_record", description: "UI flight recorder for states a screenshot misses (flicker, remounts, going backwards). start records a text signature every animation frame (URL plus data-ui-* attributes), optionally under CPU/network throttling with keyframes; browser_act, browser_navigate, browser_eval, browser_upload and browser_resize on the recorded tab mark each step; stop returns the timeline, anomalies (reversal, remount, layout_shift, errors, failed_requests, duplicate_rows) and stats, plus a contact sheet when keyframes is on. Survives client-side routing and full page loads in the same origin.", inputSchema: recordSchema },
] as const;

export type BrowserToolName = typeof TOOL_CATALOG[number]["name"];
