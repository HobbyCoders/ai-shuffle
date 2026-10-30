/**
 * The mirrored-chat sync check runs without MirrorSyncGate mounted.
 *
 * ChatView keeps the centred hero while a chat opened from it loads, and the
 * gate lives only in the message column. The check used to live inside the
 * gate, so a remote chat opened from the hero started syncing only after its
 * history arrived (transcript first, then a full-pane "Checking sync…" swap;
 * an empty mirror never mounted the gate at all). ChatView now owns the check
 * (createMirrorSync) and hands it to the gate.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, createRawSnippet } from 'svelte';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createMirrorSync, mirrorSyncHeadline, mirrorSyncRetryDelay, MIRROR_SYNC_FAILURE_LIMIT, MIRROR_SYNC_MAX_BACKOFF_MS, MIRROR_SYNC_POLL_MS, type MirrorSync } from './mirror-sync.svelte';
import MirrorSyncGate from './MirrorSyncGate.svelte';

const reply = (body: Record<string, unknown>) => new Response(JSON.stringify(body));
const children = createRawSnippet(() => ({ render: () => '<div>transcript</div>' }));

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

/** A controller in its own effect root, the way ChatView's init creates it. */
function startSync(input: { sessionId: string | null; remote: boolean; createdFromComposer?: boolean }, onCaughtUp = vi.fn()) {
	const props = $state({ ...input });
	let sync!: MirrorSync;
	const destroy = $effect.root(() => {
		sync = createMirrorSync(() => ({ ...props, onCaughtUp }));
	});
	flushSync();
	return { sync, props, onCaughtUp, destroy };
}

describe('createMirrorSync (the check ChatView runs whatever its layout)', () => {
	it('checks a remote chat with no gate mounted, and reveals it once the owner is caught up', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn()
			.mockResolvedValueOnce(reply({ state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: 3 }))
			.mockImplementation(() => Promise.resolve(reply({ state: 'current' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, onCaughtUp, destroy } = startSync({ sessionId: 'remote-1', remote: true });
		// Waiting from the first frame: the layout holds and the hero says why.
		expect(sync.active).toBe(true);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(String(fetch.mock.calls[0][0])).toContain('/sessions/remote-1/sync-status');
		expect(sync.syncState).toBe('syncing');
		expect(sync.pending).toBe(3);
		expect(sync.waiting).toBe(true);
		expect(onCaughtUp).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(2000);
		expect(sync.waiting).toBe(false);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		// Later polls never reload again.
		await vi.advanceTimersByTimeAsync(4000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		destroy();
	});

	it('keeps a fresh compose visible without an opening reload, but checks a later navigation', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockResolvedValueOnce(reply({ state: 'syncing', pending_updates: 3 }))
			.mockImplementation(() => Promise.resolve(reply({ state: 'current' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, props, onCaughtUp, destroy } = startSync({ sessionId: 'new', remote: true, createdFromComposer: true });
		expect(sync.waiting).toBe(false);
		render(MirrorSyncGate, { sync, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('transcript')).toBeTruthy();
		expect(sync.syncState).toBe('syncing');
		await vi.advanceTimersByTimeAsync(2000);
		expect(fetch).toHaveBeenCalledTimes(2);
		expect(sync.waiting).toBe(false);
		expect(onCaughtUp).not.toHaveBeenCalled();

		props.sessionId = 'existing';
		props.createdFromComposer = false;
		flushSync();
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		destroy();
	});

	it('never polls a local chat or an unbound one', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn();
		vi.stubGlobal('fetch', fetch);
		const local = startSync({ sessionId: 'local-1', remote: false });
		const unbound = startSync({ sessionId: null, remote: true });
		await vi.advanceTimersByTimeAsync(5000);
		expect(fetch).not.toHaveBeenCalled();
		expect(local.sync.waiting).toBe(false);
		expect(unbound.sync.waiting).toBe(false);
		local.destroy();
		unbound.destroy();
	});

	it('restarts for the next chat and stops when destroyed', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply({ state: 'checking' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, props, destroy } = startSync({ sessionId: 'one', remote: true });
		await vi.advanceTimersByTimeAsync(0);
		props.sessionId = 'two';
		flushSync();
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(0);
		expect(String(fetch.mock.calls.at(-1)![0])).toContain('/sessions/two/sync-status');
		destroy();
		const calls = fetch.mock.calls.length;
		await vi.advanceTimersByTimeAsync(10_000);
		expect(fetch.mock.calls.length).toBe(calls);
	});

	it('a gate handed the check renders it and starts no second poll', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply({ state: 'syncing', scope: 'session', initial_pending_updates: 2 })));
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-2', remote: true });
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).toHaveBeenCalledTimes(1);
		// The column mounts later (the history arrived): it shows the same check.
		render(MirrorSyncGate, { sync, deviceName: 'Unraid', children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('Syncing from Unraid…')).toBeTruthy();
		expect(screen.getByText('transcript').closest('[aria-hidden="true"]')).not.toBeNull();
		await vi.advanceTimersByTimeAsync(2000);
		// One poll per interval: the controller's, not one per mounted gate.
		expect(fetch).toHaveBeenCalledTimes(2);
		destroy();
	});
});

describe('a sync check that keeps failing (#814)', () => {
	it('stops waiting after a few failures, says it cannot reach the owner, and keeps retrying with backoff', async () => {
		vi.useFakeTimers();
		let healthy = false;
		const fetch = vi.fn().mockImplementation(() => healthy
			? Promise.resolve(reply({ state: 'current' }))
			: Promise.reject(new TypeError('Failed to fetch')));
		vi.stubGlobal('fetch', fetch);
		const { sync, onCaughtUp, destroy } = startSync({ sessionId: 'remote-3', remote: true });
		render(MirrorSyncGate, { sync, deviceName: 'Unraid', children });
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT - 1);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT);
		// Revealed with what it has, and a calm notice with a way out.
		expect(sync.waiting).toBe(false);
		expect(sync.unreachable).toBe(true);
		expect(screen.getByText('transcript').closest('[aria-hidden="true"]')).toBeNull();
		expect(screen.getByText(/Can't reach Unraid right now/)).toBeTruthy();
		// Background retries back off: 4s, then 8s.
		await vi.advanceTimersByTimeAsync(3999);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT);
		await vi.advanceTimersByTimeAsync(1);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT + 1);
		// Retry checks now; once the owner answers the notice goes away.
		healthy = true;
		screen.getByRole('button', { name: 'Retry' }).click();
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT + 2);
		expect(sync.unreachable).toBe(false);
		expect(sync.syncState).toBe('current');
		expect(screen.queryByText(/Can't reach/)).toBeNull();
		expect(onCaughtUp).toHaveBeenCalledOnce();
		destroy();
	});

	it('counts non-OK and invalid answers as failures too', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn()
			.mockResolvedValueOnce(new Response('nope', { status: 503 }))
			.mockImplementation(() => Promise.resolve(reply({ state: 'bogus' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-4', remote: true });
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS * (MIRROR_SYNC_FAILURE_LIMIT - 1));
		expect(sync.unreachable).toBe(true);
		expect(sync.waiting).toBe(false);
		destroy();
	});

	it('backs off up to a cap', () => {
		expect(mirrorSyncRetryDelay(0)).toBe(MIRROR_SYNC_POLL_MS);
		expect(mirrorSyncRetryDelay(MIRROR_SYNC_FAILURE_LIMIT - 1)).toBe(MIRROR_SYNC_POLL_MS);
		expect(mirrorSyncRetryDelay(MIRROR_SYNC_FAILURE_LIMIT)).toBe(MIRROR_SYNC_POLL_MS * 2);
		expect(mirrorSyncRetryDelay(50)).toBe(MIRROR_SYNC_MAX_BACKOFF_MS);
	});
});

describe('mirrorSyncHeadline', () => {
	it('names the owner, or says the owning worker', () => {
		expect(mirrorSyncHeadline('checking', 'Unraid')).toBe('Checking sync with Unraid…');
		expect(mirrorSyncHeadline('syncing', null)).toBe('Syncing from the owning worker…');
	});
});
