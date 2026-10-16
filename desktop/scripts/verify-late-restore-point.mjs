// Late restore point acceptance (desktop/LAUNCHER.md, "Launchers without
// restore points") on a throwaway Windows machine: install an installer whose
// launcher makes no restore point (0.9.6), then update it through builds of
// this checkout and check the late copy end to end with the real launcher,
// the real PostgreSQL cluster and real process kills.
//
//   P  (launcher 3)  bundled previous build: starts with no copy.
//   N1 (launcher 3)  a failed copy (a database file held open): the reason
//                    shows, closing keeps P with N1 ready, Try again in the
//                    window copies and starts N1.
//   N2 (launcher 3)  killed during the copy, before the fence: reopening runs
//                    N1, which removes the partial copy; installing N2 again
//                    makes a new one.
//   N3 (launcher 4)  killed after PostgreSQL started: the old launcher offers
//                    only Try Again, which makes no second copy; once healthy
//                    the launcher is swapped to 4 and confirmed.
//
// Publishes nothing and uses no secret. The release key is ephemeral: its
// public half replaces the key in the installed app.asar and in each staged
// source. Two modes:
//
//   prepare <folder>   (any machine with git and python, in this checkout)
//     the ephemeral key and four source archives of HEAD, each changed only
//     by that key, as the update feed would serve them.
//   run <installer.exe> <prepared folder> <output folder>   (Windows)
//     installs the app, stages each revision from a local feed, drives the
//     app and writes summary.txt and acceptance.json to the output folder.
//
// `run` installs an app and kills its processes, so it runs only on a
// disposable machine: the late-restore-acceptance job of
// desktop-installer.yml (a GitHub-hosted Windows runner), or with
// SHUFFLE_ACCEPTANCE_THROWAWAY=1 on a machine you will throw away. It refuses
// an existing install, profile or worker data.
import assert from 'node:assert/strict';
import { generateKeyPairSync, createPrivateKey, sign } from 'node:crypto';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { installPreview } from './install-preview.mjs';
import { readLauncher } from './launcher-contract.mjs';

const [mode, ...args] = process.argv.slice(2);
const desktop = path.resolve(import.meta.dirname, '..'), repo = path.resolve(desktop, '..');
const NAMES = ['P', 'N1', 'N2', 'N3'];
// Added to the cluster so a copy takes long enough to be killed part way.
const FILLER_GB = Number(process.env.SHUFFLE_ACCEPTANCE_FILLER_GB ?? 3);
if (mode === 'prepare') {
  assert(args[0], 'usage: verify-late-restore-point.mjs prepare <folder>');
  const out = path.resolve(args[0]);
  await fs.mkdir(out, { recursive: true });
  assert.equal((await fs.readdir(out)).length, 0, 'The prepare folder must be empty');
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const pem = publicKey.export({ type: 'spki', format: 'pem' });
  // Per-command config: never change the checkout's own git settings. No CRLF
  // conversion, so the archive is what the feed would serve.
  const gitEnv = { ...process.env, GIT_INDEX_FILE: path.join(out, 'acceptance.index'), GIT_CONFIG_COUNT: '1', GIT_CONFIG_KEY_0: 'core.autocrlf', GIT_CONFIG_VALUE_0: 'false',
    GIT_AUTHOR_NAME: 'acceptance', GIT_AUTHOR_EMAIL: 'acceptance@invalid', GIT_COMMITTER_NAME: 'acceptance', GIT_COMMITTER_EMAIL: 'acceptance@invalid' };
  const git = (argv, input) => execFileSync('git', argv, { cwd: repo, env: gitEnv, input, encoding: 'utf8' }).trim();
  const head = git(['rev-parse', 'HEAD']);
  const revisions = {};
  for (const name of NAMES) {
    git(['read-tree', head]);
    git(['update-index', '--cacheinfo', `100644,${git(['hash-object', '-w', '--no-filters', '--stdin'], pem)},desktop/build/application-update-key.pem`]);
    const revision = git(['commit-tree', git(['write-tree']), '-p', head, '-m', `Late restore point acceptance ${name}`]);
    const zip = path.join(out, name + '.zip');
    const archive = JSON.parse(execFileSync('python', ['scripts/source_archive.py', revision, zip], { cwd: repo, env: gitEnv, encoding: 'utf8' }));
    revisions[name] = { revision, zip: name + '.zip', sha256: archive.sha256, size: archive.size, tree: archive.tree };
    console.log(`${name} ${revision}`);
  }
  await fs.rm(path.join(out, 'acceptance.index'), { force: true });
  await fs.writeFile(path.join(out, 'public.pem'), pem);
  // Ephemeral: trusted only by the throwaway install this run makes.
  await fs.writeFile(path.join(out, 'private.pem'), privateKey.export({ type: 'pkcs8', format: 'pem' }));
  await fs.writeFile(path.join(out, 'revisions.json'), JSON.stringify({ head, revisions }, null, 2));
  console.log('Prepared ' + out);
  process.exit(0);
}
assert.equal(mode, 'run', 'usage: verify-late-restore-point.mjs prepare <folder> | run <installer.exe> <prepared folder> <output folder>');
assert(process.platform === 'win32', 'Windows only');
assert(process.env.CI === 'true' || process.env.SHUFFLE_ACCEPTANCE_THROWAWAY === '1', 'Disposable machines only: a CI runner, or SHUFFLE_ACCEPTANCE_THROWAWAY=1 on a machine you will throw away');
process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS = '1';
const require = createRequire(import.meta.url);
// This checkout's built launcher code (npm run build), the code being accepted.
const { SourceUpdates } = require('../dist/main/source-updates.js');
const { SOURCE_ORIGIN } = require('../dist/main/source-auth.js');
const { readAsarMember } = require('../dist/main/asar.js');
const asar = require('@electron/asar');

const [installerArg, preparedArg, outputArg] = args;
assert(installerArg && preparedArg && outputArg, 'usage: verify-late-restore-point.mjs run <installer.exe> <prepared folder> <output folder>');
const installer = path.resolve(installerArg), prepared = path.resolve(preparedArg), output = path.resolve(outputArg);
const work = path.join(process.env.LOCALAPPDATA, 'shuffle-late-acceptance');
const install = path.join(work, 'app'), profile = path.join(process.env.APPDATA, 'ai-shuffle-desktop');
const sourceRoot = path.join(profile, 'updates'), exe = path.join(install, 'AI Shuffle.exe'), resources = path.join(install, 'resources');
const appAsar = path.join(resources, 'app.asar');
const pgRoot = path.join(profile, 'postgres'), pgData = path.join(pgRoot, 'data'), snapshotDir = path.join(pgRoot, 'update-snapshot');
const journalFile = path.join(sourceRoot, 'restore-point.json'), stateFile = path.join(sourceRoot, 'state.json');
const floorFile = path.join(sourceRoot, 'database-floor.json'), markerFile = path.join(sourceRoot, 'launcher-swap.json');

const exists = p => fs.stat(p).then(() => true, e => { if (e.code === 'ENOENT') return false; throw e; });
const readJson = async p => { try { return JSON.parse(await fs.readFile(p, 'utf8')); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, message, timeout = 300000, every = 250) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await sleep(every); }
  throw Error(message);
}

assert(!await exists(install) && !await exists(profile), 'Refusing to use an existing installation or profile');
assert(!await exists(path.join(process.env.USERPROFILE, '.ai-shuffle')), 'Fresh worker data required');
await fs.mkdir(output, { recursive: true });
const report = { success: false, steps: [] };
let current = null;
const step = name => { current = { name, checks: [], result: 'running' }; report.steps.push(current); console.log(`\n== ${name}`); };
const pass = message => { current.checks.push(message); console.log('PASS: ' + message); };
const note = message => { (current.notes ??= []).push(message); console.log('NOTE: ' + message); };
// Safety checks (fence before PostgreSQL, no stale or second copy, no older code
// after a failed start) stay fatal assertions. Everything else is recorded as a
// failure and the run goes on, so one run shows every remaining problem.
const fail = message => { (current.failures ??= []).push(message); console.log('FAIL: ' + message); };
const expect = (ok, message, failure = message) => { if (ok) pass(message); else fail(failure); return !!ok; };
/** A wait that is not a safety check: on timeout record it and go on. */
async function expectWithin(fn, message, failure, timeout) {
  try { await until(fn, failure, timeout); pass(message); return true; } catch (error) { fail(String(error?.message ?? error)); return false; }
}
const finish = () => { current.result = current.failures?.length ? 'fail' : 'pass'; };
const launchLog = path.join(work, 'launch-output.log');
await fs.mkdir(work, { recursive: true });
const log = await fs.open(launchLog, 'a');
// As verify-launcher-swap.mjs.
const redact = line => line.replace(/github_pat_[A-Za-z0-9_]+|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{16,}/g, '[redacted]')
  .replace(/\bBearer\s+[^\s"',]+/gi, 'Bearer [redacted]').replace(/:\/\/[^\/\s:@]+:[^@\s]+@/g, '://[redacted]@')
  .replace(/\b([\w-]*(?:token|password|passwd|secret|authorization|api[_-]?key|cookie)[\w-]*)(["']?\s*[:=]\s*["']?)[^\s"',&]+/gi, '$1$2[redacted]');

// --- The running app, driven over the Chromium debugger like verify-launcher-swap.mjs.
let chromium, browser, browserPort = null;
/**
 * Pages of the running app. One connection per app process, kept open: page
 * handles taken from it stay usable while other code polls (the launcher and
 * the app it starts each write their own DevToolsActivePort).
 */
async function debuggerPages() {
  const port = (await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split(/\r?\n/)[0];
  assert.match(port, /^[1-9]\d{0,4}$/);
  if (!browser?.isConnected() || browserPort !== port) {
    if (browser) await browser.close().catch(() => {});
    browser = null; browserPort = null;
    browser = await chromium.connectOverCDP('http://127.0.0.1:' + port, { timeout: 1500 });
    browserPort = port;
  }
  return browser.contexts().flatMap(c => c.pages());
}
/** The worker UI once it answers, else null. */
async function workerPage() {
  try {
    for (const page of await debuggerPages()) if (page.url().startsWith('http://127.0.0.1:8000/') && await page.evaluate(() => !!window.shuffleDesktop)) return page;
  } catch { /* not up yet */ }
  return null;
}
/** The startup status page (pages/worker.html), else null. */
async function statusPage() {
  try {
    for (const page of await debuggerPages()) if (await page.evaluate(() => !!window.shuffleDesktopWorker).catch(() => false)) return page;
  } catch { /* not up yet */ }
  return null;
}
/**
 * Every status detail the page shows from now until it navigates away: a
 * MutationObserver reports each change over the console, and a poll backs it up.
 */
async function recordDetails(page) {
  const seen = new Set(); let stopped = false;
  page.on('console', message => { const text = message.text(); if (text.startsWith('acceptance-detail:')) seen.add(text.slice(18)); });
  await page.evaluate(() => {
    const detail = document.getElementById('status-detail');
    new MutationObserver(() => console.log('acceptance-detail:' + detail.textContent)).observe(detail, { childList: true, characterData: true, subtree: true });
  });
  const poll = (async () => {
    while (!stopped) { try { seen.add((await statusText(page)).detail); } catch { /* navigating */ } await sleep(50); }
  })();
  return { seen, stop: async () => { stopped = true; await poll; } };
}
const statusText = page => page.evaluate(() => ({
  heading: document.getElementById('status-heading')?.textContent ?? '',
  detail: document.getElementById('status-detail')?.textContent ?? '',
  error: document.getElementById('status-error')?.textContent ?? '',
}));
function launch() {
  return fs.rm(path.join(profile, 'DevToolsActivePort'), { force: true }).then(() => {
    const child = spawn(exe, ['--remote-debugging-port=0'], { windowsHide: true, stdio: ['ignore', log.fd, log.fd] });
    child.on('error', e => console.error(e));
  });
}
async function ready(revision) {
  const page = await until(workerPage, 'The worker UI did not open', 900000, 1000);
  if (revision) assert.equal(await page.evaluate(() => window.shuffleDesktop.version), 'main@' + revision.slice(0, 12));
  return page;
}
const healthy = async () => { try { return (await fetch('http://127.0.0.1:8000/health/ready', { signal: AbortSignal.timeout(500) })).ok; } catch { return false; } };
/** Close through the window, as a person would. */
async function close() {
  const page = (await workerPage()) ?? (await statusPage());
  if (page) await page.evaluate(() => window.shuffleDesktop.window.close()).catch(() => {});
  await until(async () => !await healthy(), 'The worker did not stop', 120000);
  await until(async () => !appRunning(), 'AI Shuffle did not exit after its window closed', 120000, 1000);
  await settle();
}
const appRunning = () => /AI Shuffle\.exe/i.test(execFileSync('tasklist.exe', ['/FI', 'IMAGENAME eq AI Shuffle.exe', '/NH'], { encoding: 'utf8' }));
/** Hard kill: the app's process tree at once, then anything else running from the install or the profile (worker, PostgreSQL). */
async function kill() {
  try { execFileSync('taskkill.exe', ['/F', '/T', '/IM', 'AI Shuffle.exe'], { stdio: 'ignore' }); } catch { /* none running */ }
  execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    "Get-Process | Where-Object { $_.Path -and ($_.Path.StartsWith($env:KILL_A, 'OrdinalIgnoreCase') -or $_.Path.StartsWith($env:KILL_B, 'OrdinalIgnoreCase')) } | Stop-Process -Force -ErrorAction SilentlyContinue"],
  { env: { ...process.env, KILL_A: install, KILL_B: profile }, stdio: 'inherit' });
  await settle();
}
async function settle() {
  if (browser) await browser.close().catch(() => {}); browser = null; browserPort = null;
  // The single-instance lock is held until the previous process has exited.
  await sleep(5000);
}
/**
 * Press `button` in a message box through UI Automation and return the names
 * of every button in that box; null while no such button is shown.
 */
function pressDialogButton(button) {
  const script = `
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [Windows.Automation.AutomationElement]
$byName = New-Object Windows.Automation.PropertyCondition($A::NameProperty, $env:BUTTON)
$target = $A::RootElement.FindFirst([Windows.Automation.TreeScope]::Descendants, $byName)
if (-not $target) { exit 3 }
$walker = [Windows.Automation.TreeWalker]::ControlViewWalker
$box = $target
while (($parent = $walker.GetParent($box)) -and -not $parent.Equals($A::RootElement)) { $box = $parent }
$isButton = New-Object Windows.Automation.PropertyCondition($A::ControlTypeProperty, [Windows.Automation.ControlType]::Button)
$names = @($box.FindAll([Windows.Automation.TreeScope]::Descendants, $isButton) | ForEach-Object { $_.Current.Name })
$target.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
ConvertTo-Json -Compress @{ title = $box.Current.Name; buttons = $names }`;
  try {
    return JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { env: { ...process.env, BUTTON: button }, encoding: 'utf8' }));
  } catch (error) { if (error.status === 3) return null; throw error; }
}
const holders = new Set();
/** Hold `file` open with no sharing, as a backup or antivirus scan might; resolves to its release. */
async function lock(file) {
  const holder = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    "$f = [IO.File]::Open($env:LOCK_FILE, 'Open', 'ReadWrite', 'None'); [Console]::Out.WriteLine('locked'); [void][Console]::In.ReadLine(); $f.Close()"],
  { env: { ...process.env, LOCK_FILE: file }, stdio: ['pipe', 'pipe', 'inherit'] });
  await new Promise((resolve, reject) => {
    holder.on('exit', code => reject(Error('The file lock ended early: ' + code)));
    holder.stdout.on('data', data => { if (/locked/.test(String(data))) resolve(); });
  });
  holder.removeAllListeners('exit'); holders.add(holder);
  return () => new Promise(resolve => { holder.on('exit', resolve); holder.stdin.end('\n'); });
}
/** Samples of the update files while the app starts. */
function watch() {
  const samples = []; let stopped = false;
  const loop = (async () => {
    while (!stopped) {
      try {
        const [journal, state, floor, pg, manifest] = await Promise.all([readJson(journalFile), readJson(stateFile), readJson(floorFile),
          exists(path.join(pgData, 'postmaster.pid')), fs.stat(path.join(snapshotDir, 'manifest.json')).then(s => s.mtimeMs, () => null)]);
        samples.push({ at: Date.now(), journal, state, floor, pg, manifest });
      } catch { /* a file mid-rename; sample again */ }
      await sleep(100);
    }
  })();
  return { samples, stop: async () => { stopped = true; await loop; } };
}

// --- The prepared revisions, signed under the ephemeral key as the feed would serve them.
const plan = JSON.parse(await fs.readFile(path.join(prepared, 'revisions.json'), 'utf8'));
const pem = await fs.readFile(path.join(prepared, 'public.pem'), 'utf8');
const privateKey = createPrivateKey(await fs.readFile(path.join(prepared, 'private.pem'), 'utf8'));
let build, sequence;
const updater = feed => new SourceUpdates({ root: sourceRoot, channel: 'stable', publicKey: pem, baseline: { revision: build.revision, sequence: build.sequence },
  bootstrapNode: path.join(resources, 'worker/node/node.exe'), fetch: async url => feed[String(url)]?.() ?? new Response('not found', { status: 404 }) });
/** Download and build a revision with this checkout's updater from a local feed; it becomes pending. */
async function stage(name, launcher) {
  const { revision: rev, zip, sha256, size, tree } = plan.revisions[name];
  const manifest = { format: 2, revision: rev, sequence: ++sequence, sha256, size, channel: 'stable', tree, desktop: { launcher } };
  const payload = Buffer.from(JSON.stringify(manifest));
  const signed = JSON.stringify({ payload: payload.toString('base64'), signature: sign(null, payload, privateKey).toString('base64') });
  const updates = updater({ [SOURCE_ORIGIN + '/v1/desktop/manifest']: async () => new Response(signed),
    [SOURCE_ORIGIN + '/v1/desktop/sources/' + rev]: async () => new Response(await fs.readFile(path.join(prepared, zip))) });
  await updates.load();
  let last = '';
  console.log(`Building ${name} (${rev.slice(0, 12)}, launcher ${launcher})…`);
  assert.equal((await updates.download((_, text) => { if (text && text !== last) console.log(last = text); })).kind, 'ready');
  assert.equal((await readJson(stateFile)).pending, rev);
  return rev;
}
/** What choosing "Restart to update" does for a staged build. */
async function install_(rev) {
  const updates = updater({}); await updates.load(); updates.stageDownloads(); await updates.prepareInstall();
  assert.equal((await readJson(stateFile)).pending, rev);
}

try {
  step('Install the 0.9.6 installer and start its bundled app');
  await installPreview(installer, install, () => console.log('Installing…'));
  assert(await exists(exe));
  build = await readJson(path.join(resources, 'worker/desktop-build.json'));
  sequence = build.sequence;
  report.installer = { revision: build.revision, sequence: build.sequence, launcher: build.launcher ?? 0 };
  assert((build.launcher ?? 0) <= 3, 'This needs an installer whose launcher predates restore points (0.9.6)');
  const unpacked = path.join(work, 'old-launcher'); asar.extractAll(appAsar, unpacked);
  assert(!/prepareRollback/.test(await fs.readFile(path.join(unpacked, 'dist/main/installed-entry.js'), 'utf8')), 'This installer\'s launcher already makes restore points');
  await fs.writeFile(path.join(unpacked, 'build/application-update-key.pem'), pem);
  await fs.rm(appAsar); await asar.createPackage(unpacked, appAsar);
  ({ chromium } = await import(pathToFileURL(path.join(resources, 'worker/server/browser-mcp/node_modules/playwright-core/index.mjs')).href));
  await launch(); await ready(); await close();
  pass(`Installer ${report.installer.revision.slice(0, 12)} (launcher ${report.installer.launcher}, no restore points) starts with the ephemeral key`);
  finish();

  const target = readLauncher(await fs.readFile(path.join(desktop, 'launcher.json'), 'utf8')).version;
  const [P, N1, N2, N3] = NAMES.map(name => plan.revisions[name].revision);
  report.revisions = { head: plan.head, P, N1, N2, N3 };

  step('P: an update over the bundled build starts with no copy');
  await stage('P', 3);
  let w = watch(); await launch(); await ready(P); await w.stop();
  await until(async () => (await readJson(stateFile)).active === P, 'P did not become healthy', 120000);
  expect(!w.samples.some(s => s.journal), 'P started healthy; no restore-point.json at any time', 'A restore point was made for a bundled previous build');
  await close();
  // A database large enough that the copy can be watched and interrupted.
  const filler = await fs.open(path.join(pgData, 'zz-acceptance-filler.bin'), 'w'); await filler.truncate(FILLER_GB * 1024 ** 3); await filler.close();
  pass(`Added a ${FILLER_GB} GB file to the stopped cluster so a copy takes long enough to interrupt`);
  finish();

  step('N1: a failed copy shows its reason; closing keeps P with N1 ready; Try again starts N1');
  await stage('N1', 3);
  const fillerFile = path.join(pgData, 'zz-acceptance-filler.bin');
  let unlock = await lock(fillerFile);
  await launch();
  const failurePage = message => until(async () => { const p = await statusPage(); return p && /restore point/.test((await statusText(p)).error) ? p : null; }, message, 600000, 1000);
  let page = await failurePage('The copy failure was not shown');
  let text = await statusText(page);
  report.failurePage = text;
  expect(text.heading === 'The update could not start yet', 'The failure page heading is "The update could not start yet"', 'Failure page heading: ' + JSON.stringify(text.heading));
  expect(/^Could not prepare the update's database restore point: .*EBUSY.*\. A file in the AI Shuffle data folder is in use\. .* choose Try again\. If it keeps failing, close AI Shuffle and open it again/.test(text.error),
    'The failure names the locked file and gives the in-use advice', 'Failure text: ' + JSON.stringify(text.error));
  let state = await readJson(stateFile);
  // Safety: a failed copy never fences the update or starts PostgreSQL.
  assert.equal(state.booting, N1); assert.equal(state.waiting, N1); assert.equal(state.mayHaveMigrated?.[N1], undefined);
  assert(!await exists(path.join(pgData, 'postmaster.pid')), 'PostgreSQL started after a failed copy');
  pass('No fence and PostgreSQL not started after the failed copy');
  expect(!await exists(journalFile) && !await exists(snapshotDir), 'No journal and no partial copy left', 'A journal or a partial copy was left after the failed copy');
  await close(); await launch(); await ready(P);
  assert.equal((await readJson(stateFile)).staged, N1, 'N1 is not staged after closing');
  pass('Closing and reopening runs P, with N1 still ready to install');
  await close(); await install_(N1);
  await launch();
  page = await failurePage('The copy failure was not shown again');
  await unlock();
  w = watch();
  const details = await recordDetails(page);
  await page.click('#restart');
  await ready(N1); await w.stop(); await details.stop();
  report.retryDetails = [...details.seen];
  const readyBeforeStart = w.samples.find(s => s.journal?.late && s.journal.phase === 'ready' && s.journal.revision === N1);
  // Safety: the point is ready only together with the fence.
  assert(readyBeforeStart, 'No ready late restore point was seen');
  assert.equal(readyBeforeStart.floor?.revision, N1, 'The point became ready before N1 was fenced');
  assert.notEqual(readyBeforeStart.state?.mayHaveMigrated?.[N1], undefined);
  if (readyBeforeStart.pg) note('PostgreSQL was already running at the first sample of the ready point (sampling is every 100 ms)');
  else pass('The ready late point was seen with N1 fenced (floor N1) before PostgreSQL started');
  expect([...details.seen].some(text => /Preparing the update's database restore point/.test(text)), 'The status page showed "Preparing the update\'s database restore point…"',
    'The status page did not show "Preparing the update\'s database restore point…" during the copy; it showed: ' + JSON.stringify([...details.seen]));
  await expectWithin(async () => (await readJson(stateFile)).active === N1 && !await exists(journalFile) && !await exists(snapshotDir),
    'With the lock released, Try again copied and started N1 in its own process; the point and update-snapshot were removed once healthy', 'N1 did not become active with the point removed', 120000);
  expect(await readJson(markerFile) === null, 'Launcher 3 releases do not swap the launcher', 'A launcher 3 release swapped the launcher');
  await close();
  finish();

  step('N2: killed during the copy, before the fence; reopening runs N1; installing N2 again replaces the stale copy');
  await stage('N2', 3);
  await launch();
  await until(async () => { const j = await readJson(journalFile).catch(() => null); return j?.late && j.phase === 'preparing' && j.revision === N2; }, 'The copy did not start', 600000, 50);
  await kill();
  state = await readJson(stateFile);
  assert.equal(state.waiting, N2, 'N2 was fenced before the kill; the copy was too fast to interrupt (raise SHUFFLE_ACCEPTANCE_FILLER_GB)');
  assert.equal((await readJson(journalFile)).phase, 'preparing');
  pass('Killed N2 mid-copy: journal preparing, N2 not fenced');
  await launch(); await ready(N1);
  assert.equal((await readJson(stateFile)).staged, N2, 'N2 is not staged after the kill');
  pass('Reopening ran N1 (the previous version), N2 staged');
  await expectWithin(async () => !await exists(journalFile) && !await exists(snapshotDir), 'N1 removed the abandoned partial copy once healthy', 'N1 did not remove the abandoned copy', 120000);
  await close(); await install_(N2);
  const reinstalled = Date.now();
  w = watch(); await launch(); await ready(N2); await w.stop();
  const fresh = w.samples.find(s => s.journal?.late && s.journal.phase === 'ready' && s.journal.revision === N2);
  // Safety: never start with a copy older than the data the previous build wrote since.
  assert(fresh && fresh.manifest > reinstalled, 'The stale copy was not replaced by a new one');
  pass('Installing N2 again made a new copy');
  await expectWithin(async () => (await readJson(stateFile)).active === N2 && !await exists(journalFile), 'N2 started and its point was removed once healthy', 'N2 did not finish', 120000);
  await close();
  finish();

  step('N3: killed after PostgreSQL started; the old launcher offers only Try Again, which makes no second copy; then the swap');
  await stage('N3', target);
  await launch();
  const fenced = await until(async () => {
    const [j, s, pg] = await Promise.all([readJson(journalFile).catch(() => null), readJson(stateFile).catch(() => null), exists(path.join(pgData, 'postmaster.pid'))]);
    return j?.late && j.phase === 'ready' && j.revision === N3 && s?.mayHaveMigrated?.[N3] !== undefined && pg ? j : null;
  }, 'N3 did not reach a started database', 600000, 50);
  const copied = (await fs.stat(path.join(snapshotDir, 'manifest.json'))).mtimeMs;
  await kill();
  assert.equal(fenced.revision, N3);
  pass('Killed N3 after PostgreSQL started, with a ready late point');
  await launch();
  await sleep(20000);
  assert(!await healthy(), 'A worker started after the failed first start');
  state = await readJson(stateFile);
  assert(state.failed?.includes(N3) && state.blocked === true, 'The old launcher did not fence the failed start');
  pass('The old launcher did not run P, N1 or N2: N3 failed and fenced');
  const dialog = await until(async () => pressDialogButton('Try Again'), 'The "did not finish starting" dialog did not appear', 120000, 2000);
  report.dialog = dialog;
  const buttons = [...dialog.buttons].filter(name => !/^(Close|Minimize|Maximize)$/.test(name)).sort();
  expect(JSON.stringify(buttons) === JSON.stringify(['Quit', 'Try Again']), 'The old launcher offered only Try Again and Quit', 'The old launcher offered: ' + JSON.stringify(dialog.buttons));
  w = watch(); await ready(N3); await w.stop();
  // Safety: the retry keeps the copy taken before the database started.
  assert(w.samples.every(s => s.manifest === null || s.manifest === copied), 'Try Again made a second copy');
  pass('Try Again ran N3 without a second copy');
  await expectWithin(async () => (await readJson(stateFile)).active === N3 && !await exists(journalFile) && !await exists(snapshotDir), 'The point was removed once N3 was healthy', 'The point was not removed after the healthy retry', 120000);
  let swap = null;
  try {
    swap = await until(async () => { const m = await readMarker(); if (m?.phase === 'rejected') throw Error('Self-test rejected the new launcher: ' + m.reason); return m?.phase === 'trial' ? m : null; }, 'The launcher was not replaced', 300000);
  } catch (error) { fail(String(error?.message ?? error)); }
  if (swap) {
    const swapped = readLauncher(readAsarMember(require('node:fs'), appAsar, 'launcher.json').toString('utf8')).version;
    expect(swap.revision === N3 && swap.to === target && swapped === target, `The launcher was swapped to ${target} after the healthy start`, `Swap marker ${JSON.stringify(swap)}, installed launcher ${swapped}`);
    await close(); await launch(); await ready(N3);
    await expectWithin(async () => (await readMarker())?.phase === 'confirmed', `Launcher ${target} started N3 and was confirmed`, 'The new launcher was not confirmed', 60000);
  }
  await close();
  finish();
  report.success = report.steps.every(s => s.result === 'pass');
} catch (error) {
  if (current) { current.result = 'fail'; current.error = String(error?.stack ?? error); }
  console.error(error);
  try { report.state = await readJson(stateFile); report.journal = await readJson(journalFile); report.floor = await readJson(floorFile); report.marker = await readMarker(); } catch { /* best effort */ }
} finally {
  for (const holder of holders) holder.kill();
  await kill().catch(() => {});
  await log.close();
  // Evidence is uploaded where others can read it: tails only, credentials redacted.
  for (const [folder, name] of [['logs', 'worker.log'], ['logs', 'postgres.log'], ['updates', 'build.log'], ['updates', 'launch.log']]) {
    try { const text = await fs.readFile(path.join(profile, folder, name), 'utf8'); await fs.writeFile(path.join(output, name), text.slice(-8 * 1024 ** 2).split(/\r?\n/).map(redact).join('\n')); }
    catch { /* not written in this run */ }
  }
  try { await fs.writeFile(path.join(output, 'launch-output.log'), (await fs.readFile(launchLog, 'utf8')).split(/\r?\n/).map(redact).join('\n')); } catch { /* none */ }
  await fs.writeFile(path.join(output, 'acceptance.json'), JSON.stringify(report, null, 2));
  const lines = report.steps.map(s => [`${s.result.toUpperCase().padEnd(7)} ${s.name}`,
    ...(s.failures ?? []).map(f => '        FAIL: ' + f), ...(s.error ? ['        FATAL: ' + s.error.split('\n')[0]] : [])].join('\n'));
  const summary = [`Late restore point acceptance: ${report.success ? 'PASS' : 'FAIL'}`, ...lines].join('\n');
  await fs.writeFile(path.join(output, 'summary.txt'), summary + '\n');
  console.log('\n' + summary);
  if (!report.success) process.exitCode = 1;
}
function readMarker() { return readJson(markerFile); }
