/**
 * browser_read_text — reader-mode text extraction. Prefers the main article/
 * content region (article, main, [role=main], common content-class selectors)
 * and falls back to the whole body. Includes frames and open shadow roots;
 * scope_ref restricts extraction to a subtree and its frames. Returns text
 * capped ~30k chars and a self-describing truncation note (mirrors snapshot.ts).
 * Cheaper than a screenshot and far cheaper than a full snapshot when you just
 * need the prose of a page.
 */

import { raceWithTimeout, type Session } from "../session.js";
import { observationRoots } from "./observation_scope.js";
import { jsonResult, capText, emptyContentWarning, type McpToolResult } from "./result.js";

export const READ_TEXT_MAX_CHARS = 30000;

interface Extracted {
  title: string;
  url: string;
  text: string;
}

/**
 * Runs inside each frame, traversing the rendered composed tree, including
 * open shadow roots and slots. Closed roots remain inaccessible. Self-contained
 * for Playwright serialization; prefers substantial article/main content.
 */
function extractInRoot(root: Element, scoped: boolean): string {
  const norm = (s: string) => s.replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const read = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
    if (!(node instanceof Element)) return "";
    if (node.matches("script,style,noscript,template")) return "";
    if (node.tagName === "BR") return "\n";
    const style = getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return "";
    const children = node instanceof HTMLSlotElement && node.assignedNodes().length
      ? node.assignedNodes({ flatten: true }) : Array.from((node.shadowRoot ?? node).childNodes);
    const text = children.map(read).join("");
    return style.display === "inline" || style.display === "contents" ? text : `\n${text}\n`;
  };
  if (!scoped) {
    for (const selector of ["article", "main", '[role="main"]', ".post-content", ".article-content", ".entry-content", ".markdown-body", "#content", ".content"]) {
      const region = root.querySelector(selector);
      if (region) {
        const text = norm(read(region));
        if (text.length >= 200) return text;
      }
    }
  }
  return norm(read(root));
}

export async function runReadText(session: Session, input: { offset?: number; scope_ref?: string } = {}): Promise<McpToolResult> {
  const page = await session.ensurePage();
  const extracted: Extracted = await raceWithTimeout((async () => {
    const roots = await observationRoots(session, input.scope_ref);
    const parts = await Promise.all(roots.map(root => root.evaluate(extractInRoot, Boolean(input.scope_ref))));
    return { title: await page.title(), url: page.url(), text: parts.filter(Boolean).join("\n\n") };
  })(), 5000, "read_text timed out");

  const offset = Math.max(0, Math.min(input.offset ?? 0, extracted.text.length));
  const { text, truncated, fullChars } = capText(extracted.text.slice(offset), READ_TEXT_MAX_CHARS);
  const warning = offset === 0 ? emptyContentWarning(text) : undefined;
  const total = offset + fullChars;
  const next = offset + text.length;
  const body: Record<string, unknown> = {
    ok: true,
    url: extracted.url,
    title: extracted.title,
    chars: text.length,
    truncated,
    ...(offset ? { offset } : {}),
    ...(truncated ? { total_chars: total, next_offset: next } : {}),
    ...(warning ? { warning } : {}),
  };
  let trailing = truncated
    ? `${text}\n[truncated: chars ${offset}-${next} of ${total}; call browser_read_text with offset=${next} for the rest]`
    : text;
  if (warning) trailing = `${trailing}\n[warning] ${warning}`.trim();
  return jsonResult(body, trailing);
}