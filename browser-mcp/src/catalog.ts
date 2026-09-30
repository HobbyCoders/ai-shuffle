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
  { name: "browser_status", description: "Current shared browser tabs, viewport, busy state, agent/human control and effective runtime upload configuration. Optional upload_paths checks eligibility without uploading. Available during human control; never creates a page.", inputSchema: statusSchema
  },
  { name: "browser_wait", description: "Bounded wait. condition is exactly one of url | text | element — there is no selector condition: for a CSS selector pass condition=element with selector=... (or ref=...). Element state: visible (default), hidden, attached, detached. Default 5s, maximum 15s; interrupted by human takeover.", inputSchema: waitSchema
  },
  {
    name: "browser_navigate",
    description:
      "Open a URL (bare domains get https://) or go back/forward. Acquires the page, waits up to 1.5s for visible controls, and returns a snapshot; this is not proof of app readiness. " +
      "The browser holds NO session cookies for the user's own apps: a landing on another origin (reported as redirected_to_origin) is a sign-in demand it cannot satisfy itself.",
    inputSchema: navigateSchema
  },
  {
    name: "browser_snapshot",
    description:
      "Return the page's accessibility tree as refs you can act on. Refs expire on every navigation and after a major DOM change (browser_act reports snapshot_stale: true when that happened) — never reuse refs across those. Take ONE full snapshot per page, then use filter=interactive or scope_ref=eN to re-read only the part that changed; a large page re-snapshotted in full each time costs thousands of tokens per call.",
    inputSchema: snapshotSchema
  },
  {
    name: "browser_act",
    description:
      "Perform one action (click/type/fill/key/hover/scroll/select/drag/dblclick) on exactly one of ref or coordinate — scroll included; only key may omit both to use the focused element. Refs expire on navigation and after a major DOM change (take a new snapshot). Coordinates are in the pixel space of the last browser_screenshot and need one taken since the previous action or navigation: if that screenshot is stale the server re-captures the viewport itself when the view is unchanged, otherwise it returns stale_screenshot with a fresh screenshot attached for you to re-aim from. Returns a terse change summary.",
    inputSchema: actSchema
  },
  {
    name: "browser_screenshot",
    description:
      "Capture the viewport (or a region / full page). Downscaled JPEG; the returned pixel dimensions define the coordinate space for browser_act. Any action, navigation, tab or viewport change invalidates it for coordinate use. Use it only when layout or rendering is the question — snapshots are cheaper and give refs.",
    inputSchema: screenshotSchema
  },
  {
    name: "browser_find",
    description:
      "Find elements by a plain-language query without pulling the whole tree into context. Returns up to 20 act-able refs; falls back to text matching if the model sub-call is unavailable.",
    inputSchema: findSchema
  },
  {
    name: "browser_read_text",
    description:
      "Reader-mode text of the current page: main article/content region (falls back to body). Returns title, url, and text (capped ~30k chars).",
    inputSchema: readTextSchema
  },
  {
    name: "browser_eval",
    description:
      "Run JavaScript in the page (top-level await allowed; last expression is returned). Result is JSON-ish, size-capped; errors return message + top stack frame, plus recent_requests (status and content-type) so a failed fetch names itself.",
    inputSchema: evalSchema
  },
  {
    name: "browser_console",
    description:
      "Read buffered console messages. `pattern` is REQUIRED (substring/regex). only_errors, limit (default 20, newest last), clear.",
    inputSchema: consoleSchema
  },
  {
    name: "browser_network",
    description:
      "Two-phase: without request_id, list recorded requests (filter by url_pattern); with request_id, return that response's text body (capped ~20k).",
    inputSchema: networkSchema
  },
  {
    name: "browser_tabs",
    description:
      "Manage tabs: op=list|create|close|select. Every other tool acts on the current tab; select moves that pointer. close rejects the last tab.",
    inputSchema: tabsSchema
  },
  {
    name: "browser_resize",
    description:
      "Set the viewport: preset=mobile|tablet|desktop, or explicit width+height (overrides preset).",
    inputSchema: resizeSchema
  },
  {
    name: "browser_upload",
    description:
      "Set files on a file input (by ref). paths must be under the runtime BROWSER_UPLOAD_DIR; preflight with browser_status upload_paths, not the worker shell env. No tool-imposed size cap.",
    inputSchema: uploadSchema
  },
  {
    name: "browser_batch",
    description:
      'Run several tools in order, stop on first error. actions is an array of JSON strings {"tool":"...","input":{...}}. No nested batch; max 20.',
    inputSchema: batchSchema
  }
] as const;

export type BrowserToolName = typeof TOOL_CATALOG[number]["name"];
