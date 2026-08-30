/**
 * browser_navigate — url or "back"/"forward". Doubles as session bootstrap: the
 * first call acquires the page. Returns {url, title} plus an interactive-filtered
 * snapshot so the model has an immediate map of the landing page.
 */

import type { Session } from "../session.js";
import { emptyContentWarning, jsonResult, type McpToolResult } from "./result.js";

/** Extract the host from a URL string, or "" if it has none / is unparseable. */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

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
  // A navigation that "succeeds" onto a page that painted nothing reads as
  // success unless we say otherwise.
  const warning = emptyContentWarning(snap.text, snap.refs.length);
  return jsonResult(
    {
      ok: true,
      url: page.url(),
      title: await page.title(),
      snapshot_id: snap.snapshot_id,
      ref_count: snap.refs.length,
      truncated: snap.truncated,
      ...(warning ? { warning } : {}),
    },
    warning ? `${snap.text}\n[warning] ${warning}`.trim() : snap.text,
  );
}
