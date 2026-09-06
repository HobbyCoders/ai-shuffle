/**
 * browser_navigate — url or "back"/"forward". Doubles as session bootstrap: the
 * first call acquires the page. Returns {url, title} plus an interactive-filtered
 * snapshot so the model has an immediate map of the landing page.
 */

import type { Session } from "../session.js";
import { contentWarning, jsonResult, type McpToolResult } from "./result.js";

/** Extract the host from a URL string, or "" if it has none / is unparseable. */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

/** Extract the origin from a URL string, or "" if it is unparseable. */
function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

/**
 * A cross-origin landing is the shape of an auth wall: the browser carries no
 * session cookies for the operator's own apps, so an app URL that answers with
 * a redirect to an identity provider is a sign-in demand, not a page. It still
 * reports ok:true (the navigation did happen), so the flag is what makes it
 * visible without the model having to diff hostnames itself.
 */
export const AUTH_WALL_NOTE =
  "landed on a different origin than requested — usually a sign-in redirect. " +
  "This browser holds no session cookies for your apps, so it cannot log itself " +
  "in; ask the user to sign in through the live browser panel, or use an " +
  "endpoint that does not need a session.";

export function normalizeUrl(input: string): string {
  const trimmed = input.trim();
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed)) return trimmed;
  if (trimmed.startsWith("about:") || trimmed.startsWith("data:")) return trimmed;
  // Bare domain / path → assume https.
  return `https://${trimmed}`;
}

export async function runNavigate(
  session: Session,
  input: { url: string },
): Promise<McpToolResult> {
  const page = await session.ensurePage();
  const target = input.url.trim().toLowerCase();

  // A committed navigation invalidates all refs.
  session.notifyNavigation();

  try {
    if (target === "back") {
      await page.goBack({ waitUntil: "domcontentloaded" });
    } else if (target === "forward") {
      await page.goForward({ waitUntil: "domcontentloaded" });
    } else {
      await page.goto(normalizeUrl(input.url), { waitUntil: "domcontentloaded" });
    }
  } catch (err) {
    // The SSRF guard aborts blocked requests with `blockedbyclient`, which
    // Chromium surfaces as an opaque `net::ERR_BLOCKED_BY_CLIENT`. Swap in the
    // guard's terse reason so a blocked navigate reads clearly.
    const msg = err instanceof Error ? err.message : String(err);
    if (/ERR_BLOCKED_BY_CLIENT|ERR_ABORTED|ERR_FAILED/.test(msg) && target !== "back" && target !== "forward") {
      const host = hostOf(normalizeUrl(input.url));
      const reason = host ? session.ssrfBlockReason(host) : undefined;
      if (reason) throw new Error(`blocked by SSRF policy: ${reason}`);
    }
    throw err;
  }

  const snap = await session.snapshot({ filter: "interactive" });
  // Emptiness is judged from the page, never from this interactive-filtered
  // snapshot: a login form, a canvas fixture and a prose page all have few or
  // no refs while being perfectly rendered.
  const content = await session.pageContent();
  const warning = contentWarning(content, snap.refs.length);
  const landed = page.url();
  const requestedOrigin =
    target === "back" || target === "forward" ? "" : originOf(normalizeUrl(input.url));
  const landedOrigin = originOf(landed);
  const redirected =
    requestedOrigin !== "" && landedOrigin !== "" && requestedOrigin !== landedOrigin;
  const notes = [...(redirected ? [AUTH_WALL_NOTE] : []), ...(warning ? [warning] : [])];
  return jsonResult(
    {
      ok: true,
      url: landed,
      title: content.title,
      snapshot_id: snap.snapshot_id,
      ref_count: snap.refs.length,
      truncated: snap.truncated,
      ...(redirected ? { redirected_to_origin: landedOrigin } : {}),
      ...(warning ? { warning } : {}),
    },
    notes.length ? `${snap.text}\n[warning] ${notes.join(" ")}`.trim() : snap.text,
  );
}
