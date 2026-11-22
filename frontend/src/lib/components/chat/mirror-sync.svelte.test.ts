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
import { createMirrorSync, mirrorSyncHeadline, mirrorSyncRetryDelay, MIRROR_SYNC_FAILURE_LIMIT, MIRROR_SYNC_MAX_BACKOFF_MS, MIRROR_SYNC_POLL_MS, MIRROR_SYNC_REQUEST_TIMEOUT_MS, MIRROR_SYNC_STALL_REVEAL_MS, MIRROR_SYNC_WAITING_POLL_MS, type MirrorSync } from './mirror-sync.svelte';
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

	it('keeps a chat handed to another worker while open on screen, but checks it when opened again later', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply({
			state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: 3, pending_updates: 3,
		})));
		vi.stubGlobal('fetch', fetch);
		const { sync, props, onCaughtUp, destroy } = startSync({ sessionId: 'mine', remote: false });
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).not.toHaveBeenCalled();
		// The hand-off: the open chat now runs elsewhere. Its transcript is this
		// worker's own, so it is never hidden, not even for one frame.
		props.remote = true;
		expect(sync.waiting).toBe(false);
		flushSync();
		expect(sync.waiting).toBe(false);
		await vi.advanceTimersByTimeAsync(0);
		expect(String(fetch.mock.calls[0][0])).toContain('/sessions/mine/sync-status');
		expect(sync.syncState).toBe('syncing');
		expect(sync.waiting).toBe(false);
		// Followed at the normal pace, with no opening reload.
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_WAITING_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS - MIRROR_SYNC_WAITING_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(2);
		expect(onCaughtUp).not.toHaveBeenCalled();

		// Opened again after another chat, it is a mirror like any other.
		props.sessionId = 'other';
		flushSync();
		props.sessionId = 'mine';
		flushSync();
		expect(sync.waiting).toBe(true);
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
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_WAITING_POLL_MS);
		// One poll per interval: the controller's, not one per mounted gate.
		expect(fetch).toHaveBeenCalledTimes(2);
		destroy();
	});
});

describe('the opening wait', () => {
	it('checks again quickly while the chat is hidden, then at the normal pace', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn()
			.mockResolvedValueOnce(reply({ state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: 1 }))
			.mockImplementation(() => Promise.resolve(reply({ state: 'current' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, onCaughtUp, destroy } = startSync({ sessionId: 'remote-fast', remote: true });
		await vi.advanceTimersByTimeAsync(0);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_WAITING_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(2);
		expect(sync.waiting).toBe(false);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS - 1);
		expect(fetch).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(1);
		expect(fetch).toHaveBeenCalledTimes(3);
		destroy();
	});

	it('shows a chat whose last updates never arrive, with the pill, and still reloads at its catch-up', async () => {
		vi.useFakeTimers();
		let delivered = false;
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply(delivered
			? { state: 'current' }
			: { state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: 2, pending_updates: 2 })));
		vi.stubGlobal('fetch', fetch);
		const { sync, onCaughtUp, destroy } = startSync({ sessionId: 'remote-stuck', remote: true });
		render(MirrorSyncGate, { sync, deviceName: 'Office PC', children });
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS - MIRROR_SYNC_WAITING_POLL_MS);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_WAITING_POLL_MS);
		expect(sync.waiting).toBe(false);
		expect(onCaughtUp).not.toHaveBeenCalled();
		expect(screen.getByText('transcript').closest('[aria-hidden="true"]')).toBeNull();
		expect(screen.getByText(/Syncing updates from Office PC… 2 updates remaining/)).toBeTruthy();
		// Revealed: back to the normal pace, and the catch-up still reloads once.
		const calls = fetch.mock.calls.length;
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_POLL_MS - 1);
		expect(fetch.mock.calls.length).toBe(calls);
		delivered = true;
		await vi.advanceTimersByTimeAsync(1);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(screen.queryByText(/Syncing updates/)).toBeNull();
		destroy();
	});

	it('keeps waiting while a large catch-up keeps making progress', async () => {
		vi.useFakeTimers();
		let remaining = 400;
		const fetch = vi.fn().mockImplementation(() => {
			remaining -= 10;
			return Promise.resolve(reply({ state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: remaining }));
		});
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-big', remote: true });
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS * 2);
		expect(sync.waiting).toBe(true);
		expect(sync.pending).toBeLessThan(400);
		destroy();
	});

	it('does not count an owner-wide answer (no per-chat count yet) as the lowest count', async () => {
		vi.useFakeTimers();
		let polls = 0;
		const fetch = vi.fn().mockImplementation(() => {
			polls += 1;
			// The first check ran before the owner's head arrived.
			return Promise.resolve(reply(polls === 1
				? { state: 'syncing', scope: 'owner', pending_updates: 0 }
				: { state: 'syncing', scope: 'session', initial_caught_up: false, initial_pending_updates: 400 - polls * 10 }));
		});
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-late-head', remote: true });
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS * 2);
		expect(sync.waiting).toBe(true);
		destroy();
	});

	it('restarts the stall clock when the opening checkpoint is replaced', async () => {
		vi.useFakeTimers();
		let polls = 0;
		const fetch = vi.fn().mockImplementation(() => {
			polls += 1;
			// The owner's log was replaced: a new checkpoint with a larger count, then progress on it.
			const replaced = polls > 4;
			return Promise.resolve(reply({
				state: 'syncing', scope: 'session', initial_caught_up: false,
				initial_target: replaced ? { seq: 90, epoch: 'second', tail_id: 't90' } : { seq: 10, epoch: 'first', tail_id: 't10' },
				initial_pending_updates: replaced ? 100 - polls : 5,
			}));
		});
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-replaced', remote: true });
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS * 2);
		expect(sync.waiting).toBe(true);
		destroy();
	});

	it('keeps a first transcript download hidden: there is no usable copy to show yet', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply(
			{ state: 'syncing', pending_updates: 0, transcript: 'downloading', scope: 'owner' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-new-worker', remote: true });
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS * 3);
		expect(sync.waiting).toBe(true);
		destroy();
	});

	it('shows a chat whose owner never sends a per-chat head (an older worker) after the stall time', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply(
			{ state: 'syncing', pending_updates: 4, scope: 'owner' })));
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-old-owner', remote: true });
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_STALL_REVEAL_MS - MIRROR_SYNC_WAITING_POLL_MS);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(MIRROR_SYNC_WAITING_POLL_MS);
		expect(sync.waiting).toBe(false);
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

	it('counts a check that never answers as a failure (no endless wait)', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation((_url: string, init?: RequestInit) => new Promise((_resolve, reject) => {
			init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
		}));
		vi.stubGlobal('fetch', fetch);
		const { sync, destroy } = startSync({ sessionId: 'remote-hang', remote: true });
		await vi.advanceTimersByTimeAsync(0);
		expect(sync.waiting).toBe(true);
		await vi.advanceTimersByTimeAsync(
			MIRROR_SYNC_FAILURE_LIMIT * MIRROR_SYNC_REQUEST_TIMEOUT_MS + (MIRROR_SYNC_FAILURE_LIMIT - 1) * MIRROR_SYNC_POLL_MS);
		expect(fetch).toHaveBeenCalledTimes(MIRROR_SYNC_FAILURE_LIMIT);
		expect(sync.unreachable).toBe(true);
		expect(sync.waiting).toBe(false);
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
