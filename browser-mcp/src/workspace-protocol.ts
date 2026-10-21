/** Wire protocol v2. No page-provided objects are forwarded as CDP commands. */
export interface BrowserEditor {
  id: string; type: string; label: string; value: string;
  accept?: string; multiple?: boolean; chooser?: boolean; keys?: boolean;
  /** Date/time pickers: the page's own min/max/step attributes, unparsed. */
  min?: string; max?: string; step?: string;
  /** A picker control (select, date, time…) that has the page's focus. */
  focused?: boolean;
  style?: { fontSize: number; lineHeight: number | null; paddingLeft: number; paddingTop: number; borderLeft: number; borderTop: number; fontFamily: string };
  x: number; y: number; width: number; height: number;
}
/** A select's options for the panel's own list (no option values: the panel picks by index). */
export interface BrowserChoices {
  selected: number; groups: string[];
  options: { label: string; disabled: boolean; group?: number }[];
  truncated?: boolean;
}
export interface BrowserWorkspaceState {
  editors?: BrowserEditor[];
  runtime_id: string; revision: number; available: boolean;
  target_id: string | null; active_tab_id: string | null;
  tabs: { id: string; url: string; title: string; current: boolean }[];
  viewport: { mode: "fit" | "fixed"; width: number; height: number };
  control: { owner: "agent" | "human"; viewer_id: string | null; pending_viewer_id: string | null };
  busy: boolean;
}
export interface BrowserCommand {
  id: string; runtime_id: string; tab_id: string | null; viewer_id: string;
  op: "takeover" | "release" | "heartbeat" | "tabs" | "navigate" | "command" | "viewport" | "input" | "edit" | "upload" | "copy" | "options";
  args: Record<string, unknown>; expires_at: number;
}
export function object(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
function fail(): never { throw new Error("invalid workspace command"); }
function str(v: unknown, max = 256): v is string { return typeof v === "string" && v.length > 0 && v.length <= max; }
function keys(v: Record<string, unknown>, allowed: string[]): void {
  if (Object.keys(v).some(k => !allowed.includes(k))) fail();
}
function integer(v: unknown, min: number, max: number): boolean {
  return typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
}
export function httpUrl(v: unknown): boolean {
  if (!str(v, 8192)) return false;
  try { return ["http:", "https:"].includes(new URL(v).protocol); } catch { return false; }
}
export function validateInputCommand(method: unknown, params: unknown): void {
  if (!object(params)) fail();
  if (method === "Input.insertText") {
    keys(params, ["text"]);
    if (typeof params.text !== "string" || params.text.length > 16384) fail();
  } else if (method === "Input.dispatchMouseEvent") {
    keys(params, ["type", "x", "y", "button", "buttons", "clickCount", "modifiers", "deltaX", "deltaY"]);
    if (!["mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"].includes(String(params.type))) fail();
    for (const k of ["x", "y"]) if (typeof params[k] !== "number" || !Number.isFinite(params[k]) || Math.abs(params[k] as number) > 100000) fail();
    if (params.button !== undefined && !["none", "left", "right", "middle", "back", "forward"].includes(String(params.button))) fail();
    for (const k of ["deltaX", "deltaY"]) if (params[k] !== undefined && (typeof params[k] !== "number" || !Number.isFinite(params[k]) || Math.abs(params[k] as number) > 100000)) fail();
    for (const [k, max] of [["buttons", 31], ["clickCount", 3]] as const) if (params[k] !== undefined && !integer(params[k], 0, max)) fail();
  } else if (method === "Input.dispatchKeyEvent") {
    keys(params, ["type", "key", "code", "text", "unmodifiedText", "windowsVirtualKeyCode", "nativeVirtualKeyCode", "modifiers", "autoRepeat", "isKeypad", "location"]);
    if (!["keyDown", "keyUp", "rawKeyDown", "char"].includes(String(params.type))) fail();
    for (const k of ["key", "code", "text", "unmodifiedText"]) if (params[k] !== undefined && (typeof params[k] !== "string" || (params[k] as string).length > 256)) fail();
    for (const k of ["windowsVirtualKeyCode", "nativeVirtualKeyCode"]) if (params[k] !== undefined && !integer(params[k], 0, 65535)) fail();
    if (params.location !== undefined && !integer(params.location, 0, 3)) fail();
    for (const k of ["autoRepeat", "isKeypad"]) if (params[k] !== undefined && typeof params[k] !== "boolean") fail();
  } else fail();
  if (params.modifiers !== undefined && !integer(params.modifiers, 0, 15)) fail();
}
export function validateCommand(raw: unknown, runtimeId: string, now = Date.now()): BrowserCommand {
  if (!object(raw)) fail();
  keys(raw, ["id", "runtime_id", "tab_id", "viewer_id", "op", "args", "expires_at"]);
  if (!str(raw.id) || !str(raw.viewer_id) || raw.runtime_id !== runtimeId || !(raw.tab_id === null || str(raw.tab_id))) fail();
  if (typeof raw.expires_at !== "number" || !Number.isFinite(raw.expires_at) || raw.expires_at <= now || raw.expires_at > now + 120000) throw new Error("expired or invalid workspace command");
  if (!object(raw.args)) fail();
  const a = raw.args;
  switch (raw.op) {
    case "takeover": case "release": case "heartbeat": case "copy": keys(a, []); break;
    case "tabs":
      keys(a, ["action", "tab_id", "url"]);
      if (!["create", "select", "close"].includes(String(a.action))) fail();
      if (a.action !== "create" && !str(a.tab_id)) fail();
      if (a.tab_id !== undefined && !str(a.tab_id)) fail();
      if (a.url !== undefined && (a.action !== "create" || !httpUrl(a.url))) fail();
      break;
    case "navigate": keys(a, ["url"]); if (!httpUrl(a.url)) fail(); break;
    case "command": keys(a, ["command"]); if (!["back", "forward", "reload", "stop"].includes(String(a.command))) fail(); break;
    case "viewport":
      keys(a, ["mode", "width", "height"]);
      if (!["fit", "fixed"].includes(String(a.mode)) || !integer(a.width, 240, 3840) || !integer(a.height, 240, 2160)) fail();
      break;
    case "upload":
      keys(a, ["field", "paths"]);
      if (!str(a.field, 128) || !Array.isArray(a.paths) || !a.paths.length || a.paths.length > 10 || !a.paths.every(p => str(p, 4096))) fail();
      break;
    case "edit":
      keys(a, ["field", "action", "value", "start", "end", "index"]);
      if (!str(a.field, 128) || !['focus', 'blur', 'replace', 'set'].includes(String(a.action))) fail();
      if (a.action === 'replace' && (a.index !== undefined || typeof a.value !== 'string' || a.value.length > 16384 || !integer(a.start, 0, a.value.length) || !integer(a.end, a.start as number, a.value.length))) fail();
      // A picker choice: a select option by index, or a date/time value string.
      if (a.action === 'set') {
        if (a.start !== undefined || a.end !== undefined) fail();
        if (a.index !== undefined ? a.value !== undefined || !integer(a.index, 0, 9999) : typeof a.value !== 'string' || a.value.length > 64) fail();
      }
      if (a.action !== 'set' && a.action !== 'replace' && a.index !== undefined) fail();
      break;
    case "options": keys(a, ["field"]); if (!str(a.field, 128)) fail(); break;
    case "input": keys(a, ["method", "params"]); validateInputCommand(a.method, a.params); break;
    default: fail();
  }
  return raw as unknown as BrowserCommand;
}
