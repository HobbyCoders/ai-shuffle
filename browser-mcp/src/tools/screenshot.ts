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
/** Wall clock for one capture. Playwright's own default is 30s. */
export const SCREENSHOT_TIMEOUT_MS = 10_000;

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

  // Chrome paints only the foreground tab of a window, so capturing a
  // backgrounded one blocks until the deadline below and then fails. Pull the
  // tab forward first; the live panel's screencast wants the same thing.
  await session.bringToFront();

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
  const kind: "viewport" | "region" | "full" = input.region ? "region" : input.full_page ? "full" : "viewport";
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

  let buffer: Buffer;
  try {
    buffer = await page.screenshot({
      timeout: SCREENSHOT_TIMEOUT_MS,
      type: "jpeg",
      quality: JPEG_QUALITY,
      scale: "css",
      fullPage,
      clip,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (/timeout/i.test(message)) {
      return errorResult(
        `screenshot timed out after ${SCREENSHOT_TIMEOUT_MS}ms — the page never painted a ` +
          "frame (fonts still loading, a busy renderer, or a tab Chrome will not bring " +
          "forward). browser_snapshot and browser_read_text do not need a paint.",
      );
    }
    throw err;
  }
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
      const id = session.setLastScreenshot({ scale: factor, offsetX, offsetY, kind, viewportW: pageDims.innerW, viewportH: pageDims.innerH });
      return {
        content: [
          { type: "image", data: shrunk, mimeType: "image/jpeg" },
          text(describeCapture(session, id, Math.round(width * factor), Math.round(height * factor), factor, kind, offsetX, offsetY)),
        ],
      };
    } catch {
      // fall through: emit the native-size capture below
    }
  }

  if (data.length > MAX_BASE64_LEN) {
    return errorResult(
      `screenshot_too_large: the capture is ${width}x${height} (~${Math.round(data.length / 1024)}KB encoded). ` +
        "Capture a smaller region or the viewport instead.",
    );
  }

  const id = session.setLastScreenshot({ scale: 1, offsetX, offsetY, kind, viewportW: pageDims.innerW, viewportH: pageDims.innerH });
  return {
    content: [
      { type: "image", data, mimeType: "image/jpeg" },
      text(describeCapture(session, id, width, height, 1, kind, offsetX, offsetY)),
    ],
  };
}

/**
 * The observation record that travels with the image: which capture this is
 * (pass screenshot_id to browser_act to bind coordinates to it), where it was
 * taken, and the size of the pixel space coordinates are read in. `scale` is
 * image pixels per CSS pixel and `origin` the page position of the image's
 * top-left corner; the server applies both, the model never has to.
 */
export function describeCapture(
  session: Session,
  id: string,
  width: number,
  height: number,
  scale: number,
  kind: "viewport" | "region" | "full",
  offsetX: number,
  offsetY: number,
): string {
  let url = "";
  try { url = session.currentUrl().slice(0, 300); } catch { /* page went away */ }
  return JSON.stringify({
    screenshot_id: id,
    tab_id: session.currentTabId() ?? null,
    url,
    width,
    height,
    kind,
    scale: Math.round(scale * 1000) / 1000,
    origin: [Math.round(offsetX), Math.round(offsetY)],
    captured_at: new Date().toISOString(),
    note: "browser_act coordinates use this image's pixels",
  });
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(v, hi));
}
