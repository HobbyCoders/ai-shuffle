// Exercise the real runner's selection and cleanup without a browser or app.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { deleteCaseChats, readDevloopStatus } from './runner.mjs';
import { trackCaseChats } from './lib/case-chats.mjs';
import { EventEmitter } from 'node:events';

test('paused transitions are usable without hiding a dead or broken instance', () => {
	const directory = mkdtempSync(join(tmpdir(), 'journey-idle-'));
	const path = join(directory, 'status.json');
	const previous = process.env.DEVLOOP_STATUS;
	process.env.DEVLOOP_STATUS = path;
	try {
		for (const state of ['ready', 'pausing', 'paused', 'waking']) {
			writeFileSync(path, JSON.stringify({ state, pid: process.pid, access_link: 'http://127.0.0.1:9/' }));
			assert.equal(readDevloopStatus().state, state);
		}
		for (const state of ['starting', 'app-exited', 'stopping']) {
			writeFileSync(path, JSON.stringify({ state, pid: process.pid, access_link: 'http://127.0.0.1:9/' }));
			assert.throws(readDevloopStatus, /not ready/);
		}
	} finally {
		if (previous === undefined) delete process.env.DEVLOOP_STATUS;
		else process.env.DEVLOOP_STATUS = previous;
		rmSync(directory, { recursive: true });
	}
});

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
// of its filter. Other cases/manual sends can create chats in the same window.
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
			['previous-case', older], ['case-chat', recent], ['lookback-chat', new Date(startedAt - 2000).toISOString()],
			['concurrent-case', recent], ['manual-chat', new Date(startedAt + 100).toISOString()]
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
		const count = await deleteCaseChats(context, status, startedAt, ['case-chat', 'lookback-chat', 'case-chat', ...Object.values(status.seed.chats), status.seed.claude_cli?.chat].filter(Boolean));
		assert.deepEqual(deleted, ['case-chat', 'lookback-chat']);
		assert.equal(count, 2);
		assert.deepEqual([...rows.keys()], ['native-seed', ...(withCli ? ['cli-seed'] : []), 'previous-case', 'concurrent-case', 'manual-chat']);
		assert.equal(closed, true);
	});
}

test("chat ownership follows only this context's creates, not shared-socket broadcasts", async () => {
	const page = new EventEmitter();
	const context = new EventEmitter();
	context.pages = () => [page];
	const owned = trackCaseChats(context);
	const socket = new EventEmitter();
	page.emit('websocket', socket);
	const frame = (direction, value) => socket.emit(direction, { payload: JSON.stringify(value) });
	frame('framesent', { type: 'query', request_id: 'ours' });
	frame('framesent', { type: 'query', request_id: 'existing', session_id: 'opened-seed' });
	frame('framereceived', { type: 'session_created', session_id: 'foreign', request_id: 'another-tab' });
	frame('framereceived', { type: 'user_session_created', session_id: 'manual-chat' });
	frame('framereceived', { type: 'start', session_id: 'opened-seed', request_id: 'existing' });
	frame('framereceived', { type: 'session_created', session_id: 'ws-owned', request_id: 'ours' });
	frame('framereceived', { type: 'start', session_id: 'ws-owned', request_id: 'ours' });
	socket.emit('framereceived', { payload: 'not JSON' });
	const response = (path, method, body, ok = true) => context.emit('response', {
		url: () => `http://devloop.invalid/api/v1${path}`,
		request: () => ({ method: () => method }), ok: () => ok,
		json: async () => body
	});
	response('/sessions', 'POST', { session_id: 'http-owned' });
	response('/sessions/with-worktree', 'POST', { session_id: 'worktree-owned' });
	response('/sessions', 'GET', [{ id: 'unrelated' }]);
	response('/sessions/seed/clear', 'POST', { session_id: 'opened-seed' });
	response('/sessions', 'POST', { session_id: 'refused' }, false);
	assert.deepEqual((await owned()).sort(), ['http-owned', 'worktree-owned', 'ws-owned']);

	const nextPage = new EventEmitter();
	context.emit('page', nextPage);
	const nextSocket = new EventEmitter();
	nextPage.emit('websocket', nextSocket);
	nextSocket.emit('framesent', { payload: JSON.stringify({ type: 'query', request_id: 'popup' }) });
	nextSocket.emit('framereceived', { payload: JSON.stringify({ type: 'start', request_id: 'popup', session_id: 'popup-owned' }) });
	assert.ok((await owned()).includes('popup-owned'));
});

test('a case that created no chats cannot sweep the shared instance', async () => {
	assert.equal(await deleteCaseChats({ newPage: () => assert.fail('must not open cleanup page') }, {}, Date.now(), []), 0);
});
