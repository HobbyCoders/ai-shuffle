import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { getConfig } from './builder-config.mjs';

const require = createRequire(import.meta.url);
const desktop = fileURLToPath(new URL('../..', import.meta.url));
const execute = promisify(execFile);
const psArgs = script => ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')];
const stampPath = folder => path.join(folder, 'resources', 'login-item-uninstall-v4');
const unavailable = { state: 'unavailable', failed: true };
const enabled = { state: 'enabled', failed: false };
const disabled = { state: 'disabled', failed: false };

// Spawn both interactive fixtures ahead of races. Fail with their actual stderr
// if they die or do not acknowledge a command, rather than hanging the shard.
function session(exe, args, env) {
  const child = spawn(exe, args, { env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  let stderr = '';
  child.stdin.on('error', () => {});
  child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
  const closed = new Promise(resolve => {
    child.once('error', error => resolve(String(error)));
    child.once('close', code => resolve(code));
  });
  return {
    async next() {
      const timeout = new AbortController();
      try {
        const result = await Promise.race([lines.next(), closed.then(() => ({ done: true })), delay(20_000, { done: true }, { signal: timeout.signal })]);
        assert.equal(result.done, false, `fixture ${exe} stopped answering: ${stderr}`);
        return result.value;
      } finally { timeout.abort(); }
    },
    send(line) { child.stdin.write(line + '\n'); },
    async stop() { child.kill(); await closed; }
  };
}

async function compileInstaller(root, id, product) {
  // Compile the pinned assisted-installer template with the production NSIS
  // include/options, but only tiny inert payload files and a unique app identity.
  const config = await getConfig(desktop, 'electron-builder.yml', null);
  const input = path.join(root, 'payload');
  await fs.mkdir(path.join(input, 'resources'), { recursive: true });
  await fs.writeFile(path.join(input, `${product}.exe`), 'not a launched executable');
  await fs.writeFile(path.join(input, 'resources', 'locked-payload'), 'fixture bytes');
  await fs.writeFile(path.join(root, 'package.json'), JSON.stringify({ name: product, version: '1.0.0', description: 'Isolated startup test', author: 'AI Shuffle' }));
  const configPath = path.join(root, 'fixture.json');
  await fs.writeFile(configPath, JSON.stringify({
    appId: id, productName: product, electronVersion: require('electron/package.json').version,
    artifactName: 'fixture.exe', directories: { output: 'out' },
    nsis: { ...config.nsis, include: path.join(desktop, config.nsis.include), shortcutName: product,
      createDesktopShortcut: false, createStartMenuShortcut: false }
  }));
  try {
    await execute(process.execPath, [require.resolve('electron-builder/cli.js'), '--projectDir', root, '--config', configPath,
      '--prepackaged', input, '--win', 'nsis', '--x64', '--publish', 'never'], {
      env: { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false' }, windowsHide: true, timeout: 160_000
    });
  } catch (error) { throw new Error(error.stdout + error.stderr, { cause: error }); }
  return path.join(root, 'out', 'fixture.exe');
}

export async function exerciseWindowsStartup(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle startup '));
  const product = `shuffle-test-${randomUUID()}`;
  const id = `com.hobbycoders.${product}`;
  const env = { ...process.env, FIXTURE_ROOT: root, FIXTURE_ID: id };
  const sessions = [];
  const start = (exe, args, additions) => {
    const proc = session(exe, args, { ...env, ...additions });
    sessions.push(proc);
    return proc;
  };
  const ps = async (script, additions) => (await execute('powershell.exe', psArgs("$ErrorActionPreference = 'Stop'\n" + script), {
    timeout: 12_000, env: { ...env, ...additions }, windowsHide: true
  })).stdout.trim();
  const registry = (operation, command = '') => ps(String.raw`
    $rk = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Microsoft\Windows\CurrentVersion\Run')
    $ak = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run')
    switch ($env:FIXTURE_OPERATION) {
      'seed-external' {
        $rk.SetValue($env:FIXTURE_ID + '.external', 'external-command', 'String')
        $ak.SetValue($env:FIXTURE_ID + '.external', [byte[]](3,0,0,0,1,2,3,4,5,6,7,8), 'Binary')
      }
      'seed-blocked' { $ak.SetValue($env:FIXTURE_ID, [byte[]](3,0,0,0,1,2,3,4,5,6,7,8), 'Binary') }
      'seed-foreign' { $rk.SetValue($env:FIXTURE_ID, $env:FIXTURE_COMMAND, 'String') }
      'clean' {
        foreach ($name in @($env:FIXTURE_ID, ($env:FIXTURE_ID + '.external'))) {
          $rk.DeleteValue($name, $false); $ak.DeleteValue($name, $false)
        }
      }
      'read' {
        @($env:FIXTURE_ID, ($env:FIXTURE_ID + '.external')) | ForEach-Object {
          @{run=$rk.GetValue($_); approval=$ak.GetValue($_)}
        } | ConvertTo-Json -Compress
      }
    }
  `, { FIXTURE_OPERATION: operation, FIXTURE_COMMAND: command });
  const read = async () => JSON.parse(await registry('read'));
  const unregistered = { run: null, approval: null };
  const approval = [3,0,0,0,1,2,3,4,5,6,7,8];
  const external = { run: 'external-command', approval };
  const state = expected => read().then(actual => assert.deepEqual(actual, [expected, external]));
  const runInstaller = (exe, args) => execute(exe, ['/S', '/currentuser', ...args], {
    env, windowsHide: true, windowsVerbatimArguments: true, timeout: 45_000
  });
  let uninstaller;
  let touchedRegistry = false;
  try {
    const installer = await compileInstaller(root, id, product);
    const target = path.join(root, 'install one');
    const moved = path.join(root, 'install two');
    const install = folder => runInstaller(installer, [`/D=${folder}`]);
    const copyUninstaller = async folder => {
      const name = (await fs.readdir(folder)).find(file => /^Uninstall.*\.exe$/i.test(file));
      assert.ok(name, 'the real installer produced an uninstaller');
      uninstaller = path.join(root, 'uninstaller-copy.exe');
      await fs.copyFile(path.join(folder, name), uninstaller);
    };
    const remove = (folder, ...args) => runInstaller(uninstaller, ['/KEEP_APP_DATA', ...args, `_?=${folder}`]);
    const executable = folder => path.join(folder, `${product}.exe`);
    const harness = path.join(root, 'desktop-writer.cjs');
    await fs.writeFile(harness, `
      const { app } = require('electron');
      const { loginItemControl } = require(${JSON.stringify(path.join(desktop, 'dist', 'main', 'login-item.js'))});
      app.setPath('userData', process.env.FIXTURE_PROFILE);
      app.setAppUserModelId(process.env.FIXTURE_ID);
      app.whenReady().then(async () => {
        const control = loginItemControl(app, { platform: process.platform, preview: false, packaged: false,
          installedExecutable: process.env.FIXTURE_EXE, localAppData: process.env.LOCALAPPDATA, appId: process.env.FIXTURE_ID });
        console.log(JSON.stringify(control?.get() ?? 'no capability'));
        for await (const line of require('node:readline').createInterface({ input: process.stdin })) {
          const result = line === 'get' ? control.get() : await control.set(line === 'on');
          console.log(JSON.stringify(result));
        }
        app.exit(0);
      }).catch(error => { console.error(error); app.exit(1); });
    `);
    async function writer(folder, expected = 'disabled') {
      const profile = await fs.mkdtemp(path.join(root, 'profile'));
      const desktopWriter = start(require('electron'), [`--user-data-dir=${profile}`, harness], {
        FIXTURE_EXE: executable(folder), FIXTURE_PROFILE: profile
      });
      const reply = async () => JSON.parse(await desktopWriter.next());
      assert.equal(await reply(), expected);
      return async command => { desktopWriter.send(command); return reply(); };
    }

    await install(target);
    const original = await fs.readFile(stampPath(target));
    const command = `"${executable(target)}"`;
    const first = await writer(target);
    assert.deepEqual((await read())[0], unregistered, 'install and startup are opt-in');
    touchedRegistry = true;
    await registry('seed-external');
    assert.deepEqual(await first('on'), enabled);
    await state({ run: command, approval: null });
    await registry('seed-blocked');
    const blocked = { run: command, approval };
    assert.equal(await first('get'), 'blocked');

    await copyUninstaller(target);
    const payload = path.join(target, 'resources', 'locked-payload');
    const holder = start('powershell.exe', psArgs(`
      $ErrorActionPreference = 'Stop'
      $file = [IO.File]::Open($env:FIXTURE_PAYLOAD, 'Open', 'Read', 'Read')
      try { [Console]::WriteLine('held'); [void][Console]::ReadLine() } finally { $file.Dispose() }
    `), { FIXTURE_PAYLOAD: payload });
    assert.equal(await holder.next(), 'held');
    try {
      await assert.rejects(remove(target, '--updated'), error => typeof error.code === 'number' && error.code !== 0 && !error.killed);
      assert.deepEqual(await fs.readFile(stampPath(target)), original, 'rollback restores the exact generation');
      assert.ok(existsSync(executable(target)) && existsSync(payload), 'rollback restored the installed payload');
      await state(blocked);
      assert.equal(await first('get'), 'blocked');
    } finally { await holder.stop(); }
    t.diagnostic('Locked-payload replacement rollback preserved Run, StartupApproved and the installed generation.');

    await install(target);
    assert.notDeepEqual(await fs.readFile(stampPath(target)), original, 'replacement is a fresh generation');
    await state(blocked);
    assert.deepEqual(await first('off'), unavailable, 'an old desktop cannot write into a replacement generation');
    const second = await writer(target, 'blocked');
    assert.deepEqual(await second('off'), disabled);
    await state(unregistered);
    t.diagnostic('Same-directory install preserved the OS choice; only its new desktop generation could change it.');

    // Park both real participants behind a native mutex, then let them race.
    // If Electron wins, removal must clean the write; if NSIS wins, the old
    // Electron must not re-register after the stamp disappears.
    const generation = (await fs.readFile(stampPath(target), 'utf16le')).replace(/^\uFEFF/, '').slice(0, 38);
    const gate = start('powershell.exe', psArgs(String.raw`
      $ErrorActionPreference = 'Stop'
      $mutex = [Threading.Mutex]::new($false, ('Global\AIShuffle.Startup.' + $env:FIXTURE_GENERATION))
      if (-not $mutex.WaitOne(0)) { exit 1 }
      try { [Console]::WriteLine('held'); [void][Console]::ReadLine() }
      finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
    `), { FIXTURE_GENERATION: generation });
    assert.equal(await gate.next(), 'held');
    await copyUninstaller(target);
    const undo = remove(target).then(() => null, error => error);
    const change = second('on').then(result => ({ result }), error => ({ error }));
    try {
      const pending = Symbol('both pending');
      assert.equal(await Promise.race([undo, change, delay(1500, pending)]), pending, 'neither real participant passes the held mutex');
      assert.ok(existsSync(stampPath(target)) && existsSync(executable(target)), 'NSIS did not start removal while blocked');
      await state(unregistered);
    } finally {
      // An interrupted coordinator must hand off, not strand the install.
      await gate.stop();
    }
    assert.ifError(await undo);
    assert.ifError((await change).error);
    assert.equal(existsSync(executable(target)), false);
    await state(unregistered);
    assert.deepEqual(await second('on'), unavailable, 'the same surviving desktop cannot recreate Run after removal');
    t.diagnostic('Concurrent opt-in and true removal left no Run entry or approval and did not touch a sibling entry.');

    await install(target);
    const third = await writer(target);
    assert.deepEqual(await third('on'), enabled);
    await registry('seed-blocked');
    await install(moved);
    await state(unregistered);
    assert.equal(existsSync(executable(target)), false);
    assert.deepEqual(await third('on'), unavailable);
    const fourth = await writer(moved);
    assert.deepEqual(await fourth('on'), enabled);
    await registry('seed-blocked');
    const foreign = '"C:\\unrelated\\not-our-launcher.exe"';
    await registry('seed-foreign', foreign);
    await copyUninstaller(moved);
    await remove(moved);
    await state({ run: foreign, approval });
    t.diagnostic('Moving cleaned the old matching registration; uninstall did not touch a same-name foreign command or its approval.');
    await registry('clean');
    assert.deepEqual(await read(), [unregistered, unregistered], 'the disposable registry values are cleaned up');
  } finally {
    await Promise.all(sessions.map(s => s.stop()));
    try { if (touchedRegistry) await registry('clean'); }
    finally { await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
  }
}
