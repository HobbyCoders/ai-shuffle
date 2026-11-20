import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post },
	getErrorMessage: (e: Error, fallback: string) => e?.message ?? fallback }));

import { liveBanners, notify } from '$lib/stores/notifications';
import { SETTLE_MS, installCliUpdateNotice, recheckCliUpdates } from './cli-update-notice';

const claude = { provider: 'claude', name: 'Claude Code', installed_version: '2.1.323', latest_version: '2.1.330' };
const agy = { provider: 'antigravity', name: 'Antigravity', installed_version: '1.5.7', latest_version: '1.5.13' };
let updates: unknown[];
let teardown: () => void = () => {};

const banners = () => get(liveBanners).filter((b) => b.category === 'cli-update');
const messages = () => banners().map((b) => b.message);
/** What the dock does when a banner's button is pressed. */
function press(message: RegExp) {
	const banner = banners().find((b) => message.test(b.message))!;
	banner.action!.onClick();
	notify.dismissLive(banner.id);
}
const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
	vi.useFakeTimers();
	vi.clearAllMocks();
	localStorage.clear();
	updates = [];
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/settings/cli-updates') return { updates };
		throw new Error(`unexpected GET ${path}`);
	});
});
afterEach(() => {
	teardown();
	teardown = () => {};
	for (const banner of get(liveBanners)) notify.dismissLive(banner.id);
	vi.useRealTimers();
});

describe('cli update notice', () => {
	it('shows one banner per CLI with a newer version, and nothing when there is none', async () => {
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(0);
		expect(mocks.post).not.toHaveBeenCalled();

		updates = [claude, agy];
		recheckCliUpdates();
		await settle();
		expect(messages()).toEqual([
			'Claude Code 2.1.330 is available. This worker has 2.1.323.',
			'Antigravity 1.5.13 is available. This worker has 1.5.7.'
		]);
		expect(banners().every((b) => b.action?.label === 'Update' && b.duration_ms === 0)).toBe(true);
		// Looking is all it does: no install or update request without a press.
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('asks again once a worker that just started has finished its check', async () => {
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(0);
		updates = [claude];
		await vi.advanceTimersByTimeAsync(SETTLE_MS);
		expect(messages()).toEqual(['Claude Code 2.1.330 is available. This worker has 2.1.323.']);
		expect(mocks.get).toHaveBeenCalledTimes(2);
	});

	it('does not repeat a banner already on screen, and removes one the worker no longer offers', async () => {
		updates = [claude];
		teardown = installCliUpdateNotice();
		await settle();
		recheckCliUpdates();
		await settle();
		expect(banners()).toHaveLength(1);
		updates = [];
		recheckCliUpdates();
		await settle();
		expect(banners()).toHaveLength(0);
		// Taken down by the app, not closed by the owner: the version is not hidden.
		updates = [claude];
		recheckCliUpdates();
		await settle();
		expect(banners()).toHaveLength(1);
	});

	it('hides a version the owner closed, in later loads too, until a newer one exists', async () => {
		updates = [claude];
		teardown = installCliUpdateNotice();
		await settle();
		notify.dismissLive(banners()[0].id);
		recheckCliUpdates();
		await settle();
		expect(banners()).toHaveLength(0);

		teardown();
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(0);

		updates = [{ ...claude, latest_version: '2.1.331' }];
		recheckCliUpdates();
		await settle();
		expect(messages()).toEqual(['Claude Code 2.1.331 is available. This worker has 2.1.323.']);
	});

	it('runs the Claude Code update only when Update is pressed and reports the result', async () => {
		updates = [claude];
		const running = { installed: true, job: { kind: 'update', state: 'running', message: null } };
		const done = { installed: true, job: { kind: 'update', state: 'succeeded', message: 'Claude Code updated from 2.1.323 to 2.1.330.' } };
		mocks.post.mockResolvedValue(running);
		teardown = installCliUpdateNotice();
		await settle();

		press(/Claude Code 2\.1\.330/);
		await settle();
		expect(mocks.post).toHaveBeenCalledExactlyOnceWith('/settings/claude-cli/update', {});
		expect(messages()).toEqual(['Updating Claude Code']);

		mocks.get.mockImplementation(async (path: string) => path === '/settings/claude-cli' ? done : { updates: [] });
		await vi.advanceTimersByTimeAsync(2000);
		expect(messages()).toEqual(['Claude Code updated from 2.1.323 to 2.1.330.']);
		expect(banners()[0].severity).toBe('success');
		// Pressing Update is not "not now": nothing was remembered as closed.
		expect(localStorage.getItem('ai-shuffle-cli-update-dismissed')).toBeNull();
	});

	it('runs the Antigravity update and shows a failure as an error', async () => {
		updates = [agy];
		mocks.post.mockResolvedValue({ kind: 'update', state: 'running', message: null });
		teardown = installCliUpdateNotice();
		await settle();

		press(/Antigravity 1\.5\.13/);
		await settle();
		expect(mocks.post).toHaveBeenCalledExactlyOnceWith('/antigravity/update');

		mocks.get.mockImplementation(async (path: string) => path === '/antigravity/status'
			? { installed: true, install: { kind: 'update', state: 'failed', message: 'Update check failed: network down' } }
			: { updates: [agy] });
		await vi.advanceTimersByTimeAsync(2000);
		expect(messages()).toEqual(['Update check failed: network down']);
		expect(banners()[0].severity).toBe('error');
	});

	it('says so when the update could not be started', async () => {
		updates = [claude];
		mocks.post.mockRejectedValue(new Error('Could not reach the worker'));
		teardown = installCliUpdateNotice();
		await settle();
		press(/Claude Code 2\.1\.330/);
		await settle();
		expect(messages()).toEqual(['Could not reach the worker']);
	});

	it('stays quiet when the worker cannot answer, and takes its banners down with it', async () => {
		mocks.get.mockRejectedValue(new Error('offline'));
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(0);

		mocks.get.mockResolvedValue({ updates: [claude] });
		recheckCliUpdates();
		await settle();
		expect(banners()).toHaveLength(1);
		teardown();
		teardown = () => {};
		expect(banners()).toHaveLength(0);
		expect(localStorage.getItem('ai-shuffle-cli-update-dismissed')).toBeNull();
		const asked = mocks.get.mock.calls.length;
		recheckCliUpdates();
		await vi.advanceTimersByTimeAsync(SETTLE_MS * 2);
		expect(mocks.get.mock.calls.length).toBe(asked);
	});
});
