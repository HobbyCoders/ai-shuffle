/**
 * browser_snapshot — YAML aria tree. filter interactive|all, scope_ref to a
 * subtree, max_chars budget with self-describing truncation. Repeated siblings
 * collapse automatically inside the snapshot engine.
 */

import type { Session } from "../session.js";
import { contentWarning, jsonResult, type McpToolResult } from "./result.js";

export async function runSnapshot(
  session: Session,
  input: { filter?: "interactive" | "all"; scope_ref?: string; max_chars?: number },
): Promise<McpToolResult> {
  await session.ensurePage();
  const snap = await session.snapshot({
    filter: input.filter,
    scopeRef: input.scope_ref,
    maxChars: input.max_chars,
  });
  // Add content context for zero refs without inferring app readiness.
  const warning =
    snap.refs.length === 0 ? contentWarning(await session.pageContent(), 0) : undefined;
  return jsonResult(
    {
      ok: true,
      snapshot_id: snap.snapshot_id,
      tab_id: session.currentTabId() ?? null,
      url: session.currentUrl().slice(0, 300),
      ref_count: snap.refs.length,
      truncated: snap.truncated,
      ...(warning ? { warning } : {}),
    },
    warning ? `${snap.text}\n[warning] ${warning}`.trim() : snap.text,
  );
}
