import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { closeSync, mkdtempSync, mkdirSync, openSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import { loginItemControl } from '../dist/main/login-item.js';

const win = {
	platform: 'win32', packaged: false, preview: false,
	installedExecutable: 'C:\\Program Files\\AI Shuffle\\AI Shuffle.exe',
	appPath: 'C:\\Users\\example\\AI Shuffle\\updates\\versions\\revision\\desktop',
	localAppData: 'C:\\Users\\example\\AppData\\Local',
	appId: 'com.hobbycoders.aishuffle.desktop'
};
const readWinStamp = (stampPath, encoding) => {
	assert.equal(stampPath, 'C:\\Program Files\\AI Shuffle\\resources\\login-item-uninstall-v3');
	assert.equal(encoding, 'utf16le');
	return `\uFEFF${win.localAppData.toUpperCase()}`;
};
const windowsFiles = { readStamp: readWinStamp, exists: () => true, guard: (_file, change) => change() };
const mac = {
	platform: 'darwin', packaged: true, preview: false, appId: win.appId,
	appPath: '/Applications/AI Shuffle.app/Contents/Resources/app.asar', systemVersion: '15.3'
};

test('Windows bundled and managed runtimes register the same installed executable, with no arguments', () => {
	for (const packaged of [true, false]) {
		const options = { path: `"${win.installedExecutable}"`, args: [] };
		let registered = false;
		let approved = false;
		const app = {
			getLoginItemSettings(given) {
				assert.deepEqual(given, options, 'readback uses the same quoted path');
				return { openAtLogin: registered, launchItems: [
					{ name: win.appId, scope: 'machine', enabled: true },
					{ name: 'unrelated', scope: 'user', enabled: true },
					...(registered ? [{ name: win.appId, scope: 'user', enabled: approved }] : [])
				] };
			},
			setLoginItemSettings(given) {
				assert.deepEqual(given, { ...options, name: win.appId, openAtLogin: given.openAtLogin });
				registered = given.openAtLogin;
				approved = given.openAtLogin;
			}
		};
		const control = loginItemControl(app, { ...win, packaged }, windowsFiles);
		assert.equal(control.get(), 'disabled', 'reading initially does not register the item');
		assert.deepEqual(control.set(true), { state: 'enabled', failed: false });
		approved = false; // Startup Apps disabled this item; ignore unrelated enabled entries.
		assert.equal(control.get(), 'blocked');
		assert.deepEqual(control.set(false), { state: 'disabled', failed: false });
	}
});

test('macOS uses the main bundle service and handles OS approval and missing services', () => {
	let status = 'not-registered';
	const control = loginItemControl({
		getLoginItemSettings(opts) {
			assert.deepEqual(opts, { type: 'mainAppService' });
			return { status };
		},
		setLoginItemSettings(opts) {
			assert.deepEqual(opts, { type: 'mainAppService', openAtLogin: opts.openAtLogin });
			status = opts.openAtLogin ? 'requires-approval' : 'not-registered';
		}
	}, mac);
	assert.equal(control.get(), 'disabled');
	assert.deepEqual(control.set(true), { state: 'blocked', failed: true });
	status = 'enabled';
	assert.equal(control.get(), 'enabled');
	assert.deepEqual(control.set(false), { state: 'disabled', failed: false });
	status = 'not-found';
	assert.equal(control.get(), 'unavailable');
});

test('silent or thrown OS writes report failure with readback, not the requested state', () => {
	for (const write of [() => {}, () => { throw Error('OS denied'); }]) {
		const control = loginItemControl({
			getLoginItemSettings: () => ({ status: 'not-registered' }), setLoginItemSettings: write
		}, mac);
		assert.deepEqual(control.set(true), { state: 'disabled', failed: true });
	}
	const control = loginItemControl({
		getLoginItemSettings: () => { throw Error('OS unavailable'); }, setLoginItemSettings() {}
	}, mac);
	assert.throws(() => control.set(true), /OS unavailable/);
});

test('only supported installed apps get the native capability; none writes at startup', () => {
	const reject = () => assert.fail('must not call the OS when checking capability');
	const app = { getLoginItemSettings: reject, setLoginItemSettings: reject };
	for (const host of [
		{ ...win, installedExecutable: undefined }, { ...win, installedExecutable: 'relative.exe' },
		{ ...win, preview: true }, { ...win, platform: 'linux' },
		{ ...mac, packaged: false }, { ...mac, systemVersion: '12.7.6' },
		{ ...mac, appPath: '/Volumes/AI Shuffle/AI Shuffle.app/Contents/Resources/app.asar' },
		{ ...mac, appPath: '/private/var/AppTranslocation/some-id/d/AI Shuffle.app/Contents/Resources/app.asar' }
	]) assert.equal(loginItemControl(app, host), undefined);
	assert.equal(loginItemControl({}, win), undefined);
	assert.equal(loginItemControl(app, win, { ...windowsFiles, readStamp() { throw Error('old installer has no stamp'); } }), undefined);
	assert.equal(loginItemControl(app, win, { ...windowsFiles, readStamp: () => 'C:\\Users\\another-user\\AppData\\Local' }), undefined);
	assert.equal(loginItemControl(app, win, { ...windowsFiles, exists: path => !path.endsWith('login-item-lock') }), undefined);
	assert.ok(loginItemControl(app, win, windowsFiles));
	assert.ok(loginItemControl(app, mac));
});

test('Windows rechecks install authority inside the guard and never writes if it cannot enter', () => {
	let locked = false;
	let installed = true;
	let writes = 0;
	let held = false;
	const control = loginItemControl({
		setLoginItemSettings() { assert.equal(held, true); writes++; },
		getLoginItemSettings: () => ({ openAtLogin: false })
	}, win, {
		...windowsFiles, exists: () => installed,
		guard(file, change) {
			assert.equal(file, `${win.localAppData}\\ai-shuffle-desktop\\login-item-lock`);
			if (locked) return;
			held = true;
			try { return change(); } finally { held = false; }
		}
	});
	assert.equal(control.set(false).failed, false);
	locked = true;
	assert.deepEqual(control.set(true), { state: 'unavailable', failed: true });
	locked = false;
	installed = false; // Old runtime survives removal, and the uninstaller has released the guard.
	assert.equal(control.get(), 'unavailable');
	assert.deepEqual(control.set(true), { state: 'unavailable', failed: true });
	assert.equal(writes, 1);
});

test('Windows guard excludes native no-share handles in both directions', { skip: process.platform !== 'win32' }, async () => {
	const dir = mkdtempSync(path.join(os.tmpdir(), 'shuffle-startup-guard-'));
	try {
		const installedExecutable = path.join(dir, 'AI Shuffle.exe');
		const resources = path.join(dir, 'resources');
		const host = { ...win, installedExecutable, localAppData: dir };
		const guard = path.join(dir, 'ai-shuffle-desktop', 'login-item-lock');
		mkdirSync(path.dirname(guard));
		writeFileSync(guard, '');
		writeFileSync(installedExecutable, '');
		mkdirSync(resources);
		writeFileSync(path.join(resources, 'login-item-uninstall-v3'), `\uFEFF${dir}`, 'utf16le');
		const powershell = script => ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')];
		const env = { ...process.env, SHUFFLE_GUARD_FILE: guard };
		const openExclusive = '[IO.File]::Open($env:SHUFFLE_GUARD_FILE, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)';
		let reads = 0;
		let writes = 0;
		const control = loginItemControl({
			setLoginItemSettings() { writes++; },
			getLoginItemSettings() {
				reads++;
				const probe = spawnSync('powershell.exe', powershell(`try { (${openExclusive}).Dispose(); exit 1 } catch [IO.IOException] { if ((($_.Exception.GetBaseException()).HResult -band 65535) -ne 32) { throw }; exit 0 }`), {
					encoding: 'utf8', timeout: 15_000, env
				});
				assert.ifError(probe.error);
				assert.equal(probe.status, 0, probe.stdout + probe.stderr);
				return { openAtLogin: false };
			}
		}, host);
		assert.deepEqual(control.set(false), { state: 'disabled', failed: false });
		assert.deepEqual([writes, reads], [1, 1]);

		// Keep the native handle open until the actual JS caller has tested it.
		// The same controller/host exercises both directions on the same file.
		const holder = spawn('powershell.exe', powershell(`$ErrorActionPreference = 'Stop'; $lock = ${openExclusive}; try { [Console]::WriteLine('held'); [void][Console]::ReadLine() } finally { $lock.Dispose() }`), {
			stdio: ['pipe', 'pipe', 'pipe'], timeout: 15_000, env
		});
		const closed = new Promise(resolve => {
			holder.once('error', error => resolve({ error }));
			holder.once('close', (status, signal) => resolve({ status, signal }));
		});
		let stderr = '';
		holder.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
		const lines = createInterface({ input: holder.stdout });
		try {
			const held = await Promise.race([once(lines, 'line').then(([line]) => line), closed.then(() => stderr)]);
			assert.equal(held, 'held', 'native holder must acknowledge opening the guard');
			assert.throws(() => closeSync(openSync(guard, 'r')), { code: 'EBUSY' }, 'the real guard path must be locked');
			assert.deepEqual(control.set(true), { state: 'unavailable', failed: true });
			assert.deepEqual([writes, reads], [1, 1], 'no Electron call while the native handle is held');
		} finally {
			lines.close();
			holder.stdin.end();
			const { error, status, signal } = await closed;
			assert.ifError(error);
			assert.equal(status, 0, stderr || signal);
		}
		assert.deepEqual(control.set(false), { state: 'disabled', failed: false }, 'can write again when the exclusive holder exits');
		assert.deepEqual([writes, reads], [2, 2]);
	} finally { rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
});
