/**
 * browser_tabs — manage the session's pages (tabs). One tool, `op` enum:
 *   list   → id, url, title, current marker for every open tab
 *   create → open a blank tab and make it current
 *   close  → close tab_id (rejects closing the last remaining tab)
 *   select → make tab_id current
 * The session owns multiple pages but every OTHER tool always acts on the
 * current page; select is how you move that pointer. Switching or closing the
 * current tab invalidates refs, so re-snapshot after either.
 */

import type { Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export interface TabsInput {
  op: "list" | "create" | "close" | "select";
  tab_id?: string;
}

export async function runTabs(session: Session, input: TabsInput): Promise<McpToolResult> {
  switch (input.op) {
    case "list": {
      await session.ensurePage();
      const tabs = await session.listTabs();
      return jsonResult({ ok: true, current: session.currentTabId(), tabs });
    }

    case "create": {
      // Make sure the browser/context is live, then open an additional page.
      await session.ensurePage();
      const id = await session.createTab();
      const tabs = await session.listTabs();
      return jsonResult({ ok: true, tab_id: id, current: id, tabs });
    }

    case "select": {
      if (!input.tab_id) return errorResult("op=select requires tab_id");
      try {
        await session.selectTab(input.tab_id);
      } catch (err) {
        return errorResult(err instanceof Error ? err.message : String(err));
      }
      const tabs = await session.listTabs();
      return jsonResult({ ok: true, current: session.currentTabId(), tabs });
    }

    case "close": {
      if (!input.tab_id) return errorResult("op=close requires tab_id");
      try {
        await session.closeTab(input.tab_id);
      } catch (err) {
        return errorResult(err instanceof Error ? err.message : String(err));
      }
      const tabs = await session.listTabs();
      return jsonResult({ ok: true, current: session.currentTabId(), tabs });
    }

    default: {
      const _never: never = input.op;
      return errorResult(`unsupported op: ${_never as string}`);
    }
  }
}
