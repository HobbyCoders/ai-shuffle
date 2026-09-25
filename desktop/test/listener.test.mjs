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

test('no IPC channel or preload member hands the listener token to a page', () => {
	const handlers = new Map();
	const exports = {};
	const source = readFileSync(new URL('../dist/main/ipc.js', import.meta.url), 'utf8');
	runInNewContext(source, { exports, URL, process, require: name => {
		if (name === 'electron') return { ipcMain: { handle: (key, fn) => handlers.set(key, fn), on() {} }, app: {} };
		if (name === './managed-context') return { managedContext: () => null };
		if (name === './disclosure') return require('../dist/main/disclosure.js');
		throw new Error(name);
	} });
	exports.registerIpc({ window: { isOwnSender: () => true }, updater: {}, worker: {} });
	assert.equal(handlers.has('desktop:listener-token'), false);
	const preload = readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8');
	assert.doesNotMatch(preload, /listener-token|listenerToken/);
});

test('a page-supplied copy of the header is stripped everywhere and replaced for the worker', () => {
	const headers = { Accept: 'text/html', 'x-shuffle-desktop-listener': 'forged' };
	assert.deepEqual(withListenerHeader('https://example.com/', headers, target), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, target), { Accept: 'text/html', [LISTENER_HEADER]: token });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, null), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, { ...target, token: 'short' }), { Accept: 'text/html' });
});
