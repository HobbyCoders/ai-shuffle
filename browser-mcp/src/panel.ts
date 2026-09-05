/**
 * Panel self-registration: fire-and-forget CDP target reporting so the host
 * app's panel can locate which CDP target this browser-mcp process is
 * currently driving. Entirely opt-in via env — when the panel wiring isn't
 * configured (no session id / token / register URL), every call is a silent
 * no-op. Never throws and never blocks a tool call: callers invoke
 * {@link reportCurrentTarget} without awaiting it.
 */

import type { Page } from "playwright-core";

/** Backoff between retries of the register POST, in order. */
export const RETRY_DELAYS_MS = [250, 500, 1000];

interface RegisterPayload {
  session_id: string;
  token: string;
  target_id: string | null;
  tab_id: string | null;
  url: string | null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Build the register payload. For a non-null page, resolves the CDP
 * targetId via a throwaway CDP session and captures the page's current URL.
 */
async function buildPayload(
  sessionId: string,
  token: string,
  page: Page | null,
  tabId: string | undefined,
): Promise<RegisterPayload> {
  if (!page) {
    return { session_id: sessionId, token, target_id: null, tab_id: null, url: null };
  }
  const cdp = await page.context().newCDPSession(page);
  const info = (await cdp.send("Target.getTargetInfo")) as { targetInfo: { targetId: string } };
  await cdp.detach();
  return {
    session_id: sessionId,
    token,
    target_id: info.targetInfo.targetId,
    tab_id: tabId ?? null,
    url: page.url(),
  };
}

/** Monotonic counter: only the newest in-flight report is allowed to complete. */
let generation = 0;
let legacyDisabled = false;
export function disableLegacyReports(): void { legacyDisabled = true; generation++; }

async function run(page: Page | null, tabId: string | undefined, gen: number): Promise<void> {
  const sessionId = process.env.AIS_SESSION_ID;
  const token = process.env.AIS_PANEL_TOKEN;
  const registerUrl = process.env.AIS_PANEL_REGISTER_URL;
  if (!sessionId || !token || !registerUrl) return;

  for (let attempt = 0; ; attempt++) {
    // A newer report superseded this one while we were building/sending/backing
    // off — drop this stale attempt rather than let it "win" out of order.
    if (gen !== generation) return;
    try {
      const payload = await buildPayload(sessionId, token, page, tabId);
      // A newer report may have started while we were resolving the CDP
      // target — check again right before sending so a stale attempt never
      // races a fresher one onto the wire.
      if (gen !== generation) return;
      const resp = await fetch(registerUrl, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(`http ${resp.status}`);
      return;
    } catch (err) {
      if (attempt >= RETRY_DELAYS_MS.length) {
        // stdout is the MCP stdio channel — failures are logged to stderr only.
        console.error(
          `[browser-mcp/panel] register failed after ${attempt + 1} attempts: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
        return;
      }
      await sleep(RETRY_DELAYS_MS[attempt]);
    }
  }
}

/**
 * Report the currently-driven CDP target to the panel. Fire-and-forget: never
 * awaited by callers, never throws. Pass `page: null` to report "no current
 * target" (e.g. on shutdown or when the last tab closed). No-ops silently
 * when AIS_SESSION_ID / AIS_PANEL_TOKEN / AIS_PANEL_REGISTER_URL aren't all
 * set.
 */
export function reportCurrentTarget(page: Page | null, tabId?: string): void {
  if (legacyDisabled) return;
  const gen = ++generation;
  run(page, tabId, gen).catch((err) => {
    console.error(
      `[browser-mcp/panel] unexpected error: ${err instanceof Error ? err.message : String(err)}`,
    );
  });
}
