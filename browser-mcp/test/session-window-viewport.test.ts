import { describe, expect, it, vi } from "vitest";
import type { Page } from "playwright-core";
import type { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";

function fixture() {
  const send = vi.fn().mockResolvedValue({ windowId: 7 });
  const page = (width: number, height: number) => {
    let viewport = { width, height };
    return {
      on: vi.fn(), isClosed: () => false, url: () => "about:blank",
      viewportSize: () => viewport, bringToFront: vi.fn().mockResolvedValue(undefined),
      setViewportSize: async (size: typeof viewport) => { viewport = size; },
      evaluate: async () => viewport,
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
});
