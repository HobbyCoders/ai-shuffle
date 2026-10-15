/** OS process identity: a PID and an HTTP/PG port are not proof of ownership. */
import { promises as fs } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
const run = promisify(execFile);
export interface ProcessIdentity { pid: number; executable: string; started: string; startedAt: number }
export function isProcessIdentity(value: unknown): value is ProcessIdentity {
  const p = value as ProcessIdentity;
  return !!p && Number.isSafeInteger(p.pid) && p.pid > 0 && typeof p.executable === 'string' && path.isAbsolute(p.executable) &&
    typeof p.started === 'string' && !!p.started && Number.isSafeInteger(p.startedAt) && p.startedAt > 0;
}
export function sameProcess(a: ProcessIdentity, b: ProcessIdentity): boolean {
  const canonical = (file: string) => process.platform === 'win32' ? file.toLowerCase() : file;
  return a.pid === b.pid && a.started === b.started && canonical(a.executable) === canonical(b.executable);
}
const windowsScript = String.raw`
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ProcessProof {
 [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint rights, bool inherit, int pid);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int length);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}
'@
$handle = [ProcessProof]::OpenProcess(0x1000, $false, [int]$env:SHUFFLE_ID_PID)
if ($handle -eq [IntPtr]::Zero) { throw 'Cannot open process identity handle' }
try {
 [long]$created=0; [long]$exited=0; [long]$kernel=0; [long]$user=0
 $name=New-Object Text.StringBuilder 32768; [int]$length=$name.Capacity
 if (![ProcessProof]::GetProcessTimes($handle,[ref]$created,[ref]$exited,[ref]$kernel,[ref]$user) -or ![ProcessProof]::QueryFullProcessImageName($handle,0,$name,[ref]$length)) { throw 'Cannot read process identity' }
 $identity=@{pid=[int]$env:SHUFFLE_ID_PID; executable=$name.ToString(); started=$created.ToString(); startedAt=[long][Math]::Floor(($created-116444736000000000)/10000000)}
 if ($env:SHUFFLE_ID_EXPECTED) {
  $expected=$env:SHUFFLE_ID_EXPECTED | ConvertFrom-Json
  if ($identity.started -cne $expected.started -or ![String]::Equals($identity.executable,$expected.executable,[StringComparison]::OrdinalIgnoreCase)) { throw 'Process identity mismatch; no process was signalled' }
 }
 if ($env:SHUFFLE_ID_COMMAND) {
  $arguments=@($env:SHUFFLE_ID_ARGS | ConvertFrom-Json)
  $output = & $env:SHUFFLE_ID_COMMAND @arguments 2>&1 | Out-String
  @{code=$LASTEXITCODE;stdout=$output;stderr=''} | ConvertTo-Json -Compress
 } else { $identity | ConvertTo-Json -Compress }
} finally { [void][ProcessProof]::CloseHandle($handle) }
`;
async function windows(pid: number, expected?: ProcessIdentity, command?: { file: string; args: string[]; env?: NodeJS.ProcessEnv; timeoutMs?: number }): Promise<string> {
  const { stdout } = await run('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(windowsScript, 'utf16le').toString('base64')], {
    windowsHide: true, timeout: command?.timeoutMs ?? 30_000,
    env: { ...(command?.env ?? process.env), SHUFFLE_ID_PID: String(pid), SHUFFLE_ID_EXPECTED: expected ? JSON.stringify(expected) : '',
      SHUFFLE_ID_COMMAND: command?.file ?? '', SHUFFLE_ID_ARGS: JSON.stringify(command?.args ?? []) },
  });
  return stdout;
}
let clockTicks: Promise<number> | undefined;
export async function readProcessIdentity(pid: number): Promise<ProcessIdentity> {
  if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error('Invalid process identity PID');
  if (process.platform === 'win32') {
    const result = JSON.parse(await windows(pid));
    if (!isProcessIdentity(result)) throw new Error('Invalid Windows process identity');
    return result;
  }
  if (process.platform === 'linux') {
    const stat = () => fs.readFile(`/proc/${pid}/stat`, 'utf8');
    const start = (text: string) => text.slice(text.lastIndexOf(')') + 2).split(' ')[19]; // field 22; comm can contain spaces/parentheses
    const first = await stat();
    if (first.slice(first.lastIndexOf(')') + 2).startsWith('Z ')) throw Object.assign(new Error('Process has exited'), { code: 'ESRCH' });
    const before = start(first);
    const executable = await fs.readlink(`/proc/${pid}/exe`);
    const boot = (await fs.readFile('/proc/stat', 'utf8')).match(/^btime (\d+)$/m)?.[1];
    clockTicks ??= run('getconf', ['CLK_TCK']).then(result => Number(result.stdout.trim()));
    const hz = await clockTicks;
    if (!boot || !/^\d+$/.test(before) || !Number.isFinite(hz) || hz <= 0 || executable.endsWith(' (deleted)') || start(await stat()) !== before) throw new Error('Cannot establish stable process identity');
    return { pid, executable, started: `linux:${boot}:${before}`, startedAt: Math.floor(Number(boot) + Number(before) / hz) };
  }
  const inspect = async () => (await run('ps', ['-p', String(pid), '-o', 'lstart=', '-o', 'comm='], { env: { ...process.env, LC_ALL: 'C', TZ: 'UTC' } })).stdout.trim();
  const before = await inspect();
  const match = before.match(/^(\w+\s+\w+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(\/.*)$/);
  if (!match || await inspect() !== before) throw new Error('Cannot establish stable process identity');
  return { pid, executable: await fs.realpath(match[2]), started: `ps:${match[1]}`, startedAt: Math.floor(Date.parse(match[1] + ' UTC') / 1000) };
}
export async function assertProcessIdentity(expected: ProcessIdentity): Promise<void> {
  if (!isProcessIdentity(expected) || !sameProcess(expected, await readProcessIdentity(expected.pid))) throw new Error('Process identity mismatch; no process was signalled');
}
/**
 * POSIX has no kernel-bound pidfd API in Node. Re-read identity immediately
 * before process.kill, without launching pg_ctl/kill between check and signal.
 * A microsecond-scale PID-reuse window remains: this is NOT an atomic identity
 * guarantee. A native pidfd helper would be needed to eliminate it. Post-signal
 * checks diagnose a change and prevent any follow-up signal to the replacement.
 */
export async function signalVerifiedProcess(expected: ProcessIdentity, signal: NodeJS.Signals, group = false, log: (message: string) => void = console.warn): Promise<void> {
  if (process.platform === 'win32') throw new Error('Windows must use handle-bound process signalling');
  await assertProcessIdentity(expected);
  try { process.kill(group ? -expected.pid : expected.pid, signal); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
    if (group) {
      // No group: verify again immediately before signalling the individual PID.
      await assertProcessIdentity(expected);
      try { process.kill(expected.pid, signal); }
      catch (failure) { if ((failure as NodeJS.ErrnoException).code !== 'ESRCH') throw failure; }
    }
  }
  try { await assertProcessIdentity(expected); }
  catch (error) {
    if (['ENOENT', 'ESRCH'].includes((error as NodeJS.ErrnoException).code ?? '')) {
      log(`Verified process ${expected.pid} exited after ${signal}`);
      return;
    }
    log(`Process ${expected.pid} identity could not be confirmed after ${signal}; no follow-up signal is safe`);
    throw error;
  }
}
/** On Windows keep the queried handle open while invoking the signal command, preventing PID reuse. */
export async function execWithIdentity(expected: ProcessIdentity, file: string, args: string[], opts: { env?: NodeJS.ProcessEnv; timeoutMs?: number } = {}): Promise<{code:number|null;stdout:string;stderr:string}> {
  if (!isProcessIdentity(expected)) throw new Error('Missing OS process identity; no process was signalled');
  if (process.platform === 'win32') return JSON.parse(await windows(expected.pid, expected, { file, args, ...opts }));
  await assertProcessIdentity(expected);
  try {
    const result = await run(file, args, { env: opts.env, timeout: opts.timeoutMs ?? 60_000 });
    return { code: 0, ...result };
  } catch (error) {
    const failure = error as { code?: number; stdout?: string; stderr?: string };
    return { code: typeof failure.code === 'number' ? failure.code : -1, stdout: failure.stdout ?? '', stderr: failure.stderr ?? '' };
  }
}
