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
 * DESIGN (Workstream A6 of the browser suite plan, tightened for SEC-12):
 *   - Private network ranges are BLOCKED by default: RFC1918 (10/8, 172.16/12,
 *     192.168/16), carrier-grade NAT 100.64/10 (Tailscale) and IPv6
 *     unique-local fc00::/7. A page the agent visits must not be able to reach
 *     the user's router, NAS or this app's own LAN listener. Operators who
 *     want the agent on specific LAN services list them in BROWSER_SSRF_ALLOW.
 *   - Cloud metadata (169.254.0.0/16, notably 169.254.169.254) is ALWAYS blocked
 *     and CANNOT be re-enabled through the allow-list — it is the credential
 *     theft vector and there is no legitimate reason for the browser agent to
 *     reach it.
 *   - Loopback (127.0.0.0/8, ::1, "localhost") and other bogon ranges are blocked
 *     by DEFAULT but MAY be re-enabled with an explicit BROWSER_SSRF_ALLOW entry
 *     (an operator opt-in; also how the test harness reaches its own loopback
 *     fixture server). The app injects that entry by default in every runtime
 *     (Docker, native and desktop) so an agent can view a dev server it just
 *     started, unless BROWSER_SSRF_ALLOW_LOOPBACK=false — see
 *     ``app/core/mcp_config.py``.
 *
 * Every block reason names its own remedy: a blocked address is a configuration
 * decision, not a network failure, and the agent that hits it should be able to
 * read what would unblock it instead of guessing (or silently giving up).
 *
 * PRECEDENCE (highest wins):
 *   1. Always-block set (cloud metadata 169.254.0.0/16). Non-overridable.
 *   2. BROWSER_SSRF_BLOCK env entries. Win over the allow-list.
 *   3. BROWSER_SSRF_ALLOW env entries. Override the default-block set below.
 *   4. Default-block set (loopback, private ranges, IPv6 link-local, 0.0.0.0/8).
 *   5. Default allow (public addresses).
 *
 * BROWSER_SSRF_DENY_PORTS names ports (the AI Shuffle worker's own listeners)
 * that stay blocked on every non-public address, even one the allow-list opens.
 * Loopback may be allowed for dev servers; the worker itself never is. The
 * browser's own DevTools port (CHROME_CDP_PORT, default 9221) is always added:
 * a page must never read /json/list (every tab's URL) or drive /json/* verbs.
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
  /** Ports refused on any non-public address (BROWSER_SSRF_DENY_PORTS). */
  denyPorts: number[];
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

/** NAT64 well-known prefix 64:ff9b::/96 (RFC 6052). */
const NAT64_PREFIX = [0x00, 0x64, 0xff, 0x9b, 0, 0, 0, 0, 0, 0, 0, 0];

/** NAT64 local-use prefix 64:ff9b:1::/48 (RFC 8215), in its /96 layout. */
const NAT64_LOCAL_PREFIX = [0x00, 0x64, 0xff, 0x9b, 0x00, 0x01, 0, 0, 0, 0, 0, 0];

/** IPv4-translated ::ffff:0:0/96 (RFC 2765 / SIIT). */
const TRANSLATED_PREFIX = [0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 0, 0];

/**
 * If `p` is an IPv6 address that carries an IPv4 destination, return the
 * equivalent IPv4 ParsedIp so it classifies against the v4 ranges; otherwise
 * return `p`. Covered: IPv4-mapped (::ffff:a.b.c.d), IPv4-translated
 * (::ffff:0:a.b.c.d), IPv4-compatible (::a.b.c.d, except :: and ::1, which are
 * the IPv6 unspecified and loopback addresses), NAT64 well-known
 * (64:ff9b::a.b.c.d) and NAT64 local-use in its /96 layout
 * (64:ff9b:1::a.b.c.d). A gateway or SIT route translates each to that host.
 * Other layouts inside 64:ff9b:1::/48 stay IPv6 and are default-blocked.
 */
function unmap(p: ParsedIp): ParsedIp {
  if (p.version !== 6) return p;
  const b = p.bytes;
  const v4 = (): ParsedIp => ({ version: 4, bytes: b.slice(12, 16) });
  const firstTenZero = b.slice(0, 10).every((x) => x === 0);
  if (firstTenZero && b[10] === 0xff && b[11] === 0xff) return v4();
  if (NAT64_PREFIX.every((x, i) => b[i] === x)) return v4();
  if (NAT64_LOCAL_PREFIX.every((x, i) => b[i] === x)) return v4();
  if (TRANSLATED_PREFIX.every((x, i) => b[i] === x)) return v4();
  if (b.slice(0, 12).every((x) => x === 0)) {
    const isUnspecifiedOrLoopback = b.slice(12, 15).every((x) => x === 0) && b[15] <= 1;
    if (!isUnspecifiedOrLoopback) return v4();
  }
  return p;
}

// ---------------------------------------------------------------------------
// Host normalization
// ---------------------------------------------------------------------------

/**
 * Normalize a URL host for comparison / literal detection: strip surrounding
 * brackets from an IPv6 literal, drop a zone id and a trailing root dot
 * ("localhost." names the same host as "localhost"), lowercase, trim.
 */
export function normalizeHost(host: string): string {
  let h = host.trim().toLowerCase();
  if (h.startsWith("[") && h.endsWith("]")) h = h.slice(1, -1);
  h = h.split("%")[0];
  if (h.length > 1 && h.endsWith(".")) h = h.slice(0, -1);
  return h;
}

/**
 * Cloud metadata host NAMES. They resolve to the always-blocked addresses on
 * the clouds that serve them, but the name is refused outright as well: the
 * guard's resolver and Chromium's are separate, so a name must not depend on
 * our lookup agreeing with the browser's.
 */
const METADATA_HOSTNAMES = new Set([
  "metadata.google.internal",
  "metadata.goog",
  "instance-data",
  "instance-data.ec2.internal",
]);

/** True for a hostname that names a cloud metadata service. */
export function isMetadataHostname(host: string): boolean {
  return METADATA_HOSTNAMES.has(normalizeHost(host));
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
  // The IPv6 metadata endpoints sit inside fc00::/7, which an operator may
  // allow for a LAN; these single hosts must stay closed regardless.
  labeled("fd00:ec2::254/128", "cloud metadata (AWS IMDS IPv6, credential-theft vector)"),
  labeled("fd20:ce::254/128", "cloud metadata (GCP IPv6, credential-theft vector)"),
  // Inside 100.64.0.0/10, which a Tailscale user may allow.
  labeled("100.100.100.200/32", "cloud metadata (Alibaba Cloud, credential-theft vector)"),
  labeled("168.63.129.16/32", "cloud platform endpoint (Azure WireServer, credential-theft vector)"),
];

/** Blocked by default; an explicit BROWSER_SSRF_ALLOW entry re-enables these. */
const DEFAULT_BLOCK: LabeledCidr[] = [
  labeled("127.0.0.0/8", "IPv4 loopback"),
  labeled("::1/128", "IPv6 loopback"),
  labeled("0.0.0.0/8", "unspecified / this-network"),
  // A connect() to [::] reaches the local host on Linux, exactly like 0.0.0.0.
  labeled("::/128", "IPv6 unspecified (reaches loopback)"),
  labeled("fe80::/10", "IPv6 link-local"),
  // NAT64 local-use (RFC 8215): a site-local translator; the embedded IPv4
  // address can sit anywhere, so layouts other than /96 are not classified.
  labeled("64:ff9b:1::/48", "NAT64 local-use"),
];

/** Private network ranges; blocked by default, allowed per entry by the operator. */
const PRIVATE_BLOCK: LabeledCidr[] = [
  labeled("10.0.0.0/8", "private network (RFC1918)"),
  labeled("172.16.0.0/12", "private network (RFC1918)"),
  labeled("192.168.0.0/16", "private network (RFC1918)"),
  labeled("100.64.0.0/10", "carrier-grade NAT / Tailscale"),
  labeled("fc00::/7", "IPv6 unique-local"),
];

/**
 * Remedy sentences appended to a block reason. A blocked host is a policy
 * decision an operator can change; saying so turns a dead end into one action.
 */
const REMEDY_DEFAULT_BLOCK =
  "blocked by default policy. Do this instead: bind the server to 0.0.0.0 and " +
  "browse it by IP — run `hostname -I` (or `ipconfig`) in your shell and use that " +
  "address with the same port. To allow this range permanently the operator sets " +
  "BROWSER_SSRF_ALLOW_LOOPBACK=true (or " +
  'BROWSER_SSRF_ALLOW="127.0.0.0/8,::1/128") and starts a new session';

const REMEDY_PRIVATE_BLOCK =
  "blocked by default: the agent's browser does not reach the local network " +
  "unless the operator allows it. To allow a specific LAN service the operator " +
  'sets BROWSER_SSRF_ALLOW (for example BROWSER_SSRF_ALLOW="192.168.1.20/32") ' +
  "and starts a new session";

const REMEDY_DENY_PORT =
  "this is the AI Shuffle worker's own listener (or the browser's DevTools port), " +
  "which the agent's browser may never open; there is no configuration that permits it";

const REMEDY_ALWAYS_BLOCK =
  "always blocked and NOT overridable — BROWSER_SSRF_ALLOW cannot re-enable it; " +
  "there is no configuration that permits this address";

const REMEDY_ENV_BLOCK =
  "blocked by the operator's BROWSER_SSRF_BLOCK setting — remove that entry and " +
  "start a new session to allow it";

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
  // The DevTools port is always denied: CDP over HTTP answers /json/list (every
  // tab's URL, including other sessions') and /json/activate|close to any page.
  const denyPorts = [
    ...new Set(
      `${env.BROWSER_SSRF_DENY_PORTS ?? ""} ${env.CHROME_CDP_PORT ?? "9221"}`
        .split(/[\s,]+/)
        .map((t) => Number(t))
        .filter((n) => Number.isInteger(n) && n > 0 && n <= 65535),
    ),
  ];
  return {
    alwaysBlock: ALWAYS_BLOCK,
    block,
    allow,
    defaultBlock: [...DEFAULT_BLOCK, ...PRIVATE_BLOCK],
    denyPorts,
  };
}

/**
 * Classify a single resolved IP string against the policy. Fails CLOSED: an
 * address we cannot parse is blocked.
 */
export function classifyIp(ip: string, policy: SsrfPolicy, port?: number): Verdict {
  const raw = parseIp(normalizeHost(ip));
  if (!raw) return { blocked: true, reason: `unparseable address "${ip}"` };
  const p = unmap(raw);
  const internal = firstMatch(p, policy.defaultBlock);

  // 1. Always-block (metadata) — cannot be overridden.
  const meta = firstMatch(p, policy.alwaysBlock);
  if (meta) {
    return { blocked: true, reason: `${meta.label} ${meta.cidr.text}: ${REMEDY_ALWAYS_BLOCK}` };
  }

  // 2. Operator block list — wins over allow.
  const blocked = firstMatch(p, policy.block);
  if (blocked) {
    return {
      blocked: true,
      reason: `matched ${blocked.label} (${blocked.cidr.text}): ${REMEDY_ENV_BLOCK}`,
    };
  }

  // 2b. The worker's own ports on any internal or allow-listed address (the
  // app allow-lists this host's own routable address, which may be public).
  // The allow-list cannot open them: local access to the worker is owner access.
  const allowed = firstMatch(p, policy.allow);
  const exposed = internal ?? allowed;
  if (exposed && port !== undefined && (policy.denyPorts ?? []).includes(port)) {
    return { blocked: true, reason: `port ${port} on ${exposed.label}: ${REMEDY_DENY_PORT}` };
  }

  // 3. Operator allow list — overrides the default-block set.
  if (allowed) return { blocked: false };

  // 4. Default-block set.
  if (internal) {
    const remedy = PRIVATE_BLOCK.includes(internal) ? REMEDY_PRIVATE_BLOCK : REMEDY_DEFAULT_BLOCK;
    return { blocked: true, reason: `${internal.label} ${internal.cidr.text}: ${remedy}` };
  }

  // 5. Default allow (public addresses).
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
    `deny-ports=${(policy.denyPorts ?? []).join(",") || "(none)"}`,
    "public=allow",
  ].join(" ");
}
