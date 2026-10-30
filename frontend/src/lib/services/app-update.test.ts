/**
 * Version skew (#707): a page that outlived a worker update notices, and
 * reloads only when that cannot cost the user anything.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	CHECK_MIN_INTERVAL_MS,
	RELOAD_SETTLE_MS,
	RESUME_GRACE_MS,
	canReloadSilently,
	createAppUpdateMonitor,
	isChunkLoadError,
	registerReloadBlocker,
	reloadBlockers,
	reloadDecision,
	reportChunkLoadFailure,
	reportServerReconnected,
	setAppUpdateListener
} from './app-update';

describe('isChunkLoadError', () => {
	it.each([
		['Chromium / Android WebView', new TypeError('Failed to fetch dynamically imported module: https://w.example/mobile/_app/immutable/chunks/OoYQ7O1O.js')],
		['Firefox', new TypeError('error loading dynamically imported module: https://w.example/_app/immutable/chunks/a.js')],
		['Safari / iOS WebView', new TypeError('Importing a module script failed.')],
		['Vite CSS preload', new Error('Unable to preload CSS for /mobile/_app/immutable/assets/BrowserPanel.BpWlSinN.css')],
		['a bundler ChunkLoadError', Object.assign(new Error('Loading chunk 7 failed'), { name: 'ChunkLoadError' })],
		['a vite:preloadError event', { type: 'vite:preloadError', payload: new TypeError('Failed to fetch dynamically imported module: x.js') }]
	])('recognises %s', (_label, error) => {
		expect(isChunkLoadError(error)).toBe(true);
	});

	it.each([
		['a network failure of an API call', new TypeError('Failed to fetch')],
		['a microphone refusal', Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' })],
		['nothing', null],
		['a plain object', { detail: 'Voice is off' }]
	])('does not mistake %s for one', (_label, error) => {
		expect(isChunkLoadError(error)).toBe(false);
	});
});

describe('reloadDecision', () => {
	it.each([
		// hidden, resumed, blockers → decision
		[true, false, [], 'reload'],
		[false, true, [], 'reload'],
		[false, false, [], 'banner'],
		[true, false, ['voice-call'], 'banner'],
		[false, true, ['composer-draft'], 'banner'],
		[true, true, ['voice-call', 'composer-draft'], 'banner']
	] as const)('hidden=%s resumed=%s blockers=%j → %s', (hidden, resumed, blockers, expected) => {
		expect(reloadDecision({ hidden, resumed, blockers })).toBe(expected);
	});
});

describe('reload blockers', () => {
	const cleanups: Array<() => void> = [];
	afterEach(() => cleanups.splice(0).forEach((undo) => undo()));

	it('collects every reason and forgets an unregistered one', () => {
		let typing = true;
		cleanups.push(registerReloadBlocker(() => (typing ? 'composer-draft' : null)));
		const undoCall = registerReloadBlocker(() => 'voice-call');
		expect(reloadBlockers()).toEqual(['composer-draft', 'voice-call']);
		undoCall();
		typing = false;
		expect(reloadBlockers()).toEqual([]);
		expect(canReloadSilently()).toBe(true);
	});

	it('a check that throws blocks rather than risk the reload', () => {
		cleanups.push(registerReloadBlocker(() => { throw new Error('boom'); }));
		expect(canReloadSilently()).toBe(false);
	});
});

describe('the hub', () => {
	afterEach(() => setAppUpdateListener(null));

	it('passes reconnects and chunk failures to the watcher, and only real chunk failures', () => {
		const listener = { reconnected: vi.fn(), chunkFailed: vi.fn() };
		setAppUpdateListener(listener);
		reportServerReconnected();
		expect(listener.reconnected).toHaveBeenCalledTimes(1);
		expect(reportChunkLoadFailure(new TypeError('Importing a module script failed.'))).toBe(true);
		expect(reportChunkLoadFailure(new Error('Request timed out'))).toBe(false);
		expect(listener.chunkFailed).toHaveBeenCalledTimes(1);
	});

	it('never throws without a watcher', () => {
		expect(() => reportServerReconnected()).not.toThrow();
		expect(reportChunkLoadFailure(new TypeError('Importing a module script failed.'))).toBe(true);
	});
});

describe('createAppUpdateMonitor', () => {
	function setup(opts: { newBuild?: boolean; hidden?: boolean; blockers?: string[] } = {}) {
		const state = { newBuild: opts.newBuild ?? true, hidden: opts.hidden ?? false, blockers: opts.blockers ?? [], t: 100_000 };
		const scheduled: Array<{ fn: () => void; ms: number }> = [];
		const deps = {
			schedule: (fn: () => void, ms: number) => { scheduled.push({ fn, ms }); },
			checkForNewBuild: vi.fn(async () => state.newBuild),
			isHidden: () => state.hidden,
			blockers: () => state.blockers,
			reload: vi.fn(),
			showBanner: vi.fn(),
			now: () => state.t
		};
		const monitor = createAppUpdateMonitor(deps);
		// Past the start-up grace: the user is "in the middle of using" the page.
		state.t += RESUME_GRACE_MS + 1;
		return { monitor, deps, state, scheduled };
	}

	it('does nothing while the server runs this build', async () => {
		const { monitor, deps } = setup({ newBuild: false });
		expect(await monitor.check('reconnect')).toBe(false);
		monitor.hidden();
		expect(deps.reload).not.toHaveBeenCalled();
		expect(deps.showBanner).not.toHaveBeenCalled();
	});

	it('a reconnect while the user is using the page shows the banner, then reloads once the page hides', async () => {
		const { monitor, deps, state } = setup();
		expect(await monitor.check('reconnect')).toBe(true);
		expect(deps.reload).not.toHaveBeenCalled();
		expect(deps.showBanner).toHaveBeenCalledWith(false);

		state.hidden = true;
		monitor.hidden();
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	// The incident: the phone app came back to the foreground on an old build.
	it('an app that just resumed onto a new build reloads straight away', async () => {
		const { monitor, deps } = setup();
		await monitor.visible();
		expect(deps.reload).toHaveBeenCalledTimes(1);
		expect(deps.showBanner).not.toHaveBeenCalled();
	});

	it('the reconnect that follows a resume still counts as the resume', async () => {
		const { monitor, deps, state } = setup({ newBuild: false });
		await monitor.visible();
		state.newBuild = true;
		state.t += CHECK_MIN_INTERVAL_MS + 1; // the socket took a few seconds
		await monitor.check('reconnect');
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	it('a hidden page reloads silently', async () => {
		const { monitor, deps } = setup({ hidden: true });
		await monitor.check('reconnect');
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	it('never reloads during a live call, even hidden or just resumed', async () => {
		const { monitor, deps, state } = setup({ blockers: ['voice-call'] });
		await monitor.visible();
		state.hidden = true;
		monitor.hidden();
		expect(deps.reload).not.toHaveBeenCalled();
		expect(deps.showBanner).toHaveBeenCalled();

		// The call ends; the next hide applies the update.
		state.blockers = [];
		monitor.hidden();
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	it('never reloads over an unsent draft', async () => {
		const { monitor, deps, state } = setup({ blockers: ['composer-draft'] });
		state.hidden = true;
		await monitor.check('reconnect');
		monitor.hidden();
		expect(deps.reload).not.toHaveBeenCalled();
	});

	it('the banner Reload always reloads, whatever blocks the automatic one', async () => {
		const { monitor, deps } = setup({ blockers: ['composer-draft'] });
		await monitor.check('reconnect');
		monitor.reloadNow();
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	// NotesCard's beforeunload prompt lets the user keep the page. Before, the
	// monitor then ignored every check, hide and Reload for the page's life.
	it('a reload the user cancelled leaves the monitor working', async () => {
		const { monitor, deps, state, scheduled } = setup({ hidden: true });
		await monitor.check('reconnect');
		expect(deps.reload).toHaveBeenCalledTimes(1);
		// Still here: the page did not unload.
		monitor.hidden();
		expect(deps.reload).toHaveBeenCalledTimes(1);
		// Even mid-reload, the banner's Reload is never ignored.
		monitor.reloadNow();
		expect(deps.reload).toHaveBeenCalledTimes(2);

		expect(scheduled.at(-1)!.ms).toBe(RELOAD_SETTLE_MS);
		scheduled.forEach(({ fn }) => fn());
		state.hidden = false;
		state.t += CHECK_MIN_INTERVAL_MS + 1;
		await monitor.check('online');
		expect(deps.showBanner).toHaveBeenCalledWith(false);
		state.hidden = true;
		monitor.hidden();
		expect(deps.reload).toHaveBeenCalledTimes(3);
	});

	it('a chunk failure that was the network, not an update, does not mark a later update as broken', async () => {
		const { monitor, deps, state } = setup({ newBuild: false });
		await monitor.check('chunk');
		expect(deps.showBanner).not.toHaveBeenCalled();
		state.newBuild = true;
		state.t += CHECK_MIN_INTERVAL_MS + 1;
		await monitor.check('reconnect');
		expect(deps.showBanner).toHaveBeenLastCalledWith(false);
	});

	it('a failed lazy import marks the banner as "a feature broke"', async () => {
		const { monitor, deps } = setup();
		await monitor.check('chunk');
		expect(deps.showBanner).toHaveBeenLastCalledWith(true);
	});

	it('focus and visibility together ask the server once; a chunk failure always asks', async () => {
		const { monitor, deps } = setup({ newBuild: false });
		await Promise.all([monitor.check('online'), monitor.check('reconnect')]);
		expect(deps.checkForNewBuild).toHaveBeenCalledTimes(1);
		await monitor.check('chunk');
		expect(deps.checkForNewBuild).toHaveBeenCalledTimes(2);
	});

	it('a failed version check is not an update', async () => {
		const { monitor, deps } = setup();
		deps.checkForNewBuild.mockRejectedValueOnce(new Error('offline'));
		expect(await monitor.check('online')).toBe(false);
		expect(deps.reload).not.toHaveBeenCalled();
		expect(deps.showBanner).not.toHaveBeenCalled();
	});

	it('does not reload in a loop when a reload came back on the same build', async () => {
		const { deps } = setup();
		const guarded = createAppUpdateMonitor({ ...deps, autoReloadAllowed: () => false });
		await guarded.visible();
		expect(deps.reload).not.toHaveBeenCalled();
		expect(deps.showBanner).toHaveBeenCalledWith(false);
		// The user can still choose to reload.
		guarded.reloadNow();
		expect(deps.reload).toHaveBeenCalledTimes(1);
	});

	it('markStale takes an already-confirmed update at its word', () => {
		const { monitor, deps } = setup({ newBuild: false });
		monitor.markStale();
		expect(monitor.stale).toBe(true);
		expect(deps.showBanner).toHaveBeenCalledWith(false);
		expect(deps.checkForNewBuild).not.toHaveBeenCalled();
	});

	describe('a service worker taking control (#908)', () => {
		/** A page that has only just loaded: visible, inside the start-up grace. */
		function freshPage(newBuild: boolean) {
			const state = { newBuild, t: 100_000 };
			const deps = {
				schedule: () => {},
				checkForNewBuild: vi.fn(async () => state.newBuild),
				isHidden: () => false,
				blockers: () => [] as string[],
				reload: vi.fn(),
				showBanner: vi.fn(),
				now: () => state.t
			};
			const monitor = createAppUpdateMonitor(deps);
			state.t += 500;
			return { monitor, deps, state };
		}

		it("a fresh visit's first install on the same build neither reloads nor shows a banner", async () => {
			const { monitor, deps } = freshPage(false);
			// The claim's controllerchange and the SW_UPDATED message, together.
			const answers = await Promise.all([monitor.serviceWorkerActivated(), monitor.serviceWorkerActivated()]);
			expect(answers).toEqual([false, false]);
			expect(monitor.stale).toBe(false);
			expect(deps.checkForNewBuild).toHaveBeenCalled();
			expect(deps.reload).not.toHaveBeenCalled();
			expect(deps.showBanner).not.toHaveBeenCalled();
		});

		it('a different build still reloads a page that has only just loaded', async () => {
			const { monitor, deps } = freshPage(true);
			expect(await monitor.serviceWorkerActivated()).toBe(true);
			expect(deps.reload).toHaveBeenCalledTimes(1);
		});

		it('a different build gets the banner while the user is using the page', async () => {
			const { monitor, deps } = setup();
			expect(await monitor.serviceWorkerActivated()).toBe(true);
			expect(deps.reload).not.toHaveBeenCalled();
			expect(deps.showBanner).toHaveBeenCalledWith(false);
		});

		it('the check throttle cannot hide an update the worker found moments later', async () => {
			const { monitor, deps, state } = setup({ newBuild: false });
			expect(await monitor.check('resume')).toBe(false);
			// The update lands a second later and the new worker takes over.
			state.newBuild = true;
			state.t += 1_000;
			expect(await monitor.check('online')).toBe(false); // throttled: the old answer
			expect(await monitor.serviceWorkerActivated()).toBe(true);
			expect(deps.showBanner).toHaveBeenCalledWith(false);
		});

		it('asks again after a check that was already in flight when the worker took over', async () => {
			const { monitor, deps, state } = setup({ newBuild: false });
			let answerFirst: (v: boolean) => void = () => {};
			deps.checkForNewBuild.mockImplementationOnce(() => new Promise<boolean>((done) => { answerFirst = done; }));
			const first = monitor.check('resume');
			state.newBuild = true;
			const activated = monitor.serviceWorkerActivated();
			answerFirst(false); // asked before the update landed
			expect(await first).toBe(false);
			expect(await activated).toBe(true);
			expect(deps.checkForNewBuild).toHaveBeenCalledTimes(2);
		});
	});
});
