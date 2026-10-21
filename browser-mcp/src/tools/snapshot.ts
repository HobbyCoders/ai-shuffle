/**
 * browser_snapshot — YAML aria tree. filter interactive|all, scope_ref to a
 * subtree, max_chars budget with self-describing truncation. Repeated siblings
 * collapse automatically inside the snapshot engine.
 */

import type { Session } from "../session.js";
import { contentWarning, jsonResult, type McpToolResult } from "./result.js";
import { CAPTCHA_AGENT_NOTE } from "../captcha.js";

export async function runSnapshot(
  session: Session,
  input: { filter?: "interactive" | "all"; scope_ref?: string; max_chars?: number },
): Promise<McpToolResult> {
  await session.ensurePage();
  const snap = await session.snapshot({
    filter: input.filter,
    scopeRef: input.scope_ref,
    maxChars: input.max_chars,
    // An expansion must not repeat the collapse that prompted it.
    ...(input.scope_ref ? { collapseThreshold: Infinity } : {}),
  });
  // Count only emitted refs; the registry also retains collapsed/truncated refs.
  const refCount = new Set(snap.text.match(/(?<=\[ref=)(?:f\d+)?e\d+(?=\])/g) ?? []).size;
  const empty = refCount === 0 ? contentWarning(await session.pageContent(), 0) : undefined;
  const captcha = (await session.checkCaptcha()) === "withheld";
  const warning = [empty, captcha ? CAPTCHA_AGENT_NOTE : undefined].filter(Boolean).join(" ") || undefined;
  return jsonResult(
    {
      ok: true,
      snapshot_id: snap.snapshot_id,
      tab_id: session.currentTabId() ?? null,
      url: session.currentUrl().slice(0, 300),
      ref_count: refCount,
      truncated: snap.truncated,
      ...(captcha ? { captcha_unavailable: true } : {}),
      ...(warning ? { warning } : {}),
    },
    warning ? `${snap.text}\n[warning] ${warning}`.trim() : snap.text,
  );
}
