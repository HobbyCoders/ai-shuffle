/**
 * Egress proxy: the network boundary of the supervised browser.
 *
 * The CDP request guard in browser.ts cannot be the boundary on its own. CDP
 * Fetch never sees a WebSocket handshake, and the guard resolves a host name
 * itself while Chromium resolves it again for the real socket, so a hostile
 * DNS server can answer public to the guard and private to the browser.
 *
 * Chromium is therefore launched with `--proxy-server` pointing here, and with
 * `<-loopback>` removed from its implicit bypass list, so every TCP connection
 * it makes, WebSockets and loopback included, arrives at this proxy. Chromium
 * does no DNS of its own for proxied requests. For each connection the proxy
 * resolves the host once, checks EVERY address against the SSRF policy, and
 * connects to exactly the addresses it checked. There is no second resolution
 * to rebind.
 *
 * Fails CLOSED: an unparseable target, a host that does not resolve, a
 * resolver error, or a guard error refuses the connection. It listens on
 * 127.0.0.1 only, on a random port, and denies that port as a destination. It
 * logs host, port and reason only, never paths, queries or headers.
 */

import {
  createServer,
  request as httpRequest,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import { connect as netConnect, isIP, type Socket } from "node:net";
import { spawn, type ChildProcess, type StdioOptions } from "node:child_process";
import { promises as dns } from "node:dns";
import { constants as osConstants } from "node:os";
import type { Duplex } from "node:stream";
import { classifyIp, isMetadataHostname, normalizeHost, type SsrfPolicy } from "./ssrf.js";

/** Resolve a host name to every address it has. Injectable for tests. */
export type EgressResolver = (host: string) => Promise<string[]>;

export const systemEgressResolver: EgressResolver = async (host) =>
  (await dns.lookup(host, { all: true, verbatim: true })).map((r) => r.address);

export type EgressDecision =
  | { allowed: true; addresses: string[] }
  /**
   * `unresolved`: the name has no usable address (not a policy block).
   * `internal`: the proxy itself failed deciding (a guard error); the
   * connection is refused, but it is not a policy block either.
   */
  | { allowed: false; reason: string; unresolved?: boolean; internal?: boolean };

const CONNECT_TIMEOUT_MS = 10_000;

/** Chromium maps these names to loopback without DNS; so do we. */
const LOCALHOST_ADDRESSES = ["127.0.0.1", "::1"];

/** Headers that describe one hop, never forwarded (credentials for us included). */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-connection",
  "proxy-authorization",
  "proxy-authenticate",
  "te",
  "trailer",
  "upgrade",
]);

/**
 * Proxy switches the launcher owns; any caller-supplied copy is dropped.
 * Chromium also accepts a single-dash prefix (and `/` on Windows, where switch
 * names are case-insensitive), so every spelling is matched.
 */
const PROXY_SWITCH = new RegExp(
  `^(?:--?${process.platform === "win32" ? "|/" : ""})(?:proxy-server|proxy-bypass-list|proxy-pac-url|no-proxy-server|proxy-auto-detect|winhttp-proxy-resolver)(?:=|$)`,
  "i",
);

/** A CONNECT authority: `host:port` or `[v6]:port`. No userinfo, path or spaces. */
const AUTHORITY = /^(\[[0-9A-Fa-f:.]+\]|[^\s:/?#@[\]]+):([0-9]{1,5})$/;

/** The Chromium switches that route all of its traffic through the proxy. */
export function chromiumProxyArgs(port: number): string[] {
  return [
    `--proxy-server=http://127.0.0.1:${port}`,
    // Chromium bypasses proxies for loopback implicitly; `<-loopback>`
    // removes that rule so loopback is checked too.
    "--proxy-bypass-list=<-loopback>",
    // WebRTC UDP never passes through an HTTP proxy; forbid non-proxied UDP.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
  ];
}

/**
 * `args` without any switch that would change or disable the proxy, and
 * without a bare `--` (which would turn the launcher's switches, placed after
 * these, into plain arguments).
 */
export function stripProxyArgs(args: string[]): string[] {
  return args.filter((arg) => arg !== "--" && !PROXY_SWITCH.test(arg));
}

/** Parse a CONNECT target. Undefined when it is not a plain host:port. */
export function parseAuthority(target: string): { host: string; port: number } | undefined {
  const m = AUTHORITY.exec(target);
  if (!m) return undefined;
  const port = Number(m[2]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return undefined;
  return { host: m[1], port };
}

/** The policy with `port` (the proxy's own) denied on every internal address. */
export function withDeniedPort(policy: SsrfPolicy, port: number): SsrfPolicy {
  return { ...policy, denyPorts: [...new Set([...(policy.denyPorts ?? []), port])] };
}

/**
 * Decide one connection. Resolves the host once and returns the exact
 * addresses to connect to; refuses when ANY address is blocked, when the name
 * does not resolve, or when anything about the target is malformed.
 */
export async function decideEgress(
  rawHost: string,
  port: number,
  policy: SsrfPolicy,
  resolver: EgressResolver,
): Promise<EgressDecision> {
  const refuse = (reason: string): EgressDecision => ({ allowed: false, reason });
  const host = normalizeHost(rawHost);
  if (!host) return refuse("no host");
  if (!Number.isInteger(port) || port < 1 || port > 65535) return refuse("invalid port");
  if (isMetadataHostname(host)) {
    return refuse(`host "${host}" is a cloud metadata service: always blocked`);
  }
  let addresses: string[];
  if (isIP(host)) {
    addresses = [host];
  } else if (host === "localhost" || host.endsWith(".localhost")) {
    addresses = LOCALHOST_ADDRESSES;
  } else {
    try {
      addresses = await resolver(host);
    } catch {
      return { allowed: false, reason: `${host} did not resolve`, unresolved: true };
    }
  }
  addresses = [...new Set(Array.isArray(addresses) ? addresses : [])];
  if (addresses.length === 0) return { allowed: false, reason: `${host} did not resolve`, unresolved: true };
  for (const addr of addresses) {
    if (typeof addr !== "string" || !isIP(addr)) return refuse(`${host} resolved to a non-address`);
    const verdict = classifyIp(addr, policy, port);
    if (verdict.blocked) {
      return refuse(`${addr === host ? addr : `${host} -> ${addr}`}: ${verdict.reason ?? "blocked"}`);
    }
  }
  return { allowed: true, addresses };
}

/**
 * Connect to the first reachable address of `addresses`, in order. Every
 * address is an IP literal already checked by {@link decideEgress}; the lookup
 * hook refuses outright so no name can be resolved here.
 */
export function connectPinned(
  addresses: string[],
  port: number,
  timeoutMs = CONNECT_TIMEOUT_MS,
): Promise<Socket> {
  const noLookup = (_host: string, _opts: unknown, cb: (err: Error) => void) =>
    cb(new Error("egress proxy never resolves at connect time"));
  return new Promise((resolve, reject) => {
    let index = 0;
    const next = (lastError?: Error): void => {
      if (index >= addresses.length) {
        reject(lastError ?? new Error("no address to connect to"));
        return;
      }
      const address = addresses[index++];
      if (!isIP(address)) {
        next(new Error("not an address"));
        return;
      }
      const socket = netConnect({ host: address, port, lookup: noLookup as never });
      const timer = setTimeout(() => socket.destroy(new Error("connect timeout")), timeoutMs);
      const onError = (err: Error) => {
        clearTimeout(timer);
        socket.destroy();
        next(err);
      };
      socket.once("error", onError);
      socket.once("connect", () => {
        clearTimeout(timer);
        socket.off("error", onError);
        // The caller wires its own teardown on "close"; an error must not throw.
        socket.on("error", () => socket.destroy());
        resolve(socket);
      });
    };
    next();
  });
}

/** Strip control characters so a hostile host name cannot forge log lines. */
function logSafe(text: string): string {
  return text.replace(/[\x00-\x1f\x7f-\x9f]/g, "?").slice(0, 300);
}

/** Pairs of a raw header list, without hop-by-hop headers. */
function forwardable(raw: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i + 1 < raw.length; i += 2) {
    if (!HOP_BY_HOP.has(raw[i].toLowerCase())) out.push(raw[i], raw[i + 1]);
  }
  return out;
}

function splice(a: Duplex, b: Duplex): void {
  const teardown = () => {
    a.destroy();
    b.destroy();
  };
  a.on("error", teardown);
  b.on("error", teardown);
  a.on("close", teardown);
  b.on("close", teardown);
  a.pipe(b);
  b.pipe(a);
}

/**
 * Header on every policy refusal (the CONNECT 403 and the plain-HTTP 403 page),
 * so browser-mcp can say "blocked by the browser's egress proxy" instead of
 * treating the refusal as a server that did not answer. Chromium reports a
 * refused CONNECT as ERR_TUNNEL_CONNECTION_FAILED; a target that is allowed but
 * does not answer (or a name that does not resolve) gets no response at all,
 * which Chromium reports as ERR_EMPTY_RESPONSE, so the two never share a code.
 */
export const EGRESS_REFUSAL_HEADER = "X-Browser-Egress";
export const EGRESS_REFUSAL_VALUE = "blocked";

/**
 * Label for the launcher's policy line. start-chrome.sh sets
 * BROWSER_EGRESS_POLICY_SOURCE=fallback when the app's policy could not be
 * computed and only public addresses are allowed; the log must say so.
 */
export function policyLabel(env: NodeJS.ProcessEnv): string {
  return env.BROWSER_EGRESS_POLICY_SOURCE === "fallback"
    ? "policy (FALLBACK: app policy unavailable, public addresses only)"
    : "policy";
}

function refuseSocket(socket: Duplex, status: number, text: string, blocked = false): void {
  if (socket.destroyed) return;
  const marker = blocked ? `${EGRESS_REFUSAL_HEADER}: ${EGRESS_REFUSAL_VALUE}\r\n` : "";
  socket.end(`HTTP/1.1 ${status} ${text}\r\n${marker}Content-Length: 0\r\nConnection: close\r\n\r\n`);
  setTimeout(() => socket.destroy(), 1000).unref();
}

function plain(res: ServerResponse, status: number, body: string, blocked = false): void {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    connection: "close",
    ...(blocked ? { [EGRESS_REFUSAL_HEADER]: EGRESS_REFUSAL_VALUE } : {}),
  });
  res.end(body);
}

export interface EgressProxyOptions {
  policy: SsrfPolicy;
  resolver?: EgressResolver;
  /** Listen port. 0 (the default) picks a random free port. */
  port?: number;
  log?: (line: string) => void;
  connectTimeoutMs?: number;
}

export interface EgressProxy {
  readonly port: number;
  /** The enforced policy (the given one with the proxy's own port denied). */
  readonly policy: SsrfPolicy;
  readonly server: Server;
  close(): Promise<void>;
}

const BLOCKED_BODY = "Blocked by the browser egress policy.\n";

/** Start the proxy on 127.0.0.1. Resolves once it is listening. */
export async function startEgressProxy(opts: EgressProxyOptions): Promise<EgressProxy> {
  const resolver = opts.resolver ?? systemEgressResolver;
  const log = opts.log ?? ((line: string) => process.stderr.write(`[browser-egress] ${line}\n`));
  const timeoutMs = opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS;
  const sockets = new Set<Duplex>();
  const track = (s: Duplex) => {
    sockets.add(s);
    s.once("close", () => sockets.delete(s));
  };
  let policy = opts.policy;

  const decide = async (host: string, port: number, what: string): Promise<EgressDecision> => {
    let decision: EgressDecision;
    try {
      decision = await decideEgress(host, port, policy, resolver);
    } catch (err) {
      decision = {
        allowed: false,
        reason: `guard error (${err instanceof Error ? err.message : "unknown"})`,
        internal: true,
      };
    }
    if (!decision.allowed) log(logSafe(`refused ${what} ${host}:${port}: ${decision.reason}`));
    return decision;
  };

  const onConnect = async (req: IncomingMessage, client: Duplex, head: Buffer): Promise<void> => {
    client.on("error", () => client.destroy());
    const target = parseAuthority(req.url ?? "");
    if (!target) {
      refuseSocket(client, 400, "Bad Request");
      return;
    }
    const decision = await decide(target.host, target.port, "CONNECT");
    // As for plain HTTP below: a name that does not resolve or a port nobody
    // answers closes the tunnel unanswered (a network error the navigate tool
    // retries); only a policy refusal answers, with the marked 403. A guard
    // error closes unanswered too: Chromium hides a CONNECT reply, so any
    // answer would read as ERR_TUNNEL_CONNECTION_FAILED, i.e. a policy block.
    if (!decision.allowed && (decision.unresolved || decision.internal)) {
      client.destroy();
      return;
    }
    if (!decision.allowed) {
      refuseSocket(client, 403, "Forbidden", true);
      return;
    }
    let upstream: Socket;
    try {
      upstream = await connectPinned(decision.addresses, target.port, timeoutMs);
    } catch {
      client.destroy();
      return;
    }
    track(upstream);
    if (client.destroyed) {
      upstream.destroy();
      return;
    }
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head.length) upstream.write(head);
    splice(client, upstream);
  };

  const onRequest = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    // Only absolute-form plain-HTTP requests; https and WebSockets use CONNECT.
    const raw = req.url ?? "";
    let url: URL;
    try {
      if (!/^http:\/\//i.test(raw)) throw new Error("not absolute-form http");
      url = new URL(raw);
    } catch {
      plain(res, 400, "The browser egress proxy only forwards absolute http:// requests.\n");
      return;
    }
    const port = url.port ? Number(url.port) : 80;
    const decision = await decide(url.hostname, port, "GET-form");
    // A name that does not resolve or a port nobody answers must look like the
    // network error it is (Chromium reports ERR_EMPTY_RESPONSE), not like a
    // page: navigate retries loopback URLs on network errors only.
    if (!decision.allowed && decision.unresolved) {
      req.socket.destroy();
      return;
    }
    // Fail closed on a guard error, but unmarked: it is not the policy.
    if (!decision.allowed && decision.internal) {
      plain(res, 502, "The browser egress proxy failed to check this destination.\n");
      return;
    }
    if (!decision.allowed) {
      plain(res, 403, BLOCKED_BODY, true);
      return;
    }
    let upstream: Socket;
    try {
      upstream = await connectPinned(decision.addresses, port, timeoutMs);
    } catch {
      req.socket.destroy();
      return;
    }
    track(upstream);
    const headers = forwardable(req.rawHeaders);
    if (!headers.some((_, i) => i % 2 === 0 && headers[i].toLowerCase() === "host")) {
      headers.push("Host", url.host);
    }
    headers.push("Connection", "close");
    const outgoing = httpRequest({
      method: req.method,
      path: `${url.pathname}${url.search}`,
      headers: headers as unknown as Record<string, string>,
      setHost: false,
      createConnection: () => upstream,
    });
    outgoing.on("response", (incoming) => {
      res.writeHead(incoming.statusCode ?? 502, incoming.statusMessage, forwardable(incoming.rawHeaders));
      incoming.pipe(res);
      incoming.on("error", () => res.destroy());
    });
    outgoing.on("error", () => plain(res, 502, "The destination closed the connection.\n"));
    res.on("close", () => {
      if (!res.writableFinished) outgoing.destroy();
      upstream.destroy();
    });
    req.pipe(outgoing);
  };

  // requestTimeout 0: a long upload through the proxy is not a slow-loris.
  const server = createServer({ requestTimeout: 0 });
  server.on("connection", track);
  server.on("connect", (req, socket, head) => {
    onConnect(req, socket, head).catch(() => socket.destroy());
  });
  server.on("request", (req, res) => {
    onRequest(req, res).catch(() => res.destroy());
  });
  // Chromium tunnels WebSockets with CONNECT. A plain-HTTP upgrade is refused
  // rather than relayed half-checked.
  server.on("upgrade", (_req, socket) => refuseSocket(socket, 501, "Not Implemented"));
  server.on("clientError", (_err, socket) => socket.destroy());

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(opts.port ?? 0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  const port = address && typeof address === "object" ? address.port : 0;
  policy = withDeniedPort(opts.policy, port);

  return {
    port,
    get policy() {
      return policy;
    },
    server,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        for (const s of sockets) s.destroy();
      }),
  };
}

export interface LaunchOptions extends Omit<EgressProxyOptions, "port"> {
  stdio?: StdioOptions;
  env?: NodeJS.ProcessEnv;
}

export interface LaunchedBrowser {
  proxy: EgressProxy;
  child: ChildProcess;
  /** Resolves with a shell-style exit code (128+signal) once the browser exits. */
  exited: Promise<number>;
}

/**
 * Start the proxy, then the browser with every proxy switch pointing at it.
 * The proxy closes when the browser exits. If the proxy cannot start the
 * browser is never launched.
 */
export async function launchBehindProxy(command: string[], opts: LaunchOptions): Promise<LaunchedBrowser> {
  const [exe, ...args] = command;
  if (!exe) throw new Error("no browser command");
  const proxy = await startEgressProxy(opts);
  // The launcher's switches go last: Chromium keeps the last copy of a switch,
  // so they win over any spelling the filter did not recognise.
  const child = spawn(exe, [...stripProxyArgs(args), ...chromiumProxyArgs(proxy.port)], {
    stdio: opts.stdio ?? ["ignore", "inherit", "inherit"],
    env: opts.env ?? process.env,
  });
  const exited = new Promise<number>((resolve) => {
    let settled = false;
    const finish = (code: number) => {
      if (settled) return;
      settled = true;
      void proxy.close().then(() => resolve(code));
    };
    child.once("error", () => finish(127));
    child.once("exit", (code, signal) =>
      finish(code ?? 128 + (signal ? (osConstants.signals[signal] ?? 1) : 1)),
    );
  });
  // A proxy that fails after startup must not leave the browser without one.
  proxy.server.on("error", () => child.kill("SIGTERM"));
  return { proxy, child, exited };
}
