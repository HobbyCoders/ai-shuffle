import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { Page } from "playwright-core";
import { reportCurrentTarget, RETRY_DELAYS_MS } from "../src/panel.js";

const ENV_KEYS = ["AIS_SESSION_ID", "AIS_PANEL_TOKEN", "AIS_PANEL_REGISTER_URL"] as const;

/** A stub Response. */
function fakeResponse(ok = true, status = 200): Response {
  return { ok, status } as unknown as Response;
}

/** A minimal Page stub whose CDP session yields the given targetId. */
function fakePage(url: string, targetId: string): Page {
  const cdp = {
    send: vi.fn(async () => ({ targetInfo: { targetId } })),
    detach: vi.fn(async () => undefined),
  };
  const context = { newCDPSession: vi.fn(async () => cdp) };
  return {
    url: vi.fn(() => url),
    context: vi.fn(() => context),
  } as unknown as Page;
}

function setEnv(): void {
  process.env.AIS_SESSION_ID = "sess-1";
  process.env.AIS_PANEL_TOKEN = "tok-1";
  process.env.AIS_PANEL_REGISTER_URL = "http://panel.local/register";
}

function clearEnv(): void {
  for (const k of ENV_KEYS) delete process.env[k];
}

describe("reportCurrentTarget", () => {
  let errSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    errSpy.mockRestore();
    clearEnv();
  });

  it("is a silent no-op when panel env is not fully configured", async () => {
    clearEnv();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(() => reportCurrentTarget(null)).not.toThrow();
    // Also missing just one var still no-ops.
    process.env.AIS_SESSION_ID = "sess-1";
    process.env.AIS_PANEL_TOKEN = "tok-1";
    expect(() => reportCurrentTarget(null)).not.toThrow();

    // Give any accidental microtask a chance to run, then assert nothing fired.
    await new Promise((r) => setTimeout(r, 5));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(errSpy).not.toHaveBeenCalled();
  });

  it("posts the correct body on success for a real page", async () => {
    setEnv();
    const fetchMock = vi.fn(async () => fakeResponse(true, 200));
    vi.stubGlobal("fetch", fetchMock);
    const page = fakePage("https://example.com/app", "TARGET-123");

    reportCurrentTarget(page, "tab7");

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://panel.local/register");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["content-type"]).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({
      session_id: "sess-1",
      token: "tok-1",
      target_id: "TARGET-123",
      tab_id: "tab7",
      url: "https://example.com/app",
    });
    expect(errSpy).not.toHaveBeenCalled();
  });

  it("posts nulls for target_id/tab_id/url when page is null", async () => {
    setEnv();
    const fetchMock = vi.fn(async () => fakeResponse(true, 200));
    vi.stubGlobal("fetch", fetchMock);

    reportCurrentTarget(null);

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      session_id: "sess-1",
      token: "tok-1",
      target_id: null,
      tab_id: null,
      url: null,
    });
  });

  it("retries with backoff then gives up without throwing", async () => {
    vi.useFakeTimers();
    setEnv();
    const fetchMock = vi.fn(async () => fakeResponse(false, 500));
    vi.stubGlobal("fetch", fetchMock);

    expect(() => reportCurrentTarget(null)).not.toThrow();

    // Flush the first (immediate) attempt, then each backoff step in turn.
    await vi.advanceTimersByTimeAsync(0);
    for (const delay of RETRY_DELAYS_MS) {
      await vi.advanceTimersByTimeAsync(delay);
    }
    await vi.advanceTimersByTimeAsync(0);

    // 1 initial attempt + one retry per backoff delay.
    expect(fetchMock).toHaveBeenCalledTimes(RETRY_DELAYS_MS.length + 1);
    expect(errSpy).toHaveBeenCalledTimes(1);
    expect(String(errSpy.mock.calls[0][0])).toMatch(/register failed after \d+ attempts/);
  });

  it("newest report wins: an older in-flight report is dropped, not sent", async () => {
    setEnv();
    const fetchMock = vi.fn(async () => fakeResponse(true, 200));
    vi.stubGlobal("fetch", fetchMock);

    const pageA = fakePage("https://a.example.com", "TARGET-A");
    const pageB = fakePage("https://b.example.com", "TARGET-B");

    reportCurrentTarget(pageA, "tabA");
    reportCurrentTarget(pageB, "tabB");

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Give any (unwanted) second in-flight completion a chance to also land.
    await new Promise((r) => setTimeout(r, 10));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toMatchObject({ target_id: "TARGET-B", tab_id: "tabB" });
  });
});
