import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { expectRetainedHolds } from './retained-geometry.mjs';

const VIEWPORT = 800;
const frame = (top, extra = {}) => ({ t: 0, top, height: 40, bottom: 780, viewport: VIEWPORT, ...extra });
const judge = (geometry) => {
	const lines = [];
	expectRetainedHolds(geometry, (line) => lines.push(line));
	return lines.join('\n');
};

describe('the retained reply comes into view once and stays put', () => {
	it('passes a reply that holds its place', () => {
		assert.match(judge(Array.from({ length: 31 }, () => frame(100))), /settled at 100px over 31 painted frames/);
	});

	it('allows a pixel of settling', () => {
		assert.match(judge([frame(100), frame(100.6), frame(101)]), /settled at 101px/);
	});

	it('refuses a slow drift of one pixel per frame', () => {
		const drifting = Array.from({ length: 31 }, (_, i) => frame(100 + i));
		assert.throws(() => judge(drifting), /moved while on screen: 100\.\.130/);
	});

	it('refuses a single jump', () => {
		assert.throws(() => judge([frame(100), frame(100), frame(160)]), /moved while on screen: 100\.\.160/);
	});

	it('ignores scrolling while the reply is still off screen', () => {
		const arriving = [frame(-300), frame(-120), frame(VIEWPORT + 50), frame(236), frame(236)];
		assert.match(judge(arriving), /settled at 236px over 5 painted frames/);
	});

	it('refuses a reply that ends out of view', () => {
		assert.throws(() => judge([frame(100), frame(-300)]), /in view after the rewind/);
	});

	it('refuses a composer that moved', () => {
		assert.throws(() => judge([frame(100), frame(100, { bottom: 760 })]), /bottom edge moved/);
	});

	it('refuses a reply that left the page', () => {
		assert.throws(() => judge([frame(100), { t: 0, gone: true }]), /stayed on the page/);
	});
});
