import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const desktopDir = fileURLToPath(new URL('..', import.meta.url));

function load({ empty = false, platform = 'linux' } = {}) {
	const trays = [];
	class Tray extends EventEmitter {
		constructor(image) { super(); this.image = image; trays.push(this); }
		setToolTip(text) { this.tooltip = text; }
		setContextMenu(menu) { this.menu = menu; }
	}
	const image = { isEmpty: () => empty, resize: (size) => ({ ...image, size }) };
	const electron = {
		app: { getAppPath: () => desktopDir, name: 'ai-shuffle-desktop' },
		Menu: { buildFromTemplate: (template) => template },
		nativeImage: { createFromPath: (file) => ({ ...image, file }) },
		Tray
	};
	const file = path.join(desktopDir, 'dist/main/tray.js');
	const module = { exports: {} };
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, require: (id) => (id === 'electron' ? electron : path), process: { platform }
	}, { filename: file });
	return { installTray: module.exports.installTray, trays };
}

function recorder(extra = {}) {
	const calls = [];
	const actions = {};
	for (const name of ['show', 'restartWorker', 'openWorkerStatus', 'openInBrowser', 'forceQuit']) actions[name] = () => calls.push(name);
	return { calls, actions: { ...actions, ...extra } };
}

test('the tray menu runs each action under the product name, and its Quit is the forcing quit, not the plain role', () => {
	const { installTray, trays } = load();
	const { calls, actions } = recorder();
	// The package name (app.name in stable builds) must not leak into the menu.
	const tray = installTray(actions, 'AI Shuffle');
	assert.equal(tray, trays[0]);
	assert.equal(tray.tooltip, 'AI Shuffle');
	assert.equal(tray.image.size.width, 16);
	const items = tray.menu.filter((item) => item.type !== 'separator');
	assert.deepEqual(Array.from(items, (item) => item.label), ['Open AI Shuffle', 'Open in Browser', 'Restart Worker', 'Worker Status', 'Quit']);
	for (const item of items) item.click();
	assert.deepEqual(calls, ['show', 'openInBrowser', 'restartWorker', 'openWorkerStatus', 'forceQuit']);
	const quit = items.at(-1);
	assert.equal(quit.role, undefined, 'role "quit" would wait on a wedged worker');
	// Updates are checked in the app's own Settings screen, never from the tray.
	assert.equal(tray.menu.some((item) => /update/i.test(item.label ?? '')), false);
	// No doubled, leading or trailing separators.
	const kinds = tray.menu.map((item) => item.type ?? 'item');
	assert.notEqual(kinds[0], 'separator');
	assert.notEqual(kinds.at(-1), 'separator');
	assert.equal(kinds.some((kind, i) => kind === 'separator' && kinds[i + 1] === 'separator'), false);
	tray.emit('click');
	assert.equal(calls.at(-1), 'show');
});

test('a missing icon means no tray; macOS uses the menu-bar size', () => {
	assert.equal(load({ empty: true }).installTray(recorder().actions, 'AI Shuffle'), null);
	assert.equal(load({ platform: 'darwin' }).installTray(recorder().actions, 'AI Shuffle').image.size.width, 18);
});
