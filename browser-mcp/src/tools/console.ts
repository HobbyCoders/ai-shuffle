/**
 * browser_console — query the current page's console buffer. `pattern` is
 * REQUIRED and filters messages server-side (substring or regex). Messages are
 * captured continuously via a page.on('console') listener installed when the
 * page is acquired, so this reads back history, it does not start listening.
 * Default limit 20, newest last; `clear` empties the buffer afterwards.
 */

import type { Session } from "../session.js";
import { jsonResult, type McpToolResult } from "./result.js";
import { compileMatcher } from "../match.js";

export const CONSOLE_DEFAULT_LIMIT = 20;

export interface ConsoleInput {
  pattern: string;
  only_errors?: boolean;
  limit?: number;
  clear?: boolean;
}

export async function runConsole(session: Session, input: ConsoleInput): Promise<McpToolResult> {
  await session.ensurePage();

  const matches = compileMatcher(input.pattern);
  const limit = input.limit ?? CONSOLE_DEFAULT_LIMIT;

  const filtered = session
    .consoleEntries()
    .filter((e) => (input.only_errors ? e.level === "error" : true))
    .filter((e) => matches(e.text));

  const matched = filtered.length;
  // Newest last: keep the tail of the (chronological) filtered list.
  const entries = filtered.slice(Math.max(0, filtered.length - limit));

  if (input.clear) session.clearConsole();

  return jsonResult({
    ok: true,
    matched,
    returned: entries.length,
    entries: entries.map((e) => ({ level: e.level, text: e.text, location: e.location })),
  });
}
