/**
 * browser_screenshot — viewport by default; `region` [x,y,w,h] crop (in page /
 * document pixels); `full_page` opt-in. Encoding is done natively by Playwright
 * (`page.screenshot({ type:"jpeg", quality, scale:"css", clip })`). Captures
 * whose longest edge exceeds MAX_EDGE are downscaled to it before being sent to
 * the model: the JPEG buffer is re-encoded in-page via createImageBitmap + a
 * canvas at the capped size (the stored scale mapping keeps coordinate-based
 * browser_act correct). Providers bill images by pixels, so oversized originals
 * cost tokens without adding model-visible detail.
 */

import { raceWithTimeout, type Session } from "../session.js";
import { text, errorResult, type McpToolResult } from "./result.js";

/** Longest edge (CSS px) beyond which a capture is downscaled before encoding. */
export const MAX_EDGE = 1280;
export const JPEG_QUALITY = 80;
/**
 * Hard cap on emitted base64 length (~2.5 MB). Backstop for pages where the
 * in-page re-encode fails AND the capture is huge.
 */
export const MAX_BASE64_LEN = 2_621_440;

export interface ScreenshotInput {
  region?: number[];
  full_page?: boolean;
}

/**
 * Longest-edge-<=max downscale factor (<= 1).
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

  session.invalidateScreenshot();
  const pageDims = await raceWithTimeout(page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    scrollH: document.documentElement.scrollHeight,
    innerW: window.innerWidth,
    innerH: window.innerHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
  })), 5000, "screenshot dimensions timed out");

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
    timeout: 10000,
    type: "jpeg",
    quality: JPEG_QUALITY,
    scale: "css",
    fullPage,
    clip,
  });
  let data = buffer.toString("base64");

  // Oversized capture: downscale to the vision budget. The re-encode runs
  // in-page (createImageBitmap decodes without tainting the canvas); if it
  // fails we ship the original — MAX_BASE64_LEN stays the hard backstop.
  const factor = scaleFor(width, height);
  if (factor < 1) {
    try {
      // NOTE: fully self-contained — page.evaluate serializes only this
      // function, so it cannot reference helpers from this module.
      const shrunk = await raceWithTimeout(page.evaluate(
        async ([jpegB64, tw, th, q]: [string, number, number, number]) => {
          const binary = atob(jpegB64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
          try {
            const canvas = document.createElement("canvas");
            canvas.width = tw;
            canvas.height = th;
            const ctx = canvas.getContext("2d");
            if (!ctx) throw new Error("no 2d context");
            ctx.fillStyle = "#fff";
            ctx.fillRect(0, 0, tw, th);
            ctx.imageSmoothingEnabled = true;
            ctx.drawImage(bitmap, 0, 0, tw, th);
            const dataUrl = canvas.toDataURL("image/jpeg", q);
            return dataUrl.slice(dataUrl.indexOf(",") + 1);
          } finally {
            bitmap.close();
          }
        },
        [
          data,
          Math.max(1, Math.round(width * factor)),
          Math.max(1, Math.round(height * factor)),
          JPEG_QUALITY,
        ] as [string, number, number, number],
      ), 5000, "screenshot encoding timed out");
      session.setLastScreenshot({ scale: factor, offsetX, offsetY });
      return {
        content: [
          { type: "image", data: shrunk, mimeType: "image/jpeg" },
          text(`${Math.round(width * factor)}x${Math.round(height * factor)} — coordinates for browser_act must use this space`),
        ],
      };
    } catch {
      // fall through: emit the native-size capture below
    }
  }

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

  session.setLastScreenshot({ scale: 1, offsetX, offsetY });
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
