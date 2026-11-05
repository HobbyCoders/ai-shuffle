import { afterAll, beforeAll, expect, it } from "vitest";
import { existsSync } from "node:fs";
import { chromium, type Browser } from "playwright-core";
import { recorderSource } from "../src/recorder/driver.js";

const executablePath = process.env.CHROMIUM_PATH || [
  "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome-stable",
].find(existsSync);
let browser: Browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
});
afterAll(async () => { await browser?.close(); });

it.each(["remove", "removeChild", "replaceChild", "innerHTML"])("captures a forced MessageArea ancestor remount via %s", async (method) => {
  const page = await browser.newPage();
  try {
    await page.setContent(`<main data-ui-state="chat" data-ui-mount="ChatView" data-ui-surface="active" data-ui-session="s:abc123" data-ui-messages="1">
      <section class="chat-column"><div data-ui-mount="MessageArea" style="height:100px">fixture prompt</div></section>
    </main>`);
    await page.evaluate(() => {
      const view = document.querySelector("main")!;
      (window as any).liveState = { showEmptyHero: false, isEmptyState: false, chatSurface: "active",
        presentedSurface: "active", transcriptSurface: "active", mirrorWaiting: false,
        voiceCallActive: false, session: "s:abc123", transcriptSession: "s:abc123", messages: 1, transcriptMessages: 1,
        prompt: "private text must never be copied" };
      (view as any).__flightRecorderState = () => ({ ...(window as any).liveState });
      (window as any).originalRemove = Element.prototype.remove;
      (window as any).originalRemoveChild = Node.prototype.removeChild;
    });
    await page.evaluate(recorderSource());
    // Injection alone is inert; start is the only switch.
    expect(await page.evaluate(() => Element.prototype.remove === (window as any).originalRemove)).toBe(true);
    await page.evaluate(() => (window as any).__flightRecorder.start({ persist: false }));
    await page.evaluate(async (method) => {
      const view = document.querySelector("main")!;
      const column = view.querySelector("section")!;
      (window as any).liveState.showEmptyHero = true;
      (window as any).liveState.isEmptyState = true;
      if (method === "remove") column.remove();
      else if (method === "removeChild") view.removeChild(column);
      else if (method === "replaceChild") view.replaceChild(document.createComment("gap"), column);
      else view.innerHTML = ""; // unpatched API: observer fallback must identify the removed ancestor
      // No rAF signature can see this reversal: the synchronous getter must.
      (window as any).liveState.showEmptyHero = false;
      (window as any).liveState.isEmptyState = false;
      await new Promise((resolve) => setTimeout(resolve, 100));
      view.append(column); // same node, not a new component; identity is diagnostic only
      await new Promise((resolve) => setTimeout(resolve, 40));
    }, method);
    const rec = await page.evaluate(() => (window as any).__flightRecorder.stop());
    expect(rec.anomalies.filter((a: any) => a.kind === "remount")).toMatchObject([{ name: "MessageArea" }]);
    const mounts = rec.timeline.filter((e: any) => e.kind === "mount" && e.name === "MessageArea");
    const gone = mounts[0].diagnostic;
    const back = mounts[1].diagnostic;
    expect(gone.removed).toBe("section.chat-column");
    expect(gone.parent).toBe("main[ChatView]");
    expect(gone.source).toBe(method === "innerHTML" ? "MutationObserver (no synchronous stack)" : method);
    expect(gone.state).toMatchObject({ showEmptyHero: method !== "innerHTML", messages: "1", session: "s:abc123" });
    if (method !== "innerHTML") expect(gone.stack).toMatch(/recorder-diagnostics|eval|evaluate/);
    expect(gone.observed.showEmptyHero).toBe(false);
    expect(gone.visibility).toMatchObject({ document: "visible", messageAreas: 0, visibleMessageAreas: 0 });
    expect(back.state.showEmptyHero).toBe(false);
    expect(back.node).toBe(gone.node);
    expect(rec.anomalies.filter((a: any) => a.kind === "remount")).toHaveLength(1);
    expect(back.visibility.visibleMessageAreas).toBe(1);
    const gap = rec.timeline.find((e: any) => e.kind === "note" && e.message === "MessageArea gap at pre-paint");
    expect(gap.diagnostic.visibility.messageAreas).toBe(0);
    expect(rec.stats.internalErrors).toBe(0);
    expect(rec.text).toContain('"removed":"section.chat-column"');
    expect(JSON.stringify(rec)).not.toContain("private text");
    expect(JSON.stringify(rec)).not.toContain("fixture prompt");
    expect(await page.evaluate(() => Element.prototype.remove === (window as any).originalRemove && Node.prototype.removeChild === (window as any).originalRemoveChild)).toBe(true);
    if (method === "remove") console.log("Forced remount evidence:\n" + rec.text);
  } finally { await page.close(); }
});

it("keeps remount detection and native removal semantics when a diagnostic reader throws", async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<main data-ui-state="chat"><div data-ui-mount="MessageArea"></div></main>');
    await page.evaluate(recorderSource());
    await page.evaluate(() => {
      const view = document.querySelector("main")!;
      (view as any).__flightRecorderState = () => { throw new Error("broken reader"); };
      (window as any).__flightRecorder.start({ persist: false });
      const area = view.firstElementChild!;
      (window as any).oldArea = area;
      view.removeChild(area);
    });
    await page.waitForTimeout(20);
    await page.evaluate(() => {
      const area = document.createElement("div");
      area.setAttribute("data-ui-mount", "MessageArea");
      document.querySelector("main")!.append(area);
    });
    await page.waitForTimeout(20);
    const rec = await page.evaluate(() => (window as any).__flightRecorder.stop());
    expect(rec.anomalies).toContainEqual(expect.objectContaining({ kind: "remount", name: "MessageArea" }));
    expect(rec.timeline.filter((e: any) => e.kind === "mount").map((e: any) => e.diagnostic)).toEqual([
      { unavailable: true }, { unavailable: true }
    ]);
  } finally { await page.close(); }
});

