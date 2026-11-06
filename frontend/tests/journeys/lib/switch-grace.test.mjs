import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { readFileSync } from 'node:fs';
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
	it('does not lower the product grace below its 350 ms policy floor', () => {
		const source = readFileSync(new URL('../../../src/lib/components/chat/TranscriptHydrationBoundary.svelte', import.meta.url), 'utf8');
		const grace = Number(source.match(/export const TRANSCRIPT_LOADING_GRACE_MS = (\d+);/)?.[1]);
		assert.ok(grace >= 350, `production switch grace must be at least 350 ms, got ${grace}`);
	});
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
	});

	it('captures the retained DOM at registration even if loading attributes commit after grace', () => {
		const f = fixture();
		const frames = [];
		let loading = 'false';
		f.window.__switchGraceClock.onRegistered = entry => {
			assert.ok(f.timers.has(entry.id), 'the real timer must already be scheduled');
			frames.push({ loading, loadingStarted: entry.at, t: entry.at, graceRegistered: true, loader: false });
		};
		f.at(10);
		const id = f.window.setTimeout(() => {}, 350);
		// The loading commit can be delayed by synchronous app style/layout.
		f.at(600);
		loading = 'true';
		frames.push({ loading, loadingStarted: 10, t: 600, loader: true });
		assert.deepEqual(framesDuringSwitchGrace(frames, 350), [frames[0]]);
		assert.equal(framesDuringSwitchGrace(frames, 350).every(frame => !frame.loader), true);
		assert.equal(f.timers.get(id).delay, 350, 'sampling must not extend grace');
		// Registration is evidence of loading, not permission for early skeletons.
		assert.equal(framesDuringSwitchGrace([{ ...frames[0], loader: true }], 350).every(frame => !frame.loader), false);
	});

	it('does not restart grace at a late first rAF or use its stale frame timestamp', () => {
		const early = { loading: 'true', loadingStarted: 10, t: 350, rafTime: 100 };
		const beforeCallback = { ...early, t: 600, rafTime: 580 };
		const afterCallback = { ...beforeCallback, t: 650, rafTime: 600, loader: true };
		const frames = framesDuringSwitchGrace([early, beforeCallback, afterCallback], 350);
		assert.deepEqual(frames, [early]);
		// The old first-frame + 100 ms window wrongly included this skeleton.
		assert.ok(afterCallback.rafTime - beforeCallback.rafTime < 100);
	});

	it('still judges an early skeleton and checks the entire pending grace, not only 100 ms', () => {
		const early = { loading: 'true', loadingStarted: 10, t: 300, loader: true };
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
