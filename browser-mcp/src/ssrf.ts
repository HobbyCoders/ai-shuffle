/**
 * SSRF egress policy — a pure, side-effect-free classifier.
 *
 * The browser agent can be steered (by a hostile page, a redirect, a
 * `browser_eval` fetch) into requesting internal endpoints. The classic prize is
 * the cloud metadata service (169.254.169.254), which hands out instance
 * credentials, and the container's own app / translator-gateway ports on
 * loopback. This module decides, for a single resolved IP, whether a request may
 * proceed. It performs NO I/O — DNS resolution and request interception live in
 * the browser layer; here we only classify.
 *
 * DESIGN (Workstream A6 of the browser suite plan):
 *   - RFC1918 private ranges are ALLOWED by default. Self-hosted / homelab
 *     operators legitimately browse LAN services (10/8, 172.16/12, 192.168/16,
 *     and IPv6 unique-local fc00::/7).
 *   - Cloud metadata (169.254.0.0/16, notably 169.254.169.254) is ALWAYS blocked
 *     and CANNOT be re-enabled through the allow-list — it is the credential
 *     theft vector and there is no legitimate reason for the browser agent to
 *     reach it.
 *   - Loopback (127.0.0.0/8, ::1, "localhost") and other bogon ranges are blocked
 *     by DEFAULT but MAY be re-enabled with an explicit BROWSER_SSRF_ALLOW entry
 *     (an operator opt-in; also how the test harness reaches its own loopback
 *     fixture server).
 *
 * PRECEDENCE (highest wins):
 *   1. Always-block set (cloud metadata 169.254.0.0/16). Non-overridable.
 *   2. BROWSER_SSRF_BLOCK env entries. Win over the allow-list.
 *   3. BROWSER_SSRF_ALLOW env entries. Override the default-block set below.
 *   4. Default-block set (loopback, IPv6 link-local, 0.0.0.0/8).
 *   5. Default allow (RFC1918, fc00::/7, and all public addresses).
 */

export interface ParsedIp {
  version: 4 | 6;
  /** Network-order bytes: length 4 for v4, 16 for v6. */
  bytes: number[];
}

export interface Cidr {
  version: 4 | 6;
  bytes: number[];
  prefix: number;
  /** Original text, for policy logging. */
  text: string;
}

interface LabeledCidr {
  cidr: Cidr;
  label: string;
}

export interface Verdict {
  blocked: boolean;
  reason?: string;
}

export interface SsrfPolicy {
  /** Non-overridable blocks (cloud metadata). */
  alwaysBlock: LabeledCidr[];
  /** Operator extra blocks (BROWSER_SSRF_BLOCK); win over allow. */
  block: LabeledCidr[];
  /** Operator force-allow (BROWSER_SSRF_ALLOW); override default blocks. */
  allow: LabeledCidr[];
  /** Default blocks, overridable by `allow`. */
  defaultBlock: LabeledCidr[];
}

// ---------------------------------------------------------------------------
// IP parsing
// ---------------------------------------------------------------------------

function parseIpv4(s: string): number[] | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s);
  if (!m) return null;
  const parts = [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
  if (parts.some((p) => p > 255)) return null;
  return parts;
}

function parseIpv6(input: string): number[] | null {
  // Strip an RFC 4007 zone id (fe80::1%eth0).
  let s = input.split("%")[0];
  if (s === "") return null;

  // Embedded IPv4 tail (::ffff:1.2.3.4). Convert the dotted quad to two hextets.
  let v4Groups: number[] = [];
  if (s.includes(".")) {
    const colon = s.lastIndexOf(":");
    if (colon === -1) return null;
    const v4 = parseIpv4(s.slice(colon + 1));
    if (!v4) return null;
    v4Groups = [(v4[0] << 8) | v4[1], (v4[2] << 8) | v4[3]];
    s = s.slice(0, colon); // drop ":1.2.3.4"; a trailing ":" or "::" may remain
  }

  const toNums = (arr: string[]): number[] | null => {
    const out: number[] = [];
    for (const g of arr) {
      if (!/^[0-9a-fA-F]{1,4}$/.test(g)) return null;
      out.push(parseInt(g, 16));
    }
    return out;
  };

  const dbl = s.indexOf("::");
  let all16: number[];
  if (dbl !== -1) {
    if (s.indexOf("::", dbl + 1) !== -1) return null; // only one "::" allowed
    const before = s.slice(0, dbl);
    const after = s.slice(dbl + 2);
    const head = before === "" ? [] : before.split(":");
    const tail = after === "" ? [] : after.split(":");
    const h = toNums(head);
    const t = toNums(tail);
    if (h === null || t === null) return null;
    const provided = h.length + t.length + v4Groups.length;
    const zeros = 8 - provided;
    if (zeros < 0) return null;
    all16 = [...h, ...new Array(zeros).fill(0), ...t, ...v4Groups];
  } else {
    const groups = s === "" ? [] : s.split(":");
    const g = toNums(groups);
    if (g === null) return null;
    all16 = [...g, ...v4Groups];
  }
  if (all16.length !== 8) return null;

  const bytes: number[] = [];
  for (const group of all16) bytes.push((group >> 8) & 0xff, group & 0xff);
  return bytes;
}

/** Parse an IP literal to {version, bytes}, or null if it is not an IP. */
export function parseIp(s: string): ParsedIp | null {
  const trimmed = s.trim();
  const v4 = parseIpv4(trimmed);
  if (v4) return { version: 4, bytes: v4 };
  if (trimmed.includes(":")) {
    const v6 = parseIpv6(trimmed);
    if (v6) return { version: 6, bytes: v6 };
  }
  return null;
}

/**
 * If `p` is an IPv4-mapped IPv6 address (::ffff:a.b.c.d), return the equivalent
 * IPv4 ParsedIp so it classifies against the v4 ranges; otherwise return `p`.
 */
function unmap(p: ParsedIp): ParsedIp {
  if (p.version !== 6) return p;
  const b = p.bytes;
  const firstTenZero = b.slice(0, 10).every((x) => x === 0);
  if (firstTenZero && b[10] === 0xff && b[11] === 0xff) {
    return { version: 4, bytes: b.slice(12, 16) };
  }
  return p;
}

// ---------------------------------------------------------------------------
// Host normalization
// ---------------------------------------------------------------------------

/**
 * Normalize a URL host for comparison / literal detection: strip surrounding
 * brackets from an IPv6 literal, drop a zone id, lowercase, trim.
 */
export function normalizeHost(host: string): string {
  let h = host.trim().toLowerCase();
  if (h.startsWith("[") && h.endsWith("]")) h = h.slice(1, -1);
  h = h.split("%")[0];
  return h;
}

/**
 * Canonicalize an IP string: normalize an IPv4-mapped IPv6 literal to its dotted
 * IPv4 form; otherwise return the trimmed input. Non-IP input is returned as-is.
 */
export function normalizeIp(ip: string): string {
  const parsed = parseIp(normalizeHost(ip));
  if (!parsed) return ip;
  const u = unmap(parsed);
  if (u.version === 4) return u.bytes.join(".");
  return ip.trim();
}

/** True when `host` is an IP literal (after bracket/zone stripping). */
export function isIpLiteral(host: string): boolean {
  return parseIp(normalizeHost(host)) !== null;
}

// ---------------------------------------------------------------------------
// CIDR parsing + matching
// ---------------------------------------------------------------------------

/** Parse one "IP" or "IP/prefix" token into a Cidr, or null if malformed. */
export function parseCidr(token: string): Cidr | null {
  const t = token.trim();
  if (t === "") return null;
  const slash = t.indexOf("/");
  const ipPart = slash === -1 ? t : t.slice(0, slash);
  const parsed = parseIp(normalizeHost(ipPart));
  if (!parsed) return null;
  const maxPrefix = parsed.version === 4 ? 32 : 128;
  let prefix = maxPrefix;
  if (slash !== -1) {
    const p = Number(t.slice(slash + 1));
    if (!Number.isInteger(p) || p < 0 || p > maxPrefix) return null;
    prefix = p;
  }
  return { version: parsed.version, bytes: parsed.bytes, prefix, text: t };
}

/**
 * Parse a comma- (or whitespace-) separated list of CIDRs / IPs. Tolerates
 * surrounding whitespace and empty entries; silently drops malformed tokens.
 */
export function parseCidrList(str: string | undefined | null): Cidr[] {
  if (!str) return [];
  const out: Cidr[] = [];
  for (const token of str.split(/[\s,]+/)) {
    if (token === "") continue;
    const c = parseCidr(token);
    if (c) out.push(c);
  }
  return out;
}

/** True when the parsed IP falls inside the CIDR (versions must match). */
export function ipInCidr(ip: ParsedIp, cidr: Cidr): boolean {
  if (ip.version !== cidr.version) return false;
  let bitsLeft = cidr.prefix;
  for (let i = 0; i < ip.bytes.length; i++) {
    if (bitsLeft <= 0) break;
    const take = Math.min(8, bitsLeft);
    const mask = take === 8 ? 0xff : (0xff << (8 - take)) & 0xff;
    if ((ip.bytes[i] & mask) !== (cidr.bytes[i] & mask)) return false;
    bitsLeft -= take;
  }
  return true;
}

function firstMatch(ip: ParsedIp, set: LabeledCidr[]): LabeledCidr | undefined {
  return set.find((lc) => ipInCidr(ip, lc.cidr));
}

// ---------------------------------------------------------------------------
// Policy
// ---------------------------------------------------------------------------

function labeled(text: string, label: string): LabeledCidr {
  const cidr = parseCidr(text);
  if (!cidr) throw new Error(`bad built-in CIDR: ${text}`);
  return { cidr, label };
}

/** Cloud metadata — the credential-theft vector. Never overridable. */
const ALWAYS_BLOCK: LabeledCidr[] = [
  labeled("169.254.0.0/16", "cloud metadata / IPv4 link-local (credential-theft vector)"),
];

/** Blocked by default; an explicit BROWSER_SSRF_ALLOW entry re-enables these. */
const DEFAULT_BLOCK: LabeledCidr[] = [
  labeled("127.0.0.0/8", "IPv4 loopback"),
  labeled("::1/128", "IPv6 loopback"),
  labeled("0.0.0.0/8", "unspecified / this-network"),
  labeled("fe80::/10", "IPv6 link-local"),
];

/**
 * Compile the effective policy from the environment. Reads BROWSER_SSRF_BLOCK
 * (extra blocks) and BROWSER_SSRF_ALLOW (force-allow / default-block override).
 * Pure aside from reading the passed-in env snapshot.
 */
export function compilePolicy(env: NodeJS.ProcessEnv = process.env): SsrfPolicy {
  const block = parseCidrList(env.BROWSER_SSRF_BLOCK).map((cidr) => ({
    cidr,
    label: "BROWSER_SSRF_BLOCK",
  }));
  const allow = parseCidrList(env.BROWSER_SSRF_ALLOW).map((cidr) => ({
    cidr,
    label: "BROWSER_SSRF_ALLOW",
  }));
  return { alwaysBlock: ALWAYS_BLOCK, block, allow, defaultBlock: DEFAULT_BLOCK };
}

/**
 * Classify a single resolved IP string against the policy. Fails CLOSED: an
 * address we cannot parse is blocked.
 */
export function classifyIp(ip: string, policy: SsrfPolicy): Verdict {
  const raw = parseIp(normalizeHost(ip));
  if (!raw) return { blocked: true, reason: `unparseable address "${ip}"` };
  const p = unmap(raw);

  // 1. Always-block (metadata) — cannot be overridden.
  const meta = firstMatch(p, policy.alwaysBlock);
  if (meta) return { blocked: true, reason: `${meta.label} ${meta.cidr.text}` };

  // 2. Operator block list — wins over allow.
  const blocked = firstMatch(p, policy.block);
  if (blocked) return { blocked: true, reason: `matched ${blocked.label} (${blocked.cidr.text})` };

  // 3. Operator allow list — overrides the default-block set.
  if (firstMatch(p, policy.allow)) return { blocked: false };

  // 4. Default-block set.
  const def = firstMatch(p, policy.defaultBlock);
  if (def) return { blocked: true, reason: `${def.label} ${def.cidr.text}` };

  // 5. Default allow (RFC1918, fc00::/7, public).
  return { blocked: false };
}

/** One-line, log-safe summary of the effective policy (for stderr at startup). */
export function describePolicy(policy: SsrfPolicy): string {
  const list = (set: LabeledCidr[]) => set.map((lc) => lc.cidr.text).join(",") || "(none)";
  return [
    `always-block=${list(policy.alwaysBlock)}`,
    `default-block=${list(policy.defaultBlock)}`,
    `env-block=${list(policy.block)}`,
    `env-allow=${list(policy.allow)}`,
    "RFC1918/public=allow",
  ].join(" ");
}
