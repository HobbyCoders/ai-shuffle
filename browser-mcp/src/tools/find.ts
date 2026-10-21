/** Deterministic live locators first; semantic search only for descriptions. */
import { parseAriaYaml, renderSnapshot, DEFAULT_MAX_CHARS } from "../snapshot.js";
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
  index?: number;
}

const ROLES = new Set("alert alertdialog application article banner blockquote button caption cell checkbox code columnheader combobox complementary contentinfo definition deletion dialog directory document emphasis feed figure form generic grid gridcell group heading img insertion link list listbox listitem log main marquee math menu menubar menuitem menuitemcheckbox menuitemradio meter navigation none note option paragraph presentation progressbar radio radiogroup region row rowgroup rowheader scrollbar search searchbox separator slider spinbutton status strong subscript superscript switch tab table tablist tabpanel term textbox time timer toolbar tooltip tree treegrid treeitem".split(" "));

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
  if (input.role && !ROLES.has(input.role)) return errorResult("invalid_input: unknown ARIA role");
  if (input.index !== undefined && (!Number.isInteger(input.index) || input.index < 0)) return errorResult("invalid_input: index must be a nonnegative integer");
  const page = await session.ensurePage();
  let matchedRefs: string[] | undefined;
  let candidateCount = 0;
  for (const locator of findLocators(page, input)) {
    const visible = locator.filter({ visible: true });
    const count = await visible.count();
    if (count > 0) {
      candidateCount = count;
      if (input.index !== undefined && input.index >= count) return errorResult(`invalid_input: index exceeds ${count} matching elements`);
      matchedRefs = [];
      const indexes = input.index === undefined ? Array.from({ length: Math.min(20, count) }, (_, i) => i) : [input.index];
      for (const index of indexes) {
        // Only the selected container itself, never every descendant control.
        const root = parseAriaYaml(await visible.nth(index).ariaSnapshot({ mode: "ai", timeout: 5000 }))[0];
        if (root?.ref) matchedRefs.push(root.ref);
      }
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
  } else if (!structured.length && (isDescriptiveQuery(query) || scoreDegraded(query, entries).length === 0)) {
    // Keep local matching complete, but never send the full page's prose or
    // uncapped tree to the semantic provider.
    const semanticTree = renderSnapshot(snap.text, { filter: "interactive", maxChars: DEFAULT_MAX_CHARS });
    result = await findElements({ query, snapshotText: semanticTree.text, entries, env: {
      ANTHROPIC_BASE_URL: process.env.ANTHROPIC_BASE_URL,
      ANTHROPIC_AUTH_TOKEN: process.env.ANTHROPIC_AUTH_TOKEN,
      ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
      ANTHROPIC_SMALL_FAST_MODEL: process.env.ANTHROPIC_SMALL_FAST_MODEL,
    }, fetchImpl: fetch });
  } else {
    result = { matches: structured.length ? [] : scoreDegraded(query, entries), degraded: false };
  }
  const ambiguous = matchedRefs !== undefined && candidateCount > 1 && input.index === undefined;
  return jsonResult({ ok: true, snapshot_id: snap.snapshot_id, match_count: result.matches.length,
    ...(ambiguous ? { ambiguous: true, candidate_count: candidateCount,
      hint: "Inspect nearby row text with browser_snapshot scope_ref, or select a zero-based find index. Candidates are in document order." } : {}),
    degraded: result.degraded, method: matchedRefs ? "locator" : structured.length ? "locator" : "search" },
    formatMatches(query, snap.snapshot_id, result));
}

function formatMatches(query: string, snapshotId: number, result: FindResult): string {
  const lines = [`${result.matches.length} matches for "${query}" (snapshot_id=${snapshotId})`];
  if (result.degraded) lines.push(`semantic search unavailable (${result.reason ?? "unknown"}); showing text matches`);
  for (const m of result.matches) lines.push(`${m.ref} ${m.role} "${m.name}"${m.reason ? ` — ${m.reason}` : ""}`);
  return lines.join("\n");
}
