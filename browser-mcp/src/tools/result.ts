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
 * `renderedText` is whatever content the tool is about to return (snapshot YAML,
 * extracted prose). Below this many non-whitespace characters a page has
 * effectively painted nothing — a bare <body>, an app that failed to mount, a
 * fetch that errored before first paint. `refCount` is checked only when the
 * caller has one (snapshot-shaped tools); text-only readers omit it.
 */
const MEANINGFUL_CONTENT_CHARS = 40;

export function emptyContentWarning(
  renderedText: string,
  refCount?: number,
): string | undefined {
  const solid = renderedText.replace(/\s+/g, "").length;
  if (solid < MEANINGFUL_CONTENT_CHARS) {
    return (
      "page rendered no content — likely a blank/errored app; check browser_console " +
      "and browser_network, and confirm the page finished loading, before treating " +
      "this as success"
    );
  }
  if (refCount === 0) {
    return (
      "no interactive elements found (ref_count 0) — the page has text but nothing " +
      "clickable; if you expected a UI here it may have failed to mount, so check " +
      "browser_console before treating this as success"
    );
  }
  return undefined;
}

export function errorResult(error: string, extra: Record<string, unknown> = {}): McpToolResult {
  return {
    content: [text(JSON.stringify({ ok: false, error, ...extra }))],
    isError: true,
  };
}
