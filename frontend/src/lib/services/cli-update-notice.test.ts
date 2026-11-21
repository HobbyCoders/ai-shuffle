import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, put: mocks.put },
	getErrorMessage: (e: Error, fallback: string) => e?.message ?? fallback }));
vi.mock('$lib/stores/providers.svelte', () => ({ providers: { loadClaude: async () => {} } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { checkAuth: async () => {} } }));

import { liveBanners, notify } from '$lib/stores/notifications';
import { antigravity } from '$lib/components/settings/providers/antigravity.svelte';
import { ClaudeCliInstallState } from '$lib/components/settings/providers/claude-cli-install.svelte';
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

	it('takes a banner down as soon as its check is switched off in Settings', async () => {
		updates = [claude, agy];
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(2);

		mocks.put.mockResolvedValue({ check_updates: false, latest_version: null });
		updates = [claude];
		await antigravity.setUpdateCheck(false);
		await settle();
		expect(mocks.put).toHaveBeenLastCalledWith('/antigravity/update-check', { check_updates: false });
		expect(messages()).toEqual(['Claude Code 2.1.330 is available. This worker has 2.1.323.']);

		mocks.put.mockResolvedValue({ installed: true, version: '2.1.323', check_updates: false, latest_version: null, job: null });
		updates = [];
		const settings = new ClaudeCliInstallState();
		expect(await settings.checkUpdates(false)).toBe(true);
		await settle();
		settings.dispose();
		expect(mocks.put).toHaveBeenLastCalledWith('/settings/claude-cli', { check_updates: false });
		expect(banners()).toHaveLength(0);
		// Switching the check off is not "not now" for that version.
		expect(localStorage.getItem('ai-shuffle-cli-update-dismissed')).toBeNull();
	});

	it('takes a banner down when the update was run from Settings', async () => {
		updates = [claude, agy];
		teardown = installCliUpdateNotice();
		await settle();
		expect(banners()).toHaveLength(2);

		mocks.post.mockResolvedValue({ installed: true, version: '2.1.330', check_updates: true, latest_version: null,
			job: { kind: 'update', state: 'succeeded', started_at: '2026-11-20T00:00:00Z', message: 'Claude Code updated.' } });
		updates = [agy];
		const settings = new ClaudeCliInstallState();
		await settings.start('update');
		await settle();
		settings.dispose();
		expect(messages()).toEqual(['Antigravity 1.5.13 is available. This worker has 1.5.7.']);

		const running = { kind: 'update', state: 'running', version: null, error: null, message: null };
		mocks.post.mockResolvedValue(running);
		mocks.get.mockImplementation(async (path: string) => path === '/settings/cli-updates' ? { updates: [] }
			: { installed: true, version: '1.5.13', install: { ...running, state: 'succeeded' } });
		antigravity.status = null;
		await antigravity.install('update');
		await vi.advanceTimersByTimeAsync(2000);
		expect(banners()).toHaveLength(0);
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
