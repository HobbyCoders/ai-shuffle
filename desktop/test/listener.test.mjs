import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const require = createRequire(import.meta.url);
const { withListenerHeader, isWorkerRequest, newListenerToken, isListenerToken, requestInitiator, installListenerHeader, LISTENER_HEADER, LISTENER_TOKEN_ENV } = require('../dist/main/listener.js');

const token = 'a'.repeat(64);
const target = { origin: 'http://127.0.0.1:8000', token };
// A fetch from the worker's own page.
const fromWorker = { resourceType: 'xhr', topOrigin: 'http://127.0.0.1:8000' };

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
		assert.equal(withListenerHeader(url, {}, target, fromWorker)[LISTENER_HEADER], token, url);
	}
	for (const url of ['http://127.0.0.1:8001/', 'http://127.0.0.1:3000/', 'http://localhost:8000/', 'https://127.0.0.1:8000/',
		'wss://127.0.0.1:8000/', 'https://hobbycoders.com/', 'http://user:pw@127.0.0.1:8000/', 'file:///C:/x', 'not a url']) {
		assert.equal(isWorkerRequest(url, target.origin), false, url);
		assert.equal(withListenerHeader(url, {}, target, fromWorker)[LISTENER_HEADER], undefined, url);
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
		if (name === './pages') return require('../dist/main/pages.js');
		throw new Error(name);
	} });
	exports.registerIpc({ window: { isOwnSender: () => true }, updater: {}, worker: {} });
	assert.equal(handlers.has('desktop:listener-token'), false);
	const preload = readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8');
	assert.doesNotMatch(preload, /listener-token|listenerToken/);
});

test('a page-supplied copy of the header is stripped everywhere and replaced for the worker', () => {
	const headers = { Accept: 'text/html', 'x-shuffle-desktop-listener': 'forged' };
	assert.deepEqual(withListenerHeader('https://example.com/', headers, target, fromWorker), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, target, fromWorker), { Accept: 'text/html', [LISTENER_HEADER]: token });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, null, fromWorker), { Accept: 'text/html' });
	assert.deepEqual(withListenerHeader('http://127.0.0.1:8000/', headers, { ...target, token: 'short' }, fromWorker), { Accept: 'text/html' });
});

test('NC-5: only requests made under a worker-origin top frame carry the token; any window may navigate to the worker', () => {
	const url = 'http://127.0.0.1:8000/api/v1/sessions';
	// Fetches, sockets and images from the worker UI (and its same-app frames) get it.
	for (const resourceType of ['xhr', 'webSocket', 'image', 'script', 'subFrame', 'other']) {
		assert.equal(withListenerHeader(url, {}, target, { resourceType, topOrigin: 'http://127.0.0.1:8000' })[LISTENER_HEADER], token, resourceType);
	}
	// The same requests from an OAuth, hub or Plaid pop-up, or with no known initiator, do not.
	for (const topOrigin of ['https://accounts.google.com', 'https://cdn.plaid.com', 'http://127.0.0.1:8001', 'http://localhost:8000', null]) {
		for (const resourceType of ['xhr', 'webSocket', 'image', 'subFrame']) {
			assert.equal(withListenerHeader(url, {}, target, { resourceType, topOrigin })[LISTENER_HEADER], undefined, `${resourceType} from ${topOrigin}`);
		}
	}
	// A top-level navigation to the worker (the main window, or the OAuth pop-up landing on the callback) keeps it.
	assert.equal(withListenerHeader('http://127.0.0.1:8000/api/oauth/callback', {}, target, { resourceType: 'mainFrame', topOrigin: 'https://accounts.google.com' })[LISTENER_HEADER], token);
	// A development run's dev-server page is the window's page origin, so its fetches to the worker keep working.
	assert.equal(withListenerHeader(url, {}, { ...target, pageOrigin: 'http://localhost:5173' }, { resourceType: 'xhr', topOrigin: 'http://localhost:5173' })[LISTENER_HEADER], token);
	assert.equal(withListenerHeader(url, {}, { ...target, pageOrigin: null }, { resourceType: 'xhr', topOrigin: 'http://localhost:5173' })[LISTENER_HEADER], undefined);
	// Still never to another origin, whoever asks.
	assert.equal(withListenerHeader('https://accounts.google.com/', {}, target, { resourceType: 'mainFrame', topOrigin: null })[LISTENER_HEADER], undefined);
});

test('NC-5: the initiator is the requesting frame\'s top frame, else the owning page, else unknown', () => {
	const frame = (url) => ({ top: { url } });
	const page = (url, destroyed = false) => ({ isDestroyed: () => destroyed, getURL: () => url });
	assert.deepEqual(requestInitiator({ resourceType: 'xhr', frame: frame('http://127.0.0.1:8000/chat') }), { resourceType: 'xhr', topOrigin: 'http://127.0.0.1:8000' });
	assert.deepEqual(requestInitiator({ resourceType: 'xhr', frame: frame('https://accounts.google.com/o/oauth2'), webContents: page('http://127.0.0.1:8000/') }).topOrigin, 'https://accounts.google.com');
	assert.equal(requestInitiator({ resourceType: 'webSocket', frame: null, webContents: page('http://127.0.0.1:8000/') }).topOrigin, 'http://127.0.0.1:8000');
	assert.equal(requestInitiator({ resourceType: 'xhr', frame: null, webContents: page('http://127.0.0.1:8000/', true) }).topOrigin, null);
	assert.equal(requestInitiator({ resourceType: 'xhr' }).topOrigin, null);
	assert.equal(requestInitiator({ resourceType: 'xhr', frame: { get top() { throw new Error('Render frame was disposed'); } } }).topOrigin, null);
	assert.equal(requestInitiator({ resourceType: 'image', frame: frame('about:blank') }).topOrigin, null);
});

test('NC-5: the session hook applies the initiator rule to every request', () => {
	let hook;
	installListenerHeader({ webRequest: { onBeforeSendHeaders: (fn) => { hook = fn; } } }, () => target);
	const send = (details) => { let out; hook({ requestHeaders: {}, ...details }, (result) => { out = result.requestHeaders; }); return out[LISTENER_HEADER]; };
	assert.equal(send({ url: 'http://127.0.0.1:8000/api/v1/me', resourceType: 'xhr', frame: { top: { url: 'http://127.0.0.1:8000/' } } }), token);
	assert.equal(send({ url: 'http://127.0.0.1:8000/api/v1/me', resourceType: 'xhr', frame: { top: { url: 'https://evil.example/' } } }), undefined);
	assert.equal(send({ url: 'http://127.0.0.1:8000/', resourceType: 'mainFrame', frame: { top: { url: 'file:///app/src/pages/worker.html' } } }), token);
});
