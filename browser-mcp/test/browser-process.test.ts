import { expect, it } from "vitest";
import { spawn } from "node:child_process";
import { stopBrowser } from "./browser-process.js";

it.skipIf(process.platform === "win32")("escalates if a private browser ignores termination", async () => {
  const proc = spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{},1000)"],
    { detached: true, stdio: ["ignore", "pipe", "ignore"] });
  try {
    await new Promise<void>((resolve) => proc.stdout!.once("data", () => resolve()));
    await stopBrowser(proc);
    expect(proc.exitCode !== null || proc.signalCode !== null).toBe(true);
  } finally {
    if (proc.exitCode === null && proc.signalCode === null) {
      const exited = new Promise((resolve) => proc.once("exit", resolve));
      process.kill(-proc.pid!, "SIGKILL");
      await exited;
    }
  }
});
