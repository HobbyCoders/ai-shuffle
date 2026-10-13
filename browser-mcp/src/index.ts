#!/usr/bin/env node
/** Select the lightweight proxy before importing the browser runtime. */
async function main(): Promise<void> {
  if (process.env.AIS_BROWSER_MODE === "proxy" && process.env.AIS_SESSION_ID && process.env.AIS_PANEL_TOKEN && process.env.AIS_PANEL_REGISTER_URL) {
    const { startProxy } = await import("./proxy.js");
    await startProxy();
  } else {
    const { startRuntime } = await import("./runtime.js");
    await startRuntime();
  }
}

main().catch((err) => {
  process.stderr.write(`[browser-mcp] fatal: ${err instanceof Error ? err.stack : String(err)}\n`);
  process.exit(1);
});
