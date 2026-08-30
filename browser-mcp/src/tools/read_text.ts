/**
 * browser_read_text — reader-mode text extraction. Prefers the main article/
 * content region (article, main, [role=main], common content-class selectors)
 * and falls back to the whole body. Returns {title, url, text} with the text
 * capped ~30k chars and a self-describing truncation note (mirrors snapshot.ts).
 * Cheaper than a screenshot and far cheaper than a full snapshot when you just
 * need the prose of a page.
 */

import type { Session } from "../session.js";
import { jsonResult, capText, emptyContentWarning, type McpToolResult } from "./result.js";

export const READ_TEXT_MAX_CHARS = 30000;

interface Extracted {
  title: string;
  url: string;
  text: string;
}

/**
 * Page-side extractor. Serialized and injected via page.evaluate, so it must be
 * self-contained (no closure over module scope). Walks a priority list of
 * content containers and returns the innerText of the first substantial match,
 * else the body's innerText.
 */
function extractInPage(): { title: string; url: string; text: string } {
  const selectors = [
    "article",
    "main",
    '[role="main"]',
    ".post-content",
    ".article-content",
    ".entry-content",
    ".markdown-body",
    "#content",
    ".content",
  ];
  const norm = (s: string): string =>
    s.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();

  let best = "";
  for (const sel of selectors) {
    const el = document.querySelector(sel) as HTMLElement | null;
    const t = el && typeof el.innerText === "string" ? norm(el.innerText) : "";
    // Take the first container that carries a meaningful amount of prose.
    if (t.length >= 200) {
      best = t;
      break;
    }
    if (t.length > best.length) best = t;
  }
  const bodyText = norm(document.body ? document.body.innerText : "");
  const text = best.length >= 200 ? best : bodyText;
  return { title: document.title, url: location.href, text };
}

export async function runReadText(session: Session): Promise<McpToolResult> {
  await session.ensurePage();
  const extracted: Extracted = await session.evaluate(extractInPage, "");

  const { text, truncated, fullChars } = capText(extracted.text, READ_TEXT_MAX_CHARS);
  const warning = emptyContentWarning(text);
  const body: Record<string, unknown> = {
    ok: true,
    url: extracted.url,
    title: extracted.title,
    chars: text.length,
    truncated,
    ...(warning ? { warning } : {}),
  };
  let trailing = truncated
    ? `${text}\n[truncated: full text is ${fullChars} chars]`
    : text;
  if (warning) trailing = `${trailing}\n[warning] ${warning}`.trim();
  return jsonResult(body, trailing);
}
