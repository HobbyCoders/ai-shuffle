/**
 * Security probes for the egress guard, navigation scheme gate, snapshot
 * secret masking and upload input validation. Browser-free: the guard is
 * driven through its URL decision and CDP pause handler with an injected
 * resolver and a fake CDP session.
 */

import { describe, it, expect } from "vitest";
import { SsrfGuard, type FetchSession, type HostResolver } from "../src/browser.js";
import { classifyIp, compilePolicy, isMetadataHostname, type SsrfPolicy } from "../src/ssrf.js";
import { refusedScheme } from "../src/tools/navigate.js";
import { maskSecretValues, MASKED_VALUE } from "../src/snapshot.js";
import { validatePaths } from "../src/tools/upload.js";

const DEFAULT = compilePolicy({} as NodeJS.ProcessEnv);
const LOCAL = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128" } as NodeJS.ProcessEnv);

/** A resolver that must not be consulted (IP literals classify without DNS). */
const noDns: HostResolver = async (host) => {
  throw new Error(`unexpected DNS lookup for ${host}`);
};

const table = (map: Record<string, string[]>): HostResolver => async (host) => {
  const addrs = map[host];
  if (!addrs) throw new Error("ENOTFOUND");
  return addrs;
};

describe("SSRF guard — literal address forms reach the classifier canonicalized", () => {
  const guard = new SsrfGuard(DEFAULT, noDns);

  it.each([
    "http://[::1]/",
    "http://[0:0:0:0:0:0:0:1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::]:8080/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://017700000001/",
    "http://127.1/",
    "http://0.0.0.0/",
    "http://0/",
  ])("blocks loopback / unspecified form %s by default", async (url) => {
    expect((await guard.decide(url)).blocked).toBe(true);
  });

  it.each([
    "http://169.254.169.254/latest/meta-data/",
    "http://2852039166/",
    "http://0xa9fea9fe/",
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:a9fe:a9fe]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[64:ff9b::169.254.169.254]/",
    "http://169.254.0.1/",
    "http://[fd00:ec2::254]/",
  ])("blocks metadata / link-local form %s", async (url) => {
    const v = await guard.decide(url);
    expect(v.blocked).toBe(true);
    expect(v.reason).toMatch(/metadata|link-local/i);
  });

  it("does not mistake public NAT64 destinations for internal ones", async () => {
    expect((await guard.decide("http://[64:ff9b::808:808]/")).blocked).toBe(false);
  });
});

describe("SSRF guard — metadata is never overridable", () => {
  const permissive = compilePolicy({
    BROWSER_SSRF_ALLOW: "fc00::/7,100.64.0.0/10,169.254.0.0/16,168.63.129.16/32,64:ff9b::/96",
  } as NodeJS.ProcessEnv);

  it.each(["fd00:ec2::254", "fd20:ce::254", "100.100.100.200", "168.63.129.16", "169.254.169.254", "64:ff9b::a9fe:a9fe"])(
    "keeps %s closed even when its surrounding range is allow-listed",
    (ip) => {
      const v = classifyIp(ip, permissive);
      expect(v.blocked).toBe(true);
      expect(v.reason).toMatch(/NOT overridable/);
    },
  );

  it("still opens the rest of an allow-listed unique-local / CGNAT range", () => {
    expect(classifyIp("fd00:ec2::1", permissive).blocked).toBe(false);
    expect(classifyIp("100.100.100.100", permissive).blocked).toBe(false);
  });

  it("refuses metadata host names without trusting DNS", async () => {
    // Even if our resolver says "public", the name itself is refused: Chromium
    // resolves separately and would reach the metadata service on GCP.
    const guard = new SsrfGuard(permissive, table({ "metadata.google.internal": ["8.8.8.8"] }));
    for (const url of [
      "http://metadata.google.internal/computeMetadata/v1/",
      "http://METADATA.google.internal./",
      "http://metadata.goog/",
      "http://instance-data.ec2.internal/",
    ]) {
      const v = await guard.decide(url);
      expect(v.blocked, url).toBe(true);
      expect(v.reason, url).toMatch(/metadata/);
    }
    expect(isMetadataHostname("metadata.google.internal.")).toBe(true);
    expect(isMetadataHostname("example.com")).toBe(false);
  });
});

describe("SSRF guard — hostnames are judged by every resolved address", () => {
  it("blocks a name that resolves to a private or metadata address", async () => {
    const guard = new SsrfGuard(
      DEFAULT,
      table({
        "rebind.example": ["93.184.216.34", "10.0.0.7"],
        "meta.example": ["169.254.169.254"],
        "v6loop.example": ["::1"],
        "public.example": ["93.184.216.34", "2606:2800:220:1::1"],
      }),
    );
    expect((await guard.decide("https://rebind.example/")).reason).toMatch(/10\.0\.0\.7/);
    expect((await guard.decide("https://meta.example/")).reason).toMatch(/metadata/);
    expect((await guard.decide("https://v6loop.example/")).blocked).toBe(true);
    expect((await guard.decide("https://public.example/")).blocked).toBe(false);
  });

  it("treats localhost names (including a trailing root dot) as loopback", async () => {
    const guard = new SsrfGuard(DEFAULT, noDns);
    for (const url of ["http://localhost/", "http://LOCALHOST./", "http://app.localhost:3000/"]) {
      expect((await guard.decide(url)).blocked, url).toBe(true);
    }
    const local = new SsrfGuard(LOCAL, noDns);
    expect((await local.decide("http://localhost.:5173/")).blocked).toBe(false);
  });
});

describe("SSRF guard — operator lists and denied ports", () => {
  it("BROWSER_SSRF_BLOCK wins over BROWSER_SSRF_ALLOW, and over public space", async () => {
    const p = compilePolicy({
      BROWSER_SSRF_ALLOW: "127.0.0.0/8,93.184.216.0/24",
      BROWSER_SSRF_BLOCK: "127.0.0.2/32,93.184.216.34/32",
    } as NodeJS.ProcessEnv);
    const guard = new SsrfGuard(p, noDns);
    expect((await guard.decide("http://127.0.0.1/")).blocked).toBe(false);
    expect((await guard.decide("http://127.0.0.2/")).reason).toMatch(/BROWSER_SSRF_BLOCK/);
    expect((await guard.decide("http://93.184.216.34/")).reason).toMatch(/BROWSER_SSRF_BLOCK/);
  });

  it("always denies the DevTools port on loopback, default and configured", async () => {
    const guard = new SsrfGuard(LOCAL, noDns);
    const v = await guard.decide("http://127.0.0.1:9221/json/list");
    expect(v.blocked).toBe(true);
    expect(v.reason).toMatch(/DevTools/);
    expect((await guard.decide("http://127.0.0.1:5173/")).blocked).toBe(false);

    const custom = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8", CHROME_CDP_PORT: "9555" } as NodeJS.ProcessEnv);
    expect(classifyIp("127.0.0.1", custom, 9555).blocked).toBe(true);
    expect(classifyIp("127.0.0.1", custom, 9221).blocked).toBe(false);
  });

  it("keeps worker ports closed on an allow-listed PUBLIC stand-in for loopback", () => {
    // The app allow-lists this host's own routable address (BROWSER_LOOPBACK_HOST),
    // which on a VPS is public: the worker port there is still the worker.
    const p = compilePolicy({
      BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128,203.0.113.9/32",
      BROWSER_SSRF_DENY_PORTS: "8000",
    } as NodeJS.ProcessEnv);
    expect(classifyIp("203.0.113.9", p, 8000).blocked).toBe(true);
    expect(classifyIp("203.0.113.9", p, 5173).blocked).toBe(false);
    // An unrelated public host on the same port is not the worker.
    expect(classifyIp("203.0.113.10", p, 8000).blocked).toBe(false);
  });

  it("blocks [::] by default and does not open it with a ::1 allow", () => {
    expect(classifyIp("::", DEFAULT).blocked).toBe(true);
    expect(classifyIp("::", LOCAL).blocked).toBe(true);
    expect(classifyIp("::", LOCAL).reason).toMatch(/unspecified/);
  });
});

describe("SSRF guard — schemes and failure handling", () => {
  const guard = new SsrfGuard(DEFAULT, noDns);

  it("passes schemes with no network host", async () => {
    for (const url of ["file:///etc/hostname", "data:text/html,hi", "about:blank", "blob:https://a.example/uuid", "chrome://version/"]) {
      expect((await guard.decide(url)).blocked, url).toBe(false);
    }
  });

  it("guards ws/wss URLs by the same rules", async () => {
    expect((await guard.decide("ws://127.0.0.1:8000/ws")).blocked).toBe(true);
    expect((await guard.decide("wss://169.254.169.254/")).blocked).toBe(true);
  });

  it("fails CLOSED when classification throws", async () => {
    const broken = { ...DEFAULT } as SsrfPolicy;
    Object.defineProperty(broken, "block", { get: () => { throw new Error("boom"); } });
    const v = await new SsrfGuard(broken, noDns).decide("http://8.8.8.8/");
    expect(v.blocked).toBe(true);
    expect(v.reason).toMatch(/guard error/);
  });

  it("an unresolvable name is left to fail in the browser", async () => {
    const g = new SsrfGuard(DEFAULT, table({}));
    expect((await g.decide("https://nx.invalid/")).blocked).toBe(false);
  });

  it("the CDP pause handler answers every request: fail on block, continue otherwise", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const cdp: FetchSession = {
      send: async (method: string, params: Record<string, unknown>) => {
        calls.push([method, params]);
        return {};
      },
    } as FetchSession;
    await guard.handlePaused(cdp, { requestId: "r1", request: { url: "http://169.254.169.254/" } });
    await guard.handlePaused(cdp, { requestId: "r2", request: { url: "https://93.184.216.34/" } });
    expect(calls).toEqual([
      ["Fetch.failRequest", { requestId: "r1", errorReason: "BlockedByClient" }],
      ["Fetch.continueRequest", { requestId: "r2" }],
    ]);
    // The block is remembered for navigate's friendly error.
    expect(guard.blockReasonFor("169.254.169.254")).toMatch(/metadata/);
  });

  it("a CDP error while answering does not escape the handler", async () => {
    const cdp = { send: async () => { throw new Error("Invalid InterceptionId"); } } as unknown as FetchSession;
    await expect(guard.handlePaused(cdp, { requestId: "gone", request: { url: "https://93.184.216.34/" } })).resolves.toBeUndefined();
  });
});

describe("browser_navigate scheme gate", () => {
  it.each(["http://a.example/", "https://a.example/", "file:///tmp/x.html", "about:blank", "data:text/html,hi"])(
    "opens %s",
    (url) => expect(refusedScheme(url)).toBeUndefined(),
  );

  it.each([
    "chrome://version/",
    "chrome://settings/passwords",
    "chrome-untrusted://terminal/",
    "devtools://devtools/bundled/inspector.html",
    "chrome-extension://abc/page.html",
    "view-source:file:///etc/passwd",
    "javascript:alert(1)",
    "CHROME://version",
  ])("refuses %s", (url) => {
    expect(refusedScheme(url)).toMatch(/refusing to open/);
  });
});

describe("snapshot password masking", () => {
  const yaml = [
    '- generic [ref=e1] [box=8,8,764,477]:',
    '  - textbox "User" [ref=e3] [box=42,147,185,21]: alice',
    '  - textbox "Pass" [active] [ref=e5] [box=260,147,185,21]: hunter2secret',
    '  - textbox "Tricky" [ref=e6] [box=1,1,1,1]: "p@ss: \\"x\\" #1"',
    '  - text: hunter2secret appears in prose',
  ].join("\n");

  it("masks plain and YAML-quoted password values on textbox lines only", () => {
    const out = maskSecretValues(yaml, ["hunter2secret", 'p@ss: "x" #1']);
    expect(out).not.toMatch(/: hunter2secret$/m);
    expect(out).toContain(`[box=260,147,185,21]: ${MASKED_VALUE}`);
    expect(out).toContain(`[box=1,1,1,1]: ${MASKED_VALUE}`);
    expect(out).not.toContain("p@ss");
    expect(out).toContain("textbox \"User\" [ref=e3] [box=42,147,185,21]: alice");
    // Non-textbox lines are not the field's value; they are left alone.
    expect(out).toContain("- text: hunter2secret appears in prose");
  });

  it("is a no-op without secrets", () => {
    expect(maskSecretValues(yaml, [])).toBe(yaml);
    expect(maskSecretValues(yaml, [""])).toBe(yaml);
  });
});

describe("upload path validation", () => {
  it("rejects a non-array paths value instead of iterating a string", () => {
    const r = validatePaths("/etc/passwd" as unknown as string[], "/tmp", () => ({ isFile: () => true, size: 1 }), (p) => p);
    expect("error" in r && r.error).toMatch(/must be an array/);
  });
});
