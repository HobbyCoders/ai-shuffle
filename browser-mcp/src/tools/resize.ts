/**
 * browser_resize — set the viewport size. Either a named `preset`
 * (mobile/tablet/desktop) OR an explicit width+height (which overrides any
 * preset). Exactly one of those two forms must be supplied; the check is done
 * server-side since the flat schema cannot express "one of".
 */

import type { Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export const RESIZE_PRESET_DIMS: Record<string, { width: number; height: number }> = {
  mobile: { width: 375, height: 812 },
  tablet: { width: 768, height: 1024 },
  desktop: { width: 1280, height: 800 },
};

export interface ResizeInput {
  preset?: "mobile" | "tablet" | "desktop";
  width?: number;
  height?: number;
}

/**
 * Resolve the target dimensions, or an error string. Explicit width+height wins
 * over a preset; otherwise a preset is required; width/height must come as a pair.
 */
export function resolveDims(input: ResizeInput): { width: number; height: number } | string {
  const hasW = input.width !== undefined;
  const hasH = input.height !== undefined;
  if (hasW !== hasH) return "provide both `width` and `height`, or neither";
  if (hasW && hasH) return { width: input.width!, height: input.height! };
  if (input.preset) {
    const dims = RESIZE_PRESET_DIMS[input.preset];
    if (!dims) return `unknown preset: ${input.preset}`;
    return dims;
  }
  return "provide a `preset` or explicit `width` + `height`";
}

export async function runResize(session: Session, input: ResizeInput): Promise<McpToolResult> {
  const dims = resolveDims(input);
  if (typeof dims === "string") return errorResult(dims);

  await session.ensurePage();
  await session.setViewport(dims.width, dims.height);
  return jsonResult({ ok: true, width: dims.width, height: dims.height });
}
