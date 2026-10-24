import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback
}));

import {
	CHECK_POLL_MS,
	CHECK_TIMEOUT_MS,
	IMAGE_HINT,
	INSTALLER_URL,
	UpdatesState,
	describeWorker,
	imageHint,
	installerHint
} from './updates-state.svelte';

const self = (phase: string, extra: Record<string, unknown> = {}) => ({
	device_id: 'self',
	device_name: 'Current worker',
	online: true,
	supported: true,
	phase,
	...extra
});

let state: UpdatesState;
let snapshots: ReturnType<typeof self>[][];

beforeEach(() => {
	vi.useFakeTimers();
	vi.resetAllMocks();
	snapshots = [];
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/settings/application-updates') return { allow_updates: true, update_all_workers: false };
		const workers = snapshots.length > 1 ? snapshots.shift()! : snapshots[0];
		return { allow_updates: true, update_all_workers: false, workers };
	});
	transport.post.mockResolvedValue({ results: [{ device_id: 'self', device_name: 'Current worker', accepted: true, reason: 'Request queued' }] });
	state = new UpdatesState();
});
afterEach(() => {
	state.destroy();
	vi.useRealTimers();
});

async function ready() {
	await state.refresh();
	expect(state.policy?.allow_updates).toBe(true);
}

describe('check for updates', () => {
	it('settles once a worker leaves "checking"', async () => {
		await ready();
		snapshots = [[self('checking')], [self('idle')]];
		await state.check();
		expect(state.checking).toBe(true);
		expect(state.rows).toEqual([{ id: 'self', name: 'Current worker', tone: 'busy', text: 'Checking' }]);
		await vi.advanceTimersByTimeAsync(CHECK_POLL_MS);
		expect(state.checking).toBe(false);
		expect(state.rows?.[0]).toMatchObject({ tone: 'ok', text: 'Up to date' });
	});

	it('waits a while for a worker that has not started its check, then trusts its idle state', async () => {
		await ready();
		snapshots = [[self('idle')]];
		await state.check();
		expect(state.checking).toBe(true);
		await vi.advanceTimersByTimeAsync(12_000);
		expect(state.checking).toBe(false);
		expect(state.rows?.[0].text).toBe('Up to date');
	});

	it('gives up after 90 seconds and says so', async () => {
		await ready();
		snapshots = [[self('checking')]];
		await state.check();
		await vi.advanceTimersByTimeAsync(CHECK_TIMEOUT_MS + CHECK_POLL_MS);
		expect(state.checking).toBe(false);
		expect(state.rows?.[0]).toMatchObject({ tone: 'off', text: 'Still checking' });
		const polls = transport.get.mock.calls.filter(([p]) => p === '/settings/application-updates/workers').length;
		expect(polls).toBeLessThanOrEqual(CHECK_TIMEOUT_MS / CHECK_POLL_MS + 2);
	});

	// Ported from main's ApplicationUpdates test (#1623).
	it('waits out queued requests, lists every asked worker and pings the sidebar twice', async () => {
		const events = vi.fn();
		window.addEventListener('application-update-policy', events);
		try {
			await ready();
			const peer = (phase: string, extra: Record<string, unknown> = {}) => ({ ...self(phase, extra), device_id: 'peer', device_name: 'ServerWorker' });
			snapshots = [
				[self('idle', { request_pending: true }), peer('idle', { request_pending: true })],
				[self('checking'), peer('idle')],
				[self('available', { version: 'cccccccccccc', running_version: 'a'.repeat(40) }), peer('idle')]
			];
			transport.post.mockResolvedValueOnce({
				results: [
					{ device_id: 'peer', device_name: 'ServerWorker', accepted: true, reason: 'Request queued' },
					{ device_id: 'off', device_name: 'Laptop', accepted: false, reason: 'Offline; not queued' },
					{ device_id: 'self', device_name: 'Current worker', accepted: true, reason: 'Request queued' }
				]
			});
			events.mockClear();
			await state.check();
			expect(events).toHaveBeenCalledTimes(1);
			expect(state.checking).toBe(true);
			await vi.advanceTimersByTimeAsync(CHECK_POLL_MS * 2);
			expect(state.checking).toBe(false);
			expect(state.rows?.map((r) => [r.name, r.text])).toEqual([
				['ServerWorker', 'Up to date'],
				['Laptop', 'Offline; not queued'],
				['Current worker', 'Version cccccccccccc available']
			]);
			expect(events).toHaveBeenCalledTimes(2);
		} finally {
			window.removeEventListener('application-update-policy', events);
		}
	});

	// #519: the sidebar's rule. A worker already checking refuses a second
	// check, but its own check answers this one: wait for it, then show it.
	describe('a worker that refused because its own check was running', () => {
		const peer = (phase: string, extra: Record<string, unknown> = {}) => ({
			...self(phase, extra),
			device_id: 'peer',
			device_name: 'ServerWorker'
		});
		const refusedBy = (reason: string) => ({
			results: [
				{ device_id: 'peer', device_name: 'ServerWorker', accepted: false, reason },
				{ device_id: 'self', device_name: 'Current worker', accepted: true, reason: 'Request queued' }
			]
		});
		const found = { version: 'cccccccccccc', running_version: 'a'.repeat(40) };

		it('waits while it checks, then shows the update it found', async () => {
			await ready();
			transport.post.mockResolvedValueOnce(refusedBy('An update is already in progress'));
			snapshots = [
				[peer('checking'), self('checking')],
				[peer('checking'), self('idle')],
				[peer('available', found), self('idle')]
			];
			await state.check();
			expect(state.rows?.[0]).toMatchObject({ name: 'ServerWorker', tone: 'busy', text: 'Checking' });
			await vi.advanceTimersByTimeAsync(CHECK_POLL_MS);
			expect(state.checking).toBe(true);
			await vi.advanceTimersByTimeAsync(CHECK_POLL_MS);
			expect(state.checking).toBe(false);
			expect(state.rows?.map((r) => [r.name, r.text])).toEqual([
				['ServerWorker', 'Version cccccccccccc available'],
				['Current worker', 'Up to date']
			]);
		});

		it('shows its own result when its check finished before the first poll', async () => {
			await ready();
			transport.post.mockResolvedValueOnce(refusedBy('An update is already in progress'));
			snapshots = [[peer('idle'), self('checking')], [peer('idle'), self('idle')]];
			await state.check();
			expect(state.rows?.[0]).toMatchObject({ name: 'ServerWorker', tone: 'ok', text: 'Up to date' });
			await vi.advanceTimersByTimeAsync(CHECK_POLL_MS);
			expect(state.checking).toBe(false);
			expect(state.rows?.some((r) => r.text.includes('already in progress'))).toBe(false);
		});

		it('shows a download that was running rather than the refusal', async () => {
			await ready();
			transport.post.mockResolvedValueOnce(refusedBy('An update is already in progress'));
			snapshots = [
				[peer('downloading', { ...found, percent: 40 }), self('checking')],
				[peer('downloading', { ...found, percent: 60 }), self('idle')]
			];
			await state.check();
			expect(state.rows?.[0]).toMatchObject({ name: 'ServerWorker', text: 'Downloading, 40%' });
			await vi.advanceTimersByTimeAsync(CHECK_POLL_MS);
			expect(state.checking).toBe(false);
			expect(state.rows?.[0]).toMatchObject({ name: 'ServerWorker', text: 'Downloading, 60%' });
		});

		it('still names any other refusal at once', async () => {
			await ready();
			transport.post.mockResolvedValueOnce(refusedBy('Updates disabled'));
			snapshots = [[peer('idle'), self('available', found)]];
			await state.check();
			expect(state.checking).toBe(false);
			expect(state.rows?.[0]).toMatchObject({ name: 'ServerWorker', tone: 'off', text: 'Updates disabled' });
		});
	});

	it('keeps waiting on a worker that never takes the request', async () => {
		await ready();
		snapshots = [[self('idle', { request_pending: true })]];
		await state.check();
		await vi.advanceTimersByTimeAsync(20_000);
		expect(state.checking).toBe(true);
		await vi.advanceTimersByTimeAsync(CHECK_TIMEOUT_MS);
		expect(state.rows?.[0]).toMatchObject({ text: 'Still checking' });
	});

	it('reports a refused request and does not poll', async () => {
		await ready();
		transport.post.mockRejectedValueOnce({ detail: 'Application updates are disabled in Account settings' });
		await state.check();
		expect(state.checkError).toBe('Application updates are disabled in Account settings');
		expect(state.rows).toBeNull();
		expect(transport.get).not.toHaveBeenCalledWith('/settings/application-updates/workers');
	});

	it('does nothing while updates are off', async () => {
		transport.get.mockResolvedValueOnce({ allow_updates: false, update_all_workers: false });
		await state.refresh();
		await state.check();
		expect(transport.post).not.toHaveBeenCalled();
	});

	it('stops polling when the page goes away', async () => {
		await ready();
		snapshots = [[self('checking')]];
		await state.check();
		state.destroy();
		const before = transport.get.mock.calls.length;
		await vi.advanceTimersByTimeAsync(CHECK_POLL_MS * 3);
		expect(transport.get.mock.calls.length).toBe(before);
	});
});

describe('describeWorker', () => {
	it('names each phase', () => {
		expect(describeWorker(self('available', { version: '0.8.23' }))).toEqual({ tone: 'warn', text: 'Version 0.8.23 available' });
		expect(describeWorker(self('ready'))).toEqual({ tone: 'warn', text: 'Update downloaded, ready to install' });
		expect(describeWorker(self('image-required'))).toEqual({ tone: 'err', text: 'Needs a new Docker image' });
		expect(describeWorker(self('image-required', { message: 'Pull 0.9' }))).toEqual({ tone: 'err', text: 'Pull 0.9' });
		// The container updater names the running revision on this phase (#1901).
		expect(describeWorker(self('image-required', { version: '0.8.23', running_version: '0.8.23+abc' }))).toEqual({
			tone: 'err',
			text: 'Needs a new Docker image'
		});
		expect(describeWorker(self('available', { version: '0.8.23', running_version: '0.8.23+abc' }))).toEqual({ tone: 'ok', text: 'Up to date' });
		expect(describeWorker(self('idle', { request_pending: true }))).toEqual({ tone: 'busy', text: 'Checking' });
		expect(describeWorker(self('error', { error: 'Signature mismatch' }))).toEqual({ tone: 'err', text: 'Signature mismatch' });
		expect(describeWorker(self('downloading', { percent: 41.6 }))).toEqual({ tone: 'busy', text: 'Downloading, 42%' });
		expect(describeWorker({ ...self('idle'), online: false })).toEqual({ tone: 'off', text: 'Offline' });
		expect(describeWorker(self('idle'), { device_name: 'x', accepted: false, reason: 'Updates disabled' })).toEqual({
			tone: 'off',
			text: 'Updates disabled'
		});
	});
});

describe('newer Docker image hint (#1821)', () => {
	it('says the current version keeps working and how to get a newer image', () => {
		expect(IMAGE_HINT).toMatch(/^Your current version keeps working\. /);
		expect(IMAGE_HINT).not.toMatch(/ServerWorker|Force update/);
		expect(IMAGE_HINT).toContain('docker compose up -d');
		expect(IMAGE_HINT).toContain('docker compose pull');
		expect(imageHint(self('image-required'))).toBe(IMAGE_HINT);
		expect(imageHint(self('ready', { notice: 'A coming update needs a newer Docker image.' }))).toMatch(
			/^A coming update needs a newer Docker image\. To get a newer Docker image:/
		);
		expect(imageHint(self('idle'))).toBeUndefined();
	});

	it('shows the hint once under the results of a check', async () => {
		await ready();
		transport.post.mockResolvedValue({ results: [] });
		snapshots = [[self('image-required'), { ...self('image-required'), device_id: 'peer', device_name: 'ServerWorker' }]];
		await state.check();
		expect(state.checking).toBe(false);
		expect(state.rows?.map((row) => row.hint)).toEqual([IMAGE_HINT, IMAGE_HINT]);
		expect(state.rows?.[0]).toMatchObject({ tone: 'err', text: 'Needs a new Docker image' });
	});
});

describe('a desktop only the latest installer can update', () => {
	const failed = (extra: Record<string, unknown> = {}) =>
		self('error', { error: 'No healthy source build is available. Install the latest AI Shuffle installer to recover.', remedy: 'installer', ...extra });

	it('says so in plain words, with data reassurance only when the launcher kept it', () => {
		expect(describeWorker(failed())).toEqual({ tone: 'err', text: 'Needs the latest installer' });
		expect(installerHint(failed())).toBe('This update cannot finish inside the app. Run the latest installer to finish it.');
		expect(installerHint(failed({ data_kept: true }))).toBe(
			'This update cannot finish inside the app. Run the latest installer to finish it. Your data is kept as it was before the update.'
		);
		expect(installerHint(self('unsupported', { remedy: 'installer' }))).toMatch(/^This app cannot update itself\. /);
		expect(installerHint(self('error', { error: 'Source archive checksum or size mismatch' }))).toBeUndefined();
	});

	it('wins over the refusal the launcher state caused', () => {
		const refusal = { device_id: 'self', device_name: 'Current worker', accepted: false, reason: 'Updater unavailable; install a release with update support' };
		expect(describeWorker(self('unsupported', { supported: false, remedy: 'installer' }), refusal)).toEqual({ tone: 'err', text: 'Needs the latest installer' });
	});

	it('links the latest installer under the results of a check', async () => {
		await ready();
		snapshots = [[failed({ data_kept: true })]];
		await state.check();
		expect(state.checking).toBe(false);
		expect(state.rows?.[0]).toMatchObject({
			tone: 'err',
			text: 'Needs the latest installer',
			hint: installerHint(failed({ data_kept: true })),
			link: { label: 'Download the latest installer', href: INSTALLER_URL }
		});
		expect(INSTALLER_URL).toBe('https://github.com/HobbyCoders/ai-shuffle/releases/latest');
	});
});

describe('restart waiting state in check results (#344)', () => {
	it('says what a downloaded update waits for, and keeps plain ready and unknown state as before', () => {
		const blocker = { session_id: 's', title: 'Fix login', kind: 'shell', task_id: 't', label: 'pytest -q', running_turn: true };
		expect(describeWorker(self('ready', { install_state: 'blocked', wait_reason: 'running_work', blocked_by: [blocker] }))).toEqual({
			tone: 'warn',
			text: 'Downloaded and ready. Waiting for Fix login: pytest -q.'
		});
		expect(describeWorker(self('ready', { install_state: 'ready', blocked_by: [] }))).toEqual({ tone: 'warn', text: 'Update downloaded, ready to install' });
		expect(describeWorker(self('ready', { install_state: null }))).toEqual({ tone: 'warn', text: 'Update downloaded, ready to install' });
		expect(
			describeWorker(self('available', { version: 'b'.repeat(40), staged_version: 'a'.repeat(40), install_state: 'unavailable', wait_reason: 'newer_offer' }))
		).toEqual({ tone: 'warn', text: 'Downloaded aaaaaaaaaaaa is retained. A newer update is available; download it to continue.' });
	});
});
