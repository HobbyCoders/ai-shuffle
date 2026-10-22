import { spawn, type ChildProcess } from "node:child_process";

async function waitForExit(proc: ChildProcess, milliseconds: number): Promise<boolean> {
  if (proc.exitCode !== null || proc.signalCode !== null) return true;
  return new Promise((resolve) => {
    const done = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { proc.off("exit", done); resolve(false); }, milliseconds);
    proc.once("exit", done);
  });
}

/** Caller launches a detached POSIX group; never signal the test runner's group. */
export async function stopBrowser(proc?: ChildProcess): Promise<void> {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { stdio: "ignore" });
    await new Promise<void>((resolve, reject) => {
      killer.once("error", reject);
      killer.once("exit", () => resolve());
    });
  } else {
    const send = (signal: NodeJS.Signals) => {
      try { process.kill(-proc.pid!, signal); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; }
    };
    send("SIGTERM");
    if (!(await waitForExit(proc, 1000))) send("SIGKILL");
  }
  if (!(await waitForExit(proc, 5000))) throw new Error("Private test browser did not exit");
}
