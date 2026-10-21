/** Deterministic live locators first; semantic search only for descriptions. */
import type { Page, Locator } from "playwright-core";
import type { Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";
import { findElements, entriesFromSnapshot, scoreDegraded, isDescriptiveQuery, type FindResult } from "../find.js";

export interface FindInput {
  query?: string;
  role?: string;
  name?: string;
  label?: string;
  placeholder?: string;
  test_id?: string;
  text?: string;
}

/** Exact lookup: never choose the first of several equally valid controls. */
export function findLocators(page: Page, input: FindInput): Locator[] {
  if (input.role) return [page.getByRole(input.role as Parameters<Page["getByRole"]>[0], { name: input.name, exact: true })];
  if (input.label) return [page.getByLabel(input.label, { exact: true })];
  if (input.placeholder) return [page.getByPlaceholder(input.placeholder, { exact: true })];
  if (input.test_id) return [page.getByTestId(input.test_id)];
  if (input.text) return [page.getByText(input.text, { exact: true })];
  const q = input.query!;
  return [page.getByRole("button", { name: q, exact: true }), page.getByRole("link", { name: q, exact: true }),
    page.getByLabel(q, { exact: true }), page.getByPlaceholder(q, { exact: true }), page.getByTestId(q), page.getByText(q, { exact: true })];
}

export async function runFind(session: Session, input: FindInput): Promise<McpToolResult> {
  const structured = [input.role, input.label, input.placeholder, input.test_id, input.text].filter(v => v !== undefined);
  if (Object.values(input).some(value => typeof value === "string" && !value.trim()) ||
      structured.length > 1 || (!structured.length && !input.query?.trim()) || (input.name !== undefined && !input.role)) {
    return errorResult("invalid_input: provide query or one of role (with optional name), label, placeholder, test_id, text");
  }
  const page = await session.ensurePage();
  let matchedRefs: string[] | undefined;
  for (const locator of findLocators(page, input)) {
    const visible = locator.filter({ visible: true });
    const count = await visible.count();
    if (count > 1) return jsonResult({ ok: false, code: "ambiguous_match", match_count: count,
      error: "More than one element matches exactly; use role+name, label, placeholder or test_id to disambiguate." });
    if (count === 1) {
      matchedRefs = entriesFromSnapshot(await visible.ariaSnapshot({ mode: "ai", timeout: 5000 })).map(e => e.ref);
      break;
    }
  }
  // No output budget/collapse here: both deterministic refs and fallback search
  // must reach controls near the end of very large pages. This also restores
  // Playwright's full ref map after a locator-scoped ariaSnapshot.
  const snap = await session.snapshot({ filter: "all", maxChars: Infinity, collapseThreshold: Infinity });
  const entries = entriesFromSnapshot(snap.text);
  const query = input.query ?? input.name ?? input.label ?? input.placeholder ?? input.test_id ?? input.text ?? input.role!;
  let result: FindResult;
  if (matchedRefs) {
    result = { matches: entries.filter(e => matchedRefs.includes(e.ref)).slice(0, 20), degraded: false };
  } else if (!structured.length && isDescriptiveQuery(query)) {
    result = await findElements({ query, snapshotText: snap.text, entries, env: {
      ANTHROPIC_BASE_URL: process.env.ANTHROPIC_BASE_URL,
      ANTHROPIC_AUTH_TOKEN: process.env.ANTHROPIC_AUTH_TOKEN,
      ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
      ANTHROPIC_SMALL_FAST_MODEL: process.env.ANTHROPIC_SMALL_FAST_MODEL,
    }, fetchImpl: fetch });
  } else {
    result = { matches: structured.length ? [] : scoreDegraded(query, entries), degraded: false };
  }
  return jsonResult({ ok: true, snapshot_id: snap.snapshot_id, match_count: result.matches.length,
    degraded: result.degraded, method: matchedRefs ? "locator" : structured.length ? "locator" : "search" },
    formatMatches(query, snap.snapshot_id, result));
}

function formatMatches(query: string, snapshotId: number, result: FindResult): string {
  const lines = [`${result.matches.length} matches for "${query}" (snapshot_id=${snapshotId})`];
  if (result.degraded) lines.push(`semantic search unavailable (${result.reason ?? "unknown"}); showing text matches`);
  for (const m of result.matches) lines.push(`${m.ref} ${m.role} "${m.name}"${m.reason ? ` — ${m.reason}` : ""}`);
  return lines.join("\n");
}
