import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { SettingsStore, validatePostgres, plainCipher, DEFAULT_POSTGRES } = require('../dist/main/settings.js');

const good = { host: ' db.local ', port: '5433', user: 'shuffle', password: 'pw', database: 'ai_shuffle' };

test('validatePostgres trims and coerces a good submission', () => {
	const result = validatePostgres(good);
	assert.equal(result.ok, true);
	assert.deepEqual(result.config, { host: 'db.local', port: 5433, user: 'shuffle', password: 'pw', database: 'ai_shuffle' });
});

test('validatePostgres rejects what the server would choke on', () => {
	assert.match(validatePostgres({ ...good, host: '' }).error, /host/i);
	assert.match(validatePostgres({ ...good, host: 'postgres://db' }).error, /scheme/i);
	assert.match(validatePostgres({ ...good, port: '70000' }).error, /port/i);
	assert.match(validatePostgres({ ...good, database: '1db' }).error, /database name/i);
	assert.match(validatePostgres({ ...good, database: 'a b' }).error, /database name/i);
	assert.match(validatePostgres({ ...good, password: '' }).error, /password/i);
});

test('SettingsStore round-trips the connection through the cipher', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'worker-settings.json');
	const calls = [];
	const cipher = {
		available: () => true,
		encrypt: (text) => {
			calls.push('encrypt');
			return Buffer.from(text.split('').reverse().join(''), 'utf8');
		},
		decrypt: (data) => data.toString('utf8').split('').reverse().join('')
	};
	try {
		const store = new SettingsStore(file, cipher);
		await store.load();
		assert.equal(store.configured, false);
		assert.deepEqual(store.postgresPublic(), DEFAULT_POSTGRES);

		await store.savePostgres({ host: 'db', port: 5432, user: 'u', password: 's3cret', database: 'd' });
		assert.deepEqual(calls, ['encrypt']);
		const raw = await readFile(file, 'utf8');
		assert.ok(!raw.includes('s3cret'), 'password must not be stored in clear');

		const reloaded = new SettingsStore(file, cipher);
		await reloaded.load();
		assert.equal(reloaded.configured, true);
		assert.equal(reloaded.passwordStoredPlain, false);
		assert.deepEqual(reloaded.postgres(), { host: 'db', port: 5432, user: 'u', password: 's3cret', database: 'd', managed: false });
		assert.deepEqual(reloaded.postgresPublic(), { host: 'db', port: 5432, user: 'u', database: 'd' });
		assert.equal(reloaded.managed, false, 'a saved external connection is not the bundled server');
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('SettingsStore falls back to plain storage without a keyring and says so', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'worker-settings.json');
	try {
		const store = new SettingsStore(file, plainCipher);
		await store.load();
		await store.savePostgres({ host: 'db', port: 5432, user: 'u', password: 'pw', database: 'd' });
		assert.equal(store.passwordStoredPlain, true);
		const reloaded = new SettingsStore(file, plainCipher);
		await reloaded.load();
		assert.equal(reloaded.postgres().password, 'pw');
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('validatePostgres carries the managed flag through only when given', () => {
	assert.equal(validatePostgres(good).config.managed, undefined);
	assert.equal(validatePostgres({ ...good, managed: true }).config.managed, true);
	assert.equal(validatePostgres({ ...good, managed: 'yes' }).config.managed, false);
});

test('SettingsStore wants the bundled server on a first run and round-trips a managed connection', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'worker-settings.json');
	try {
		const store = new SettingsStore(file, plainCipher);
		await store.load();
		assert.equal(store.managed, true, 'nothing configured means the bundled server');

		const bundled = { host: '127.0.0.1', port: 54321, user: 'ai_shuffle', password: 'gen', database: 'ai_shuffle', managed: true };
		await store.savePostgres(bundled);
		assert.equal(store.managed, true);
		assert.equal(store.configured, true);
		const reloaded = new SettingsStore(file, plainCipher);
		await reloaded.load();
		assert.equal(reloaded.managed, true);
		assert.deepEqual(reloaded.postgres(), bundled);
		assert.equal(JSON.parse(await readFile(file, 'utf8')).postgres.managed, true);

		// Choosing an external server replaces it; going back drops the external one.
		await reloaded.savePostgres({ host: 'db', port: 5432, user: 'u', password: 'pw', database: 'd', managed: false });
		assert.equal(reloaded.managed, false);
		await reloaded.useBundled();
		assert.equal(reloaded.managed, true);
		assert.equal(reloaded.configured, false);
		assert.equal(reloaded.postgres(), null);
		const again = new SettingsStore(file, plainCipher);
		await again.load();
		assert.equal(again.managed, true);
		assert.equal(again.postgres(), null);

		// useBundled keeps a managed connection as it is.
		await again.savePostgres(bundled);
		await again.useBundled();
		assert.deepEqual(again.postgres(), bundled);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('SettingsStore reads files written before the managed flag existed as external', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'worker-settings.json');
	try {
		const legacy = { version: 1, postgres: { host: 'db', port: 5432, user: 'u', database: 'd', password: { plain: 'pw' } } };
		await (await import('node:fs/promises')).writeFile(file, JSON.stringify(legacy), 'utf8');
		const store = new SettingsStore(file, plainCipher);
		await store.load();
		assert.equal(store.configured, true);
		assert.equal(store.managed, false);
		assert.equal(store.postgres().managed, false);
		// A malformed flag makes the entry invalid rather than silently managed.
		await (await import('node:fs/promises')).writeFile(file, JSON.stringify({ ...legacy, postgres: { ...legacy.postgres, managed: 'yes' } }), 'utf8');
		const broken = new SettingsStore(file, plainCipher);
		await broken.load();
		assert.equal(broken.configured, false);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('SettingsStore ignores a corrupt file', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-desktop-'));
	const file = path.join(dir, 'worker-settings.json');
	try {
		await (await import('node:fs/promises')).writeFile(file, '{oops', 'utf8');
		const store = new SettingsStore(file, plainCipher);
		await store.load();
		assert.equal(store.configured, false);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});
