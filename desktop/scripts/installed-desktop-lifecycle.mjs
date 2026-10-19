import { execFileSync } from 'node:child_process';

// Acceptance must wait for Electron itself, not just its worker HTTP server.
// During PostgreSQL shutdown the old window, debugger and instance lock live on.
export async function closeInstalledDesktop(page, browser, waitForWorkerStop, timeout = 90000) {
 let disconnected = !browser?.isConnected();
 const onDisconnected = () => { disconnected = true; };
 browser?.on('disconnected', onDisconnected);
 try {
  if (page && !page.isClosed()) await page.evaluate(() => window.shuffleDesktop.window.close()).catch(() => {});
  await waitForWorkerStop();
  const end = Date.now() + timeout;
  while (!disconnected) {
   if (Date.now() >= end) throw Error('Installed Electron did not exit');
   await new Promise(resolve => setTimeout(resolve, 50));
  }
  // Closing a CDP client ourselves also emits disconnected: do not do that
  // until the app has disconnected it naturally, after its shutdown completes.
  if (browser) await browser.close().catch(() => {});
 } finally {
  browser?.off('disconnected', onDisconnected);
 }
}

/**
 * The Electron processes of an installed Windows desktop: the launcher is
 * "AI Shuffle.exe", but an updated app runs the Electron its source update
 * downloaded, "electron.exe". Each takes the same single-instance lock, and a
 * start that finds it held quits at once, so the next start waits for both.
 */
export const DESKTOP_IMAGES = ['AI Shuffle.exe', 'electron.exe'];
/** `tasklist /FO CSV /NH` rows as { image, pid }; its "no tasks" notice has none. */
export function parseTasklist(text) {
 return text.split(/\r?\n/).filter(line => line.startsWith('"'))
  .map(line => line.slice(1, -1).split('","')).filter(cells => cells.length > 1 && /^\d+$/.test(cells[1]))
  .map(([image, pid]) => ({ image, pid: Number(pid) }));
}
const tasklist = image => execFileSync('tasklist.exe', ['/FI', `IMAGENAME eq ${image}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8', timeout: 30000, windowsHide: true });
const electronProcessDetails = () => {
 const script = "Get-CimInstance Win32_Process -Filter \"Name = 'electron.exe'\" | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress";
 const text = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { encoding: 'utf8', timeout: 30000, windowsHide: true });
 return text.trim() ? [].concat(JSON.parse(text)) : [];
};
/** A source update's notice and its Chromium children hold no app instance lock. */
export function splashProcessIds(processes) {
 const ids = new Set(processes.filter(p => /(?:^|[\\/\s"])update-splash-main\.js(?=["\s]|$)/i.test(p.CommandLine ?? '')).map(p => p.ProcessId));
 let changed;
 do {
  changed = false;
  for (const p of processes) if (ids.has(p.ParentProcessId) && !ids.has(p.ProcessId)) { ids.add(p.ProcessId); changed = true; }
 } while (changed);
 return ids;
}
/** Every lock-owning desktop Electron process; unknown command lines remain in the wait. */
export function desktopProcesses(list = tasklist, details = electronProcessDetails) {
 const processes = DESKTOP_IMAGES.flatMap(image => parseTasklist(list(image)));
 if (!processes.some(p => p.image.toLowerCase() === 'electron.exe')) return processes;
 try {
  const splash = splashProcessIds(details());
  return processes.filter(p => p.image.toLowerCase() !== 'electron.exe' || !splash.has(p.pid));
 } catch { return processes; } // An unavailable process query is not proof the app exited.
}
/** Wait until no desktop Electron process is left; the ones still running on timeout are in the error. */
export async function waitForDesktopExit(timeout = 120000, list = desktopProcesses, every = 250) {
 const end = Date.now() + timeout;
 for (;;) {
  const left = list();
  if (!left.length) return;
  if (Date.now() >= end) {
   throw Error(`AI Shuffle is still running ${Math.round(timeout / 1000)} s after it was closed (${left.map(p => `${p.image} ${p.pid}`).join(', ')}); a new start would find its single-instance lock taken`);
  }
  await new Promise(resolve => setTimeout(resolve, every));
 }
}

// Diagnostics for a start that never opens: which processes of this desktop
// run (launcher, updated app, self-test, worker, PostgreSQL) with their
// parents and command lines, and their titled windows and dialogs with the
// dialog text. A window shown under SW_HIDE (spawn's windowsHide) is listed
// as hidden, so a message box nobody can see still shows up here.
const SNAPSHOT_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$dirs = @($env:SNAPSHOT_DIRS -split ';' | Where-Object { $_ })
Add-Type @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class DesktopWindows {
  delegate bool EnumProc(IntPtr hwnd, IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc proc, IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc proc, IntPtr data);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int max);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  static string Text(IntPtr h) { var s = new StringBuilder(1024); GetWindowText(h, s, 1024); return s.ToString(); }
  static string Class(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }
  public static List<string> List(int[] pids) {
    var list = new List<string>();
    EnumWindows((h, d) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (Array.IndexOf(pids, (int)pid) < 0) return true;
      string title = Text(h), cls = Class(h);
      if (title.Length == 0 && cls != "#32770") return true;
      var parts = new List<string>();
      if (cls == "#32770") EnumChildWindows(h, (c, e) => { var t = Text(c); if (t.Length > 0) parts.Add(t); return true; }, IntPtr.Zero);
      list.Add(pid + " " + (IsWindowVisible(h) ? "visible" : "hidden") + " " + cls + " '" + title + "'" + (parts.Count > 0 ? " [" + string.Join(" | ", parts) + "]" : ""));
      return true;
    }, IntPtr.Zero);
    return list;
  }
}
'@
$images = @('AI Shuffle.exe', 'electron.exe')
$processes = @(foreach ($p in Get-CimInstance Win32_Process) {
  $exe = [string]$p.ExecutablePath
  $ours = $images -contains $p.Name
  foreach ($d in $dirs) { if ($exe -and $exe.StartsWith($d, [StringComparison]::OrdinalIgnoreCase)) { $ours = $true } }
  if ($ours) { $p }
})
$out = [ordered]@{
  processes = @($processes | ForEach-Object { $c = [string]$_.CommandLine
    [ordered]@{ pid = [int]$_.ProcessId; parent = [int]$_.ParentProcessId; name = $_.Name; started = $_.CreationDate.ToUniversalTime().ToString('o'); command = $c.Substring(0, [Math]::Min(300, $c.Length)) } })
  windows = @([DesktopWindows]::List([int[]]@($processes | ForEach-Object { [int]$_.ProcessId })))
  tasklist = @($images | ForEach-Object { & tasklist.exe /V /FO CSV /NH /FI "IMAGENAME eq $_" } | Where-Object { $_.StartsWith('"') })
}
$out | ConvertTo-Json -Depth 4 -Compress
`;
/** A snapshot for the evidence; never throws (a failure is recorded instead). */
export function desktopSnapshot(dirs, run = execFileSync) {
 const at = new Date().toISOString();
 try {
  const text = run('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(SNAPSHOT_SCRIPT, 'utf16le').toString('base64')],
   { encoding: 'utf8', timeout: 60000, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, SNAPSHOT_DIRS: dirs.join(';') } });
  const value = JSON.parse(text);
  return { at, processes: [].concat(value.processes ?? []), windows: [].concat(value.windows ?? []), tasklist: [].concat(value.tasklist ?? []) };
 } catch (error) {
  return { at, error: String(error).slice(0, 2000) };
 }
}
/** One line per process and window, for the job log. */
export function describeSnapshot(snapshot) {
 if (snapshot.error) return `  snapshot failed: ${snapshot.error}`;
 const lines = snapshot.processes.map(p => `  ${p.name} ${p.pid} (parent ${p.parent}, started ${p.started}): ${p.command}`);
 lines.push(...snapshot.windows.map(w => `  window ${w}`));
 return lines.length ? lines.join('\n') : '  no AI Shuffle process is running';
}

/**
 * Debugger ports to try for an updated app, newest first: the ones it printed
 * to launch.log since this start ("DevTools listening on ..."), then the
 * profile's DevToolsActivePort. The launcher and the updated app both write
 * that file, and the launcher exits as the app starts, so the app's write can
 * fail ("being used by another process") and leave the launcher's dead port.
 */
export function debuggerPorts(launchText, activePortText) {
 const ports = [...(launchText ?? '').matchAll(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d{1,5})\//g)].map(match => match[1]).reverse();
 if (activePortText !== undefined && activePortText !== null) ports.push(activePortText.split(/\r?\n/)[0]);
 return [...new Set(ports)].filter(port => /^[1-9]\d{0,4}$/.test(port));
}
