/**
 * browser_find — semantic element search that returns only a handful of refs,
 * keeping the full accessibility tree out of the calling agent's context.
 *
 * Takes a full interactive snapshot internally (never returned), asks a small
 * fast model for the matching refs, and prints one terse line per match anchored
 * to a snapshot_id. When no auth env is present, or the sub-call times out /
 * errors / returns garbage, it degrades to a deterministic local text scorer and
 * says so. The scoring + network client live in ../find.js; this file is the
 * thin session-facing wrapper.
 */

import type { Session } from "../session.js";
import { jsonResult, type McpToolResult } from "./result.js";
import { findElements, entriesFromSnapshot, type FindResult } from "../find.js";

export async function runFind(session: Session, input: { query: string }): Promise<McpToolResult> {
  await session.ensurePage();
  const snap = await session.snapshot({ filter: "interactive" });
  const entries = entriesFromSnapshot(snap.text);

  const result = await findElements({
    query: input.query,
    snapshotText: snap.text,
    entries,
    env: {
      ANTHROPIC_BASE_URL: process.env.ANTHROPIC_BASE_URL,
      ANTHROPIC_AUTH_TOKEN: process.env.ANTHROPIC_AUTH_TOKEN,
      ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
      ANTHROPIC_SMALL_FAST_MODEL: process.env.ANTHROPIC_SMALL_FAST_MODEL,
    },
    fetchImpl: fetch,
  });

  return jsonResult(
    {
      ok: true,
      snapshot_id: snap.snapshot_id,
      match_count: result.matches.length,
      degraded: result.degraded,
    },
    formatMatches(input.query, snap.snapshot_id, result),
  );
}

/** Terse rendering: a header line, an optional degradation note, then matches. */
function formatMatches(query: string, snapshotId: number, result: FindResult): string {
  const lines: string[] = [
    `${result.matches.length} matches for "${query}" (snapshot_id=${snapshotId})`,
  ];
  if (result.degraded) {
    lines.push(`semantic search unavailable (${result.reason ?? "unknown"}); showing text matches`);
  }
  for (const m of result.matches) {
    const base = `${m.ref} ${m.role} "${m.name}"`;
    lines.push(m.reason ? `${base} — ${m.reason}` : base);
  }
  return lines.join("\n");
}
