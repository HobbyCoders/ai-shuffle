import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loginItemControl } from '../dist/main/login-item.js';

const win = {
	platform: 'win32', packaged: false, preview: false,
	installedExecutable: 'C:\\Program Files\\AI Shuffle\\AI Shuffle.exe',
	appPath: 'C:\\Users\\example\\AI Shuffle\\updates\\versions\\revision\\desktop',
	appData: 'C:\\Users\\example\\AppData\\Roaming',
	appId: 'com.hobbycoders.aishuffle.desktop'
};
const readWinStamp = (stampPath, encoding) => {
	assert.equal(stampPath, 'C:\\Program Files\\AI Shuffle\\resources\\login-item-uninstall-v1');
	assert.equal(encoding, 'utf16le');
	return `\uFEFF${win.appData.toUpperCase()}`;
};
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
		const control = loginItemControl(app, { ...win, packaged }, readWinStamp);
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
	assert.equal(loginItemControl(app, win, () => { throw Error('old installer has no stamp'); }), undefined);
	assert.equal(loginItemControl(app, win, () => 'C:\\Users\\another-user\\AppData\\Roaming'), undefined);
	assert.ok(loginItemControl(app, win, readWinStamp));
	assert.ok(loginItemControl(app, mac));
});
