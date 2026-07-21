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

export function errorResult(error: string, extra: Record<string, unknown> = {}): McpToolResult {
  return {
    content: [text(JSON.stringify({ ok: false, error, ...extra }))],
    isError: true,
  };
}
