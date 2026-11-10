import { test } from 'node:test';
import assert from 'node:assert/strict';
import { oldRowsUnderNewTitle, previousChatFrames, shortFrames } from './switch-frames.mjs';

const left = { session: 's:aaa', rows: ['a1', 'a2', 'a3'] };

test('a clean switch has no frame of the chat left', () => {
	const frames = [
		{ t: 0, session: 's:aaa', messages: 3, active: 'A', rows: ['a1', 'a2', 'a3'] },
		{ t: 120, session: 's:bbb', messages: 2, active: 'B', rows: ['b1', 'b2'] }
	];
	assert.deepEqual(previousChatFrames(frames, left), { selectedAt: 120, bad: [] });
});

test('the left rows coming back after the new chat was selected are caught (#1352)', () => {
	const frames = [
		{ t: 0, session: 's:aaa', messages: 3, active: 'A', rows: ['a1', 'a2', 'a3'] },
		{ t: 100, session: 's:bbb', messages: 0, active: 'B', rows: [] },
		{ t: 116, session: 's:bbb', messages: 3, active: 'B', rows: ['a2', 'a3'] },
		{ t: 133, session: 's:aaa', messages: 3, active: 'B', rows: ['a1', 'a2', 'a3'] },
		{ t: 150, session: 's:bbb', messages: 2, active: 'B', rows: ['b1', 'b2'] }
	];
	const { selectedAt, bad } = previousChatFrames(frames, left);
	assert.equal(selectedAt, 100);
	assert.deepEqual(bad.map((f) => [f.t, f.leftRows]), [[116, 2], [133, 3]]);
});

test('a loading frame tagged none is not the new chat selected', () => {
	const frames = [
		{ t: 0, session: 's:aaa', messages: 3, active: 'A', rows: ['a1'] },
		{ t: 50, session: 'none', messages: 0, active: 'B', rows: [] }
	];
	assert.deepEqual(previousChatFrames(frames, left), { selectedAt: null, bad: [] });
});

test('a chat painted a message short is caught (#1142)', () => {
	const frames = [
		{ t: 0, session: 's:aaa', messages: 10, rows: [] },
		{ t: 170, session: 's:aaa', messages: 9, rows: [] },
		{ t: 186, session: 's:aaa', messages: 10, rows: [] },
		{ t: 200, session: 's:bbb', messages: 2, rows: [] }
	];
	assert.deepEqual(shortFrames(frames, 's:aaa', 10).map((f) => f.t), [170]);
});

test('old rows under the new chat highlighted in the sidebar are listed', () => {
	const frames = [
		{ t: 0, session: 's:aaa', messages: 3, active: 'A', rows: ['a1'] },
		{ t: 80, session: 's:aaa', messages: 3, active: 'B', rows: ['a1'] },
		{ t: 160, session: 's:bbb', messages: 2, active: 'B', rows: ['b1'] }
	];
	assert.deepEqual(oldRowsUnderNewTitle(frames, left, 'B').map((f) => f.t), [80]);
});
