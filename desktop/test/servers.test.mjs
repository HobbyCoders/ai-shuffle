import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { ServerStore, defaultServerName, parseServerInput } = require('../dist/main/servers.js');

test('parseServerInput accepts bare hosts and keeps only the origin', () => {
	assert.deepEqual(parseServerInput('10.0.0.20:8000'), { origin: 'http://10.0.0.20:8000', pairingCode: null });
	assert.deepEqual(parseServerInput(' https://studio.example.com/ '), { origin: 'https://studio.example.com', pairingCode: null });
	assert.deepEqual(parseServerInput('https://studio.example.com/settings?tab=devices'), {
		origin: 'https://studio.example.com',
		pairingCode: null
	});
});

test('parseServerInput extracts a pairing code from a pairing link', () => {
	assert.deepEqual(parseServerInput('https://studio.example.com/pair#token=abcd-efgh-1234'), {
		origin: 'https://studio.example.com',
		pairingCode: 'ABCDEFGH1234'
	});
	assert.throws(() => parseServerInput('https://studio.example.com/pair#token=short'), /12 characters/);
});

test('parseServerInput rejects things that are not worker addresses', () => {
	assert.throws(() => parseServerInput(''), /Enter the worker address/);
	assert.throws(() => parseServerInput('ftp://box'), /http/);
	assert.throws(() => parseServerInput('https://user:pw@box'), /credentials/);
	assert.throws(() => parseServerInput('not a url at all'), /valid address/);
});

test('defaultServerName labels loopback as this computer', () => {
	assert.equal(defaultServerName('http://localhost:8000'), 'This computer');
	assert.equal(defaultServerName('http://127.0.0.1:8000'), 'This computer');
	assert.equal(defaultServerName('https://studio.example.com'), 'studio.example.com');
});

test('ServerStore persists, de-duplicates by origin, and tracks the last worker', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'servers.json');
	try {
		const store = new ServerStore(file);
		await store.load();
		const a = await store.add({ url: 'http://10.0.0.20:8000', name: 'Homelab' });
		const again = await store.add({ url: '10.0.0.20:8000/' });
		assert.equal(again.id, a.id, 'same origin returns the saved entry');
		await store.markUsed(a.id);
		assert.equal(store.last()?.id, a.id);

		const reloaded = new ServerStore(file);
		await reloaded.load();
		assert.equal(reloaded.list().length, 1);
		assert.equal(reloaded.last()?.name, 'Homelab');

		await reloaded.remove(a.id);
		assert.equal(reloaded.last(), null);
		assert.deepEqual(JSON.parse(await readFile(file, 'utf8')).servers, []);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('ServerStore ignores a corrupt file', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'servers.json');
	try {
		await (await import('node:fs/promises')).writeFile(file, '{not json', 'utf8');
		const store = new ServerStore(file);
		await store.load();
		assert.deepEqual(store.list(), []);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});
