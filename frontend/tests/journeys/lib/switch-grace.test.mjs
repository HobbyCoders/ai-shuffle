import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { framesDuringSwitchGrace, installSwitchGraceClock } from './switch-grace.mjs';

function fixture() {
	let now = 0, id = 0;
	const timers = new Map();
	const window = {
		setTimeout(fn, delay, ...args) { timers.set(++id, { fn, delay, args }); return id; },
		clearTimeout(id) { timers.delete(id); }
	};
	runInNewContext(`(${installSwitchGraceClock})({ graceMs: 350 })`, {
		window, performance: { now: () => now }
	});
	return { window, timers, at(value) { now = value; }, fire(id) {
		const timer = timers.get(id);
		timers.delete(id);
		timer.fn(...timer.args);
	} };
}

describe('the switch sampler follows the production grace clock (#941)', () => {
	it('observes ordinary timers without changing their delay or gating their callback', () => {
		const f = fixture();
		let received;
		f.at(10);
		const id = f.window.setTimeout(value => { received = value; }, 350, 'real callback');
		assert.equal(f.timers.get(id).delay, 350);
		assert.equal(f.window.__switchGraceClock.timers[0].at, 10);
		assert.equal(f.window.__switchGraceGate.registered.length, 0);
		f.at(600); // Layout blocked the main thread beyond the deadline.
		f.fire(id);
		assert.equal(received, 'real callback');
		assert.equal(f.window.__switchGraceClock.timers[0].firedAt, 600);
	});

	it('does not restart grace at a late first rAF or use its stale frame timestamp', () => {
		const early = { loading: 'true', loadingStarted: 10, graceFiredAt: null, t: 350, rafTime: 100 };
		const beforeCallback = { ...early, t: 600, rafTime: 580 };
		const afterCallback = { ...beforeCallback, graceFiredAt: 610, t: 650, rafTime: 600, loader: true };
		const frames = framesDuringSwitchGrace([early, beforeCallback, afterCallback], 350);
		assert.deepEqual(frames, [early]);
		// The old first-frame + 100 ms window wrongly included this skeleton.
		assert.ok(afterCallback.rafTime - beforeCallback.rafTime < 100);
	});

	it('still judges an early skeleton and checks the entire pending grace, not only 100 ms', () => {
		const early = { loading: 'true', loadingStarted: 10, graceFiredAt: null, t: 300, loader: true };
		assert.deepEqual(framesDuringSwitchGrace([early], 350), [early]);
		assert.equal(framesDuringSwitchGrace([early], 350).every(frame => !frame.loader), false);
	});

	it('preserves rapid-return deferral and cancellation, and leaves unrelated timers alone', () => {
		const f = fixture();
		const gate = f.window.__switchGraceGate;
		gate.arm();
		let calls = 0;
		const id = f.window.setTimeout(() => calls++, 350);
		f.at(400); f.fire(id);
		assert.equal(calls, 0);
		assert.equal(gate.deferred.size, 1);
		f.window.clearTimeout(id);
		assert.equal(gate.registered[0].cleared, true);
		assert.equal(gate.release(), 0);
		const other = f.window.setTimeout(() => calls++, 250);
		assert.equal(f.timers.get(other).delay, 250);
		f.fire(other);
		assert.equal(calls, 1);
	});
});
