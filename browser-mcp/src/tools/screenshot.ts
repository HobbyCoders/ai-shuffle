/**
 * browser_screenshot — viewport by default; `region` [x,y,w,h] crop (in page /
 * document pixels); `full_page` opt-in. Encoding is done natively by Playwright
 * (`page.screenshot({ type:"jpeg", quality, scale:"css" })`) — we no longer hand
 * a PNG to page-controlled canvas JS to re-encode. Native CSS-pixel captures are
 * emitted 1:1, so the returned pixel dimensions are exactly the coordinate space
 * browser_act must use. If the encoded image would be very large (a big
 * full_page or region on a tall page) we return a text note instead of blowing
 * the token budget on an oversized image.
 */

import type { Session } from "../session.js";
import { text, errorResult, type McpToolResult } from "./result.js";

/** Longest edge (CSS px) beyond which a native capture is considered oversized. */
export const MAX_EDGE = 1280;
export const JPEG_QUALITY = 80;
/**
 * Hard cap on emitted base64 length (~2.5 MB). Native JPEG can't downscale to a
 * target size, so instead of shipping a huge image we bail with a text note
 * telling the model to capture a smaller region/viewport.
 */
export const MAX_BASE64_LEN = 2_621_440;

export interface ScreenshotInput {
  region?: number[];
  full_page?: boolean;
}

/**
 * Longest-edge-<=max downscale factor (<= 1). Retained for callers/tests that
 * reason about oversize captures; the native path emits at CSS scale (factor 1).
 */
export function scaleFor(width: number, height: number, maxEdge = MAX_EDGE): number {
  const longest = Math.max(width, height);
  if (longest <= maxEdge) return 1;
  return maxEdge / longest;
}

interface Clip {
  x: number;
  y: number;
  width: number;
  height: number;
}

export async function runScreenshot(session: Session, input: ScreenshotInput): Promise<McpToolResult> {
  // ---- validation (m6) -----------------------------------------------------
  if (input.region !== undefined) {
    if (input.region.length !== 4) {
      return errorResult("`region` must be [x, y, width, height]");
    }
    if (input.full_page) {
      return errorResult("provide either `region` or `full_page`, not both");
    }
    const [, , rw, rh] = input.region;
    if (!(rw > 0) || !(rh > 0)) {
      return errorResult("`region` width and height must be positive");
    }
  }

  const page = await session.ensurePage();

  const pageDims = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    scrollH: document.documentElement.scrollHeight,
    innerW: window.innerWidth,
    innerH: window.innerHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
  }));

  let clip: Clip | undefined;
  let fullPage = false;
  let offsetX = 0;
  let offsetY = 0;
  let width: number;
  let height: number;

  if (input.region) {
    // Region coordinates are DOCUMENT (page) pixels. Clamp to page bounds and
    // capture with fullPage:true so the clip is document-relative (a non-full
    // clip is viewport-relative and would be wrong on a scrolled page).
    let [rx, ry, rw, rh] = input.region as [number, number, number, number];
    rx = clamp(rx, 0, pageDims.scrollW);
    ry = clamp(ry, 0, pageDims.scrollH);
    rw = Math.min(rw, pageDims.scrollW - rx);
    rh = Math.min(rh, pageDims.scrollH - ry);
    if (!(rw > 0) || !(rh > 0)) {
      return errorResult("`region` is outside the page bounds");
    }
    clip = { x: rx, y: ry, width: rw, height: rh };
    fullPage = true;
    offsetX = rx;
    offsetY = ry;
    width = Math.round(rw);
    height = Math.round(rh);
  } else if (input.full_page) {
    fullPage = true;
    offsetX = 0;
    offsetY = 0;
    width = pageDims.scrollW;
    height = pageDims.scrollH;
  } else {
    // Viewport: document-space origin is the current scroll position.
    offsetX = pageDims.scrollX;
    offsetY = pageDims.scrollY;
    width = pageDims.innerW;
    height = pageDims.innerH;
  }

  const buffer = await page.screenshot({
    type: "jpeg",
    quality: JPEG_QUALITY,
    scale: "css",
    fullPage,
    clip,
  });
  const data = buffer.toString("base64");

  // Store the document-space mapping (scale 1: native CSS pixels are 1:1).
  session.setLastScreenshot({ scale: 1, offsetX, offsetY });

  if (data.length > MAX_BASE64_LEN) {
    return {
      content: [
        text(
          `screenshot too large to return (${width}x${height}, ~${Math.round(data.length / 1024)}KB encoded). ` +
            `Capture a smaller region or a viewport screenshot instead.`,
        ),
      ],
    };
  }

  return {
    content: [
      { type: "image", data, mimeType: "image/jpeg" },
      text(`${width}x${height} — coordinates for browser_act must use this space`),
    ],
  };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(v, hi));
}
