// node --test tests/visual-diff.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import { changedAreas, MIN_AREA, verdict } from './visual-diff.mjs';

const W = 1440, H = 900;
const mask = (paint) => {
	const m = new Uint8Array(W * H);
	paint((x, y) => { m[y * W + x] = 1; });
	return m;
};

test('ten 1px rail ticks moving fail although they are far under the whole-image tolerance', () => {
	// The #1644 case at desktop: ten ticks, 10px wide, each leaves its old row and lands on a new one.
	const m = mask((set) => {
		for (let t = 0; t < 10; t++) {
			for (let x = 8; x < 18; x++) {
				set(x, 135 + t * 25);
				set(x, 300 + t * 25);
			}
		}
	});
	const areas = changedAreas(m, W, H);
	assert.deepEqual(areas, { changed: 200, largest: 10 });
	const ratio = areas.changed / (W * H);
	assert.ok(ratio < 0.002);
	assert.equal(verdict({ ratio, largest: areas.largest }), 'CHANGED 0.015%, one area of 10 px');
});

test('scattered single pixels stay ok, and are counted for the drift report', () => {
	const m = mask((set) => {
		for (let i = 0; i < 40; i++) set(10 + i * 30, 20 + i * 15);
		set(W - 1, H - 1);
	});
	const areas = changedAreas(m, W, H);
	assert.deepEqual(areas, { changed: 41, largest: 1 });
	assert.equal(verdict({ ratio: areas.changed / (W * H), largest: areas.largest }), 'ok');
});

test('areas join diagonally and never across the image edge', () => {
	// A diagonal of MIN_AREA pixels is one area.
	const diagonal = changedAreas(mask((set) => { for (let i = 0; i < MIN_AREA; i++) set(100 + i, 100 + i); }), W, H);
	assert.equal(diagonal.largest, MIN_AREA);
	// The last pixel of one row and the first of the next are not neighbours.
	const wrap = changedAreas(mask((set) => { set(W - 1, 5); set(0, 6); }), W, H);
	assert.deepEqual(wrap, { changed: 2, largest: 1 });
});

test('the whole-image share still fails on its own, and a size change counts as all changed', () => {
	assert.equal(verdict({ ratio: 0.0021, largest: 1 }), 'CHANGED 0.21%');
	assert.equal(verdict({ ratio: 1, largest: 0 }), 'CHANGED 100.00%');
	assert.equal(verdict({ ratio: 0.0001, largest: MIN_AREA - 1 }), 'ok');
	assert.equal(verdict({ ratio: 0.0001, largest: 20 }, { minArea: 40 }), 'ok');
});
