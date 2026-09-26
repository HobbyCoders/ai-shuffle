/**
 * browser_network — two-phase network inspector over the current page's
 * response buffer (populated by a page.on('response') listener from page
 * acquisition).
 *   • Without request_id: LIST recorded requests (method, url, status,
 *     resourceType, id), filtered by url_pattern, newest last, default limit 20.
 *   • With request_id: return that response's BODY (text only, capped ~20k).
 *     Bodies are fetched lazily at ask-time; response.text() can fail after a
 *     navigation, which we surface as a clear error rather than throwing.
 * Non-text bodies are noted with mimeType/size instead of dumped.
 */

import type { Session } from "../session.js";
import { raceWithTimeout } from "../session.js";
import { jsonResult, errorResult, capText, type McpToolResult } from "./result.js";
import { compileMatcher } from "../match.js";

export const NETWORK_DEFAULT_LIMIT = 20;
export const NETWORK_BODY_MAX_CHARS = 20000;
/**
 * Hard cap on a response body we are willing to pull into memory. When the
 * response declares a `content-length` larger than this we return a summary
 * note instead of calling `.text()` (which would buffer the whole body first).
 * Bodies with no declared length still fall through to the read-then-cap path.
 */
export const NETWORK_BODY_MAX_BYTES = 5 * 1024 * 1024;
/**
 * Ceiling for reading a recorded body. `response.text()` waits for the body to
 * finish, which an event stream or a long poll never does; unbounded, one such
 * request id would hold the session's queue (and every later tool call) forever.
 */
export const NETWORK_BODY_TIMEOUT_MS = 5000;
const BODY_TIMEOUT_MESSAGE = "body read timed out";

/**
 * A body sent without a content-type is read as text; a NUL in its first
 * characters says it is binary (images, archives, wasm), which would reach the
 * model as mojibake instead of the non-text note typed bodies get.
 */
function looksBinary(raw: string): boolean {
  return raw.slice(0, 4096).includes("\u0000");
}

export interface NetworkInput {
  url_pattern?: string;
  request_id?: string;
  limit?: number;
  clear?: boolean;
}

/** content-types we return verbatim as text. Everything else gets a size note. */
function isTextualType(contentType: string): boolean {
  const ct = contentType.toLowerCase();
  return (
    ct.startsWith("text/") ||
    ct.includes("json") ||
    ct.includes("xml") ||
    ct.includes("javascript") ||
    ct.includes("ecmascript") ||
    ct.includes("x-www-form-urlencoded") ||
    ct.includes("csv") ||
    ct.includes("html")
  );
}

export async function runNetwork(session: Session, input: NetworkInput): Promise<McpToolResult> {
  await session.ensurePage();

  // ---- body phase ----------------------------------------------------------
  if (input.request_id !== undefined) {
    const entry = session.findNetworkEntry(input.request_id);
    if (!entry) {
      return errorResult(`no recorded request with id ${input.request_id}`);
    }
    const headers = entry.response.headers();
    const contentType = headers["content-type"] ?? "";
    const sizeHeader = headers["content-length"];
    const declaredSize =
      sizeHeader !== undefined && Number.isFinite(Number(sizeHeader)) ? Number(sizeHeader) : null;

    // Size precheck FIRST, and independent of content-type: an over-cap body is
    // never read, even when the type is unknown/missing (which would otherwise
    // slip past the non-text note below and get fully buffered by `.text()`).
    if (declaredSize !== null && declaredSize > NETWORK_BODY_MAX_BYTES) {
      return jsonResult({
        ok: true,
        id: entry.id,
        url: entry.url,
        note: `body too large: ${declaredSize} bytes, not fetched`,
        mimeType: contentType || null,
        size: declaredSize,
      });
    }

    if (contentType && !isTextualType(contentType)) {
      return jsonResult({
        ok: true,
        id: entry.id,
        url: entry.url,
        note: "non-text body not returned",
        mimeType: contentType,
        size: declaredSize,
      });
    }

    let raw: string;
    try {
      raw = await raceWithTimeout(entry.response.text(), NETWORK_BODY_TIMEOUT_MS, BODY_TIMEOUT_MESSAGE);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg === BODY_TIMEOUT_MESSAGE) {
        return errorResult(
          `response body not complete after ${NETWORK_BODY_TIMEOUT_MS / 1000}s — the response is ` +
            "still streaming (an event stream or long poll) or stalled; its body cannot be read here",
          { id: entry.id, url: entry.url, mimeType: contentType || null },
        );
      }
      return errorResult(`response body unavailable (${msg}) — it may have been evicted after navigation`);
    }

    if (!contentType && looksBinary(raw)) {
      return jsonResult({
        ok: true,
        id: entry.id,
        url: entry.url,
        note: "non-text body not returned",
        mimeType: null,
        size: declaredSize,
      });
    }

    const { text, truncated, fullChars } = capText(raw, NETWORK_BODY_MAX_CHARS);
    const body: Record<string, unknown> = {
      ok: true,
      id: entry.id,
      url: entry.url,
      status: entry.status,
      mimeType: contentType || null,
      chars: text.length,
      truncated,
    };
    if (truncated) body.full_chars = fullChars;
    return jsonResult(body, text);
  }

  // ---- list phase ----------------------------------------------------------
  if (input.clear) {
    const entries = listPhase(session, input);
    session.clearNetwork();
    return entries;
  }
  return listPhase(session, input);
}

function listPhase(session: Session, input: NetworkInput): McpToolResult {
  const limit = input.limit ?? NETWORK_DEFAULT_LIMIT;
  const matches = input.url_pattern ? compileMatcher(input.url_pattern) : () => true;

  const filtered = session.networkEntries().filter((e) => matches(e.url));
  const matched = filtered.length;
  const window = filtered.slice(Math.max(0, filtered.length - limit));

  return jsonResult({
    ok: true,
    matched,
    returned: window.length,
    requests: window.map((e) => ({
      id: e.id,
      method: e.method,
      url: e.url,
      status: e.status,
      resourceType: e.resourceType,
    })),
  });
}
