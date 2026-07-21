/**
 * browser_navigate — url or "back"/"forward". Doubles as session bootstrap: the
 * first call acquires the page. Returns {url, title} plus an interactive-filtered
 * snapshot so the model has an immediate map of the landing page.
 */

import type { Session } from "../session.js";
import { jsonResult, type McpToolResult } from "./result.js";

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

  if (target === "back") {
    await page.goBack({ waitUntil: "domcontentloaded" });
  } else if (target === "forward") {
    await page.goForward({ waitUntil: "domcontentloaded" });
  } else {
    await page.goto(normalizeUrl(input.url), { waitUntil: "domcontentloaded" });
  }

  const snap = await session.snapshot({ filter: "interactive" });
  return jsonResult(
    {
      ok: true,
      url: page.url(),
      title: await page.title(),
      snapshot_id: snap.snapshot_id,
      truncated: snap.truncated,
    },
    snap.text,
  );
}
