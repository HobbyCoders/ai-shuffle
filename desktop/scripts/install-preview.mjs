// NSIS has no useful stdout in silent mode. Wait for process exit, not inherited
// pipe closure, and collect bounded, pre-app diagnostics before timeout cleanup.
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
const exec = promisify(execFile);

export function waitForInstaller(child, { timeoutMs = 600000, intervalMs = 30000,
  progress = async () => {}, terminate = async () => {
    if (process.platform === 'win32' && child.pid) {
      await exec('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { timeout: 15000 });
    } else child.kill('SIGKILL');
  },
} = {}) {
  return new Promise((resolve, reject) => {
    let ended = false;
    let sampling = Promise.resolve();
    const sample = () => {
      sampling = sampling.then(() => progress(child.pid)).catch(() => {});
      return sampling;
    };
    const finish = (error) => {
      if (ended) return;
      ended = true;
      clearTimeout(deadline); clearInterval(heartbeat);
      child.removeListener('error', failed); child.removeListener('exit', exited);
      sampling.then(() => error ? reject(error) : resolve());
    };
    const failed = error => finish(error);
    const exited = (code, signal) => finish(code === 0 && !signal ? undefined :
      new Error(`NSIS installer exited with code ${code}, signal ${signal ?? 'none'}`));
    const heartbeat = setInterval(() => { void sample(); }, intervalMs);
    const deadline = setTimeout(async () => {
      if (ended) return;
      // Fence exit handling first: termination must never turn a timeout green.
      ended = true;
      clearInterval(heartbeat);
      child.removeListener('error', failed); child.removeListener('exit', exited);
      await sample();
      let cleanup = '';
      try { await terminate(); } catch { cleanup = '; process-tree cleanup failed'; }
      reject(new Error(`NSIS installation timed out after ${timeoutMs}ms${cleanup}`));
    }, timeoutMs);
    child.once('error', failed); child.once('exit', exited);
  });
}

export async function installPreview(installer, installDir, progress) {
  const child = spawn(installer, ['/S', '/currentuser', `/D=${installDir}`], {
    stdio: 'ignore', windowsHide: true,
  });
  await waitForInstaller(child, { progress });
}
