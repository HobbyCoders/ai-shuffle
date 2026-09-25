import { describe, expect, it } from "vitest";
import {
  EGRESS_REFUSED_NET_ERRORS,
  RETRYABLE_NET_ERRORS,
  behindEgressProxy,
  egressRefusal,
  isLoopbackHost,
  isLoopbackUrl,
  loopbackGuidance,
  loopbackHost,
  rewriteLoopback,
} from "../src/loopback.js";
import { runNavigate } from "../src/tools/navigate.js";
import type { Session } from "../src/session.js";
import type { McpToolResult } from "../src/tools/result.js";

describe("loopback classification", () => {
  it.each(["localhost", "LOCALHOST", "127.0.0.1", "127.1.2.3", "0.0.0.0", "::1", "[::1]"])(
    "treats %s as loopback",
    (host) => expect(isLoopbackHost(host)).toBe(true),
  );
  it.each(["10.0.0.200", "172.17.0.5", "example.com", "192.168.1.4", "localhost.evil.com"])(
    "treats %s as routable",
    (host) => expect(isLoopbackHost(host)).toBe(false),
  );
  it("classifies whole URLs", () => {
    expect(isLoopbackUrl("http://localhost:5179/app")).toBe(true);
    expect(isLoopbackUrl("http://172.17.0.5:5179/app")).toBe(false);
    expect(isLoopbackUrl("not a url")).toBe(false);
  });
});

describe("BROWSER_LOOPBACK_HOST", () => {
  it("is ignored when unset, blank, or itself loopback", () => {
    expect(loopbackHost({})).toBeUndefined();
    expect(loopbackHost({ BROWSER_LOOPBACK_HOST: "  " })).toBeUndefined();
    expect(loopbackHost({ BROWSER_LOOPBACK_HOST: "127.0.0.1" })).toBeUndefined();
    expect(loopbackHost({ BROWSER_LOOPBACK_HOST: "localhost" })).toBeUndefined();
  });
  it("is used when it names a routable address", () => {
    expect(loopbackHost({ BROWSER_LOOPBACK_HOST: "172.17.0.5" })).toBe("172.17.0.5");
  });
});

describe("rewriteLoopback", () => {
  it("swaps only the host and keeps port, path and query", () => {
    expect(rewriteLoopback("http://localhost:5179/app?x=1#f", "172.17.0.5")).toBe(
      "http://172.17.0.5:5179/app?x=1#f",
    );
  });
  it("brackets a bare IPv6 replacement", () => {
    expect(rewriteLoopback("http://localhost:8000/", "fd00::5")).toBe("http://[fd00::5]:8000/");
  });
  it("declines when there is nothing to rewrite", () => {
    expect(rewriteLoopback("http://localhost:5179/", undefined)).toBeUndefined();
    expect(rewriteLoopback("https://example.com/", "172.17.0.5")).toBeUndefined();
  });
});

describe("loopbackGuidance", () => {
  it("leads with the exact URL to use when the app knows one", () => {
    const message = loopbackGuidance("http://localhost:5179/", "172.17.0.5");
    expect(message.startsWith("Browse http://172.17.0.5:5179/ instead")).toBe(true);
  });
  it("leads with the bind-and-browse-by-IP step otherwise", () => {
    const message = loopbackGuidance("http://localhost:5179/", undefined);
    expect(message.startsWith("Bind the server to 0.0.0.0")).toBe(true);
    expect(message).toContain("hostname -I");
  });
  it("names an egress refusal as policy, not as an unreachable host", () => {
    const message = loopbackGuidance("http://127.0.0.1:8000/", undefined, "IPv4 loopback: blocked");
    expect(message).toContain("refused by the browser's egress policy");
    expect(message).toContain("Policy detail: IPv4 loopback: blocked");
    expect(message).not.toContain("did not answer");
  });
});

describe("browser_navigate loopback retry", () => {
  const REFUSED = "page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:5179/";

  function navSession(
    accept: (url: string) => boolean,
    visited: string[],
    failure: string | ((url: string) => string) = REFUSED,
    responseHeaders: Record<string, string> | ((url: string) => Record<string, string>) = {},
  ): Session {
    const page = {
      url: () => visited[visited.length - 1] ?? "about:blank",
      goto: async (url: string) => {
        if (!accept(url)) throw new Error(typeof failure === "function" ? failure(url) : failure);
        visited.push(url);
        const headers = typeof responseHeaders === "function" ? responseHeaders(url) : responseHeaders;
        return { headers: () => headers };
      },
      title: async () => "Dev server",
      locator: () => ({ first: () => ({ isVisible: async () => true }) }),
    };
    return {
      ensurePage: async () => page,
      currentTabId: () => "tab1",
      notifyNavigation: () => {},
      pageContent: async () => ({ title: "Dev server", textChars: 200 }),
      ssrfBlockReason: () => undefined,
      snapshot: async () => ({ text: "heading \"Dev\"", snapshot_id: 1, refs: ["e1"], truncated: false }),
    } as unknown as Session;
  }

  const body = (res: McpToolResult) =>
    JSON.parse(res.content[0].type === "text" ? res.content[0].text : "{}");

  it("retries a refused loopback URL against the configured host", async () => {
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    const visited: string[] = [];
    try {
      const res = await runNavigate(
        navSession((url) => url.includes("172.17.0.5"), visited),
        { url: "http://localhost:5179" },
      );
      const parsed = body(res);
      expect(visited).toEqual(["http://172.17.0.5:5179/"]);
      expect(parsed.ok).toBe(true);
      expect(parsed.rewrote_loopback_to).toBe("http://172.17.0.5:5179/");
      expect(String(parsed.note)).toContain("172.17.0.5");
      // The rewrite is not a cross-origin sign-in redirect.
      expect(parsed.redirected_to_origin).toBeUndefined();
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
    }
  });

  it("explains the failure when the rewrite also fails", async () => {
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    try {
      await expect(
        runNavigate(navSession(() => false, []), { url: "http://localhost:5179" }),
      ).rejects.toThrow(/^Browse http:\/\/172\.17\.0\.5:5179\/ instead/);
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
    }
  });

  it("explains the failure when no host is configured", async () => {
    delete process.env.BROWSER_LOOPBACK_HOST;
    await expect(
      runNavigate(navSession(() => false, []), { url: "http://localhost:5179" }),
    ).rejects.toThrow(/^Bind the server to 0\.0\.0\.0/);
  });

  it("leaves a routable URL's failure alone", async () => {
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    try {
      await expect(
        runNavigate(navSession(() => false, []), { url: "https://example.com" }),
      ).rejects.toThrow(/ERR_CONNECTION_REFUSED/);
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
    }
  });

  it("recognises the net errors worth one retry", () => {
    expect(RETRYABLE_NET_ERRORS.test(REFUSED)).toBe(true);
    expect(RETRYABLE_NET_ERRORS.test("net::ERR_CERT_AUTHORITY_INVALID")).toBe(false);
    // The egress proxy's refusal is policy, not "nothing answered".
    expect(RETRYABLE_NET_ERRORS.test(TUNNEL_REFUSED)).toBe(false);
    expect(EGRESS_REFUSED_NET_ERRORS.test(TUNNEL_REFUSED)).toBe(true);
  });

  const TUNNEL_REFUSED = "page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://localhost:5179/";

  it("reports an egress-proxy refusal of an https URL without retrying", async () => {
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    process.env.BROWSER_EGRESS_PROXY = "1";
    const attempts: string[] = [];
    try {
      const session = navSession((url) => (attempts.push(url), false), [], TUNNEL_REFUSED);
      await expect(runNavigate(session, { url: "https://localhost:5179" })).rejects.toThrow(
        /^blocked by the browser's egress proxy: https:\/\/localhost:5179 /,
      );
      expect(attempts).toEqual(["https://localhost:5179"]);
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
      delete process.env.BROWSER_EGRESS_PROXY;
    }
  });

  it("does not call a refused tunnel policy when the browser is not known to be behind the egress proxy", async () => {
    // R8-6: a browser reached without the launcher (or through a corporate
    // proxy) also reports ERR_TUNNEL_CONNECTION_FAILED; that is not a verdict
    // of the app's policy, and "retrying will not help" is not known.
    delete process.env.BROWSER_EGRESS_PROXY;
    const failure = "page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://example.com/";
    const error = await runNavigate(navSession(() => false, [], failure), {
      url: "https://example.com",
    }).catch((e: Error) => e);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).not.toMatch(/^blocked by the browser's egress proxy/);
    expect((error as Error).message).not.toContain("will not help");
    expect((error as Error).message).toContain("ERR_TUNNEL_CONNECTION_FAILED");
  });

  it("retries a refused loopback tunnel when the proxy is not known to be the egress proxy", async () => {
    delete process.env.BROWSER_EGRESS_PROXY;
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    const attempts: string[] = [];
    try {
      const res = await runNavigate(
        navSession((url) => (attempts.push(url), url.includes("172.17.0.5")), [], TUNNEL_REFUSED),
        { url: "https://localhost:5179" },
      );
      expect(attempts).toEqual(["https://localhost:5179", "https://172.17.0.5:5179/"]);
      expect(body(res).rewrote_loopback_to).toBe("https://172.17.0.5:5179/");
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
    }
  });

  it("reports the proxy's marked 403 on the loopback retry as a refusal", async () => {
    // R8-6: the retry's response was discarded, so the proxy's 403 page for
    // the alternative address read as a successful navigation.
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    try {
      const session = navSession(
        (url) => url.includes("172.17.0.5"),
        [],
        REFUSED,
        (url) => (url.includes("172.17.0.5") ? { "x-browser-egress": "blocked" } : {}),
      );
      await expect(runNavigate(session, { url: "http://localhost:5179" })).rejects.toThrow(
        /^blocked by the browser's egress proxy: http:\/\/172\.17\.0\.5:5179\//,
      );
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
    }
  });

  it("reports the egress proxy refusing the loopback retry's tunnel as a refusal", async () => {
    // R8-6: over https the alternative's refusal was reported as the first
    // attempt's "nothing answered".
    process.env.BROWSER_LOOPBACK_HOST = "172.17.0.5";
    process.env.BROWSER_EGRESS_PROXY = "1";
    const attempts: string[] = [];
    try {
      const session = navSession(
        (url) => (attempts.push(url), false),
        [],
        (url) =>
          url.includes("172.17.0.5")
            ? "page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://172.17.0.5:5179/"
            : "page.goto: net::ERR_EMPTY_RESPONSE at https://localhost:5179/",
      );
      await expect(runNavigate(session, { url: "https://localhost:5179" })).rejects.toThrow(
        /^blocked by the browser's egress proxy: https:\/\/172\.17\.0\.5:5179\//,
      );
      expect(attempts).toEqual(["https://localhost:5179", "https://172.17.0.5:5179/"]);
    } finally {
      delete process.env.BROWSER_LOOPBACK_HOST;
      delete process.env.BROWSER_EGRESS_PROXY;
    }
  });

  it("reports the proxy's marked 403 page for plain http as a refusal", async () => {
    await expect(
      runNavigate(navSession(() => true, [], REFUSED, { "x-browser-egress": "blocked" }), {
        url: "http://127.0.0.1:5173/",
      }),
    ).rejects.toThrow(/^blocked by the browser's egress proxy: http:\/\/127\.0\.0\.1:5173\//);
  });

  it("reads BROWSER_EGRESS_PROXY as a flag", () => {
    expect(behindEgressProxy({})).toBe(false);
    expect(behindEgressProxy({ BROWSER_EGRESS_PROXY: "0" })).toBe(false);
    expect(behindEgressProxy({ BROWSER_EGRESS_PROXY: "1" })).toBe(true);
    expect(behindEgressProxy({ BROWSER_EGRESS_PROXY: " TRUE " })).toBe(true);
  });

  it("says a loopback refusal may be the fallback policy", () => {
    const message = egressRefusal("http://127.0.0.1:5173/", "IPv4 loopback: blocked");
    expect(message).toContain("not a server that is down");
    expect(message).toContain("public addresses only");
    expect(message).toContain("Policy detail: IPv4 loopback: blocked");
    expect(egressRefusal("https://example.com/")).not.toContain("public addresses only");
  });
});
