// node --test scripts/perf/stats.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { nearestRank, summarize } from './stats.mjs';
import { normalizePath, sidebarFull } from './journeys.mjs';
import { initScript, collect, windowCounters, PLATFORMS } from './browser-helpers.mjs';
import { parseArgs, summarizeCells } from './chat-journeys.mjs';
import { main as startupMain } from './startup-repeat.mjs';


test('startup rejects unsafe configuration before spawning anything', async () => {
	await assert.rejects(startupMain(['--runs', '0']), /Invalid startup flags/);
	await assert.rejects(startupMain(['--out', '/tmp/x.json', '--session', 'https://secret']), /session/);
});

test('chat and original rig share the serializable instrumentation', () => {
	assert.equal(typeof initScript, 'function');
	assert.equal(typeof collect, 'function');
	assert.equal(PLATFORMS.mobile.route, '/mobile/');
	assert.deepEqual(PLATFORMS.mobile.viewport, { width: 390, height: 844 });
	assert.equal(PLATFORMS.mobile.cpu, 4);
	assert.equal(PLATFORMS.desktop.cpu, 1);
});

test('window counters subtract ongoing SSE bytes and count exact zero replays', () => {
	const result = windowCounters({ feeds: [
		{ start: 0, url: '/events?from_seq=0', bytes: 150 },
		{ start: 20, url: '/events?from_seq=0&live=1', bytes: 200 },
		{ start: 25, url: '/events?live=1&from_seq=01', bytes: 10 },
		{ start: 30, url: '/events?from_seq=42', bytes: 5 }
	], longtasks: [[0, 25], [30, 70], [150, 20]], dom: 50000 }, [100], 20, 80, 7);
	assert.deepEqual(result, { sseBytes: 265, fromSeqZeroReplays: 1, apiRequests: 7,
		longTaskMs: 55, liveDomNodes: 50000 });
});

test('CLI cannot accept access URLs/files and rejects invalid measurement counts', () => {
	assert.throws(() => parseArgs(['--access', 'secret']), /Unknown flag/);
	assert.throws(() => parseArgs(['--accessFile', 'file']), /Unknown flag/);
	assert.throws(() => parseArgs(['--runs', '0']), /positive integer/);
	assert.throws(() => parseArgs(['--platform', 'phone-size']), /platform must/);
	const args = parseArgs(['--journeys', 'remote', '--remote', 'remote-1,remote-2',
		'--small', 'local-1', '--runs', '1', '--out', '/tmp/result.json', '--field']);
	assert.equal(args.field, true);
	assert.equal(args.remote, 'remote-1,remote-2');
});

test('failed runs are reported, not coerced into zero-latency successes', () => {
	const summary = summarizeCells({ cold: [{ usableMs: 8, sseBytes: 128 }, { failed: true }] });
	assert.equal(summary.cold.failed, 1);
	assert.equal(summary.cold.usableMs.n, 1);
	assert.equal(summary.cold.usableMs.p75, 8);
	assert.equal(summary.cold.apiRequests.p75, null);
});


test('nearest-rank p75 picks an observed sample, rank = ceil(0.75 n)', () => {
	// n=20 -> rank 15
	const xs = Array.from({ length: 20 }, (_, i) => 20 - i); // 20..1, unsorted input
	assert.equal(nearestRank(xs, 75), 15);
	assert.equal(nearestRank([5], 75), 5);
	assert.equal(nearestRank([1, 2, 3, 4], 75), 3);
	assert.equal(nearestRank([1, 2, 3, 4, 5], 75), 4);
	assert.equal(nearestRank([], 75), null);
});

test('summarize ignores non-finite samples', () => {
	assert.deepEqual(summarize([3, null, 1, Number.NaN, 2]), { n: 3, p50: 2, p75: 3, min: 1, max: 3 });
});

test('sidebarFull is the first frame the card count reached its final value', () => {
	// deep-linked chat card first, then the fetched list
	assert.equal(sidebarFull([[100, 0], [370, 1], [860, 12]]), 860);
	assert.equal(sidebarFull([[100, 0], [400, 12], [500, 11], [600, 12]]), 400);
	assert.equal(sidebarFull([[100, 0]]), null);
	assert.equal(sidebarFull([]), null);
});

test('normalizePath groups ids, project git paths and hashed assets', () => {
	assert.equal(
		normalizePath('http://h:1/api/v1/sessions/0b0e3a52-1111-4222-8333-944445555666/live?since_update_id=3&include_messages=1'),
		'/api/v1/sessions/:id/live?since_update_id&include_messages'
	);
	assert.equal(normalizePath('http://h:1/api/v1/projects/ai-shuffle/git/status?session_id=x'), '/api/v1/projects/:p/git/status?session_id');
	assert.equal(normalizePath('http://h:1/_app/immutable/chunks/Abc123.js'), 'static:js');
	assert.equal(normalizePath('http://h:1/api/v1/x?token=secret&a=1'), '/api/v1/x?a');
});
