import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { holdFocusTimer } from './focus-timer.mjs';

/** A fake window with a virtual clock; the script runs serialized, as addInitScript does. */
function fixture() {
	let now = 0;
	let id = 0;
	const timers = [];
	const listeners = {};
	const win = {
		event: undefined,
		performance: { now: () => now },
		setTimeout(fn, delay, ...args) { timers.push({ id: ++id, at: now + delay, fn, args, delay }); return id; },
		addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }
	};
	runInNewContext(`(${holdFocusTimer})(win)`, { win });
	const advance = (ms) => {
		const end = now + ms;
		for (;;) {
			timers.sort((a, b) => a.at - b.at);
			const next = timers[0];
			if (!next || next.at > end) break;
			timers.shift();
			now = next.at;
			next.fn(...next.args);
		}
		now = end;
	};
	const dispatch = (type, event = {}) => {
		win.event = { type, ...event };
		for (const fn of listeners[type] || []) fn(win.event);
		win.event = undefined;
	};
	/** Arm a timer the way ProseMirror's focus handler does: inside the focus event. */
	const focusTimer = (delay = 20) => {
		const fired = [];
		win.event = { type: 'focus' };
		win.setTimeout(() => fired.push(now), delay);
		win.event = undefined;
		return fired;
	};
	return { win, timers, advance, dispatch, focusTimer, key: (key) => { dispatch('keydown', { key }); dispatch('keyup', { key }); } };
}

describe('holdFocusTimer', () => {
	it('runs the focus timer at its time when no key input arrives', () => {
		const f = fixture();
		const fired = f.focusTimer();
		f.advance(19);
		assert.deepEqual(fired, []);
		f.advance(10);
		assert.equal(fired.length, 1);
		assert.ok(fired[0] >= 20 && fired[0] <= 25, `ran at ${fired[0]}`);
	});

	it('holds it while keys stream in and runs it inside the first due ArrowLeft keyup', () => {
		const f = fixture();
		const fired = f.focusTimer();
		for (let t = 0; t < 30; t++) { f.key('A'); f.advance(3); }
		assert.deepEqual(fired, [], 'typing alone never lets the held timer run');
		f.dispatch('keydown', { key: 'ArrowLeft' });
		assert.deepEqual(fired, []);
		f.dispatch('keyup', { key: 'ArrowLeft' });
		assert.equal(fired.length, 1, 'runs between the caret move and its selectionchange');
		f.key('ArrowLeft');
		f.advance(100);
		assert.equal(fired.length, 1, 'once only');
	});

	it('runs it once key input has been quiet for 40ms', () => {
		const f = fixture();
		const fired = f.focusTimer();
		f.key('A');
		f.advance(39);
		assert.deepEqual(fired, []);
		f.advance(10);
		assert.equal(fired.length, 1);
	});

	it('leaves other timers alone', () => {
		const f = fixture();
		const fired = [];
		f.win.setTimeout(() => fired.push('plain'), 20);
		f.focusTimer(50);
		f.dispatch('keydown', { key: 'A' });
		f.advance(20);
		assert.deepEqual(fired, ['plain']);
		assert.equal(f.timers.filter((t) => t.delay === 50).length, 1, 'a focus-time timer of another delay is not held');
	});
});
