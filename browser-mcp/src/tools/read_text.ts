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
import { observationRoots, includeObservationRoot, observationTask, boundedObservation, observationReady } from "./observation_scope.js";
import { jsonResult, errorResult, capText, emptyContentWarning, type McpToolResult } from "./result.js";

export const READ_TEXT_MAX_CHARS = 30000;

interface Extracted {
  title: string;
  url: string;
  text: string;
  frames_skipped: number;
  frames_hidden: number;
}
interface ReadPart { text: string; hidden?: boolean; excluded?: boolean }

/**
 * Runs inside each frame, traversing the rendered composed tree, including
 * open shadow roots and slots. Closed roots remain inaccessible. Self-contained
 * for Playwright serialization; prefers substantial article/main content.
 */
function extractInRoot(root: Element | null, scoped: boolean): string {
  if (!root || !root.ownerDocument.body) return "";
  if (root.tagName === "HTML") root = root.ownerDocument.body;
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
  const roots = await observationRoots(session, input.scope_ref);
  let extracted: Extracted;
  try {
    const key = Symbol("read");
    const read = (root: typeof roots[number], budget: number) => observationTask<ReadPart>(root, key, async () => {
      if (!await includeObservationRoot(root, false)) return { text: "", excluded: true };
      if (!await includeObservationRoot(root)) return { text: "", hidden: true };
      const text = root.primary && root.scope
        ? await root.scope.evaluate(extractInRoot, true, { timeout: budget })
        : await root.frame.locator(":root").evaluate(extractInRoot, Boolean(input.scope_ref), { timeout: budget });
      return { text: !text || root.frame === page.mainFrame() ? text : `[frame ${new URL(root.frame.url()).origin}]\n${text}` };
    });
    // Protect main prose from child-frame contention; a failed main read is an
    // error, never a successful empty result or a diagnosis of a blank app.
    const mainDeadline = Date.now() + 5000;
    await observationReady(roots[0], mainDeadline);
    const mainBudget = Math.max(1, mainDeadline - Date.now());
    const main = await boundedObservation(read(roots[0], mainBudget), mainBudget);
    if (main.hidden || main.excluded) throw new Error("primary observation root is unavailable");
    const children = roots.slice(1, 49);
    const parts: (ReadPart | undefined)[] = new Array(children.length);
    let next = 0;
    const deadline = Date.now() + 2500;
    await Promise.all(Array.from({ length: Math.min(6, children.length) }, async () => {
      while (next < children.length && Date.now() < deadline) {
        const index = next++;
        try {
          const budget = Math.min(300, deadline - Date.now());
          parts[index] = await boundedObservation(read(children[index], budget), budget);
        } catch { /* child failure is reported in frames_skipped */ }
      }
    }));
    extracted = {
      title: await raceWithTimeout(page.title(), 1000, "title timed out").catch(() => ""),
      url: page.url(), text: [main, ...parts].map(part => part?.text).filter(Boolean).join("\n\n"),
      frames_skipped: roots.length - 1 - parts.filter(part => part !== undefined).length,
      frames_hidden: parts.filter(part => part?.hidden).length,
    };
  } catch (err) {
    return errorResult(`observation_failed: main-frame text could not be read: ${err instanceof Error ? err.message : String(err)}`);
  }

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
    frames_skipped: extracted.frames_skipped,
    frames_hidden: extracted.frames_hidden,
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