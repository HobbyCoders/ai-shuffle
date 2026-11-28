import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Page } from "playwright-core";
import type { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";

// A tab in the person's own Chrome (reached through the browser extension) has
// no emulated viewport: Playwright reports none, and the window decides.
function fixture(inner: { width: number; height: number }) {
  const send = vi.fn().mockResolvedValue({ targetInfo: { targetId: "T1" }, windowId: 7 });
  const listeners: Record<string, () => void> = {};
  const page = {
    on: vi.fn(), isClosed: () => false, url: () => "about:blank", title: async () => "",
    viewportSize: () => null, bringToFront: vi.fn().mockResolvedValue(undefined),
    setViewportSize: vi.fn(),
    evaluate: async () => ({ ...inner, deviceScaleFactor: 1 }),
    context: () => ({ newCDPSession: async () => ({ send, on: (name: string, fn: () => void) => { listeners[name] = fn; } }) }),
  } as unknown as Page;
  const manager = { newPage: async () => page } as unknown as BrowserManager;
  return { session: new Session(manager), send, page, listeners };
}

describe("extension browser viewport", () => {
  beforeEach(() => {
    vi.stubEnv("AIS_BROWSER_TARGET", "extension");
    vi.stubEnv("AIS_BROWSER_CDP_ENDPOINT", "ws://127.0.0.1:1/api/v1/browser-extension/cdp/token");
  });
  afterEach(() => vi.unstubAllEnvs());

  it("leaves the person's tab at its own size and reports that size to the panel", async () => {
    const { session, send, page } = fixture({ width: 1280, height: 657 });
    await session.ensurePage();
    expect(page.setViewportSize).not.toHaveBeenCalled();
    expect(send.mock.calls.some(([method]) => method === "Emulation.setDeviceMetricsOverride")).toBe(false);
    const metadata = await session.workspaceMetadata();
    expect(metadata.viewport).toMatchObject({ width: 1280, height: 657 });
  });

  it("tells the panel when the person resizes the window", async () => {
    const { session, listeners } = fixture({ width: 1280, height: 657 });
    await session.ensurePage();
    await session.currentCDP();
    const changed = vi.fn();
    session.onWorkspaceChange = changed;
    listeners["Page.frameResized"]?.();
    expect(changed).toHaveBeenCalled();
  });
});
