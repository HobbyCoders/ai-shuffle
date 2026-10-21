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
} from "./schemas.js";

export const TOOL_CATALOG = [
  { name: "browser_status", description: "Shared browser state without touching the page: control (agent or human), busy, tabs, viewport, pending dialog, downloads, upload root. Works during human control. upload_paths checks files without uploading.", inputSchema: statusSchema },
  { name: "browser_wait", description: "Wait for condition url, text or element (element takes ref or selector; there is no selector condition). state: visible (default), hidden, attached, detached. timeout_ms default 5000, max 15000. Text/selector waits search every attached same/cross-origin frame and open shadow roots, using Playwright element visibility without reader injection filters; scope_ref limits the subtree. Negative states require known absence in every included frame; failed or timed-out checks are unknown, not absent. Invalid selectors return invalid_input. A timeout returns ok:false.", inputSchema: waitSchema },
  { name: "browser_navigate", description: "Open a URL, or \"back\" / \"forward\". Returns url, title and an interactive snapshot. redirected_to_origin means a sign-in wall this browser cannot pass itself.", inputSchema: navigateSchema },
  { name: "browser_snapshot", description: "Accessibility tree of the current tab with refs to act on. Refs expire on navigation and may after a major DOM change. After one full snapshot of a page, use filter=interactive or scope_ref.", inputSchema: snapshotSchema },
  { name: "browser_act", description: "One action on exactly one target: a ref, or a coordinate from the latest browser_screenshot (key may omit both; dialog answers take none). type sends keystrokes, fill sets the value. Returns url_changed, dom_mutated, snapshot_stale and events (new_tab, download, dialog).", inputSchema: actSchema },
  { name: "browser_screenshot", description: "JPEG of the viewport, a region or the full page, with screenshot_id and size. Its pixels are the coordinate space for browser_act. For layout, images and canvas; snapshots are cheaper.", inputSchema: screenshotSchema },
  { name: "browser_find", description: "Find by exact role+name, label, placeholder, test_id or text; query still accepts labels or descriptions. Exact lookups search the main document and open shadow roots. Ambiguity returns ok:true, ambiguous:true and candidate refs; do not act until chosen: snapshot the parent row or container, then pass index. Full local search includes frame refs beyond snapshot output limits; the model receives a capped interactive tree only when description search or an unmatched query needs it. Returns up to 20 refs.", inputSchema: findSchema },
  { name: "browser_read_text", description: "Readable text (main content, else body), including same/cross-origin iframes and open shadow roots. Frames use document geometry, not viewport visibility or hit testing: below-fold, overlaid and pointer-events:none frames are read. Hidden/clipped/off-document frames are excluded and counted as frames_hidden; prose is labelled by origin. Reads at most 48 children with bounded concurrency; frames_skipped counts cap/timeout/busy skips. Main-frame failures are errors, not blank content. Optional scope_ref limits the subtree. 30000 chars per call; continue with offset.", inputSchema: readTextSchema },
  { name: "browser_eval", description: "Run JavaScript in the page and return the result (size capped). Errors include recent network responses.", inputSchema: evalSchema },
  { name: "browser_console", description: "Buffered console messages matching pattern, newest last.", inputSchema: consoleSchema },
  { name: "browser_network", description: "Without request_id: list recorded requests. With request_id: that response's text body, 20000 chars per call; continue with offset.", inputSchema: networkSchema },
  { name: "browser_tabs", description: "op=list, create, select or close. Every other tool acts on the current tab only.", inputSchema: tabsSchema },
  { name: "browser_resize", description: "Set the viewport by preset or width+height. Returns the applied size.", inputSchema: resizeSchema },
  { name: "browser_upload", description: "Set files on a file input ref. Paths must be under the runtime upload root; check them with browser_status upload_paths.", inputSchema: uploadSchema },
  { name: "browser_batch", description: "Run up to 20 tool calls in order. Stops at the first failed step; after a step that navigates or opens a tab or dialog, only observation steps still run. Keeps final observation output and fresh trees within an 80000-character shared text budget; earlier steps are compact. Screenshots stay beside their metadata.", inputSchema: batchSchema },
] as const;

export type BrowserToolName = typeof TOOL_CATALOG[number]["name"];
