import { describe, it, expect } from "vitest";
import { validateTarget, scrollDelta, type ActInput } from "../src/tools/act.js";
import { normalizeUrl, runNavigate } from "../src/tools/navigate.js";
import { runSnapshot } from "../src/tools/snapshot.js";
import { runReadText } from "../src/tools/read_text.js";
import { runScreenshot, scaleFor, MAX_EDGE } from "../src/tools/screenshot.js";
import type { Session } from "../src/session.js";
import type { McpToolResult } from "../src/tools/result.js";

function firstText(res: McpToolResult): string {
  const t = res.content.find((c) => c.type === "text");
  return t && t.type === "text" ? t.text : "";
}
// A session whose ensurePage would throw — proves the check ran BEFORE it.
const noPageSession = {
  ensurePage: () => Promise.reject(new Error("should not reach ensurePage")),
} as unknown as Session;

describe("act.validateTarget", () => {
  const base = { kind: "click" } as ActInput;
  it("requires exactly one of ref or coordinate", () => {
    expect(validateTarget({ ...base })).toMatch(/exactly one/);
    expect(validateTarget({ ...base, ref: "e1", coordinate: [1, 2] })).toMatch(/exactly one/);
    expect(validateTarget({ ...base, ref: "e1" })).toBeNull();
    expect(validateTarget({ ...base, coordinate: [1, 2] })).toBeNull();
  });
  it("allows targetless keys only, retaining explicit-target validation", () => {
    expect(validateTarget({ kind: "key", key: "Escape" })).toBeNull();
    expect(validateTarget({ kind: "key", key: "Escape", ref: "e1", coordinate: [1, 2] })).toMatch(/exactly one/);
    expect(validateTarget({ kind: "key", key: "Escape", coordinate: [1] })).toMatch(/\[x, y\]/);
  });
  it("rejects malformed coordinates", () => {
    expect(validateTarget({ ...base, coordinate: [1] })).toMatch(/\[x, y\]/);
  });
});

describe("act.scrollDelta", () => {
  it("maps directions to wheel deltas", () => {
    expect(scrollDelta("down", 400)).toEqual([0, 400]);
    expect(scrollDelta("up", 400)).toEqual([0, -400]);
    expect(scrollDelta("right", 250)).toEqual([250, 0]);
    expect(scrollDelta("left", 250)).toEqual([-250, 0]);
  });
});

describe("navigate.normalizeUrl", () => {
  it("prepends https:// for bare domains", () => {
    expect(normalizeUrl("example.com")).toBe("https://example.com");
    expect(normalizeUrl("example.com/path?q=1")).toBe("https://example.com/path?q=1");
  });
  it("leaves explicit schemes untouched", () => {
    expect(normalizeUrl("http://foo.test")).toBe("http://foo.test");
    expect(normalizeUrl("https://foo.test")).toBe("https://foo.test");
    expect(normalizeUrl("about:blank")).toBe("about:blank");
    expect(normalizeUrl("data:text/html,hi")).toBe("data:text/html,hi");
  });
});

describe("screenshot.scaleFor", () => {
  it("returns 1 when within the max edge", () => {
    expect(scaleFor(800, 600)).toBe(1);
    expect(scaleFor(MAX_EDGE, 100)).toBe(1);
  });
  it("downscales by the longest edge", () => {
    expect(scaleFor(2560, 1440)).toBeCloseTo(MAX_EDGE / 2560);
    expect(scaleFor(1000, 4000)).toBeCloseTo(MAX_EDGE / 4000);
  });
});

describe("screenshot region validation (m6)", () => {
  it("rejects a region that is not [x,y,w,h]", async () => {
    const res = await runScreenshot(noPageSession, { region: [0, 0, 100] });
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("[x, y, width, height]");
  });
  it("rejects region + full_page together", async () => {
    const res = await runScreenshot(noPageSession, { region: [0, 0, 100, 100], full_page: true });
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("not both");
  });
  it("rejects non-positive width/height", async () => {
    const zero = await runScreenshot(noPageSession, { region: [0, 0, 0, 100] });
    expect(zero.isError).toBe(true);
    const neg = await runScreenshot(noPageSession, { region: [0, 0, 100, -5] });
    expect(neg.isError).toBe(true);
    expect(firstText(neg)).toContain("positive");
  });
});

// ---------------------------------------------------------------------------
// Empty-result warning: a page that painted nothing must not read as success.
// ---------------------------------------------------------------------------

function bodyOf(res: McpToolResult): Record<string, unknown> {
  return JSON.parse(firstText(res)) as Record<string, unknown>;
}

function trailingOf(res: McpToolResult): string {
  const texts = res.content.filter((c) => c.type === "text");
  const last = texts[texts.length - 1];
  return texts.length > 1 && last && last.type === "text" ? last.text : "";
}

/** A session whose snapshot returns exactly the text/refs given. */
function snapshotSession(
  text: string,
  refCount: number,
  page: { title: string; textChars: number } = { title: "", textChars: 0 },
): Session {
  return {
    ensurePage: async () => ({}),
    notifyNavigation: () => {},
    pageContent: async () => page,
    snapshot: async () => ({
      text,
      snapshot_id: 7,
      refs: new Array(refCount).fill({ ref: "e1" }),
      truncated: false,
    }),
  } as unknown as Session;
}

const RICH_SNAPSHOT = 'button "Save" [ref=e1]\nlink "Docs" [ref=e2]\nheading "Dashboard"';
const RENDERED = { title: "Dashboard", textChars: 400 };

describe("content warning via browser_snapshot", () => {
  it("reports missing refs without declaring the app failed", async () => {
    const res = await runSnapshot(snapshotSession("", 0), {});
    const body = bodyOf(res);
    expect(body.ok).toBe(true); // ok semantics unchanged
    expect(body.ref_count).toBe(0);
    expect(String(body.warning)).toMatch(/no actionable refs yet/);
    expect(String(body.warning)).not.toMatch(/likely a blank/);
    expect(String(body.warning)).toMatch(/browser_wait/);
    // The warning is also visible in the trailing block, not just the JSON.
    expect(trailingOf(res)).toContain("[warning]");
  });

  it("warns about a whitespace-only render too", async () => {
    const body = bodyOf(
      await runSnapshot(snapshotSession("   \n\n  ", 0, { title: "  ", textChars: 0 }), {}),
    );
    expect(String(body.warning)).toMatch(/no actionable refs yet/);
  });

  it("flags a rendered page with no act-able elements more gently", async () => {
    const body = bodyOf(await runSnapshot(snapshotSession("", 0, RENDERED), {}));
    expect(body.ok).toBe(true);
    expect(String(body.warning)).toMatch(/no actionable refs yet/);
    expect(String(body.warning)).not.toMatch(/rendered no content/);
  });

  it("keeps quiet when the page could not be measured", async () => {
    const unknown = { title: "", textChars: -1 };
    const body = bodyOf(await runSnapshot(snapshotSession(RICH_SNAPSHOT, 2, unknown), {}));
    expect(body.warning).toBeUndefined();
  });

  it("stays silent on a normal page", async () => {
    const res = await runSnapshot(snapshotSession(RICH_SNAPSHOT, 2, RENDERED), {});
    expect(bodyOf(res).warning).toBeUndefined();
    expect(trailingOf(res)).toBe(RICH_SNAPSHOT);
  });
});

describe("content warning via browser_navigate", () => {
  function navSession(
    text: string,
    refCount: number,
    page: { title: string; textChars: number } = { title: "", textChars: 0 },
    landing = "http://127.0.0.1:5173/",
  ): Session {
    const handle = {
      goto: async () => {},
      goBack: async () => {},
      goForward: async () => {},
      url: () => landing,
      title: async () => page.title,
      locator: () => ({ first: () => ({ isVisible: async () => true }) }),
    };
    return {
      ensurePage: async () => handle,
      currentTabId: () => "tab1",
      notifyNavigation: () => {},
      pageContent: async () => page,
      snapshot: async () => ({
        text,
        snapshot_id: 1,
        refs: new Array(refCount).fill({ ref: "e1" }),
        truncated: false,
      }),
    } as unknown as Session;
  }

  it("warns when a navigation lands on a blank page", async () => {
    const body = bodyOf(await runNavigate(navSession("", 0), { url: "http://127.0.0.1:5173/" }));
    expect(body.ok).toBe(true);
    expect(body.ref_count).toBe(0);
    expect(String(body.warning)).toMatch(/no actionable refs yet/);
  });

  it("stays silent when the landing page has content", async () => {
    const body = bodyOf(
      await runNavigate(navSession(RICH_SNAPSHOT, 2, RENDERED), { url: "http://127.0.0.1:5173/" }),
    );
    expect(body.warning).toBeUndefined();
  });

  // Audit R7: a rendered login form and a canvas fixture both have zero
  // snapshot refs, and both were announced as "rendered no content".
  it.each([
    ["a login form", { title: "Login - AI Shuffle", textChars: 120 }],
    ["a canvas fixture", { title: "Reply suggestion fixture", textChars: 60 }],
    ["a titled page with almost no prose", { title: "Canvas", textChars: 0 }],
  ])("does not call %s a blank page", async (_label, page) => {
    const body = bodyOf(
      await runNavigate(navSession("", 0, page), { url: "http://127.0.0.1:5173/" }),
    );
    expect(body.title).toBe(page.title);
    expect(String(body.warning ?? "")).not.toMatch(/rendered no content/);
  });

  it("flags a landing on another origin as a sign-in redirect", async () => {
    const login = { title: "Sign in", textChars: 300 };
    const body = bodyOf(
      await runNavigate(
        navSession(RICH_SNAPSHOT, 2, login, "https://auth.example.com/cdn-cgi/access/login/app"),
        { url: "https://app.example.com/dashboard" },
      ),
    );
    expect(body.ok).toBe(true);
    expect(body.redirected_to_origin).toBe("https://auth.example.com");
    expect(body.warning).toBeUndefined();
  });

  it("does not flag a same-origin redirect or a history move", async () => {
    const same = bodyOf(
      await runNavigate(
        navSession(RICH_SNAPSHOT, 2, RENDERED, "https://app.example.com/login"),
        { url: "https://app.example.com/dashboard" },
      ),
    );
    expect(same.redirected_to_origin).toBeUndefined();
    const back = bodyOf(
      await runNavigate(
        navSession(RICH_SNAPSHOT, 2, RENDERED, "https://elsewhere.example.com/"),
        { url: "back" },
      ),
    );
    expect(back.redirected_to_origin).toBeUndefined();
  });
});

describe("emptyContentWarning via browser_read_text", () => {
  function readSession(text: string): Session {
    return {
      ensurePage: async () => ({}),
      evaluate: async () => ({ title: "App", url: "http://127.0.0.1:5173/", text }),
    } as unknown as Session;
  }

  it("warns when the extracted prose is empty", async () => {
    const body = bodyOf(await runReadText(readSession("")));
    expect(body.ok).toBe(true);
    expect(body.chars).toBe(0);
    expect(String(body.warning)).toMatch(/rendered no content/);
  });

  it("stays silent on a page with real prose", async () => {
    const prose = "The quick brown fox jumps over the lazy dog, repeatedly and at length.";
    const body = bodyOf(await runReadText(readSession(prose)));
    expect(body.warning).toBeUndefined();
  });
});

describe("content warning cost", () => {
  it("does not ask the page anything when the snapshot has refs", async () => {
    let asked = 0;
    const session = {
      ensurePage: async () => ({}),
      notifyNavigation: () => {},
      pageContent: async () => {
        asked += 1;
        return { title: "", textChars: 0 };
      },
      snapshot: async () => ({
        text: RICH_SNAPSHOT,
        snapshot_id: 3,
        refs: ["e1", "e2"],
        truncated: false,
      }),
    } as unknown as Session;
    const body = bodyOf(await runSnapshot(session, {}));
    expect(body.warning).toBeUndefined();
    expect(asked).toBe(0);
  });
});
