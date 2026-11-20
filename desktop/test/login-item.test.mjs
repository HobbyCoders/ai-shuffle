import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loginItemControl } from '../dist/main/login-item.js';
import { exerciseWindowsStartup } from './helpers/windows-startup.mjs';

const win = {
	platform: 'win32', packaged: false, preview: false,
	installedExecutable: 'C:\\Program Files\\AI Shuffle\\AI Shuffle.exe',
	appPath: 'C:\\Users\\example\\AI Shuffle\\updates\\versions\\revision\\desktop',
	localAppData: 'C:\\Users\\example\\AppData\\Local',
	appId: 'com.hobbycoders.aishuffle.desktop'
};
const generation = '{01234567-89AB-CDEF-0123-456789ABCDEF}';
const readWinStamp = (stampPath, encoding) => {
	assert.equal(stampPath, 'C:\\Program Files\\AI Shuffle\\resources\\login-item-uninstall-v4');
	assert.equal(encoding, 'utf16le');
	return `\uFEFF${generation}${win.localAppData.toUpperCase()}`;
};
const windowsFiles = { readStamp: readWinStamp, exists: () => true, write: async () => {} };
const mac = {
	platform: 'darwin', packaged: true, preview: false, appId: win.appId,
	appPath: '/Applications/AI Shuffle.app/Contents/Resources/app.asar', systemVersion: '15.3'
};

test('Windows bundled and managed runtimes register the same installed executable, with no arguments', async () => {
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
			setLoginItemSettings() { assert.fail('the Windows writer must hold the native mutex'); }
		};
		const control = loginItemControl(app, { ...win, packaged }, { ...windowsFiles, async write(given) {
			assert.equal(given.SHUFFLE_LOGIN_EXE, win.installedExecutable);
			assert.equal(given.SHUFFLE_LOGIN_NAME, win.appId);
			assert.equal(given.SHUFFLE_LOGIN_GENERATION, generation);
			registered = approved = given.SHUFFLE_LOGIN_ENABLE === '1';
		} });
		assert.equal(control.get(), 'disabled', 'reading initially does not register the item');
		assert.deepEqual(await control.set(true), { state: 'enabled', failed: false });
		approved = false; // Startup Apps disabled this item; ignore unrelated enabled entries.
		assert.equal(control.get(), 'blocked');
		assert.deepEqual(await control.set(false), { state: 'disabled', failed: false });
	}
});

test('macOS uses the main bundle service and handles OS approval and missing services', async () => {
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
	assert.deepEqual(await control.set(true), { state: 'blocked', failed: true });
	status = 'enabled';
	assert.equal(control.get(), 'enabled');
	assert.deepEqual(await control.set(false), { state: 'disabled', failed: false });
	status = 'not-found';
	assert.equal(control.get(), 'unavailable');
});

test('silent or thrown OS writes report failure with readback, not the requested state', async () => {
	for (const write of [() => {}, () => { throw Error('OS denied'); }]) {
		const control = loginItemControl({
			getLoginItemSettings: () => ({ status: 'not-registered' }), setLoginItemSettings: write
		}, mac);
		assert.deepEqual(await control.set(true), { state: 'disabled', failed: true });
	}
	const control = loginItemControl({
		getLoginItemSettings: () => { throw Error('OS unavailable'); }, setLoginItemSettings() {}
	}, mac);
	await assert.rejects(control.set(true), /OS unavailable/);
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
	assert.equal(loginItemControl(app, win, { ...windowsFiles, readStamp: () => generation + 'C:\\Users\\another-user\\AppData\\Local' }), undefined);
	assert.equal(loginItemControl(app, win, { ...windowsFiles, exists: path => !path.endsWith('.exe') }), undefined);
	assert.ok(loginItemControl(app, win, windowsFiles));
	assert.ok(loginItemControl(app, mac));
});


// This writes only unique fixture values in a disposable runner's HKCU.
test('Windows desktop writer and compiled installer serialize startup across rollback, upgrades and removal', {
	skip: process.platform !== 'win32' || process.env.GITHUB_ACTIONS !== 'true', timeout: 300_000
}, exerciseWindowsStartup);
