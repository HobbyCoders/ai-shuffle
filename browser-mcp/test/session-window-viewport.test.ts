import { describe, expect, it, vi } from "vitest";
import type { Page } from "playwright-core";
import type { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";

function fixture(screen?: { width: number; height: number }) {
  const send = vi.fn().mockResolvedValue({ windowId: 7 });
  const page = (width: number, height: number) => {
    let viewport = { width, height };
    return {
      on: vi.fn(), isClosed: () => false, url: () => "about:blank",
      viewportSize: () => viewport, bringToFront: vi.fn().mockResolvedValue(undefined),
      setViewportSize: async (size: typeof viewport) => { viewport = size; },
      evaluate: async () => screen ?? viewport,
      context: () => ({ newCDPSession: async () => ({ send }) }),
    } as unknown as Page;
  };
  const pages = [page(1280, 800), page(390, 844)];
  const manager = { newPage: async () => pages.shift()! } as unknown as BrowserManager;
  return { session: new Session(manager), send };
}

describe("foreground native viewport", () => {
  it("matches window contents to each active tab, including switching back", async () => {
    const { session, send } = fixture();
    await session.ensurePage();
    const first = session.currentTabId()!;
    expect(send).toHaveBeenLastCalledWith("Browser.setContentsSize", { windowId: 7, width: 1280, height: 800 });
    await session.createTab();
    await session.setViewport(390, 844);
    expect(send).toHaveBeenLastCalledWith("Browser.setContentsSize", { windowId: 7, width: 390, height: 844 });
    await session.selectTab(first);
    expect(send).toHaveBeenLastCalledWith("Browser.setContentsSize", { windowId: 7, width: 1280, height: 800 });
  });

  it("does not resize a stale tab after the active tab changes during lookup", async () => {
    const { session, send } = fixture();
    await session.ensurePage();
    const first = session.currentTabId()!;
    await session.createTab();
    await session.setViewport(390, 844);
    send.mockClear();
    send.mockImplementationOnce(async () => {
      await session.selectTab(first);
      return { windowId: 7 };
    });
    await session.bringToFront();
    const resizes = send.mock.calls.filter(([method]) => method === "Browser.setContentsSize");
    expect(resizes).toEqual([["Browser.setContentsSize", { windowId: 7, width: 1280, height: 800 }]]);
  });

  it("keeps foregrounding best-effort when a native window is unavailable", async () => {
    const { session, send } = fixture();
    await session.ensurePage();
    send.mockRejectedValueOnce(new Error("target closed"));
    expect(await session.bringToFront()).toBe(false);
  });

  it("re-issues device metrics with the native screen wherever the viewport is pinned (#1791)", async () => {
    const { session, send } = fixture({ width: 1920, height: 1080 });
    const metrics = (width: number, height: number, screenWidth = 1920, screenHeight = 1080) => ["Emulation.setDeviceMetricsOverride", {
      width, height, deviceScaleFactor: 1, mobile: false, screenWidth, screenHeight,
      screenOrientation: { angle: 0, type: "landscapePrimary" },
    }];
    const overrides = () => send.mock.calls.filter(([method]) => method === "Emulation.setDeviceMetricsOverride");
    await session.ensurePage();
    expect(overrides()).toEqual([metrics(1280, 800)]);
    await session.setViewport(390, 844, true);
    await session.createTab();
    await session.setViewport(2560, 1440);
    expect(overrides()).toEqual([metrics(1280, 800), metrics(390, 844), metrics(390, 844), metrics(2560, 1440, 2560, 1440)]);
    await session.restorePinnedViewport();
    expect(overrides().at(-1)).toEqual(metrics(390, 844));
  });

  it("leaves the screen to Playwright when the native size cannot be read", async () => {
    const { session, send } = fixture({ width: 0, height: 0 });
    await session.ensurePage();
    await session.setViewport(390, 844);
    expect(send.mock.calls.some(([method]) => method === "Emulation.setDeviceMetricsOverride")).toBe(false);
  });
});
