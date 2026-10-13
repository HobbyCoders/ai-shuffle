/**
 * Egress proxy: policy decisions (pure, injected resolver) and the proxy's
 * behavior over real loopback sockets. Browser-free; the Chromium end-to-end
 * checks are in egress.browser.test.ts.
 */

import { describe, it, expect, afterEach } from "vitest";
import { connect, type Socket } from "node:net";
import { createServer as createHttpServer, request, type IncomingHttpHeaders, type Server as HttpServer } from "node:http";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  chromiumProxyArgs,
  connectPinned,
  decideEgress,
  launchBehindProxy,
  parseAuthority,
  policyLabel,
  startEgressProxy,
  stripProxyArgs,
  withDeniedPort,
  type EgressProxy,
  type EgressResolver,
} from "../src/egress.js";
import { compilePolicy } from "../src/ssrf.js";

const env = (e: Record<string, string>) => e as NodeJS.ProcessEnv;
const DEFAULT = compilePolicy(env({}));
const LOOPBACK = compilePolicy(env({ BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128", BROWSER_SSRF_DENY_PORTS: "8000" }));
/** 127.0.0.1 stands in for a public address; 127.0.0.2 for an internal one. */
const ONLY_ONE = compilePolicy(env({ BROWSER_SSRF_ALLOW: "127.0.0.1/32" }));

const noDns: EgressResolver = async (host) => {
  throw new Error(`unexpected lookup of ${host}`);
};
const table = (map: Record<string, string[]>): EgressResolver => async (host) => {
  if (!(host in map)) throw new Error("ENOTFOUND");
  return map[host];
};

describe("decideEgress", () => {
  it("classifies IP literals without DNS", async () => {
    expect((await decideEgress("127.0.0.1", 80, DEFAULT, noDns)).allowed).toBe(false);
    expect((await decideEgress("[::1]", 80, DEFAULT, noDns)).allowed).toBe(false);
    expect(await decideEgress("[::1]", 80, LOOPBACK, noDns)).toEqual({ allowed: true, addresses: ["::1"] });
    expect(await decideEgress("93.184.216.34", 443, DEFAULT, noDns)).toEqual({
      allowed: true,
      addresses: ["93.184.216.34"],
    });
  });

  it.each(["[::]", "[fd00:ec2::254]", "[64:ff9b::a9fe:a9fe]", "[::ffff:a9fe:a9fe]", "169.254.169.254", "0.0.0.0"])(
    "refuses %s even with private ranges allowed",
    async (host) => {
      const open = compilePolicy(env({ BROWSER_SSRF_ALLOW: "127.0.0.0/8,fc00::/7" }));
      const d = await decideEgress(host, 80, open, noDns);
      expect(d.allowed, host).toBe(false);
    },
  );

  it("refuses metadata host names without resolving them", async () => {
    const d = await decideEgress("metadata.google.internal.", 80, DEFAULT, noDns);
    expect(d).toMatchObject({ allowed: false, reason: expect.stringMatching(/metadata/) });
  });

  it("refuses when ANY resolved address is blocked, and returns exactly the checked addresses", async () => {
    const r = table({ "multi.test": ["93.184.216.34", "10.0.0.5"], "ok.test": ["93.184.216.34", "2606:2800:220:1::1", "93.184.216.34"] });
    expect(await decideEgress("multi.test", 80, DEFAULT, r)).toMatchObject({
      allowed: false,
      reason: expect.stringMatching(/multi\.test -> 10\.0\.0\.5/),
    });
    expect(await decideEgress("ok.test", 80, DEFAULT, r)).toEqual({
      allowed: true,
      addresses: ["93.184.216.34", "2606:2800:220:1::1"],
    });
  });

  it("fails closed on resolver failure, an empty answer, or a non-address answer", async () => {
    for (const r of [table({}), table({ "x.test": [] }), table({ "x.test": ["evil.example"] })]) {
      expect((await decideEgress("x.test", 80, DEFAULT, r)).allowed).toBe(false);
    }
    const garbage = (async () => "127.0.0.1") as unknown as EgressResolver;
    expect((await decideEgress("x.test", 80, DEFAULT, garbage)).allowed).toBe(false);
  });

  it("maps localhost to both loopback addresses and checks each", async () => {
    expect(await decideEgress("localhost", 5173, LOOPBACK, noDns)).toEqual({
      allowed: true,
      addresses: ["127.0.0.1", "::1"],
    });
    expect((await decideEgress("app.localhost", 5173, ONLY_ONE, noDns)).allowed).toBe(false);
  });

  it("keeps deny ports and the DevTools port closed on allowed addresses", async () => {
    expect((await decideEgress("127.0.0.1", 8000, LOOPBACK, noDns)).allowed).toBe(false);
    expect((await decideEgress("127.0.0.1", 9221, LOOPBACK, noDns)).allowed).toBe(false);
    expect((await decideEgress("127.0.0.1", 5173, LOOPBACK, noDns)).allowed).toBe(true);
  });

  it("refuses an empty host or an out-of-range port", async () => {
    expect((await decideEgress("", 80, DEFAULT, noDns)).allowed).toBe(false);
    expect((await decideEgress("93.184.216.34", 0, DEFAULT, noDns)).allowed).toBe(false);
    expect((await decideEgress("93.184.216.34", 70000, DEFAULT, noDns)).allowed).toBe(false);
  });
});

describe("parseAuthority and launch switches", () => {
  it("accepts host:port and [v6]:port only", () => {
    expect(parseAuthority("example.com:443")).toEqual({ host: "example.com", port: 443 });
    expect(parseAuthority("[::1]:8080")).toEqual({ host: "[::1]", port: 8080 });
    for (const bad of ["example.com", "user:pw@example.com:443", "example.com:99999", "example.com:443/x", " example.com:1", "[::1]", "a b:1"]) {
      expect(parseAuthority(bad), bad).toBeUndefined();
    }
  });

  it("routes loopback and WebRTC through the proxy and drops caller proxy switches", () => {
    expect(chromiumProxyArgs(4321)).toEqual([
      "--proxy-server=http://127.0.0.1:4321",
      "--proxy-bypass-list=<-loopback>",
      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]);
    expect(
      stripProxyArgs(["--headless=new", "--proxy-server=socks5://x:1", "--no-proxy-server", "--proxy-bypass-list=*", "--proxy-pac-url=http://x/p", "--proxy-auto-detect", "about:blank"]),
    ).toEqual(["--headless=new", "about:blank"]);
  });

  it("drops single-dash and mixed-case proxy switches and a bare `--`", () => {
    expect(
      stripProxyArgs(["-no-proxy-server", "-proxy-server=direct://", "-Proxy-Bypass-List=*", "--NO-PROXY-SERVER", "--", "--headless=new"]),
    ).toEqual(["--headless=new"]);
  });
});

// ---------------------------------------------------------------------------
// The proxy over real sockets.

interface Target {
  server: HttpServer;
  port: number;
  hits: { method: string; url: string; headers: IncomingHttpHeaders }[];
  connections: number;
}

function startTarget(host: string, port = 0): Promise<Target> {
  const target: Target = { server: createHttpServer(), port: 0, hits: [], connections: 0 };
  target.server.on("connection", () => target.connections++);
  target.server.on("request", (req, res) => {
    target.hits.push({ method: req.method ?? "", url: req.url ?? "", headers: req.headers });
    res.writeHead(200, { "content-type": "text/plain" });
    res.end(`from ${host}`);
  });
  return new Promise((resolve, reject) => {
    target.server.once("error", reject);
    target.server.listen(port, host, () => {
      target.port = (target.server.address() as { port: number }).port;
      resolve(target);
    });
  });
}

/**
 * Send a CONNECT; resolve with the status code, the response head and the open
 * socket. A proxy that closes without answering resolves with status 0.
 */
function tunnel(proxyPort: number, authority: string): Promise<{ status: number; head: string; socket: Socket }> {
  return new Promise((resolve, reject) => {
    const socket = connect(proxyPort, "127.0.0.1", () => {
      socket.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n\r\n`);
    });
    let buf = "";
    const onData = (chunk: Buffer) => {
      buf += chunk.toString("latin1");
      const end = buf.indexOf("\r\n\r\n");
      if (end === -1) return;
      socket.off("data", onData);
      resolve({ status: Number(buf.split(" ")[1]), head: buf.slice(0, end), socket });
    };
    socket.on("data", onData);
    socket.on("close", () => resolve({ status: 0, head: buf, socket }));
    socket.on("error", reject);
  });
}

/** An absolute-form request through the proxy. */
function viaProxy(
  proxyPort: number,
  url: string,
  headers: Record<string, string> = {},
): Promise<{ status: number; body: string; headers: IncomingHttpHeaders }> {
  return new Promise((resolve, reject) => {
    const req = request({ host: "127.0.0.1", port: proxyPort, method: "GET", path: url, headers }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body, headers: res.headers }));
    });
    req.on("error", reject);
    req.end();
  });
}

const closers: (() => Promise<unknown> | void)[] = [];
afterEach(async () => {
  while (closers.length) await closers.pop()!();
});

async function proxyWith(policy = ONLY_ONE, resolver: EgressResolver = noDns, log?: (l: string) => void): Promise<EgressProxy> {
  const proxy = await startEgressProxy({ policy, resolver, log: log ?? (() => {}) });
  closers.push(() => proxy.close());
  return proxy;
}

async function target(host: string, port = 0): Promise<Target> {
  const t = await startTarget(host, port);
  closers.push(
    () =>
      new Promise<void>((r) => {
        t.server.closeAllConnections();
        t.server.close(() => r());
      }),
  );
  return t;
}

describe("egress proxy", () => {
  it("tunnels CONNECT to an allowed address and refuses a blocked one before connecting", async () => {
    const allowed = await target("127.0.0.1");
    const blocked = await target("127.0.0.2", allowed.port);
    const proxy = await proxyWith();

    const ok = await tunnel(proxy.port, `127.0.0.1:${allowed.port}`);
    expect(ok.status).toBe(200);
    ok.socket.write(`GET /tunneled HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n`);
    const reply = await new Promise<string>((r) => {
      let s = "";
      ok.socket.on("data", (c) => (s += c));
      ok.socket.on("close", () => r(s));
    });
    expect(reply).toMatch(/^HTTP\/1\.1 200[\s\S]*from 127\.0\.0\.1/);

    const refused = await tunnel(proxy.port, `127.0.0.2:${allowed.port}`);
    expect(refused.status).toBe(403);
    refused.socket.destroy();
    expect(blocked.connections).toBe(0);
  });

  it("refuses its own port as a destination", async () => {
    const proxy = await proxyWith(LOOPBACK);
    expect(proxy.policy.denyPorts).toContain(proxy.port);
    const self = await tunnel(proxy.port, `127.0.0.1:${proxy.port}`);
    expect(self.status).toBe(403);
    self.socket.destroy();
  });

  it("DNS rebinding: resolves once per connection and connects to exactly the address it checked", async () => {
    const pub = await target("127.0.0.1");
    const internal = await target("127.0.0.2", pub.port);
    const answers = [["127.0.0.1"], ["127.0.0.2"]];
    const lookups: string[] = [];
    const rebinding: EgressResolver = async (host) => {
      lookups.push(host);
      return answers[Math.min(lookups.length - 1, answers.length - 1)];
    };
    const proxy = await proxyWith(ONLY_ONE, rebinding);

    // First connection: the answer is "public"; the proxy connects to it and
    // never asks again, so a flip after the check cannot redirect this socket.
    const first = await tunnel(proxy.port, `rebind.test:${pub.port}`);
    expect(first.status).toBe(200);
    expect(lookups).toEqual(["rebind.test"]);
    first.socket.write(`GET / HTTP/1.1\r\nHost: rebind.test\r\nConnection: close\r\n\r\n`);
    await new Promise((r) => first.socket.on("close", r));
    expect(pub.hits.map((h) => h.url)).toEqual(["/"]);

    // Second connection: the name now answers "private" and is refused.
    const second = await tunnel(proxy.port, `rebind.test:${pub.port}`);
    expect(second.status).toBe(403);
    second.socket.destroy();
    const plainHttp = await viaProxy(proxy.port, `http://rebind.test:${pub.port}/secret`);
    expect(plainHttp.status).toBe(403);
    expect(lookups).toHaveLength(3);
    expect(internal.connections).toBe(0);
  });

  it("forwards absolute-form HTTP with Host kept and proxy credentials dropped", async () => {
    const t = await target("127.0.0.1");
    const proxy = await proxyWith();
    const res = await viaProxy(proxy.port, `http://127.0.0.1:${t.port}/p?q=1`, {
      host: `app.test:${t.port}`,
      "proxy-authorization": "Basic c2VjcmV0",
      "x-kept": "yes",
    });
    expect({ status: res.status, body: res.body }).toEqual({ status: 200, body: "from 127.0.0.1" });
    expect(t.hits).toHaveLength(1);
    expect(t.hits[0].url).toBe("/p?q=1");
    expect(t.hits[0].headers.host).toBe(`app.test:${t.port}`);
    expect(t.hits[0].headers["x-kept"]).toBe("yes");
    expect(t.hits[0].headers["proxy-authorization"]).toBeUndefined();
  });

  it("refuses origin-form requests, plain-HTTP upgrades and blocked hosts; logs no path, query or credentials", async () => {
    const internal = await target("127.0.0.2");
    const lines: string[] = [];
    const proxy = await proxyWith(ONLY_ONE, noDns, (l) => lines.push(l));

    expect((await viaProxy(proxy.port, "/direct")).status).toBe(400);
    const blocked = await viaProxy(
      proxy.port,
      `http://user:hunter2@127.0.0.2:${internal.port}/private-path?token=abc123`,
      { authorization: "Bearer sekret" },
    );
    expect(blocked.status).toBe(403);
    expect(blocked.body).toMatch(/egress policy/);
    expect(internal.connections).toBe(0);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/127\.0\.0\.2/);
    expect(lines[0]).not.toMatch(/hunter2|private-path|token|abc123|sekret|user/);

    const upgrade = await new Promise<number>((resolve, reject) => {
      const req = request({
        host: "127.0.0.1",
        port: proxy.port,
        path: `http://127.0.0.1:${internal.port}/ws`,
        headers: { connection: "Upgrade", upgrade: "websocket" },
      });
      req.on("response", (res) => resolve(res.statusCode ?? 0));
      req.on("upgrade", () => reject(new Error("upgraded")));
      req.on("error", reject);
      req.end();
    });
    expect(upgrade).toBe(501);
  });

  it("fails closed when the resolver throws or the name is unknown", async () => {
    const t = await target("127.0.0.1");
    const proxy = await proxyWith(ONLY_ONE, async () => {
      throw new Error("SERVFAIL");
    });
    // An unresolvable name is a network error (closed, no tunnel), like the
    // absolute-form path: only a policy refusal answers with 403.
    const r = await tunnel(proxy.port, `anything.test:${t.port}`);
    expect(r.status).toBe(0);
    r.socket.destroy();
    expect(t.connections).toBe(0);
  });

  it("marks policy refusals so they are never mistaken for a server that is down", async () => {
    const internal = await target("127.0.0.2");
    const closed = await target("127.0.0.1");
    const closedPort = closed.port;
    await new Promise<void>((r) => closed.server.close(() => r()));
    const proxy = await proxyWith(ONLY_ONE, table({}));

    // Refused by policy: 403 plus the marker (Chromium: ERR_TUNNEL_CONNECTION_FAILED).
    const refused = await tunnel(proxy.port, `127.0.0.2:${internal.port}`);
    expect(refused.status).toBe(403);
    expect(refused.head).toMatch(/^X-Browser-Egress: blocked$/im);
    refused.socket.destroy();
    const page = await viaProxy(proxy.port, `http://127.0.0.2:${internal.port}/`);
    expect(page.status).toBe(403);
    expect(page.headers["x-browser-egress"]).toBe("blocked");

    // Allowed but nobody answers, or the name does not resolve: the tunnel is
    // closed without a response (Chromium: ERR_EMPTY_RESPONSE, retryable).
    const down = await tunnel(proxy.port, `127.0.0.1:${closedPort}`);
    expect(down.status).toBe(0);
    down.socket.destroy();
    const unknown = await tunnel(proxy.port, `nowhere.test:443`);
    expect(unknown.status).toBe(0);
    unknown.socket.destroy();
    expect(internal.connections).toBe(0);
  });

  it("fails closed on a guard error without the policy marker", async () => {
    const t = await target("127.0.0.1");
    // A malformed policy makes the decision itself throw: an internal failure.
    const broken = { ...ONLY_ONE, defaultBlock: null } as unknown as typeof ONLY_ONE;
    const lines: string[] = [];
    const proxy = await proxyWith(broken, noDns, (l) => lines.push(l));

    // CONNECT: closed unanswered, never a (marked or unmarked) refusal that
    // Chromium would surface as ERR_TUNNEL_CONNECTION_FAILED.
    const r = await tunnel(proxy.port, `127.0.0.1:${t.port}`);
    expect(r.status).toBe(0);
    expect(r.head).not.toMatch(/X-Browser-Egress/i);
    r.socket.destroy();

    // Plain HTTP: an explanatory 502 page, unmarked.
    const page = await viaProxy(proxy.port, `http://127.0.0.1:${t.port}/`);
    expect(page.status).toBe(502);
    expect(page.headers["x-browser-egress"]).toBeUndefined();

    expect(t.connections).toBe(0);
    expect(lines.some((l) => l.includes("guard error"))).toBe(true);
  });

  it("turns an unresolvable name or a closed port into a network error, not a page", async () => {
    const closed = await target("127.0.0.1");
    const closedPort = closed.port;
    await new Promise<void>((r) => closed.server.close(() => r()));
    const proxy = await proxyWith(ONLY_ONE, table({}));
    await expect(viaProxy(proxy.port, "http://nowhere.test/")).rejects.toThrow();
    await expect(viaProxy(proxy.port, `http://127.0.0.1:${closedPort}/`)).rejects.toThrow();
    // A policy refusal stays an explanatory page.
    expect((await viaProxy(proxy.port, `http://127.0.0.2:${closedPort}/`)).status).toBe(403);
  });

  it("reaches an allowed IPv6 loopback target", async () => {
    let t: Target;
    try {
      t = await target("::1");
    } catch {
      return; // no IPv6 loopback on this host
    }
    const proxy = await proxyWith(LOOPBACK);
    const r = await tunnel(proxy.port, `[::1]:${t.port}`);
    expect(r.status).toBe(200);
    r.socket.destroy();
    expect((await viaProxy(proxy.port, `http://[::1]:${t.port}/v6`)).body).toBe("from ::1");
  });
});

describe("launcher policy line", () => {
  it("labels the start-chrome fallback policy", () => {
    expect(policyLabel(env({ BROWSER_EGRESS_POLICY_SOURCE: "fallback" }))).toMatch(/FALLBACK.*public addresses only/);
    expect(policyLabel(env({ BROWSER_EGRESS_POLICY_SOURCE: "app" }))).toBe("policy");
    expect(policyLabel(env({}))).toBe("policy");
  });
});

describe.skipIf(process.platform === "win32")("launchBehindProxy", () => {
  it("passes the proxy switches, drops the caller's, propagates the exit code and closes the proxy", async () => {
    const dir = mkdtempSync(join(tmpdir(), "egress-launch-"));
    closers.push(() => rmSync(dir, { recursive: true, force: true }));
    const out = join(dir, "argv");
    const fake = join(dir, "fake-browser");
    writeFileSync(fake, `#!/bin/sh\nprintf '%s\\n' "$@" > "${out}"\nexit 7\n`);
    chmodSync(fake, 0o755);

    const launched = await launchBehindProxy(
      [fake, "--no-proxy-server", "-no-proxy-server", "--proxy-server=http://evil:1", "about:blank"],
      { policy: ONLY_ONE, stdio: "ignore", log: () => {} },
    );
    expect(await launched.exited).toBe(7);
    // The launcher's switches come last so they win over any unrecognised copy.
    expect(readFileSync(out, "utf8").trim().split("\n")).toEqual([
      "about:blank",
      ...chromiumProxyArgs(launched.proxy.port),
    ]);
    await expect(
      new Promise((resolve, reject) => {
        const s = connect(launched.proxy.port, "127.0.0.1", () => resolve("open"));
        s.on("error", reject);
      }),
    ).rejects.toThrow();
  });

  it("reports a signal exit as 128+N", async () => {
    const dir = mkdtempSync(join(tmpdir(), "egress-launch-"));
    closers.push(() => rmSync(dir, { recursive: true, force: true }));
    const fake = join(dir, "fake-browser");
    writeFileSync(fake, "#!/bin/sh\nexec sleep 30\n");
    chmodSync(fake, 0o755);
    const launched = await launchBehindProxy([fake], { policy: ONLY_ONE, stdio: "ignore", log: () => {} });
    await new Promise((r) => setTimeout(r, 100));
    launched.child.kill("SIGTERM");
    expect(await launched.exited).toBe(143);
  });
});

describe("egress proxy forwarding edges", () => {
  it("connectPinned falls through a refused or non-address entry to the next checked address", async () => {
    const live = await target("127.0.0.1");
    const closed = await target("127.0.0.1");
    const closedPort = closed.port;
    await new Promise<void>((r) => closed.server.close(() => r()));
    // Same port on two loopback addresses: the first refuses, the second answers.
    const socket = await connectPinned(["not-an-ip", "127.0.0.2", "127.0.0.1"], live.port);
    expect(socket.remoteAddress).toBe("127.0.0.1");
    socket.destroy();
    await expect(connectPinned(["127.0.0.1"], closedPort)).rejects.toThrow();
    await expect(connectPinned([], live.port)).rejects.toThrow("no address to connect to");
  });

  it("withDeniedPort adds the proxy port once and keeps the configured ones", () => {
    const policy = withDeniedPort(withDeniedPort(LOOPBACK, 4321), 4321);
    expect(policy.denyPorts.filter((p) => p === 4321)).toHaveLength(1);
    expect(policy.denyPorts).toContain(8000);
    expect(LOOPBACK.denyPorts).not.toContain(4321);
  });

  it("forwards a request body and strips hop-by-hop headers from the response", async () => {
    const received: string[] = [];
    const upstream = createHttpServer((req, res) => {
      let data = "";
      req.on("data", (c) => (data += c));
      req.on("end", () => {
        received.push(`${req.method} ${data}`);
        res.writeHead(201, {
          "content-type": "text/plain",
          "proxy-authenticate": "Basic realm=upstream",
          "keep-alive": "timeout=99",
          "x-app": "kept",
        });
        res.end("created");
      });
    });
    await new Promise<void>((r) => upstream.listen(0, "127.0.0.1", () => r()));
    closers.push(() => new Promise<void>((r) => { upstream.closeAllConnections(); upstream.close(() => r()); }));
    const port = (upstream.address() as { port: number }).port;
    const proxy = await proxyWith();
    const res = await new Promise<{ status: number; body: string; headers: IncomingHttpHeaders }>((resolve, reject) => {
      const req = request(
        { host: "127.0.0.1", port: proxy.port, method: "POST", path: `http://127.0.0.1:${port}/items`, headers: { "content-type": "application/json" } },
        (r) => {
          let body = "";
          r.setEncoding("utf8");
          r.on("data", (c) => (body += c));
          r.on("end", () => resolve({ status: r.statusCode ?? 0, body, headers: r.headers }));
        },
      );
      req.on("error", reject);
      req.end('{"name":"x"}');
    });
    expect(received).toEqual(['POST {"name":"x"}']);
    expect({ status: res.status, body: res.body }).toEqual({ status: 201, body: "created" });
    expect(res.headers["x-app"]).toBe("kept");
    expect(res.headers["proxy-authenticate"]).toBeUndefined();
    // The proxy's own hop may carry its own Keep-Alive; the upstream's never passes.
    expect(res.headers["keep-alive"]).not.toBe("timeout=99");
  });

  it("answers an unmarked 502 when the destination drops the request", async () => {
    const upstream = createHttpServer((req) => req.socket.destroy());
    await new Promise<void>((r) => upstream.listen(0, "127.0.0.1", () => r()));
    closers.push(() => new Promise<void>((r) => { upstream.closeAllConnections(); upstream.close(() => r()); }));
    const port = (upstream.address() as { port: number }).port;
    const proxy = await proxyWith();
    const res = await viaProxy(proxy.port, `http://127.0.0.1:${port}/`);
    expect(res.status).toBe(502);
    expect(res.headers["x-browser-egress"]).toBeUndefined();
  });

  it("answers a malformed CONNECT target with 400, never a tunnel", async () => {
    const proxy = await proxyWith();
    for (const authority of ["127.0.0.1", "user@127.0.0.1:80", "127.0.0.1:0", "127.0.0.1:99999"]) {
      const r = await tunnel(proxy.port, authority);
      expect(r.status).toBe(400);
      r.socket.destroy();
    }
  });
});
