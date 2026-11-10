import assert from 'node:assert/strict';
import { test } from 'node:test';
import { firstPart, secondPart } from './split.mjs';

const journey = { name: 'j', allow: ['x'], cases: [{ name: 'a' }, { name: 'b' }, { name: 'c' }] };

test('the two halves cover every case exactly once', () => {
	const first = firstPart(journey, ['b']);
	const second = secondPart(journey, ['b']);
	assert.deepEqual(first.cases.map((c) => c.name), ['b']);
	assert.deepEqual(second.cases.map((c) => c.name), ['a', 'c']);
	assert.equal(first.name, 'j');
	assert.equal(second.name, 'j-2');
	assert.deepEqual(second.allow, ['x']);
});

test('a stale or lopsided list fails instead of dropping cases', () => {
	assert.throws(() => firstPart(journey, ['gone']), /no such case: gone/);
	assert.throws(() => secondPart(journey, ['a', 'a']), /listed twice/);
	assert.throws(() => secondPart(journey, ['a', 'b', 'c']), /both halves/);
	assert.throws(() => firstPart(journey, []), /both halves/);
});
