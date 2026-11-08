import assert from 'node:assert/strict';
import test from 'node:test';
import { movements, transcriptMovements } from './frame-movement.mjs';

const frames = (rows, { content = rows.map(() => 2000), composer = rows.map(() => 700) } = {}) =>
	rows.map((row, i) => ({ row, content: content[i], composer: composer[i] }));

test('one unbroken run is one movement, even when eased over several frames', () => {
	assert.equal(movements([500, 500, 510, 530, 540, 540]), 1);
});

test('a still frame between two steps makes two movements', () => {
	assert.equal(movements([500, 500, 520, 520, 540, 540]), 2);
});

test('a couple of pixels of noise is not a movement', () => {
	assert.equal(movements([500, 500.4, 502, 501]), 0);
});

test('a position that reverses inside consecutive frames is two movements', () => {
	assert.equal(movements([500, 460, 500]), 2);
});

test('the composer handing back height, then new content, is explained', () => {
	const rows = [-38, -38, 48, 48, -534, -534, -559];
	const content = [2116, 2116, 2116, 2116, 2698, 2698, 2723];
	const composer = [592, 592, 678, 678, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('the row landing a frame after the content or composer is still explained', () => {
	const rows = [68, 68, 68, 76, 68, -322, -322];
	const content = [3650, 3650, 3658, 3658, 3658, 4048, 4048];
	const composer = [744, 744, 752, 752, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('the row catching up with the composer after an idle frame is explained', () => {
	const rows = [68, 68, 68, 76, -296, -296];
	const content = [3650, 3650, 3658, 3658, 4022, 4022];
	const composer = [744, 752, 752, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('a bounce long after the composer settled still counts', () => {
	const rows = [68, 68, 68, 68, 68, 68, 8, 68, 68];
	const composer = [592, 678, 678, 678, 678, 678, 678, 678, 678];
	assert.ok(transcriptMovements(frames(rows, { composer })) >= 2);
});

test('the scroll that brings a new turn into view is one movement', () => {
	const rows = [74, 74, -304, -304, -352, -352];
	const content = [1076, 1076, 1076, 1076, 1124, 1124];
	assert.equal(transcriptMovements(frames(rows, { content })), 1);
});

test('two unexplained steps with a still frame between are two movements', () => {
	assert.equal(transcriptMovements(frames([500, 500, 520, 520, 540, 540])), 2);
});

test('a bounce with no new content counts, in one frame or spread out', () => {
	assert.ok(transcriptMovements(frames([68, 108, 68, 68])) >= 2);
	assert.ok(transcriptMovements(frames([68, 108, 108, 68, 68])) >= 2);
});

test('a bounce bigger than the composer step still counts', () => {
	const composer = [744, 752, 752, 752];
	assert.ok(transcriptMovements(frames([68, 108, 68, 68], { composer })) >= 2);
});

test('frames without the row are skipped, not read as a jump', () => {
	assert.equal(transcriptMovements([{ row: 10, content: 2000, composer: 700 }, { row: null, content: 2000, composer: 700 }, { row: 10, content: 2000, composer: 700 }]), 0);
});
