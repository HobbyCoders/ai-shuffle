#!/usr/bin/env node
/**
 * Launch Chromium behind the egress proxy (see egress.ts).
 *
 *   node egress-launch.js [--watch-stdin] -- <browser> [browser args...]
 *
 * The SSRF policy comes from the environment, exactly as for the MCP server
 * (BROWSER_SSRF_ALLOW / BLOCK / DENY_PORTS, CHROME_CDP_PORT). The browser's
 * exit code is this process's exit code (128+N for a signal), so a supervisor
 * sees the same status it saw when it ran the browser directly. SIGTERM,
 * SIGINT and SIGHUP are forwarded. With --watch-stdin, end of stdin (the
 * parent went away) stops the browser; native_chrome.py uses it because a
 * Windows parent cannot deliver a signal. If the proxy fails, the browser is
 * stopped: it never runs without its proxy.
 */

import { compilePolicy, describePolicy } from "./ssrf.js";
import { launchBehindProxy } from "./egress.js";

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  const split = argv.indexOf("--");
  const flags = split === -1 ? [] : argv.slice(0, split);
  const command = split === -1 ? [] : argv.slice(split + 1);
  const unknown = flags.filter((f) => f !== "--watch-stdin");
  if (command.length === 0 || unknown.length > 0) {
    process.stderr.write("usage: egress-launch [--watch-stdin] -- <browser> [args...]\n");
    process.exit(2);
  }

  const launched = await launchBehindProxy(command, { policy: compilePolicy(process.env) });
  const { child, proxy } = launched;
  process.stderr.write(
    `[browser-egress] proxy 127.0.0.1:${proxy.port} for pid ${child.pid ?? "?"}; policy: ${describePolicy(proxy.policy)}\n`,
  );

  const stop = (signal: NodeJS.Signals) => {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  };
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
    process.on(signal, () => stop(signal));
  }
  if (flags.includes("--watch-stdin")) {
    const gone = () => {
      stop("SIGTERM");
      setTimeout(() => stop("SIGKILL"), 5000).unref();
    };
    process.stdin.on("end", gone);
    process.stdin.on("close", gone);
    process.stdin.on("error", gone);
    process.stdin.resume();
  }
  process.on("uncaughtException", (err) => {
    process.stderr.write(`[browser-egress] fatal: ${err instanceof Error ? err.message : String(err)}\n`);
    stop("SIGKILL");
    process.exit(1);
  });

  process.exit(await launched.exited);
}

main().catch((err) => {
  process.stderr.write(`[browser-egress] could not start: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
