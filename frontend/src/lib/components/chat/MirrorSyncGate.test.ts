import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import MirrorSyncGate from './MirrorSyncGate.svelte';

const children = createRawSnippet(() => ({ render: () => '<div>2 running tasks, 2 agents</div>' }));
const status = (state: string, pending_updates = 0) => new Response(JSON.stringify({ state, pending_updates }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('mirrored session freshness', () => {
	it('holds stale task/agent state, keeps the notice on poll errors, and refreshes on drain', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockResolvedValueOnce(status('syncing', 40))
			.mockRejectedValueOnce(new Error('connection lost')).mockResolvedValue(status('current'));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, { sessionId: 'remote-chat', remote: true, deviceName: 'Unraid', onCaughtUp, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByRole('status').textContent).toContain('Syncing from Unraid');
		expect(screen.getByText('40 updates remaining')).toBeTruthy();
		expect(screen.queryByText('2 running tasks, 2 agents')).toBeNull();
		await vi.advanceTimersByTimeAsync(2000);
		expect(screen.getByText('40 updates remaining')).toBeTruthy();
		expect(onCaughtUp).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(screen.queryByRole('status')).toBeNull();
		expect(screen.getByText('2 running tasks, 2 agents')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
	});

	it('fences late responses when switching sessions and cancels polling on unmount', async () => {
		vi.useFakeTimers();
		let resolve!: (value: Response) => void;
		const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(r => { resolve = r; }))
			.mockResolvedValue(status('syncing', 5));
		vi.stubGlobal('fetch', fetch);
		const props = { sessionId: 'first', remote: true, onCaughtUp: vi.fn(), children };
		const view = render(MirrorSyncGate, props);
		await view.rerender({ ...props, sessionId: 'second' });
		await vi.advanceTimersByTimeAsync(0);
		resolve(status('current'));
		await vi.advanceTimersByTimeAsync(0);
		expect(props.onCaughtUp).not.toHaveBeenCalled();
		expect(screen.getByText('5 updates remaining')).toBeTruthy();
		view.unmount();
		await vi.advanceTimersByTimeAsync(4000);
		expect(fetch).toHaveBeenCalledTimes(2);
	});

	it('checks a session before its sidebar ownership row has arrived', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(status('syncing', 7)));
		render(MirrorSyncGate, { sessionId: 'not-in-sidebar', onCaughtUp: vi.fn(), children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('7 updates remaining')).toBeTruthy();
		expect(screen.queryByText('2 running tasks, 2 agents')).toBeNull();
	});

	it('does not gate a new conversation before it has a session id', () => {
		const fetch = vi.fn();
		vi.stubGlobal('fetch', fetch);
		render(MirrorSyncGate, { sessionId: null, onCaughtUp: vi.fn(), children });
		expect(screen.queryByRole('status')).toBeNull();
		expect(fetch).not.toHaveBeenCalled();
	});

	it('labels offline saved history and leaves local sessions alone', async () => {
		const fetch = vi.fn().mockResolvedValue(status('offline'));
		vi.stubGlobal('fetch', fetch);
		const props = { sessionId: 'remote', remote: true, onCaughtUp: vi.fn(), children };
		const view = render(MirrorSyncGate, props);
		expect(await screen.findByText('Owning worker offline. Showing the last saved conversation.')).toBeTruthy();
		await view.rerender({ ...props, remote: false });
		expect(screen.queryByRole('status')).toBeNull();
		expect(fetch).toHaveBeenCalledOnce();
	});
});
