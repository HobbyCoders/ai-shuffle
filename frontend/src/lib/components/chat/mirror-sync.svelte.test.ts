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
import { createMirrorSync, mirrorSyncHeadline, type MirrorSync } from './mirror-sync.svelte';
import MirrorSyncGate from './MirrorSyncGate.svelte';

const reply = (body: Record<string, unknown>) => new Response(JSON.stringify(body));
const children = createRawSnippet(() => ({ render: () => '<div>transcript</div>' }));

afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

/** A controller in its own effect root, the way ChatView's init creates it. */
function startSync(input: { sessionId: string | null; remote: boolean }, onCaughtUp = vi.fn()) {
	const props = $state({ ...input });
	let sync!: MirrorSync;
	const destroy = $effect.root(() => {
		sync = createMirrorSync(() => ({ sessionId: props.sessionId, remote: props.remote, onCaughtUp }));
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

	it('a chat moved away while open keeps its transcript: no gate, no reload', async () => {
		// The open chat ran here and was just moved to another worker: what it
		// shows is this worker's own transcript, current at the move. Gating it
		// as a first open blanked it for a frame and reloaded it (found by the
		// two-worker-move-chat journey on a real pair).
		vi.useFakeTimers();
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply({ state: 'syncing', scope: 'session', pending_updates: 1 })));
		vi.stubGlobal('fetch', fetch);
		const { sync, props, onCaughtUp, destroy } = startSync({ sessionId: 'moved', remote: false });
		props.remote = true;
		flushSync();
		expect(sync.active).toBe(true);
		expect(sync.waiting).toBe(false);
		await vi.advanceTimersByTimeAsync(0);
		expect(String(fetch.mock.calls[0][0])).toContain('/sessions/moved/sync-status');
		fetch.mockImplementation(() => Promise.resolve(reply({ state: 'current' })));
		await vi.advanceTimersByTimeAsync(4000);
		expect(sync.waiting).toBe(false);
		expect(onCaughtUp).not.toHaveBeenCalled();

		// Another chat that is already remote is still checked first, and so is
		// the moved chat when it is opened again later (it may have moved on).
		props.sessionId = 'other';
		flushSync();
		expect(sync.waiting).toBe(true);
		props.sessionId = 'moved';
		flushSync();
		expect(sync.waiting).toBe(true);
		destroy();
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
		expect(screen.queryByText('transcript')).toBeNull();
		await vi.advanceTimersByTimeAsync(2000);
		// One poll per interval: the controller's, not one per mounted gate.
		expect(fetch).toHaveBeenCalledTimes(2);
		destroy();
	});
});

describe('mirrorSyncHeadline', () => {
	it('names the owner, or says the owning worker', () => {
		expect(mirrorSyncHeadline('checking', 'Unraid')).toBe('Checking sync with Unraid…');
		expect(mirrorSyncHeadline('syncing', null)).toBe('Syncing from the owning worker…');
	});
});
