/**
 * Shared helpers for shaping MCP tool results. Tool text payloads are compact
 * JSON so downstream models get a stable, terse contract.
 */

export interface McpTextContent {
  type: "text";
  text: string;
  [key: string]: unknown;
}

export interface McpImageContent {
  type: "image";
  data: string;
  mimeType: string;
  [key: string]: unknown;
}

export type McpContent = McpTextContent | McpImageContent;

export interface McpToolResult {
  content: McpContent[];
  isError?: boolean;
  [key: string]: unknown;
}

export function text(value: string): McpTextContent {
  return { type: "text", text: value };
}

/** JSON body plus an optional trailing human/agent-readable block (e.g. a snapshot). */
export function jsonResult(body: Record<string, unknown>, trailing?: string): McpToolResult {
  const parts: McpContent[] = [text(JSON.stringify(body))];
  if (trailing !== undefined && trailing !== "") parts.push(text(trailing));
  return { content: parts };
}

/**
 * Cap a text payload to `maxChars`, cutting at a UTF-16 boundary. Returns the
 * (possibly shortened) text plus the original length so callers can attach a
 * self-describing truncation note in their own vocabulary.
 */
export function capText(
  value: string,
  maxChars: number,
): { text: string; truncated: boolean; fullChars: number } {
  const fullChars = value.length;
  if (fullChars <= maxChars) return { text: value, truncated: false, fullChars };
  return { text: value.slice(0, maxChars), truncated: true, fullChars };
}

/**
 * A page that rendered nothing looks exactly like a successful read of an empty
 * page: ok:true, ref_count:0, no text. Models have shipped broken UIs off that
 * silence, so an empty result says out loud that it is suspicious. Returns the
 * warning to attach, or undefined when the page clearly rendered something.
 *
 * `renderedText` is whatever content the tool is about to return (extracted
 * prose). Below this many non-whitespace characters a page has effectively
 * painted nothing — a bare <body>, an app that failed to mount, a fetch that
 * errored before first paint.
 */
export const MEANINGFUL_CONTENT_CHARS = 40;

export function emptyContentWarning(renderedText: string): string | undefined {
  if (renderedText.replace(/\s+/g, "").length < MEANINGFUL_CONTENT_CHARS) {
    return BLANK_PAGE_WARNING;
  }
  return undefined;
}

export const BLANK_PAGE_WARNING =
  "page rendered no content — likely a blank/errored app; check browser_console " +
  "and browser_network, and confirm the page finished loading, before treating " +
  "this as success";

export const NO_REFS_NOTE =
  "no interactive elements found (ref_count 0) — the page has content but nothing " +
  "the snapshot can act on (a canvas, an image, plain prose). Use browser_read_text " +
  "or browser_screenshot to read it, or browser_act with coordinates.";

/** What the page says about itself; `textChars` < 0 means we could not measure. */
export interface PageContent {
  title: string;
  textChars: number;
}

/**
 * Judge emptiness from the PAGE, never from a filtered snapshot.
 *
 * The old heuristic measured the interactive-filtered snapshot, so any page
 * whose content is not clickable — a canvas fixture, a rendered login form, a
 * prose page — was announced as "rendered no content" while it was plainly
 * fine. Four sessions in one day chased that warning. A page with a title or a
 * body is not blank; a ref_count of 0 is a routing hint, not an alarm.
 */
export function contentWarning(
  page: PageContent,
  refCount?: number,
): string | undefined {
  const measured = page.textChars >= 0;
  const blank = page.title.trim() === "" && page.textChars < MEANINGFUL_CONTENT_CHARS;
  if (measured && blank) return BLANK_PAGE_WARNING;
  if (refCount === 0) return NO_REFS_NOTE;
  return undefined;
}

export function errorResult(error: string, extra: Record<string, unknown> = {}): McpToolResult {
  return {
    content: [text(JSON.stringify({ ok: false, error, ...extra }))],
    isError: true,
  };
}
