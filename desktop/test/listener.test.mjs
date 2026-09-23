import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const require = createRequire(import.meta.url);
const { withListenerHeader, isWorkerRequest, newListenerToken, isListenerToken, LISTENER_HEADER, LISTENER_TOKEN_ENV } = require('../dist/main/listener.js');

const token = 'a'.repeat(64);
const target = { origin: 'http://127.0.0.1:8000', token };

test('the listener token is 32 random bytes of hex and changes every time', () => {
	const one = newListenerToken();
	assert.equal(isListenerToken(one), true);
	assert.notEqual(one, newListenerToken());
	assert.equal(isListenerToken('A'.repeat(64)), false);
	assert.equal(isListenerToken(null), false);
	assert.equal(LISTENER_TOKEN_ENV, 'SHUFFLE_DESKTOP_LISTENER_TOKEN');
});

test('only the worker origin over http and ws gets the header', () => {
	for (const url of ['http://127.0.0.1:8000/', 'http://127.0.0.1:8000/api/v1/sessions?x=1', 'ws://127.0.0.1:8000/api/v1/ws']) {
		assert.equal(isWorkerRequest(url, target.origin), true, url);
		assert.equal(withListenerHeader(url, {}, target)[LISTENER_HEADER], token, url);
	}
	for (const url of ['http://127.0.0.1:8001/', 'http://127.0.0.1:3000/', 'http://localhost:8000/', 'https://127.0.0.1:8000/',
		'wss://127.0.0.1:8000/', 'https://hobbycoders.com/', 'http://user:pw@127.0.0.1:8000/', 'file:///C:/x', 'not a url']) {
		assert.equal(isWorkerRequest(url, target.origin), false, url);
		assert.equal(withListenerHeader(url, {}, target)[LISTENER_HEADER], undefined, url);
	}
});

test('listener token IPC answers only the worker main frame of the app window', () => {
	const handlers = new Map();
	const exports = {};
	const source = readFileSync(new URL('../dist/main/ipc.js', import.meta.url), 'utf8');
	runInNewContext(source, { exports, URL, process, require: name => {
		if (name === 'electron') return { ipcMain: { handle: (key, fn) => handlers.set(key, fn), on() {} }, app: {} };
		if (name === './managed-context') return { managedContext: () => null };
		if (name === './diagnostics') return { desktopSampler: () => () => ({}) };
		if (name === './window') return { TITLE_BAR_HEIGHT: 52 };
		if (name === './listener') return require('../dist/main/listener.js');
		throw new Error(name);
	} });
	let current = { origin: 'http://127.0.0.1:8000', token };
	exports.registerIpc({ window: { isOwnSender: id => id === 1, currentWorkerUrl: 'http://127.0.0.1:8000' }, updater: {}, worker: { listener: () => current } });
	const invoke = handlers.get('desktop:listener-token');
	const frame = { url: 'http://127.0.0.1:8000/sessions' };
	const sender = { id: 1, mainFrame: frame };
	assert.deepEqual({ ...invoke({ sender, senderFrame: frame }) }, { header: LISTENER_HEADER, token });
	assert.throws(() => invoke({ sender: { ...sender, id: 2 }, senderFrame: frame }), /not allowed/);
	assert.throws(() => invoke({ sender, senderFrame: { ...frame } }), /not allowed/, 'subframes get nothing');
	for (const url of ['file:///C:/app/src/pages/worker.html', 'https://hobbycoders.com/', 'http://127.0.0.1:3000/']) {
		assert.throws(() => invoke({ sender, senderFrame: { url }, }), /not allowed/, url);
		const main = { url };
		assert.throws(() => invoke({ sender: { id: 1, mainFrame: main }, senderFrame: main }), /not allowed/, url);
	}
	current = { origin: 'http://127.0.0.1:9000', token };
	assert.equal(invoke({ sender, senderFrame: frame }), null, 'a token for another worker is never handed out');
	current = null;
	assert.equal(invoke({ sender, senderFrame: frame }), null);
});

test('a page-supplied copy of the header is stripped everywhere and replaced for the worker', () => {
	const headers = { Accept: 'text/html', 'x-shuffle-desktop-listener': 'forged' };
	assert.deepEqual(withListenerHeader('https://example.com/', headers, target), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, target), { Accept: 'text/html', [LISTENER_HEADER]: token });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, null), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, { ...target, token: 'short' }), { Accept: 'text/html' });
});
