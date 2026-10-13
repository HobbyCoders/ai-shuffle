import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import vm from 'node:vm';

const desktopDir = fileURLToPath(new URL('..', import.meta.url));
const PAGE_URL = pathToFileURL(path.join(desktopDir, 'src/pages/call.html')).href;
const STATE = { label: 'Voice call · live', title: 'Planning the trip', micMuted: false, agentMuted: true, micProblem: false };
const PRIMARY = { x: 0, y: 0, width: 1920, height: 1040 };
const SECOND = { x: 1920, y: 0, width: 1280, height: 1000 };

/** dist/main/call-popout.js against a fake Electron, with a fake main window to host it. */
function load({ userData = mkdtempSync(path.join(os.tmpdir(), 'call-popout-')) } = {}) {
	const created = [];
	const ipc = new Map();
	const permissions = {};
	let nextId = 20;
	class WebContents extends EventEmitter {
		constructor() {
			super();
			this.id = nextId++;
			this.sent = [];
			this.mainFrame = { url: '', processId: 4, routingId: 1 };
		}
		send(channel, payload) { this.sent.push([channel, payload]); }
		setWindowOpenHandler(fn) { this.openHandler = fn; }
	}
	class BrowserWindow extends EventEmitter {
		constructor(options) {
			super();
			this.options = options;
			this.destroyed = false;
			this.position = [options.x, options.y];
			this.webContents = new WebContents();
			created.push(this);
		}
		isDestroyed() { return this.destroyed; }
		setAlwaysOnTop(...args) { this.onTop = args; }
		setVisibleOnAllWorkspaces() {}
		showInactive() { this.shown = true; }
		getPosition() { return this.position; }
		async loadFile(file) {
			this.loaded = file;
			this.webContents.mainFrame.url = pathToFileURL(file).href;
			this.emit('ready-to-show');
			this.webContents.emit('did-finish-load');
		}
		close() {
			if (this.destroyed) return;
			this.destroyed = true;
			this.emit('closed');
		}
	}
	const electron = {
		app: { getAppPath: () => desktopDir },
		BrowserWindow,
		ipcMain: { on: (channel, fn) => ipc.set(channel, fn) },
		nativeTheme: { shouldUseDarkColors: true },
		screen: { getAllDisplays: () => [{ workArea: PRIMARY }, { workArea: SECOND }], getPrimaryDisplay: () => ({ workArea: PRIMARY }) },
		session: {
			fromPartition: (name) => ({
				setPermissionRequestHandler: (fn) => { permissions[name] = { ...permissions[name], request: fn }; },
				setPermissionCheckHandler: (fn) => { permissions[name] = { ...permissions[name], check: fn }; }
			})
		}
	};
	const file = path.join(desktopDir, 'dist/main/call-popout.js');
	const module = { exports: {} };
	const real = createRequire(file);
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, __dirname: path.dirname(file), process, URL, setTimeout, clearTimeout,
		require: (id) => (id === 'electron' ? electron : real(id))
	}, { filename: file });

	// The main window: its page owns the call.
	const hostWindow = Object.assign(new EventEmitter(), { webContents: new EventEmitter(), isDestroyed: () => false });
	const host = { browserWindow: hostWindow, sent: [], focused: 0, send(channel, payload) { this.sent.push([channel, payload]); }, focus() { this.focused++; } };
	const popout = new module.exports.CallPopout(userData, host);
	popout.register();
	/** A `call-popout:command` message from `contents` (the pop-out by default). */
	const send = (command, { contents = created.at(-1).webContents, url, subframe = false } = {}) => {
		if (url) contents.mainFrame.url = url;
		const frame = subframe ? { url: contents.mainFrame.url, processId: 4, routingId: 9 } : contents.mainFrame;
		ipc.get('call-popout:command')({ sender: contents, senderFrame: frame }, command);
	};
	return { exports: module.exports, popout, created, host, hostWindow, send, permissions, userData };
}

const until = async (check) => {
	for (let i = 0; i < 100 && !check(); i++) await new Promise((resolve) => setTimeout(resolve, 10));
	assert.ok(check());
};

test('call state is validated: strings trimmed and capped, flags true only when true', () => {
	const { callState } = load().exports;
	for (const bad of [null, 'live', 3, [], {}, { label: 'x' }, { label: 1, title: 'x' }, { label: 'x', title: {} }]) {
		assert.equal(callState(bad), null, JSON.stringify(bad));
	}
	const state = callState({ label: '\u0007Voice\ncall ', title: 'x'.repeat(500), micMuted: 'yes', agentMuted: true, micProblem: 1, extra: '<script>' });
	assert.equal(state.label, 'Voice call');
	assert.equal(state.title.length, 120);
	assert.ok(state.title.endsWith('…'));
	assert.equal(state.micMuted, false);
	assert.equal(state.agentMuted, true);
	assert.equal(state.micProblem, false);
	assert.equal('extra' in state, false);
});

test('the pop-out opens in the primary corner, or where it was left if that display is still there', () => {
	const { placement, POPOUT_SIZE } = load().exports;
	const areas = [PRIMARY, SECOND];
	const corner = placement(null, areas, PRIMARY);
	assert.deepEqual({ ...corner }, { x: 1920 - POPOUT_SIZE.width - 24, y: 1040 - POPOUT_SIZE.height - 24 });
	assert.deepEqual({ ...placement({ x: 2000, y: 300 }, areas, PRIMARY) }, { x: 2000, y: 300 }, 'kept on the second display');
	assert.deepEqual({ ...placement({ x: 3100, y: 990 }, areas, PRIMARY) }, { x: 3200 - POPOUT_SIZE.width, y: 1000 - POPOUT_SIZE.height }, 'pulled fully onto its display');
	assert.deepEqual({ ...placement({ x: 9000, y: 9000 }, areas, PRIMARY) }, { ...corner }, 'a disconnected display falls back to the corner');
});

test('opening makes one isolated, frameless, always-on-top window and shows the call in it', async () => {
	const { popout, created, permissions } = load();
	assert.equal(await popout.open(STATE), true);
	assert.equal(created.length, 1);
	const [win] = created;
	const { webPreferences: prefs, ...options } = win.options;
	assert.equal(prefs.contextIsolation, true);
	assert.equal(prefs.sandbox, true);
	assert.equal(prefs.nodeIntegration, false);
	assert.equal(prefs.partition, 'call-popout', 'an in-memory session without the worker cookies');
	assert.equal(path.basename(prefs.preload), 'call-popout.js');
	assert.equal(options.frame, false);
	assert.equal(options.alwaysOnTop, true);
	assert.equal(options.skipTaskbar, true);
	assert.equal(options.resizable, false);
	assert.deepEqual(win.onTop, [true, 'floating']);
	assert.equal(win.shown, true);
	assert.equal(pathToFileURL(win.loaded).href, PAGE_URL);
	assert.deepEqual(JSON.parse(JSON.stringify(win.webContents.sent.at(-1))), ['call-popout:state', STATE]);
	// Every permission is refused in its session.
	let granted;
	permissions['call-popout'].request(null, 'media', (value) => { granted = value; });
	assert.equal(granted, false);
	assert.equal(permissions['call-popout'].check(), false);

	// Opening again refreshes the same window.
	assert.equal(await popout.open({ ...STATE, micMuted: true }), true);
	assert.equal(created.length, 1);
	assert.equal(win.webContents.sent.at(-1)[1].micMuted, true);
	popout.update({ ...STATE, title: 'Renamed' });
	assert.equal(win.webContents.sent.at(-1)[1].title, 'Renamed');
	popout.update('garbage');
	assert.equal(win.webContents.sent.at(-1)[1].title, 'Renamed', 'invalid state never reaches the page');
});

test('nothing opens for invalid state or without a main window', async () => {
	const { popout, created, host } = load();
	assert.equal(await popout.open({ label: 'x' }), false);
	host.browserWindow = null;
	assert.equal(await popout.open(STATE), false);
	assert.equal(created.length, 0);
});

test('the pop-out cannot navigate, open windows or attach webviews', async () => {
	const { popout, created } = load();
	await popout.open(STATE);
	const contents = created[0].webContents;
	for (const name of ['will-navigate', 'will-redirect', 'will-attach-webview']) {
		let prevented = false;
		contents.emit(name, { preventDefault: () => { prevented = true; } }, 'https://example.com/');
		assert.equal(prevented, true, name);
	}
	assert.deepEqual({ ...contents.openHandler({ url: 'https://example.com/' }) }, { action: 'deny' });
});

test('commands are taken only from the pop-out page, and only from the allowlist', async () => {
	const { popout, created, host, send } = load();
	await popout.open(STATE);
	const contents = created[0].webContents;
	send('toggle-mic');
	send('hangup');
	assert.deepEqual(host.sent, [['desktop:call-popout-command', 'toggle-mic'], ['desktop:call-popout-command', 'hangup']]);
	host.sent.length = 0;

	send('toggle-mic', { subframe: true });
	send('toggle-mic', { contents: Object.assign(new EventEmitter(), { id: 99, mainFrame: { url: PAGE_URL, processId: 4, routingId: 1 } }) });
	for (const bad of ['delete-session', 'TOGGLE-MIC', '', { toString: () => 'hangup' }, ['hangup']]) send(bad);
	send('toggle-mic', { url: 'file:///tmp/evil.html' });
	assert.deepEqual(host.sent, [], 'a subframe, another window, an unknown command or another page gets nothing through');

	contents.mainFrame.url = PAGE_URL;
	send('return');
	assert.equal(host.focused, 1, 'return brings the main window forward');
	assert.deepEqual(host.sent, [['desktop:call-popout-command', 'return']]);
});

test('the dock button closes the pop-out and tells the main window', async () => {
	const { popout, created, host, send } = load();
	await popout.open(STATE);
	send('dock');
	assert.equal(created[0].destroyed, true);
	assert.equal(popout.isOpen, false);
	assert.deepEqual(host.sent.at(-1), ['desktop:call-popout-closed', undefined]);
	send('hangup', { contents: created[0].webContents });
	assert.equal(host.sent.filter(([channel]) => channel === 'desktop:call-popout-command').length, 0, 'a closed pop-out sends nothing');
});

test('the pop-out closes when the main window page goes, and only then', async () => {
	const { popout, created, hostWindow } = load();
	await popout.open(STATE);
	hostWindow.webContents.emit('did-start-navigation', {}, 'http://127.0.0.1:8000/#x', true, true);
	hostWindow.webContents.emit('did-start-navigation', {}, 'http://127.0.0.1:8000/frame', false, false);
	assert.equal(popout.isOpen, true, 'a hash change or a subframe load keeps the call');
	hostWindow.webContents.emit('did-start-navigation', {}, 'http://127.0.0.1:8000/', false, true);
	assert.equal(created[0].destroyed, true, 'a reload ends the call');
	assert.equal(hostWindow.webContents.listenerCount('did-start-navigation'), 0, 'its listeners go with it');

	await popout.open(STATE);
	hostWindow.webContents.emit('render-process-gone');
	assert.equal(created[1].destroyed, true);
	await popout.open(STATE);
	hostWindow.emit('closed');
	assert.equal(created[2].destroyed, true);
	await popout.open(STATE);
	created[3].webContents.emit('render-process-gone');
	assert.equal(created[3].destroyed, true, 'a crashed pop-out page closes, so the bar returns to the app');
});

test('the pop-out reopens where it was left', async () => {
	const first = load();
	await first.popout.open(STATE);
	first.created[0].position = [2100, 400];
	first.created[0].emit('moved');
	first.popout.close();
	const file = path.join(first.userData, 'call-popout.json');
	await until(() => existsSync(file));
	assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { x: 2100, y: 400 });

	const second = load({ userData: first.userData });
	await second.popout.open(STATE);
	assert.equal(second.created[0].options.x, 2100);
	assert.equal(second.created[0].options.y, 400);

	writeFileSync(file, '{"x":"left","y":null}');
	const third = load({ userData: first.userData });
	await third.popout.open(STATE);
	assert.equal(third.created[0].options.x, 1920 - 420 - 24, 'a corrupt file opens in the corner');
});

test('the pop-out page has its own strict CSP and its preload exposes only state and commands', () => {
	const html = readFileSync(path.join(desktopDir, 'src/pages/call.html'), 'utf8');
	const csp = html.match(/Content-Security-Policy" content="([^"]+)"/)[1];
	assert.match(csp, /default-src 'none'/);
	assert.match(csp, /script-src 'self'/);
	assert.match(csp, /connect-src 'none'/);
	assert.doesNotMatch(csp, /unsafe/);
	assert.doesNotMatch(html, /<script>(?!<)/, 'no inline script');
	const preload = readFileSync(path.join(desktopDir, 'dist/preload/call-popout.js'), 'utf8');
	assert.match(preload, /exposeInMainWorld\('shuffleCall'/);
	assert.deepEqual([...preload.matchAll(/ipcRenderer\.(\w+)\('([\w:-]+)'/g)].map((m) => `${m[1]} ${m[2]}`).sort(),
		['on call-popout:state', 'removeListener call-popout:state', 'send call-popout:command']);
});

test('the pop-out page draws state as text and sends its buttons', () => {
	const source = readFileSync(path.join(desktopDir, 'src/pages/call.js'), 'utf8');
	const module = { exports: {} };
	vm.runInNewContext(source, { module });
	const { render, start } = module.exports;
	const element = () => {
		const node = new EventTarget();
		node.attributes = {};
		node.classes = new Set();
		node.textContent = '';
		node.setAttribute = (name, value) => { node.attributes[name] = value; };
		node.classList = { toggle: (name, on) => (on ? node.classes.add(name) : node.classes.delete(name)), add: (name) => node.classes.add(name) };
		return node;
	};
	const nodes = Object.fromEntries(['label', 'title', 'mic', 'agent', 'return', 'dock', 'hangup'].map((id) => [id, element()]));
	const doc = Object.assign(new EventTarget(), { getElementById: (id) => nodes[id], body: element() });
	const commands = [];
	let listener;
	start(doc, { command: (name) => commands.push(name), onState: (fn) => { listener = fn; return () => {}; } });

	listener({ ...STATE, label: '<b>live</b>', micProblem: true });
	assert.equal(nodes.label.textContent, '<b>live</b>');
	assert.ok(nodes.label.classes.has('mic-problem'));
	assert.equal(nodes.title.textContent, 'Planning the trip');
	assert.equal(nodes.mic.attributes['aria-pressed'], 'false');
	assert.equal(nodes.mic.attributes['aria-label'], 'Mute microphone');
	assert.equal(nodes.agent.attributes['aria-pressed'], 'true');
	assert.equal(nodes.agent.attributes['aria-label'], 'Unmute speaker');
	assert.ok(doc.body.classes.has('ready'));
	render(doc, null);

	for (const id of ['return', 'mic', 'agent', 'dock', 'hangup']) nodes[id].dispatchEvent(new Event('click'));
	const escape = new Event('keydown');
	escape.key = 'Escape';
	doc.dispatchEvent(escape);
	assert.deepEqual(commands, ['return', 'toggle-mic', 'toggle-agent', 'dock', 'hangup', 'dock']);
});
