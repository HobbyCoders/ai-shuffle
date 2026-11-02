// Exercise the real runner's planning path, before it needs a browser or app.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

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
