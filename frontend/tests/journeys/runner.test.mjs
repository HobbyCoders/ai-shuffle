// Exercise the real runner's selection and cleanup without a browser or app.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { deleteCaseChats } from './runner.mjs';

const runner = new URL('./runner.mjs', import.meta.url).href;
function list(args, env = {}) {
	const code = `
		import { runJourney } from ${JSON.stringify(runner)};
		process.argv = ['node', 'selection-test', ...${JSON.stringify(args)}];
		await runJourney({ name: 'selection', cases: [
			{ name: 'copy' }, { name: 'prefix-copy' }, { name: 'copy-more' }
		] });`;
	return spawnSync(process.execPath, ['--input-type=module', '-e', code], {
		encoding: 'utf8', env: { ...process.env, JOURNEY_ONLY: '', ...env }
	});
}

test('exact IDs cannot pull in prefix/suffix cases or other variants', () => {
	const result = list(['--list', '--case=copy@desktop-fast', '--case=copy@phone-slow']);
	assert.equal(result.status, 0, result.stderr);
	assert.deepEqual(result.stdout.trim().split('\n'), ['copy@desktop-fast', 'copy@phone-slow']);
});

test('an absent exact ID fails closed, even with --list', () => {
	for (const id of ['copy', 'copy@desktop', 'missing@desktop-fast', '']) {
		const result = list(['--list', `--case=${id}`]);
		assert.notEqual(result.status, 0);
		assert.match(result.stderr, /exact case ID/);
	}
});

test('a partially matched exact selection also fails closed', () => {
	const result = list(['--list', '--case=copy@desktop-fast', '--case=missing@phone-slow']);
	assert.notEqual(result.status, 0);
	assert.match(result.stderr, /missing@phone-slow/);
});

test('substring and exact selectors cannot silently combine (including env)', () => {
	for (const [args, env] of [
		[['--list', '--only=copy', '--case=copy@desktop-fast'], {}],
		[['--list', '--case=copy@desktop-fast'], { JOURNEY_ONLY: 'copy' }]
	]) {
		const result = list(args, env);
		assert.notEqual(result.status, 0);
		assert.match(result.stderr, /cannot combine/);
	}
});

test('local default and substring selection retain their behavior', () => {
	const all = list(['--list']);
	assert.equal(all.status, 0, all.stderr);
	assert.equal(all.stdout.trim().split('\n').length, 12);
	const substring = list(['--list', '--only=copy@desktop-fast']);
	assert.equal(substring.status, 0, substring.stderr);
	assert.deepEqual(substring.stdout.trim().split('\n'), ['copy@desktop-fast', 'prefix-copy@desktop-fast']);
});

// Run the real cleanup callback against a fake session API, not a second copy
// of its filter. A cold devloop can seed the CLI inside the case's 2s lookback.
for (const withCli of [false, true]) {
	test(`cleanup preserves every seed and deletes only case chats (CLI seed: ${withCli})`, async (t) => {
		const startedAt = Date.parse('2026-11-04T22:00:00Z');
		const recent = new Date(startedAt - 500).toISOString();
		const older = new Date(startedAt - 3000).toISOString();
		const status = { urls: { ui: 'http://devloop.invalid/' }, seed: {
			chats: { long: 'native-seed' },
			...(withCli ? { claude_cli: { chat: 'cli-seed', agent_id: 'dev-claude-code' } } : {})
		} };
		const rows = new Map([
			['native-seed', recent], ...(withCli ? [['cli-seed', recent]] : []),
			['previous-case', older], ['case-chat', recent], ['lookback-chat', new Date(startedAt - 2000).toISOString()]
		].map(([id, created_at]) => [id, { id, created_at }]));
		const deleted = [];
		t.mock.method(globalThis, 'fetch', async (url, options) => {
			if (url === '/api/v1/sessions?limit=100') return { ok: true, json: async () => [...rows.values()] };
			assert.equal(options.method, 'DELETE');
			const id = decodeURIComponent(url.slice('/api/v1/sessions/'.length));
			deleted.push(id);
			assert.equal(rows.delete(id), true, `unexpected deletion: ${id}`);
			return { ok: true };
		});
		let closed = false;
		const context = { newPage: async () => ({
			goto: async (url) => assert.equal(url, 'http://devloop.invalid/api/v1/auth/status'),
			evaluate: async (callback, args) => callback(args),
			close: async () => { closed = true; }
		}) };
		const count = await deleteCaseChats(context, status, startedAt);
		assert.deepEqual(deleted, ['case-chat', 'lookback-chat']);
		assert.equal(count, 2);
		assert.deepEqual([...rows.keys()], ['native-seed', ...(withCli ? ['cli-seed'] : []), 'previous-case']);
		assert.equal(closed, true);
	});
}
