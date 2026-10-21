// Own every Node/Chromium scratch directory for one test invocation.
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const directory = mkdtempSync(join(tmpdir(), 'ai-shuffle-browser-'));
let child;
let interrupted;
function signal(name) {
  interrupted = name;
  if (!child?.pid) return;
  try {
    if (process.platform === 'win32') child.kill(name);
    else process.kill(-child.pid, name);
  } catch (error) { if (error.code !== 'ESRCH') throw error; }
}
const stop = () => signal('SIGTERM');
const interrupt = () => signal('SIGINT');
try {
  if (process.platform === 'linux') {
    const start = readFileSync(`/proc/${process.pid}/stat`, 'utf8').split(')').slice(1).join(')').trim().split(/\s+/)[19];
    writeFileSync(join(directory, '.ai-shuffle-scratch-owner'), JSON.stringify({
      version: 1, pid: process.pid, start, created: Date.now() / 1000, uid: process.getuid(),
    }));
  }
  process.on('SIGTERM', stop);
  process.on('SIGINT', interrupt);
  child = spawn(process.execPath, [fileURLToPath(new URL('../node_modules/vitest/vitest.mjs', import.meta.url)), 'run', ...process.argv.slice(2)], {
    stdio: 'inherit', detached: process.platform !== 'win32',
    env: { ...process.env, TMPDIR: directory, TEMP: directory, TMP: directory },
  });
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, sig) => resolve(code ?? (sig === 'SIGINT' ? 130 : 143)));
  });
  process.exitCode = interrupted ? (interrupted === 'SIGINT' ? 130 : 143) : code;
} finally {
  process.off('SIGTERM', stop);
  process.off('SIGINT', interrupt);
  // Vitest's teardown has finished. Catch any descendant left in our OWN group.
  if (child?.pid && process.platform !== 'win32') {
    try { process.kill(-child.pid, 'SIGKILL'); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
