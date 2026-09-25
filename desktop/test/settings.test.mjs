import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { SettingsStore, isManagedConnection, plainCipher } = require('../dist/main/settings.js');
const good = { host: '127.0.0.1', port: 54321, user: 'ai_shuffle', password: 'generated-test-secret', database: 'ai_shuffle', managed: true };

test('managed connection validation rejects external, malformed and unsafe endpoints', () => {
	assert.equal(isManagedConnection(good), true);
	for (const patch of [{ host: 'db.local' }, { host: 'localhost' }, { host: '127.0.0.2' }, { port: '5432' }, { port: 0 }, { port: 65536 }, { port: 5.5 }, { password: '' }, { password: 'a\nb' }, { password: 'a\tb' }, { database: 'db;DROP' }, { user: 'x'.repeat(64) }]) {
		assert.equal(isManagedConnection({ ...good, ...patch }), false);
	}
});

test('only generated managed credentials are persisted, encrypted and reusable after restart', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-settings-'));
	const file = path.join(dir, 'worker-settings.json');
	const cipher = { available: () => true, encrypt: (value) => Buffer.from(value.split('').reverse().join('')), decrypt: (value) => value.toString().split('').reverse().join('') };
	try {
		const store = new SettingsStore(file, cipher); await store.load();
		assert.equal(store.postgres(), null);
		await store.savePostgres(good);
		assert.equal((await readFile(file, 'utf8')).includes(good.password), false);
		const again = new SettingsStore(file, cipher); await again.load();
		assert.deepEqual(again.postgres(), good);
		assert.equal('encrypted' in JSON.parse(await readFile(file, 'utf8')).postgres.password, true);
		for (const patch of [{ host: 'external' }, { managed: false }, { managed: undefined }]) {
			await assert.rejects(again.savePostgres({ ...good, ...patch }), /Only generated/);
		}
		assert.deepEqual(again.postgres(), good);
		assert.equal(typeof again.useBundled, 'undefined');
		assert.equal(typeof again.postgresPublic, 'undefined');
		if (process.platform !== 'win32') assert.equal((await stat(file)).mode & 0o777, 0o600);
	} finally { await rm(dir, { recursive: true, force: true }); }
});

test('legacy external settings are ignored without modifying or importing them', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-settings-'));
	const file = path.join(dir, 'worker-settings.json');
	try {
		for (const managed of [undefined, false]) {
			const original = JSON.stringify({ version: 1, postgres: { ...good, managed, password: { plain: 'old-test-secret' } } });
			await writeFile(file, original);
			const store = new SettingsStore(file, plainCipher); await store.load();
			assert.equal(store.postgres(), null);
			assert.equal(await readFile(file, 'utf8'), original);
		}
	} finally { await rm(dir, { recursive: true, force: true }); }
});

test('private fallback storage persists, and unavailable keyrings do not leak encrypted values', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-settings-')); const file = path.join(dir, 'worker-settings.json');
	try {
		const store = new SettingsStore(file, plainCipher); await store.load(); await store.savePostgres(good);
		const again = new SettingsStore(file, plainCipher); await again.load();
		assert.equal('plain' in JSON.parse(await readFile(file, 'utf8')).postgres.password, true); assert.deepEqual(again.postgres(), good);
		await writeFile(file, JSON.stringify({ version: 1, postgres: { ...good, password: { encrypted: 'dGVzdA==' } } }));
		const broken = new SettingsStore(file, { available: () => true, decrypt: () => { throw Error('keyring locked'); } });
		await broken.load(); assert.equal(broken.postgres(), null);
		await writeFile(file, '{broken'); await again.load(); assert.equal(again.postgres(), null);
	} finally { await rm(dir, { recursive: true, force: true }); }
});
