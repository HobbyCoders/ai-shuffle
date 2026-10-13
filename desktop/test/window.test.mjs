import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const desktopDir = fileURLToPath(new URL('..', import.meta.url));

/** dist/main/window.js against a fake Electron whose saved window state loads when the test says so. */
function load() {
	const created = [];
	let release;
	const loading = new Promise((resolve) => { release = resolve; });
	class BrowserWindow extends EventEmitter {
		constructor(options) {
			super();
			this.options = options;
			this.destroyed = false;
			this.loads = [];
			this.webContents = Object.assign(new EventEmitter(), { id: created.length + 1 });
			created.push(this);
		}
		isDestroyed() { return this.destroyed; }
		async loadFile(file, options) { this.loads.push(['file', file, options]); }
		async loadURL(url, options) { this.loads.push(['url', url, options]); }
		setAppDetails() {}
	}
	const electron = { app: { getAppPath: () => desktopDir }, BrowserWindow, nativeTheme: { shouldUseDarkColors: false }, shell: {} };
	const replacements = {
		electron,
		'./navigation': { attachNavigationPolicy() {} },
		'./state': { WindowStateStore: class { async load() { await loading; return { bounds: null, maximized: false }; } track() {} } },
		'./managed-context': { managedContext: () => null }
	};
	const file = path.join(desktopDir, 'dist/main/window.js');
	const module = { exports: {} };
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, __dirname: path.dirname(file),
		require: (id) => (id in replacements ? replacements[id] : createRequireLike(id)), process, URL
	}, { filename: file });
	return { MainWindow: module.exports.MainWindow, created, release };
}

function createRequireLike(id) {
	if (id === 'node:path') return path;
	if (id === 'node:url') return { pathToFileURL: (p) => new URL(`file://${p}`) };
	throw new Error(`unexpected require ${id}`);
}

test('two opens while the saved window state loads share one window', async () => {
	const { MainWindow, created, release } = load();
	const main = new MainWindow('/profile');
	// The first open shows the status page; a dock click arrives before the window exists.
	const status = main.showPage('status');
	const worker = main.loadWorker('http://127.0.0.1:8000/');
	release();
	await Promise.all([status, worker]);
	assert.equal(created.length, 1, 'one window, so the IPC sender check and the close guard see the only one');
	assert.equal(main.browserWindow, created[0]);
	assert.equal(main.isOwnSender(created[0].webContents.id), true);
	assert.deepEqual(created[0].loads.map(([kind]) => kind), ['file', 'url']);
	assert.equal(created[0].loads[1][2].extraHeaders, 'Cache-Control: no-cache\r\nPragma: no-cache\r\n', 'reopening the same worker URL must revalidate old HTML');
	// A window closed later is replaced by a fresh one, not the finished promise's.
	created[0].destroyed = true;
	await main.showPage('status');
	assert.equal(created.length, 2);
	assert.equal(main.browserWindow, created[1]);
});
