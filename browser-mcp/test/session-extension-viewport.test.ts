import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Page } from "playwright-core";
import type { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";

// A tab in the person's own Chrome (reached through the browser extension) has
// no emulated viewport: Playwright reports none, and the window decides.
function fixture(inner: { width: number; height: number }, kept: Array<{ url: string; visible: boolean }> = []) {
  const send = vi.fn().mockResolvedValue({ targetInfo: { targetId: "T1" }, windowId: 7 });
  const listeners: Record<string, () => void> = {};
  const makePage = (url: string, visible = true) => ({
    on: vi.fn(), isClosed: () => false, url: () => url, title: async () => "",
    viewportSize: () => null, bringToFront: vi.fn().mockResolvedValue(undefined),
    setViewportSize: vi.fn(),
    // The visibility probe returns a boolean; the screen probe an object.
    evaluate: async (fn: () => unknown) => (String(fn).includes("visibilityState") ? visible : { ...inner, deviceScaleFactor: 1 }),
    context: () => ({ newCDPSession: async () => ({ send, on: (name: string, fn: () => void) => { listeners[name] = fn; } }) }),
  }) as unknown as Page;
  const page = makePage("about:blank");
  const keptPages = kept.map(({ url, visible }) => makePage(url, visible));
  const newPage = vi.fn(async () => page);
  const manager = { newPage, chatPages: async () => keptPages } as unknown as BrowserManager;
  return { session: new Session(manager), send, page, listeners, newPage };
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

  it("picks the chat's kept tabs back up instead of opening a blank one", async () => {
    const { session, newPage } = fixture({ width: 1280, height: 657 }, [
      { url: "https://example.com/", visible: false },
      { url: "https://example.org/", visible: true },
    ]);
    const current = await session.ensurePage();
    expect(newPage).not.toHaveBeenCalled();
    expect(current.url()).toBe("https://example.org/");
    const tabs = await session.listTabs();
    expect(tabs.map((tab) => [tab.url, tab.current])).toEqual([
      ["https://example.com/", false],
      ["https://example.org/", true],
    ]);
  });

  it("opens a tab when the chat has none yet", async () => {
    const { session, newPage } = fixture({ width: 1280, height: 657 });
    await session.ensurePage();
    expect(newPage).toHaveBeenCalledTimes(1);
  });
});
