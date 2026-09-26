import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import vm from 'node:vm';

const WORKER = 'http://127.0.0.1:8000';
const PAGE = 'file:///opt/app/src/pages/worker.html';

/** registerIpc from dist/main/ipc.js with fake Electron; returns a way to call each channel as a given frame. */
function setup({ workerUrl = WORKER } = {}) {
	const handlers = new Map();
	const listeners = new Map();
	const calls = [];
	const file = new URL('../dist/main/ipc.js', import.meta.url);
	const module = { exports: {} };
	const electron = {
		app: { getVersion: () => '1.0.0' },
		ipcMain: { handle: (name, fn) => handlers.set(name, fn), on: (name, fn) => listeners.set(name, fn) },
		shell: { showItemInFolder: (file) => calls.push(['open-logs', file]) }
	};
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, process, URL,
		require: (id) => (id === 'electron' ? electron : id === './managed-context' ? { managedContext: () => null } : createRequire(file)(id))
	});
	const window = {
		isOwnSender: (id) => id === 7,
		currentWorkerUrl: workerUrl,
		bundledPageUrl: PAGE,
		browserWindow: { close: () => calls.push('close') },
		windowState: () => ({ maximized: false, fullscreen: false, focused: true }),
		setTitleBarTheme: () => calls.push('theme')
	};
	const worker = {
		state: () => ({ phase: 'ready', logPath: '/logs/worker.log' }),
		tail: () => ['line'],
		restart: async () => { calls.push('restart'); },
		openUi: async () => { calls.push('open-ui'); }
	};
	const updater = { current: { phase: 'idle' }, check: async () => calls.push('check'), install: async () => calls.push('install') };
	module.exports.registerIpc({ window, updater, worker });

	const main = { url: '', processId: 3, routingId: 1 };
	/** An event from the main window's main frame showing `url`, or from a subframe / another window. */
	const event = (url, { sender = 7, subframe = false } = {}) => {
		main.url = url;
		const frame = subframe ? { url, processId: 3, routingId: 9 } : main;
		return { sender: { id: sender, mainFrame: main }, senderFrame: frame };
	};
	const invoke = async (channel, url, options, ...args) => handlers.get(channel)(event(url, options), ...args);
	const info = (url, options) => { const e = event(url, options); listeners.get('desktop:info')(e); return e.returnValue; };
	return { handlers, invoke, info, calls, window };
}

const WEB_UI_CHANNELS = ['desktop:window-state', 'desktop:update-state', 'desktop:update-check', 'desktop:update-install', 'desktop:worker-state'];
const STATUS_PAGE_CHANNELS = ['desktop:worker-restart', 'desktop:worker-open-logs', 'desktop:worker-tail', 'desktop:worker-open-ui'];

test('NC-4: the worker UI and the bundled page may use the bridge; a foreign page in the main window may not', async () => {
	const { invoke, info } = setup();
	for (const channel of WEB_UI_CHANNELS) {
		await invoke(channel, `${WORKER}/chat`);
		await invoke(channel, `${PAGE}?mode=status`);
	}
	assert.equal(info(`${WORKER}/`).version, '1.0.0');
	// Where a redirect or a stray navigation could leave the window.
	for (const url of ['https://peer.example.com/artifact', 'http://127.0.0.1:8001/', 'file:///tmp/evil.html', 'about:blank', 'data:text/html,x']) {
		for (const channel of [...WEB_UI_CHANNELS, ...STATUS_PAGE_CHANNELS, 'desktop:window-action', 'desktop:set-title-bar-theme']) {
			await assert.rejects(invoke(channel, url), /not allowed/, `${channel} from ${url}`);
		}
		assert.equal(info(url), null, url);
	}
});

test('NC-4: only the main frame counts, and only the main window', async () => {
	const { invoke, info } = setup();
	await assert.rejects(invoke('desktop:update-install', `${WORKER}/`, { subframe: true }), /not allowed/);
	await assert.rejects(invoke('desktop:worker-tail', PAGE, { subframe: true }), /not allowed/);
	await assert.rejects(invoke('desktop:update-install', `${WORKER}/`, { sender: 8 }), /not allowed/);
	assert.equal(info(`${WORKER}/`, { subframe: true }), null);
	assert.equal(info(`${WORKER}/`, { sender: 8 }), null);
});

test('NC-4: restart and the log folder, like the raw log, are for the bundled status page only', async () => {
	const { invoke, calls } = setup();
	for (const channel of STATUS_PAGE_CHANNELS) await assert.rejects(invoke(channel, `${WORKER}/`), /not allowed/, channel);
	assert.deepEqual(calls, []);
	await invoke('desktop:worker-restart', PAGE);
	await invoke('desktop:worker-open-logs', `${PAGE}?mode=status`);
	assert.deepEqual(await invoke('desktop:worker-tail', PAGE), ['line']);
	assert.deepEqual(calls, ['restart', ['open-logs', '/logs/worker.log']]);
});

test('NC-4: with the status page showing, the worker origin is not trusted until the window loads it', async () => {
	const { invoke } = setup({ workerUrl: null });
	await assert.rejects(invoke('desktop:update-install', `${WORKER}/`), /not allowed/);
	await invoke('desktop:update-state', PAGE);
});

test('NC-15: the bridge has no download channel, and the preload has no download member', () => {
	const { handlers } = setup();
	assert.equal(handlers.has('desktop:update-download'), false);
	const preload = readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8');
	assert.doesNotMatch(preload, /update-download/);
});

test('window actions and the title-bar theme take only what they expect from a trusted page', async () => {
	const { invoke, calls } = setup();
	// Only "close" does anything; other actions are ignored rather than forwarded.
	await invoke('desktop:window-action', `${WORKER}/`, undefined, 'minimize');
	await invoke('desktop:window-action', `${WORKER}/`, undefined, { toString: () => 'close' });
	assert.deepEqual(calls, []);
	await invoke('desktop:window-action', `${WORKER}/`, undefined, 'close');
	assert.deepEqual(calls, ['close']);
	calls.length = 0;
	for (const theme of [null, 'dark', {}, { background: '#fff', symbol: '#000000' }, { background: 'red', symbol: '#000000' },
		{ background: '#000000', symbol: 'url(javascript:1)' }, { background: '#00000000', symbol: '#000000' }, ['#000000', '#ffffff']]) {
		await invoke('desktop:set-title-bar-theme', `${WORKER}/`, undefined, theme);
	}
	assert.deepEqual(calls, [], 'malformed themes never reach the native title bar');
	await invoke('desktop:set-title-bar-theme', `${WORKER}/`, undefined, { background: '#1A2b3C', symbol: '#ffffff' });
	assert.deepEqual(calls, ['theme']);
	assert.deepEqual(await invoke('desktop:window-state', `${PAGE}?mode=status`), { maximized: false, fullscreen: false, focused: true });
});
