import { describe, it, expect } from "vitest";
import {
  parseIp,
  parseCidr,
  parseCidrList,
  ipInCidr,
  normalizeHost,
  normalizeIp,
  isIpLiteral,
  classifyIp,
  compilePolicy,
  describePolicy,
} from "../src/ssrf.js";

/** Default policy (no env overrides). */
const DEFAULT = compilePolicy({} as NodeJS.ProcessEnv);

describe("parseIp", () => {
  it("parses IPv4", () => {
    expect(parseIp("127.0.0.1")).toEqual({ version: 4, bytes: [127, 0, 0, 1] });
    expect(parseIp("169.254.169.254")).toEqual({ version: 4, bytes: [169, 254, 169, 254] });
  });

  it("rejects out-of-range and malformed IPv4", () => {
    expect(parseIp("256.0.0.1")).toBeNull();
    expect(parseIp("1.2.3")).toBeNull();
    expect(parseIp("example.com")).toBeNull();
    expect(parseIp("")).toBeNull();
  });

  it("parses IPv6 including :: compression", () => {
    expect(parseIp("::1")?.version).toBe(6);
    expect(parseIp("::1")?.bytes).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]);
    expect(parseIp("fe80::1")?.bytes.slice(0, 2)).toEqual([0xfe, 0x80]);
    expect(parseIp("::")?.bytes).toEqual(new Array(16).fill(0));
    expect(parseIp("2001:db8::ff00:42:8329")?.version).toBe(6);
  });

  it("parses IPv4-mapped IPv6 with embedded dotted quad", () => {
    const m = parseIp("::ffff:127.0.0.1");
    expect(m?.version).toBe(6);
    expect(m?.bytes.slice(10)).toEqual([0xff, 0xff, 127, 0, 0, 1]);
  });

  it("rejects a second '::' and bad hextets", () => {
    expect(parseIp("::1::2")).toBeNull();
    expect(parseIp("gggg::1")).toBeNull();
    expect(parseIp("12345::1")).toBeNull();
  });
});

describe("host / ip normalization", () => {
  it("normalizeHost strips brackets, zone id, lowercases", () => {
    expect(normalizeHost("[::1]")).toBe("::1");
    expect(normalizeHost("FE80::1%eth0")).toBe("fe80::1");
    expect(normalizeHost("  Example.COM ")).toBe("example.com");
  });

  it("normalizeIp collapses IPv4-mapped IPv6 to dotted quad", () => {
    expect(normalizeIp("::ffff:127.0.0.1")).toBe("127.0.0.1");
    expect(normalizeIp("[::ffff:169.254.169.254]")).toBe("169.254.169.254");
    expect(normalizeIp("10.0.0.5")).toBe("10.0.0.5");
  });

  it("isIpLiteral distinguishes literals from hostnames", () => {
    expect(isIpLiteral("127.0.0.1")).toBe(true);
    expect(isIpLiteral("[::1]")).toBe(true);
    expect(isIpLiteral("localhost")).toBe(false);
    expect(isIpLiteral("metadata.google.internal")).toBe(false);
  });
});

describe("parseCidr / parseCidrList", () => {
  it("parses IP and CIDR, defaulting the prefix to the full length", () => {
    expect(parseCidr("10.0.0.0/8")).toMatchObject({ version: 4, prefix: 8 });
    expect(parseCidr("192.168.1.1")).toMatchObject({ version: 4, prefix: 32 });
    expect(parseCidr("fc00::/7")).toMatchObject({ version: 6, prefix: 7 });
    expect(parseCidr("::1")).toMatchObject({ version: 6, prefix: 128 });
  });

  it("rejects malformed CIDRs and out-of-range prefixes", () => {
    expect(parseCidr("10.0.0.0/33")).toBeNull();
    expect(parseCidr("fc00::/129")).toBeNull();
    expect(parseCidr("not-an-ip/8")).toBeNull();
    expect(parseCidr("10.0.0.0/-1")).toBeNull();
    expect(parseCidr("")).toBeNull();
  });

  it("tolerates whitespace, commas, empties, and drops junk", () => {
    const list = parseCidrList(" 10.0.0.0/8 ,, 192.168.0.0/16 ,garbage, ::1 ");
    expect(list.map((c) => c.text)).toEqual(["10.0.0.0/8", "192.168.0.0/16", "::1"]);
    expect(parseCidrList(undefined)).toEqual([]);
    expect(parseCidrList("")).toEqual([]);
  });
});

describe("ipInCidr", () => {
  it("matches within and rejects outside the range", () => {
    const c = parseCidr("172.16.0.0/12")!;
    expect(ipInCidr(parseIp("172.16.5.5")!, c)).toBe(true);
    expect(ipInCidr(parseIp("172.31.255.255")!, c)).toBe(true);
    expect(ipInCidr(parseIp("172.32.0.1")!, c)).toBe(false);
    expect(ipInCidr(parseIp("10.0.0.1")!, c)).toBe(false);
  });

  it("does not cross IP versions", () => {
    const c = parseCidr("127.0.0.0/8")!;
    expect(ipInCidr(parseIp("::1")!, c)).toBe(false);
  });

  it("handles a /0 that matches everything of its version", () => {
    const c = parseCidr("0.0.0.0/0")!;
    expect(ipInCidr(parseIp("8.8.8.8")!, c)).toBe(true);
  });
});

describe("classifyIp — default policy", () => {
  it("BLOCKS cloud metadata (credential-theft vector)", () => {
    expect(classifyIp("169.254.169.254", DEFAULT).blocked).toBe(true);
    expect(classifyIp("169.254.0.1", DEFAULT).blocked).toBe(true);
    expect(classifyIp("169.254.169.254", DEFAULT).reason).toMatch(/metadata/i);
  });

  it("BLOCKS loopback, including ::ffff:127.0.0.1 and ::1", () => {
    expect(classifyIp("127.0.0.1", DEFAULT).blocked).toBe(true);
    expect(classifyIp("127.1.2.3", DEFAULT).blocked).toBe(true);
    expect(classifyIp("::1", DEFAULT).blocked).toBe(true);
    // IPv4-mapped loopback must normalize before classification.
    expect(classifyIp("::ffff:127.0.0.1", DEFAULT).blocked).toBe(true);
  });

  it("BLOCKS 0.0.0.0/8 and IPv6 link-local by default", () => {
    expect(classifyIp("0.0.0.0", DEFAULT).blocked).toBe(true);
    expect(classifyIp("fe80::1", DEFAULT).blocked).toBe(true);
  });

  it("ALLOWS RFC1918 private ranges by default", () => {
    expect(classifyIp("10.0.0.5", DEFAULT).blocked).toBe(false);
    expect(classifyIp("172.16.9.9", DEFAULT).blocked).toBe(false);
    expect(classifyIp("192.168.1.10", DEFAULT).blocked).toBe(false);
    expect(classifyIp("fc00::1234", DEFAULT).blocked).toBe(false); // IPv6 unique-local
  });

  it("ALLOWS public addresses", () => {
    expect(classifyIp("8.8.8.8", DEFAULT).blocked).toBe(false);
    expect(classifyIp("2606:4700:4700::1111", DEFAULT).blocked).toBe(false);
  });

  it("fails closed on an unparseable address", () => {
    expect(classifyIp("not-an-ip", DEFAULT).blocked).toBe(true);
  });
});

describe("classifyIp — env overrides & precedence", () => {
  it("BROWSER_SSRF_BLOCK adds a block for an otherwise-allowed range", () => {
    const p = compilePolicy({ BROWSER_SSRF_BLOCK: "10.0.0.0/8" } as NodeJS.ProcessEnv);
    expect(classifyIp("10.1.2.3", p).blocked).toBe(true);
    expect(classifyIp("10.1.2.3", p).reason).toMatch(/BROWSER_SSRF_BLOCK/);
    // A public IP is still allowed.
    expect(classifyIp("8.8.8.8", p).blocked).toBe(false);
  });

  it("BROWSER_SSRF_ALLOW re-enables an otherwise default-blocked range (loopback)", () => {
    const p = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8" } as NodeJS.ProcessEnv);
    expect(classifyIp("127.0.0.1", p).blocked).toBe(false);
    // A loopback allow does not leak to metadata.
    expect(classifyIp("169.254.169.254", p).blocked).toBe(true);
  });

  it("metadata stays BLOCKED even when explicitly listed in BROWSER_SSRF_ALLOW", () => {
    const p = compilePolicy({
      BROWSER_SSRF_ALLOW: "169.254.0.0/16,169.254.169.254",
    } as NodeJS.ProcessEnv);
    expect(classifyIp("169.254.169.254", p).blocked).toBe(true);
    expect(classifyIp("169.254.169.254", p).reason).toMatch(/metadata/i);
  });

  it("BROWSER_SSRF_BLOCK wins over BROWSER_SSRF_ALLOW for the same range", () => {
    const p = compilePolicy({
      BROWSER_SSRF_BLOCK: "192.168.0.0/16",
      BROWSER_SSRF_ALLOW: "192.168.0.0/16",
    } as NodeJS.ProcessEnv);
    expect(classifyIp("192.168.1.1", p).blocked).toBe(true);
  });
});

describe("block reasons name their remedy", () => {
  it("a default-block reason leads with the workaround, then the settings", () => {
    const v = classifyIp("127.0.0.1", DEFAULT);
    expect(v.blocked).toBe(true);
    expect(v.reason).toMatch(/IPv4 loopback/);
    expect(v.reason).toContain("BROWSER_SSRF_ALLOW");
    expect(v.reason).toContain("BROWSER_SSRF_ALLOW_LOOPBACK");
    expect(v.reason).toMatch(/0\.0\.0\.0/);
    expect(v.reason).toMatch(/hostname -I/);
    // One session read this reason, found the remedy buried at the end and gave
    // up after a single attempt: the action must come before the settings.
    expect(v.reason!.indexOf("0.0.0.0")).toBeLessThan(v.reason!.indexOf("BROWSER_SSRF_ALLOW"));
  });

  it("the metadata reason says plainly that no setting re-enables it", () => {
    const v = classifyIp("169.254.169.254", DEFAULT);
    expect(v.blocked).toBe(true);
    expect(v.reason).toMatch(/NOT overridable/);
    // It must not dangle BROWSER_SSRF_ALLOW as if it were a way in.
    expect(v.reason).toMatch(/cannot re-enable/);
  });

  it("an operator BROWSER_SSRF_BLOCK reason points at that setting", () => {
    const p = compilePolicy({ BROWSER_SSRF_BLOCK: "10.0.0.0/8" } as NodeJS.ProcessEnv);
    const v = classifyIp("10.1.2.3", p);
    expect(v.reason).toContain("BROWSER_SSRF_BLOCK");
    expect(v.reason).toMatch(/remove that entry/);
  });

  it("an allowed loopback carries no reason at all", () => {
    const p = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128" } as NodeJS.ProcessEnv);
    expect(classifyIp("127.0.0.1", p)).toEqual({ blocked: false });
    expect(classifyIp("::1", p)).toEqual({ blocked: false });
  });
});

describe("local-mode loopback default (as injected by app/core/mcp_config.py)", () => {
  // The app injects BROWSER_SSRF_ALLOW="127.0.0.0/8,::1/128" on local/self-hosted
  // installs. These assert the server honours that exact string end to end.
  const LOCAL = compilePolicy({
    BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128",
  } as NodeJS.ProcessEnv);

  it("allows the whole loopback range, v4 and v6", () => {
    expect(classifyIp("127.0.0.1", LOCAL).blocked).toBe(false);
    expect(classifyIp("127.99.1.2", LOCAL).blocked).toBe(false);
    expect(classifyIp("::1", LOCAL).blocked).toBe(false);
    // IPv4-mapped loopback resolves through the same rule.
    expect(classifyIp("::ffff:127.0.0.1", LOCAL).blocked).toBe(false);
  });

  it("still blocks metadata, link-local and 0.0.0.0/8", () => {
    expect(classifyIp("169.254.169.254", LOCAL).blocked).toBe(true);
    expect(classifyIp("fe80::1", LOCAL).blocked).toBe(true);
    expect(classifyIp("0.0.0.0", LOCAL).blocked).toBe(true);
  });

  it("an operator's explicit allow-list is used verbatim, not merged with loopback", () => {
    // mcp_config passes an explicit BROWSER_SSRF_ALLOW straight through; the
    // server must not quietly widen it.
    const explicit = compilePolicy({
      BROWSER_SSRF_ALLOW: "127.0.0.1/32",
    } as NodeJS.ProcessEnv);
    expect(classifyIp("127.0.0.1", explicit).blocked).toBe(false);
    expect(classifyIp("127.0.0.2", explicit).blocked).toBe(true);
    expect(classifyIp("::1", explicit).blocked).toBe(true);
  });

  it("BROWSER_SSRF_BLOCK still overrides the injected loopback allow", () => {
    const p = compilePolicy({
      BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128",
      BROWSER_SSRF_BLOCK: "127.0.0.0/8",
    } as NodeJS.ProcessEnv);
    expect(classifyIp("127.0.0.1", p).blocked).toBe(true);
  });
});

describe("describePolicy", () => {
  it("summarizes the effective policy on one line", () => {
    const p = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8" } as NodeJS.ProcessEnv);
    const s = describePolicy(p);
    expect(s).toContain("always-block=169.254.0.0/16");
    expect(s).toContain("env-allow=127.0.0.0/8");
    expect(s).toContain("RFC1918/public=allow");
  });
});
