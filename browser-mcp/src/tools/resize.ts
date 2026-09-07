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

/**
 * Requested and applied sizes may differ: the live panel drives the same page
 * and a viewer holding control can resize it, and Chrome clamps sizes it cannot
 * honour. Reporting the request as if it were the result sent one session
 * measuring a 412px layout in a 369px viewport and debugging its own
 * assertions, so the answer is always read back from the page.
 */
export async function runResize(session: Session, input: ResizeInput): Promise<McpToolResult> {
  const dims = resolveDims(input);
  if (typeof dims === "string") return errorResult(dims);

  await session.ensurePage();
  const applied = await session.setViewport(dims.width, dims.height, true);
  const matches = applied.width === dims.width && applied.height === dims.height;
  return jsonResult({
    ok: true,
    width: applied.width,
    height: applied.height,
    device_scale_factor: applied.deviceScaleFactor,
    requested: { width: dims.width, height: dims.height },
    ...(matches ? {} : {
      warning:
        `viewport is ${applied.width}x${applied.height}, not the ${dims.width}x${dims.height} ` +
        "requested — a viewer holding control of the shared browser, a scrollbar, or a " +
        "Chrome clamp changed it. Measure against the applied size.",
    }),
  });
}
