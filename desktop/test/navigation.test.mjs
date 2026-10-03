import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const { isBundledPage, trustedPage, externalHttpUrl } = require('../dist/main/pages.js');

const WORKER = 'http://127.0.0.1:8000';
const PAGE = 'file:///opt/AI%20Shuffle/resources/app/src/pages/worker.html';

/** dist/main/navigation.js against a fake Electron that records what it is given. */
function loadNavigation() {
	const opened = [];
	/** Per partition: its permission handlers and the storages each clearStorageData call named. */
	const sessions = {};
	const sessionFor = (name) => (sessions[name] ??= {
		handlers: {},
		cleared: [],
		setPermissionRequestHandler(fn) { this.handlers.request = fn; },
		setPermissionCheckHandler(fn) { this.handlers.check = fn; },
		async clearStorageData(options) { this.cleared.push(options.storages); }
	});
	/** Windows the policy creates itself (isolated pop-ups). */
	const windows = [];
	class BrowserWindow extends EventEmitter {
		constructor(options) {
			super();
			this.options = options;
			this.loaded = [];
			this.webContents = { setWindowOpenHandler: (fn) => { this.webContents.openHandler = fn; } };
			windows.push(this);
		}
		async loadURL(url) { this.loaded.push(url); }
	}
	const electron = {
		BrowserWindow,
		shell: { openExternal: async (url) => { opened.push(url); } },
		session: { fromPartition: sessionFor }
	};
	const file = new URL('../dist/main/navigation.js', import.meta.url);
	const module = { exports: {} };
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, process, URL, console,
		require: (id) => {
			if (id === 'electron') return electron;
			if (id === './context-menu') return { attachTextContextMenu() {} };
			if (id === './window') return { PARTITION: 'test' };
			return createRequire(file)(id);
		}
	});
	return { nav: module.exports, opened, handlers: sessionFor('test').handlers, sessions, windows };
}

test('NC-3: only the worker origin gets permissions, and only microphone, clipboard write and fullscreen', () => {
	const { nav } = loadNavigation();
	const allowed = (permission, url, mediaTypes) => nav.permissionAllowed(permission, url, WORKER, mediaTypes);
	for (const permission of ['clipboard-sanitized-write', 'fullscreen']) assert.equal(allowed(permission, `${WORKER}/chat`), true, permission);
	assert.equal(allowed('media', `${WORKER}/chat`, ['audio']), true, 'voice and dictation');
	assert.equal(allowed('media', `${WORKER}/chat`, ['audio', 'video']), false, 'no camera');
	assert.equal(allowed('media', `${WORKER}/chat`, ['video']), false);
	// Nothing in the web UI reads the clipboard, captures the screen, locks the pointer or notifies.
	for (const permission of ['clipboard-read', 'display-capture', 'pointerLock', 'notifications', 'geolocation', 'openExternal', 'hid', 'usb', 'serial', 'midi', 'unknown']) {
		assert.equal(allowed(permission, `${WORKER}/chat`), false, permission);
	}
	// Other origins, including the OAuth, hub and Plaid pop-ups, get nothing at all.
	for (const url of ['https://accounts.google.com/o/oauth2/auth', 'https://hobbycoders.com/signin', 'https://cdn.plaid.com/link', 'http://127.0.0.1:8001/', 'http://localhost:8000/', 'file:///opt/app/src/pages/worker.html', 'about:blank', '', null]) {
		for (const permission of ['clipboard-read', 'clipboard-sanitized-write', 'media', 'fullscreen', 'display-capture', 'pointerLock']) {
			assert.equal(allowed(permission, url, ['audio']), false, `${permission} for ${url}`);
		}
	}
	// With no worker shown (the status page), nobody gets anything.
	assert.equal(nav.permissionAllowed('clipboard-sanitized-write', `${WORKER}/`, null), false);
});

test('NC-3: the request and check handlers both apply the worker-origin rule', () => {
	const { nav, handlers } = loadNavigation();
	let origin = WORKER;
	nav.installPermissionPolicy(() => origin);
	const request = (permission, details, url = 'https://accounts.google.com/') => {
		let granted;
		handlers.request({ getURL: () => url }, permission, (value) => { granted = value; }, details);
		return granted;
	};
	assert.equal(request('clipboard-read', { requestingUrl: 'https://accounts.google.com/' }), false, 'a pop-up cannot read the clipboard');
	assert.equal(request('clipboard-read', { requestingUrl: `${WORKER}/` }), false, 'neither can the worker UI: it never does');
	assert.equal(request('clipboard-sanitized-write', { requestingUrl: `${WORKER}/` }), true);
	assert.equal(request('media', { requestingUrl: `${WORKER}/`, mediaTypes: ['audio'] }), true);
	assert.equal(request('media', { requestingUrl: `${WORKER}/`, mediaTypes: ['video'] }), false);
	assert.equal(request('clipboard-sanitized-write', { requestingUrl: '' }, `${WORKER}/`), true, 'falls back to the page URL');
	assert.equal(request('pointerLock', { requestingUrl: `${WORKER}/` }), false);
	assert.equal(request('display-capture', { requestingUrl: `${WORKER}/` }), false);

	assert.equal(handlers.check(null, 'clipboard-sanitized-write', WORKER, { isMainFrame: true }), true);
	assert.equal(handlers.check(null, 'media', WORKER, { isMainFrame: true, mediaType: 'audio' }), true);
	assert.equal(handlers.check(null, 'media', WORKER, { isMainFrame: true, mediaType: 'video' }), false);
	assert.equal(handlers.check(null, 'clipboard-read', WORKER, { isMainFrame: true }), false);
	assert.equal(handlers.check(null, 'clipboard-sanitized-write', 'https://cdn.plaid.com', { isMainFrame: true }), false);
	origin = null;
	assert.equal(handlers.check(null, 'clipboard-sanitized-write', WORKER, { isMainFrame: true }), false);
});

/** A window whose webContents records the handlers the policy installs. */
function fakeWindow() {
	const contents = new EventEmitter();
	contents.setWindowOpenHandler = (fn) => { contents.openHandler = fn; };
	contents.downloaded = [];
	contents.downloadURL = (url) => { contents.downloaded.push(url); };
	return { webContents: contents };
}

function navigate(contents, event, url) {
	let prevented = false;
	contents.emit(event, { preventDefault: () => { prevented = true; } }, url);
	return prevented;
}

function redirect(contents, url, isMainFrame = true) {
	let prevented = false;
	contents.emit('will-redirect', { url, isMainFrame, preventDefault: () => { prevented = true; } });
	return prevented;
}

test('NC-4: the main frame stays on the worker origin or the bundled page, redirects included', () => {
	const { nav, opened } = loadNavigation();
	const win = fakeWindow();
	let origin = WORKER;
	nav.attachNavigationPolicy(win, () => origin, () => {}, PAGE);
	const contents = win.webContents;

	assert.equal(navigate(contents, 'will-navigate', `${WORKER}/settings`), false);
	assert.equal(navigate(contents, 'will-navigate', `${PAGE}?mode=status`), false);
	assert.equal(navigate(contents, 'will-navigate', 'file:///etc/passwd'), true, 'not any file: URL');
	assert.equal(navigate(contents, 'will-navigate', 'https://github.com/HobbyCoders'), true);
	assert.deepEqual(opened, ['https://github.com/HobbyCoders'], 'http(s) links go to the system browser');

	// A worker URL that 3xx's to another origin (e.g. a peer's artifact proxy) is stopped.
	assert.equal(redirect(contents, `${WORKER}/login`), false);
	assert.equal(redirect(contents, 'https://peer.example.com/artifact/1'), true);
	assert.equal(opened.at(-1), 'https://peer.example.com/artifact/1');
	assert.equal(redirect(contents, 'http://127.0.0.1:8001/'), true, 'another loopback port is another origin');
	// Nothing that is not plain http(s) reaches the OS, and credentials in a URL are refused.
	const before = opened.length;
	assert.equal(redirect(contents, 'file:///tmp/x.html'), true);
	assert.equal(redirect(contents, 'ms-settings:privacy'), true);
	assert.equal(redirect(contents, 'https://user:pw@evil.example/'), true);
	assert.equal(navigate(contents, 'will-navigate', 'javascript:alert(1)'), true);
	assert.equal(opened.length, before);
	// Subframe redirects are the page's business.
	assert.equal(redirect(contents, 'https://cdn.example.com/embed', false), false);
	// On the status page there is no worker origin to allow.
	origin = null;
	assert.equal(navigate(contents, 'will-navigate', `${WORKER}/`), true);
	assert.equal(navigate(contents, 'will-navigate', PAGE), false);
});

test('NC-4: the main frame never lands on a worker API response; file links download instead', () => {
	const { nav, opened } = loadNavigation();
	const win = fakeWindow();
	let origin = WORKER;
	nav.attachNavigationPolicy(win, () => origin, () => {}, PAGE);
	const contents = win.webContents;
	const refused = `${WORKER}/api/files/by-path?path=%2Fhome%2Fuser%2Fsecret.txt`;
	const image = `${WORKER}/api/generated-images/by-path?path=%2Fwork%2Fa.png`;

	// The owner's report: a refused chat file link replaced the whole app with its JSON.
	assert.equal(navigate(contents, 'will-navigate', refused), true, 'the app stays put');
	assert.equal(navigate(contents, 'will-navigate', image), true, 'an inline type would have replaced it too');
	assert.deepEqual(contents.downloaded, [refused, image], 'the link is fetched as a download instead');
	assert.deepEqual(opened, [], 'worker files never go to the system browser');

	// WebSocket endpoints are not pages either, and are not downloaded.
	assert.equal(navigate(contents, 'will-navigate', `${WORKER}/ws/chat`), true);
	assert.equal(contents.downloaded.length, 2);

	// App routes, including ones that merely start with the same letters, still load.
	for (const path of ['/', '/?session=abc', '/settings', '/workspace', '/apis', '/mobile/', '/plaid-link']) {
		assert.equal(navigate(contents, 'will-navigate', `${WORKER}${path}`), false, path);
	}
	// Another origin's /api/ is an external link, as before.
	assert.equal(navigate(contents, 'will-navigate', 'https://example.com/api/x'), true);
	assert.deepEqual(opened, ['https://example.com/api/x']);

	// A redirect onto the API is stopped without a download or a browser hand-off.
	assert.equal(redirect(contents, `${WORKER}/api/v1/auth/status`), true);
	assert.equal(contents.downloaded.length, 2);
	assert.equal(opened.length, 1);
	assert.equal(redirect(contents, `${WORKER}/`), false, 'sign-in redirects to the app still land');

	// Pop-ups at a worker file (Ctrl+click, a _blank link) download, never a raw child window.
	assert.deepEqual(plain(openWindow(contents, refused, 'foreground-tab')), { action: 'deny' });
	assert.equal(contents.downloaded.at(-1), refused);
	// The pop-ups the page needs are unchanged.
	assert.equal(openWindow(contents, 'about:blank').action, 'allow');
	assert.equal(openWindow(contents, `${WORKER}/plaid-link`).action, 'allow');

	// The bundled status page has no worker to protect: a worker URL is simply refused there.
	origin = null;
	assert.equal(navigate(contents, 'will-navigate', refused), true);
	assert.equal(contents.downloaded.length, 3);
});

test('NC-4: workerEndpoint and mainFrameAllowed agree on what is a page', () => {
	const { nav } = loadNavigation();
	assert.equal(nav.workerEndpoint(`${WORKER}/api/files/by-path?path=x`, WORKER), true);
	assert.equal(nav.workerEndpoint(`${WORKER}/api`, WORKER), true);
	assert.equal(nav.workerEndpoint(`${WORKER}/ws`, WORKER), true);
	assert.equal(nav.workerEndpoint(`${WORKER}/apix`, WORKER), false);
	assert.equal(nav.workerEndpoint('http://127.0.0.1:8001/api/x', WORKER), false, 'another port is another origin');
	assert.equal(nav.workerEndpoint(`${WORKER}/api/x`, null), false);
	assert.equal(nav.workerEndpoint('not a url', WORKER), false);
	assert.equal(nav.mainFrameAllowed(`${WORKER}/api/x`, WORKER, PAGE), false);
	assert.equal(nav.mainFrameAllowed(`${WORKER}/chat`, WORKER, PAGE), true);
	assert.equal(nav.mainFrameAllowed(PAGE, WORKER, PAGE), true);
});

test('NC-4: the bundled page is matched by its exact path, whatever the query or encoding', () => {
	assert.equal(isBundledPage(`${PAGE}?mode=status&error=x#top`, PAGE, false), true);
	assert.equal(isBundledPage('file:///opt/AI Shuffle/resources/app/src/pages/worker.html', PAGE, false), true);
	assert.equal(isBundledPage('file:///opt/AI%20Shuffle/resources/app/src/pages/close.html', PAGE, false), false);
	assert.equal(isBundledPage('file:///opt/ai%20shuffle/resources/app/src/pages/worker.html', PAGE, false), false);
	assert.equal(isBundledPage('file://host/opt/AI%20Shuffle/resources/app/src/pages/worker.html', PAGE, false), false);
	// Windows paths compare case-insensitively.
	const windows = 'file:///C:/Program%20Files/AI%20Shuffle/resources/app/src/pages/worker.html';
	assert.equal(isBundledPage('file:///c:/program%20files/ai%20shuffle/resources/app/src/pages/worker.html', windows, true), true);
	assert.equal(isBundledPage('http://127.0.0.1:8000/src/pages/worker.html', PAGE, false), false);
	assert.equal(trustedPage(`${WORKER}/chat`, WORKER, PAGE), 'worker');
	assert.equal(trustedPage('ws://127.0.0.1:8000/chat', WORKER, PAGE), null);
	assert.equal(trustedPage(`${WORKER}/chat`, null, PAGE), null);
	assert.equal(externalHttpUrl('https://example.com/a b'), 'https://example.com/a%20b');
	assert.equal(externalHttpUrl('mailto:x@example.com'), null);
});

/** Runs the window-open handler the policy installed, as Electron would. */
function openWindow(contents, url, disposition = 'new-window') {
	return contents.openHandler({ url, disposition, frameName: '', features: '', referrer: { url: '', policy: 'default' }, postBody: undefined });
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
/** Objects made inside the vm context are another realm's: compare them as plain data. */
const plain = (value) => JSON.parse(JSON.stringify(value));

test('R10-10: where each pop-up goes', () => {
	const { nav } = loadNavigation();
	assert.equal(nav.POPUP_PARTITION.startsWith('persist:'), false, 'the pop-up session is in memory only');
	assert.equal(nav.popupPlacement('about:blank', 'foreground-tab', WORKER), 'child', 'account sign-in');
	assert.equal(nav.popupPlacement('about:blank', 'new-window', WORKER), 'child', 'Google connector OAuth');
	assert.equal(nav.popupPlacement(`${WORKER}/plaid-link`, 'new-window', `${WORKER}/`), 'child', 'Plaid Link');
	assert.equal(nav.popupPlacement('https://accounts.example.com/authorize', 'new-window', WORKER), 'isolated');
	assert.equal(nav.popupPlacement('http://127.0.0.1:8001/', 'new-window', WORKER), 'isolated', 'another loopback port is another site');
	assert.equal(nav.popupPlacement(`${WORKER}/plaid-link`, 'new-window', null), 'isolated', 'no worker shown: nothing is the app\'s own');
	assert.equal(nav.popupPlacement('https://example.com/', 'foreground-tab', WORKER), 'external');
	for (const url of ['data:text/html,x', 'javascript:alert(1)', 'file:///etc/passwd', 'https://user:pw@evil.example/']) {
		assert.equal(nav.popupPlacement(url, 'new-window', WORKER), 'external', url);
	}
});

test('R10-10: a pop-up at another site opens in the isolated session, and what it registered is cleared when it closes', async () => {
	const { nav, sessions, windows, opened } = loadNavigation();
	const win = fakeWindow();
	nav.attachNavigationPolicy(win, () => WORKER, () => {}, PAGE);
	const url = 'https://accounts.example.com/authorize?client_id=1';
	assert.deepEqual(plain(openWindow(win.webContents, url)), { action: 'deny' }, 'no native child in the app\'s session');
	assert.equal(windows.length, 1);
	const [popup] = windows;
	assert.equal(popup.options.parent, win);
	assert.equal(popup.options.webPreferences.partition, nav.POPUP_PARTITION);
	assert.notEqual(popup.options.webPreferences.partition, 'test');
	assert.equal(popup.options.webPreferences.preload, undefined);
	assert.equal(popup.options.webPreferences.sandbox, true);
	assert.equal(popup.options.webPreferences.contextIsolation, true);
	assert.deepEqual(popup.loaded, [url]);
	assert.deepEqual(opened, []);
	// Its own window.open calls go to the system browser.
	assert.deepEqual(plain(popup.webContents.openHandler({ url: 'https://bank.example.com/' })), { action: 'deny' });
	assert.deepEqual(opened, ['https://bank.example.com/']);
	popup.emit('closed');
	await flush();
	assert.deepEqual(plain(sessions[nav.POPUP_PARTITION].cleared), [['serviceworkers', 'cachestorage']]);
	assert.equal(sessions.test?.cleared.length ?? 0, 0);
});

test('R10-10: pop-ups the page talks to stay in the app session, and its service workers are cleared when they close', async () => {
	const { nav, sessions, windows } = loadNavigation();
	const win = fakeWindow();
	nav.attachNavigationPolicy(win, () => WORKER, () => {}, PAGE);
	for (const url of ['about:blank', `${WORKER}/plaid-link`]) {
		const result = openWindow(win.webContents, url);
		assert.equal(result.action, 'allow', url);
		assert.equal(result.overrideBrowserWindowOptions.webPreferences.partition, 'test', url);
		assert.equal(result.overrideBrowserWindowOptions.webPreferences.preload, undefined, url);
	}
	assert.equal(windows.length, 0);
	const child = new EventEmitter();
	child.webContents = { setWindowOpenHandler() {} };
	win.webContents.emit('did-create-window', child);
	assert.equal(sessions.test.cleared.length, 0);
	child.emit('closed');
	await flush();
	assert.deepEqual(plain(sessions.test.cleared), [['serviceworkers', 'cachestorage']]);
});

test('R10-10: the isolated pop-up session gets no permissions, and clearing never throws', async () => {
	const { nav, sessions } = loadNavigation();
	nav.installPermissionPolicy(() => WORKER);
	const popups = sessions[nav.POPUP_PARTITION].handlers;
	for (const permission of ['media', 'clipboard-sanitized-write', 'fullscreen', 'notifications']) {
		let granted;
		popups.request({ getURL: () => `${WORKER}/` }, permission, (value) => { granted = value; }, { requestingUrl: `${WORKER}/` });
		assert.equal(granted, false, permission);
		assert.equal(popups.check(null, permission, WORKER, { isMainFrame: true }), false, permission);
	}
	const warn = console.warn;
	console.warn = () => {};
	try {
		await nav.forgetServiceWorkers({ clearStorageData: async () => { throw new Error('gone'); } });
	} finally {
		console.warn = warn;
	}
});
