// Exercise the real, silently installed NSIS preview, never development Electron.
// CI profiles contain generated credentials: only this script's masked PNGs and
// public report may be uploaded, never the profile, raw logs or descriptors.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { installPreview } from './install-preview.mjs';

const exec = promisify(execFile);
assert(process.platform === 'win32' && process.env.CI === 'true', 'Windows CI only');
const [installerArg, outputArg] = process.argv.slice(2);
assert(installerArg && outputArg, 'Usage: verify-windows-preview.mjs INSTALLER EVIDENCE_DIR');
const installer = path.resolve(installerArg);
const output = path.resolve(outputArg);
const temporary = path.resolve(process.env.RUNNER_TEMP);
assert(output.startsWith(temporary + path.sep), 'Evidence must be under RUNNER_TEMP');
const installDir = path.join(temporary, 'shuffle-preview-installed');
const appData = process.env.APPDATA;
assert(appData && process.env.LOCALAPPDATA, 'Windows profile roots missing');
const previewRoot = path.join(appData, 'AI Shuffle Preview');
const dataDir = path.join(previewRoot, 'data');
const workspace = path.join(previewRoot, 'workspace', 'acceptance-workspace');
const descriptorPath = path.join(dataDir, 'managed-postgres.json');
const report = { sourceCommit: process.env.PREVIEW_SOURCE_COMMIT, checks: [], success: false };
const passed = message => { report.checks.push(message); console.log(`PASS: ${message}`); };
const exists = async p => fs.stat(p).then(() => true, e => { if (e.code === 'ENOENT') return false; throw e; });
assert(!(await exists(previewRoot)), 'Refusing to test over an existing preview profile');
assert(!(await exists(installDir)), 'Refusing to overwrite an existing installation');
await fs.mkdir(output, { recursive: true });
const guarded = [
  path.join(appData, 'AI Shuffle'), path.join(appData, 'ai-shuffle-desktop'),
  path.join(os.homedir(), '.ai-shuffle'), path.join(temporary, 'old-data'),
  path.join(temporary, 'old-workspace'),
];
for (const dir of guarded) {
  assert(!(await exists(dir)), 'Expected a fresh runner for stable-state sentinels');
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'preview-must-not-touch.txt'), 'preserve-existing-installation\n');
}
const oldEnv = path.join(temporary, 'old-config.env');
await fs.writeFile(oldEnv, 'DATABASE_URL=postgresql://never:never@127.0.0.1:9/never\nPOSTGRES_PASSWORD=never\n');
const childEnv = {
  ...process.env, DATA_DIR: guarded[3], WORKSPACE_DIR: guarded[4], AI_SHUFFLE_ENV_FILE: oldEnv,
  DATABASE_MODE: 'external', DATABASE_URL: 'postgresql://never:never@127.0.0.1:9/never',
  POSTGRES_HOST: 'invalid.invalid', POSTGRES_PORT: '9', POSTGRES_USER: 'never',
  POSTGRES_PASSWORD: 'never', POSTGRES_DB: 'never',
};
let application;
let page;
const sentinels = [];
const pageErrors = [];
let databasePort;
let browserWasRunning = false;
const canConnect = port => new Promise(resolve => {
  const socket = net.connect({ host: '127.0.0.1', port });
  const finish = result => { socket.destroy(); resolve(result); };
  socket.setTimeout(500); socket.once('connect', () => finish(true));
  socket.once('error', () => finish(false)); socket.once('timeout', () => finish(false));
});
async function until(check, message, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (!(await check())) {
    assert(Date.now() < deadline, message);
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}
async function screenshot(name) {
  if (page && !page.isClosed()) await page.screenshot({
    path: path.join(output, name), mask: [page.locator('code, textarea, input[type="password"]')],
  });
}
async function closeApplication() {
  if (!application) return;
  const running = application;
  application = undefined;
  await running.close();
}
async function stopSentinels() {
  await Promise.all(sentinels.splice(0).map(server => new Promise((resolve, reject) =>
    server.close(error => error ? reject(error) : resolve()))));
}
async function verifyStableState() {
  for (const dir of guarded) {
    assert.deepEqual(await fs.readdir(dir), ['preview-must-not-touch.txt'], 'Stable state directory was modified');
    assert.equal(await fs.readFile(path.join(dir, 'preview-must-not-touch.txt'), 'utf8'), 'preserve-existing-installation\n');
  }
}
async function publicState() {
  return page.evaluate(() => window.shuffleDesktop.worker.getState());
}
async function networkStatus() {
  return page.evaluate(async () => {
    const response = await fetch('/api/v1/network/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Network status HTTP ${response.status}`);
    const value = await response.json();
    return { networkId: value.network?.network_id ?? value.network?.id, name: value.network?.name,
      instanceId: value.instance_id, deviceId: value.device?.device_id };
  });
}
try {
  const installStarted = Date.now();
  report.installerBytes = (await fs.stat(installer)).size;
  report.installation = [];
  await installPreview(installer, installDir, async pid => {
    // No app is running yet. Only process counters/dialog labels and existence
    // flags are retained, never command lines, environment, or runtime logs.
    const sample = { elapsedMs: Date.now() - installStarted };
    for (const [name, relative] of Object.entries({ executable: 'AI Shuffle Preview.exe',
      manifest: 'resources/worker/manifest.json', postgres: 'resources/postgres/bin/initdb.exe' })) {
      sample[name] = await exists(path.join(installDir, relative));
    }
    if (Number.isInteger(pid) && pid > 0) {
      const command = `
        $ErrorActionPreference = 'Stop'
        $rows = @(Get-CimInstance Win32_Process)
        $ids = @(${pid})
        for ($i = 0; $i -lt 4; $i++) {
          $ids += @($rows | Where-Object { $_.ParentProcessId -in $ids } | ForEach-Object { $_.ProcessId })
        }
        @($rows | Where-Object { $_.ProcessId -in $ids } | ForEach-Object {
          $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
          [pscustomobject]@{ name=$_.Name; pid=$_.ProcessId; parent=$_.ParentProcessId;
            readBytes=$_.ReadTransferCount; writeBytes=$_.WriteTransferCount;
            cpuUser=$_.UserModeTime; cpuKernel=$_.KernelModeTime; window=$p.MainWindowTitle }
        }) | ConvertTo-Json -Compress
      `;
      try {
        const { stdout } = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], { timeout: 10000 });
        sample.processes = JSON.parse(stdout.trim() || '[]');
      } catch { sample.processDiagnostics = 'unavailable within 10s'; }
    }
    report.installation.push(sample);
    console.log('NSIS progress:', JSON.stringify(sample));
    await fs.writeFile(path.join(output, 'installation.json'), JSON.stringify(report.installation, null, 2));
  });
  report.installationDurationMs = Date.now() - installStarted;
  const executable = path.join(installDir, 'AI Shuffle Preview.exe');
  assert(await exists(executable), 'NSIS did not install the preview executable');
  const worker = path.join(installDir, 'resources', 'worker');
  const manifest = JSON.parse(await fs.readFile(path.join(worker, 'manifest.json'), 'utf8'));
  assert(!manifest.stub && manifest.postgres, 'Installer must contain real PostgreSQL and worker');
  assert(manifest.cloudflared?.version && manifest.cloudflared?.sha256, 'Installer must include cloudflared metadata');
  assert.equal(manifest.cloudflared.path, `server/downloads/cloudflared/${manifest.cloudflared.version}/cloudflared.exe`);
  const cloudflared = path.join(worker, manifest.cloudflared.path);
  assert.equal(createHash('sha256').update(await fs.readFile(cloudflared)).digest('hex'), manifest.cloudflared.sha256,
    'Installed cloudflared must match the verified build');
  const clientVersion = await exec(cloudflared, ['--version'], { timeout: 15000, windowsHide: true });
  assert(clientVersion.stdout.startsWith(`cloudflared version ${manifest.cloudflared.version} `), 'Installed cloudflared version mismatch');
  const build = JSON.parse(await fs.readFile(path.join(worker, 'preview-build.json'), 'utf8'));
  assert.equal(build.sourceCommit, report.sourceCommit, 'Installer revision differs from checked-out candidate');
  for (const required of ['python/python.exe', 'node/node.exe', 'server/app/main.py',
    'server/app/static/index.html', 'server/tools/dist/index.js', 'server/browser-mcp/dist/index.js']) {
    assert(await exists(path.join(worker, required)), `Missing bundled resource: ${required}`);
  }
  await exec(path.join(worker, 'python', 'python.exe'), ['--version'], { timeout: 30000 });
  await exec(path.join(worker, 'node', 'node.exe'), ['--version'], { timeout: 30000 });
  passed('NSIS installed complete production runtime from the exact candidate');
  const { _electron } = await import(pathToFileURL(path.join(worker, 'server', 'browser-mcp', 'node_modules', 'playwright-core', 'index.mjs')).href);
  async function launch() {
    application = await _electron.launch({ executablePath: executable, env: childEnv, timeout: 180000 });
    page = await application.firstWindow({ timeout: 180000 });
    page.on('pageerror', error => pageErrors.push(error.message));
    const identity = await application.evaluate(({ app }) => ({ name: app.getName(), data: app.getPath('userData') }));
    assert.equal(identity.name, 'AI Shuffle Preview');
    assert.equal(path.resolve(identity.data).toLowerCase(), previewRoot.toLowerCase());
    const update = await page.evaluate(() => window.shuffleDesktop.updates.getState());
    assert.equal(update.phase, 'unsupported', 'Preview must not update itself from the stable feed');
  }
  // A running stable app must produce a refusal, never attachment or migration.
  for (const port of [8000, 8001]) {
    const server = http.createServer((request, response) => {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ status: 'healthy', service: 'AI Shuffle API' }));
    });
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
    sentinels.push(server);
  }
  await launch();
  await until(async () => {
    const state = await publicState();
    assert.notEqual(state.postgres?.phase, 'failed', 'Bundled PostgreSQL failed before occupied-port refusal could be tested');
    return state.phase === 'failed';
  }, 'Occupied stable ports were not refused', 120000);
  const refusal = await publicState();
  assert(refusal.external !== true, 'Preview attached to the existing worker');
  await screenshot('occupied-port-refusal.png');
  assert(await canConnect(8000) && await canConnect(8001), 'Preview stopped a stable sentinel');
  await closeApplication();
  await stopSentinels();
  await verifyStableState();
  passed('Existing worker ports fail closed; stable profile/data remain untouched');

  await launch();
  await page.waitForURL('http://127.0.0.1:8000/**', { timeout: 180000 });
  await page.goto('http://127.0.0.1:8000/setup', { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Welcome to AI Shuffle', exact: true }).waitFor({ timeout: 60000 });
  const state = await publicState();
  assert.equal(state.external, false, 'A bundled worker must be owned');
  await fs.mkdir(workspace, { recursive: true });
  const descriptorText = await fs.readFile(descriptorPath, 'utf8');
  const descriptor = JSON.parse(descriptorText);
  assert(descriptor.version === 1 && descriptor.host === '127.0.0.1', 'Invalid managed connection contract');
  assert(Number.isInteger(descriptor.port) && descriptor.port > 0 && descriptor.port < 65536, 'Invalid local PostgreSQL port');
  assert(descriptor.user !== 'never' && descriptor.database !== 'never' && descriptor.password !== 'never', 'Inherited DB config was adopted');
  databasePort = descriptor.port;
  const acl = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    "$ErrorActionPreference='Stop'; $rules=@([System.IO.File]::GetAccessControl($env:PREVIEW_DESCRIPTOR_PATH).Access | Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }); @{owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;allow=$rules} | ConvertTo-Json -Compress"],
    { env: { ...process.env, PREVIEW_DESCRIPTOR_PATH: descriptorPath }, timeout: 30000 });
  const access = JSON.parse(acl.stdout);
  assert(access.allow.includes(access.owner), 'Descriptor ACL does not grant its owner access');
  assert(access.allow.every(sid => [access.owner, 'S-1-5-18', 'S-1-5-32-544'].includes(sid)), 'Descriptor ACL grants another account/group access');
  passed('Generated database descriptor has a restrictive Windows ACL');
  // Query the actual bundled database without putting its credentials into argv/env/logs.
  const probe = await exec(path.join(worker, 'python', 'python.exe'), ['-c',
    'import json,sys,psycopg2; d=json.load(open(sys.argv[1])); c=psycopg2.connect(host=d["host"],port=d["port"],user=d["user"],password=d["password"],dbname=d["database"],connect_timeout=5); q=c.cursor(); q.execute("select count(*) from information_schema.tables where table_schema=\'public\'"); print(q.fetchone()[0]); c.close()', descriptorPath], { timeout: 30000 });
  assert(Number(probe.stdout.trim()) > 0, 'App did not create schema in its managed PostgreSQL');
  passed('Bundled PostgreSQL authenticates and contains the actual application schema; inherited DB env ignored');
  // Start step: a clean install needs no account, invite or approval.
  await page.getByRole('button', { name: 'Continue without an account', exact: true }).click();
  await page.getByRole('heading', { name: 'Select Workspace Folder', exact: true }).waitFor();
  await page.getByLabel('Or type a path manually').fill(workspace);
  const saved = page.waitForResponse(response => response.url().endsWith('/workspace/config') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  const response = await saved;
  assert.equal(response.status(), 200, 'Real workspace producer rejected onboarding');
  const workspaceURL = response.url();
  // Providers & GitHub is the last step; Claude Code is not asked for.
  await page.getByText('Model providers', { exact: true }).waitFor();
  assert.equal(await page.getByText(/Claude Code/).count(), 0, 'Setup must not ask for Claude Code');
  const originalNetwork = await networkStatus();
  assert(originalNetwork.networkId && originalNetwork.instanceId, 'Network producer did not persist identity');
  browserWasRunning = await canConnect(19221);
  await screenshot('real-onboarding.png');
  passed('Packaged UI prepares local settings and workspace without an account, recovery key, or remote worker');

  await closeApplication();
  await until(async () => !(await canConnect(8000)) && !(await canConnect(8001)) && !(await canConnect(databasePort)), 'Owned app/PostgreSQL listeners survived quit', 45000);
  if (browserWasRunning) await until(async () => !(await canConnect(19221)), 'Owned browser listener survived quit', 45000);
  await verifyStableState();
  assert(!(await exists(descriptorPath)), 'Transient database descriptor survived shutdown');
  passed('Desktop quit stops its own worker and PostgreSQL');
  await launch();
  await page.waitForURL('http://127.0.0.1:8000/**', { timeout: 180000 });
  const restored = await page.evaluate(async url => {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Workspace consumer HTTP ${response.status}`);
    return response.json();
  }, workspaceURL);
  assert.equal(restored.workspace_path, workspace);
  assert.deepEqual(await networkStatus(), originalNetwork, 'Network/instance identity changed across quit and relaunch');
  const nextDescriptor = JSON.parse(await fs.readFile(descriptorPath, 'utf8'));
  // The supervisor may allocate another loopback port, but credentials/database stay stable.
  assert(nextDescriptor.user === descriptor.user && nextDescriptor.database === descriptor.database && nextDescriptor.password === descriptor.password, 'Database identity changed across restart');
  databasePort = nextDescriptor.port;
  await screenshot('restart-persistence.png');
  passed('Full relaunch preserves workspace, network, instance and PostgreSQL credentials');
  // Resume the unfinished setup through its real UI, then finish without
  // introducing provider credentials or an account link into this CI profile.
  await page.goto('http://127.0.0.1:8000/setup', { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Select Workspace Folder', exact: true }).waitFor();
  assert.equal(await page.getByLabel('Or type a path manually').inputValue(), workspace);
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.getByText('Model providers', { exact: true }).waitFor();
  const completed = page.waitForResponse(response => response.url().endsWith('/auth/onboarding/complete') && response.request().method() === 'POST');
  await page.getByRole('button', { name: /^Finish/ }).click();
  assert.equal((await completed).status(), 200, 'Standalone onboarding completion was rejected');
  await page.waitForURL(url => url.origin === 'http://127.0.0.1:8000' && url.pathname !== '/setup');
  passed('Interrupted local setup resumes and completes without account sign-in or provider credentials');
  // Navigate through the real native menu. Awaiting renderer IPC here would
  // destroy its own execution context while loadFile replaces the web app.
  await application.evaluate(({ Menu }) => {
    const workerMenu = Menu.getApplicationMenu()?.items.find(item => item.label === 'Worker');
    const status = workerMenu?.submenu?.items.find(item => item.label.startsWith('Worker Status'));
    if (!status || !status.enabled) throw new Error('Worker Status menu is unavailable');
    status.click();
  });
  await page.waitForURL('file:///**', { timeout: 30000 });
  await page.getByText('Startup diagnostics', { exact: true }).click();
  await page.locator('#status-postgres').waitFor();
  assert.equal(await page.locator('input, select, form').count(), 0, 'Database configuration controls remain in status UI');
  await screenshot('embedded-only-status.png');
  passed('Installed desktop status shows lifecycle/logs without database setup inputs');
  await closeApplication();
  await until(async () => !(await canConnect(8000)) && !(await canConnect(8001)) && !(await canConnect(databasePort)), 'Owned listeners survived final quit', 45000);
  if (browserWasRunning) await until(async () => !(await canConnect(19221)), 'Owned browser listener survived final quit', 45000);
  assert(!(await exists(descriptorPath)), 'Transient database descriptor survived final shutdown');
  await verifyStableState();
  assert.deepEqual(pageErrors, [], 'Installed UI raised JavaScript errors');
  passed('Stable sentinels unchanged after every lifecycle; no renderer errors');
  report.success = true;
  report.gaps = ['No live provider credentials/account login, remote enrollment, physical handset, or user PC exercised. Local onboarding completed through the UI; no onboarding flags were seeded.'];
} catch (error) {
  // Do not serialize page DOM, runtime descriptors, or full process output.
  report.failure = String(error.message).slice(0, 2400);
  await screenshot('failure.png').catch(() => {});
  throw error;
} finally {
  await closeApplication().catch(error => { report.cleanupFailure = String(error.message).slice(0, 500); });
  await stopSentinels();
  await fs.writeFile(path.join(output, 'acceptance.json'), JSON.stringify(report, null, 2));
}
