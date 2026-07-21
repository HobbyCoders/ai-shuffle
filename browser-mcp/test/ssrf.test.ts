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

describe("describePolicy", () => {
  it("summarizes the effective policy on one line", () => {
    const p = compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.0/8" } as NodeJS.ProcessEnv);
    const s = describePolicy(p);
    expect(s).toContain("always-block=169.254.0.0/16");
    expect(s).toContain("env-allow=127.0.0.0/8");
    expect(s).toContain("RFC1918/public=allow");
  });
});
