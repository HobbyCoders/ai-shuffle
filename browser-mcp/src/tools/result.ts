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

export function errorResult(error: string, extra: Record<string, unknown> = {}): McpToolResult {
  return {
    content: [text(JSON.stringify({ ok: false, error, ...extra }))],
    isError: true,
  };
}
