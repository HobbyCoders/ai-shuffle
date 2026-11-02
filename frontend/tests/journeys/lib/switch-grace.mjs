/** Observe the product's real grace timer; only the existing rapid-return
 * gate may defer a callback. Ordinary switches keep their unmodified clock. */
export function installSwitchGraceClock({ graceMs }) {
	const realSetTimeout = window.setTimeout.bind(window);
	const realClearTimeout = window.clearTimeout.bind(window);
	const clock = window.__switchGraceClock = { timers: [] };
	const gate = window.__switchGraceGate = {
		armed: false, registered: [], deferred: new Map(),
		arm() { this.armed = true; this.registered = []; },
		release() {
			this.armed = false;
			const due = [...this.deferred.values()];
			this.deferred.clear();
			for (const run of due) realSetTimeout(run, 0);
			return due.length;
		}
	};
	window.setTimeout = function (handler, delay, ...args) {
		if (typeof handler !== 'function' || Number(delay) !== graceMs) {
			return realSetTimeout(handler, delay, ...args);
		}
		const gated = gate.armed;
		const entry = { at: performance.now(), firedAt: null, cleared: false };
		const run = () => { entry.firedAt = performance.now(); handler(...args); };
		entry.id = realSetTimeout(() => {
			if (gated && gate.armed) gate.deferred.set(entry.id, run);
			else run();
		}, delay);
		clock.timers.push(entry);
		if (gated) gate.registered.push(entry);
		return entry.id;
	};
	window.clearTimeout = function (id) {
		gate.deferred.delete(id);
		for (const entry of clock.timers) if (entry.id === id) entry.cleared = true;
		return realClearTimeout(id);
	};
}

/** Use the actual registration time and sampling time, not a late first rAF
 * or its stale frame timestamp. The production deadline is never extended. */
export function framesDuringSwitchGrace(frames, graceMs) {
	return frames.filter(frame => frame.loading === 'true'
		&& frame.loadingStarted !== null && frame.t - frame.loadingStarted < graceMs);
}
