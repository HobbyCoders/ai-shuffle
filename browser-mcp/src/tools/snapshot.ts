/**
 * browser_snapshot — YAML aria tree. filter interactive|all, scope_ref to a
 * subtree, max_chars budget with self-describing truncation. Repeated siblings
 * collapse automatically inside the snapshot engine.
 */

import type { Session } from "../session.js";
import { jsonResult, type McpToolResult } from "./result.js";

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
  return jsonResult(
    {
      ok: true,
      snapshot_id: snap.snapshot_id,
      ref_count: snap.refs.length,
      truncated: snap.truncated,
    },
    snap.text,
  );
}
