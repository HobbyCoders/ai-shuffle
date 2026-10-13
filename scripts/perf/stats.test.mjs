// node --test scripts/perf/stats.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { nearestRank, summarize } from './stats.mjs';
import { normalizePath, sidebarFull } from './journeys.mjs';

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
