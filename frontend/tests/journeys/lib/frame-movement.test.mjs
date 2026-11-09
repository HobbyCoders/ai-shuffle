import assert from 'node:assert/strict';
import test from 'node:test';
import { movements, transcriptMovements, unmeasuredFrames } from './frame-movement.mjs';

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
	assert.equal(transcriptMovements(frames(rows, { content, composer }), { knownCorrection: 8.5 }), 0);
});

test('that same return is a bounce when the correction is not named, as on a phone', () => {
	const rows = [68, 68, 68, 76, 68, 68, -322, -322];
	const content = [3650, 3650, 3658, 3658, 3658, 3658, 4048, 4048];
	const composer = [744, 744, 752, 752, 752, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
});

test('the named correction is forgiven only at its own size', () => {
	const rows = [68, 68, 68, 88, 68, 68];
	const content = [3650, 3650, 3670, 3670, 3670, 3670];
	const composer = [744, 744, 764, 764, 764, 764];
	assert.equal(transcriptMovements(frames(rows, { content, composer }), { knownCorrection: 8.5 }), 2);
});

test('the named correction is forgiven only once', () => {
	const rows = [68, 68, 68, 76, 68, 68, 68, 68, 68, 76, 68, 68];
	const content = [3650, 3650, 3658, 3658, 3658, 3658, 3658, 3658, 3666, 3666, 3666, 3666];
	const composer = [744, 744, 752, 752, 752, 752, 752, 752, 760, 760, 760, 760];
	assert.equal(transcriptMovements(frames(rows, { content, composer }), { knownCorrection: 8.5 }), 2);
});

test('the named correction must be the composer step, then the bottom-follow', () => {
	const rows = [68, 68, 76, 68, 68];
	const composer = [744, 744, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { composer }), { knownCorrection: 8.5 }), 2);
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

test('a bounce on a phone right after the composer gave height back counts twice', () => {
	const composer = [592, 678, 678, 678, 678];
	assert.equal(transcriptMovements(frames([68, 68, 8, 68, 68], { composer })), 2);
	assert.equal(transcriptMovements(frames([68, 68, 8, 68, 68], { composer }), { knownCorrection: 8.5 }), 2);
});

test('a row that follows the composer and snaps straight back counts twice', () => {
	const composer = [592, 678, 678, 678];
	assert.equal(transcriptMovements(frames([68, 154, 68, 68], { composer })), 2);
});

test('two separate jumps count separately while content keeps growing', () => {
	const rows = [500, 500, 460, 460, 420, 420];
	const content = [2000, 2024, 2048, 2072, 2096, 2120];
	assert.equal(transcriptMovements(frames(rows, { content })), 2);
});

test('a reader kept at the bottom of a growing reply is not moving', () => {
	const rows = [500, 476, 452, 428, 404];
	const content = [2000, 2024, 2048, 2072, 2096];
	assert.equal(transcriptMovements(frames(rows, { content })), 0);
});

test('a spent explanation cannot explain a second jump', () => {
	const rows = [68, 68, 154, 154, 240, 240];
	const composer = [592, 592, 678, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { composer })), 1);
});

test('a multi-frame return counts twice, however each step is explained', () => {
	const rows = [68, 154, 124, 68];
	const content = [2000, 2000, 2030, 2086];
	const composer = [592, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
	const padded = (list) => [...list, ...Array(21).fill(list[list.length - 1])];
	assert.equal(transcriptMovements(frames(padded(rows), { content: padded(content), composer: padded(composer) })), 2);
});

test('a row that returns after a long stay away still counts twice', () => {
	const rows = [68, 154, ...Array(40).fill(154), 68, 68];
	assert.equal(transcriptMovements(frames(rows)), 2);
});

test('a row that passes its old position on the way up has not returned', () => {
	const rows = [-38, 48, -534, -559];
	const content = [2116, 2116, 2698, 2723];
	const composer = [592, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('a return is read against where the row rested, including its drift', () => {
	const rows = [68, 70, 154, 124, 72, 72];
	const content = [2000, 2000, 2000, 2030, 2082, 2082];
	const composer = [592, 592, 676, 676, 676, 676];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
});

test('a return that stops a few pixels short still counts', () => {
	const content = [2000, 2000, 2030, 2083];
	const composer = [592, 678, 678, 678];
	assert.equal(transcriptMovements(frames([68, 154, 124, 71], { content, composer })), 2);
});

test('a slow drift back to the old place is a return', () => {
	const back = Array.from({ length: 86 }, (_, i) => 153 - i);
	assert.equal(transcriptMovements(frames([68, 154, ...back, 68])), 2);
});

test('repeated bounces each count', () => {
	assert.ok(transcriptMovements(frames([68, 154, 68, 154, 68])) >= 4);
});

test('the real phone path passes its old position and has not returned', () => {
	const rows = [-38, 47, -534, -558];
	const content = [2116, 2116, 2697, 2721];
	const composer = [592, 677, 677, 677];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('a scroll easing past the old place is not a return', () => {
	const rows = [100, 186, 150, 110, 60, 0, -60, -60];
	const content = [2000, 2000, 2036, 2076, 2126, 2186, 2246, 2246];
	const composer = [592, 678, 678, 678, 678, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('a forgiven correction uses up what explained it', () => {
	const rows = [68, 76, 68, 60, 60, 52];
	const content = [2000, 2000, 2008, 2008, 2008, 2008];
	const composer = [744, 752, 752, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { content, composer }), { knownCorrection: 8.5 }), 2);
});

test('a bounce around a one-frame turning point counts', () => {
	const rows = [68, 98, 38, 58, 38, 38];
	const content = [2000, 2000, 2060, 2060, 2080, 2080];
	const composer = [592, 622, 622, 622, 622, 622];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
});

test('a one-frame turn is a place the row can return to, at the start, middle or end', () => {
	assert.equal(transcriptMovements(frames([68, 98, 68, 68, 68, 68])), 2);
	assert.equal(transcriptMovements(frames([68, 68, 98, 68, 68, 68])), 2);
	assert.equal(transcriptMovements(frames([68, 68, 68, 68, 98, 68])), 2);
});

test('a frame landing near the old place while the row carries on past it is not a return', () => {
	const rows = [100, 186, 150, 101, 60, 0, -60, -60];
	const content = [2000, 2000, 2036, 2085, 2126, 2186, 2246, 2246];
	const composer = [592, 678, 678, 678, 678, 678, 678, 678];
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 0);
});

test('a follow that stops short by the measured room below it is the same movement', () => {
	const rows = [-246, -120, -361, -361, -781, -829];
	const content = [867, 867, 1108, 1536, 1536, 1584];
	const composer = [188, 322, 322, 322, 322, 322];
	const gap = [8, 0, 0, 428, 8, 8];
	const measured = rows.map((row, i) => ({ row, content: content[i], composer: composer[i], gap: gap[i] }));
	assert.equal(transcriptMovements(measured), 0);
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
});

test('the content that arrives after the row is back still explains the next step', () => {
	const rows = [68, 68, 68, 76, 68, 68, 68, 68, -323, -323];
	const content = [7941, 7941, 7941, 7949, 7949, 7949, 7949, 8340, 8340, 8340];
	const composer = [744, 744, 744, 752, 752, 752, 752, 752, 752, 752];
	assert.equal(transcriptMovements(frames(rows, { content, composer }), { knownCorrection: 8.5 }), 0);
	assert.equal(transcriptMovements(frames(rows, { content, composer })), 2);
});

test('content that shrinks does not explain a row moving down', () => {
	assert.equal(transcriptMovements(frames([500, 500, 540, 540], { content: [2040, 2040, 2000, 2000] })), 1);
});

test('frames without the row are skipped, not read as a jump', () => {
	assert.equal(transcriptMovements([{ row: 10, content: 2000, composer: 700 }, { row: null, content: 2000, composer: 700 }, { row: 10, content: 2000, composer: 700 }]), 0);
});

test('a frame with no reading, or one that never reported, is unmeasured', () => {
	const at = (seq, row = 10, composer = 700) => ({ seq, row, composer });
	assert.equal(unmeasuredFrames([at(0), at(1), at(2)]), 0);
	assert.equal(unmeasuredFrames([at(0), at(1, null), at(2)]), 1);
	assert.equal(unmeasuredFrames([at(0), at(1, 10, null), at(2)]), 1);
	assert.equal(unmeasuredFrames([at(0), at(3)]), 2);
});
