import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import MirrorSyncGate from './MirrorSyncGate.svelte';

const children = createRawSnippet(() => ({ render: () => '<div>2 running tasks, 2 agents</div>' }));
const status = (state: string, pending_updates = 0) => new Response(JSON.stringify({ state, pending_updates }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
// The status slot stays mounted (a live region) once the chat is shown; it is
// empty while the chat is current.
const statusText = () => screen.queryByRole('status')?.textContent?.trim() ?? '';

describe('mirrored session freshness', () => {
	it('withholds stale content without unmounting it at an opening freshness barrier', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(status('syncing', 4)).mockImplementation(() => Promise.resolve(status('current'))));
		const view = render(MirrorSyncGate, { sessionId: 'chat', remote: false, children });
		const transcript = screen.getByText('2 running tasks, 2 agents');
		await view.rerender({ sessionId: 'chat', remote: true, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(transcript.isConnected).toBe(true);
		const withheld = transcript.closest('[aria-hidden="true"]') as HTMLElement;
		expect(withheld).not.toBeNull();
		expect(withheld.style.visibility).toBe('hidden');
		expect(withheld.inert).toBe(true);
		await vi.advanceTimersByTimeAsync(2000);
		expect(screen.getByText('2 running tasks, 2 agents')).toBe(transcript);
		expect(withheld.inert).toBe(false);
		expect(withheld.hasAttribute('aria-hidden')).toBe(false);
	});

	it('the kill switch restores withholding by unmounting', async () => {
		vi.useFakeTimers();
		vi.stubEnv('VITE_PERF_KEEP_MIRROR_CACHE', '0');
		try {
			vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(status('syncing', 4))));
			render(MirrorSyncGate, { sessionId: 'chat', remote: true, children });
			await vi.advanceTimersByTimeAsync(0);
			expect(screen.queryByText('2 running tasks, 2 agents')).toBeNull();
		} finally {
			vi.unstubAllEnvs();
		}
	});

	it('reveals a fixed opening checkpoint while live updates keep arriving', async () => {
		vi.useFakeTimers();
		const target = { seq: 10, epoch: 'first/event', tail_id: 'tail&event' };
		const reply = (initial_caught_up: boolean, initial_pending_updates: number, pending_updates: number) => new Response(JSON.stringify({
			state: 'syncing', scope: 'session', initial_target: target,
			initial_caught_up, initial_pending_updates, pending_updates,
		}));
		const fetch = vi.fn().mockResolvedValueOnce(reply(false, 4, 4))
			.mockRejectedValueOnce(new Error('temporary outage'))
			.mockResolvedValueOnce(reply(false, 2, 15))
			.mockImplementation(() => Promise.resolve(reply(true, 0, 20)));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, { sessionId: 'busy', onCaughtUp, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('4 updates remaining')).toBeTruthy();
		expect(screen.getByText('2 running tasks, 2 agents').closest('[aria-hidden="true"]')).not.toBeNull();
		await vi.advanceTimersByTimeAsync(4000);
		expect(screen.getByText('2 updates remaining')).toBeTruthy();
		expect(onCaughtUp).not.toHaveBeenCalled();
		for (const call of fetch.mock.calls.slice(1)) {
			const url = new URL(String(call[0]), 'http://test');
			expect(url.searchParams.get('initial_seq')).toBe('10');
			expect(url.searchParams.get('initial_epoch')).toBe(target.epoch);
			expect(url.searchParams.get('initial_tail_id')).toBe(target.tail_id);
		}
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(screen.getByText('2 running tasks, 2 agents')).toBeTruthy();
		expect(statusText()).toContain('20 updates remaining');
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(String(fetch.mock.calls.at(-1)![0])).not.toContain('initial_seq');
	});

	it('adopts a replacement checkpoint on clear and discards it on session switch', async () => {
		vi.useFakeTimers();
		const reply = (epoch: string) => new Response(JSON.stringify({ state: 'syncing',
			initial_target: { seq: 4, epoch, tail_id: `${epoch}-tail` }, initial_caught_up: false,
		}));
		const fetch = vi.fn().mockResolvedValueOnce(reply('old'))
			.mockImplementation(() => Promise.resolve(reply('new')));
		vi.stubGlobal('fetch', fetch);
		const props = { sessionId: 'one', onCaughtUp: vi.fn(), children };
		const view = render(MirrorSyncGate, props);
		await vi.advanceTimersByTimeAsync(4000);
		expect(String(fetch.mock.calls[1][0])).toContain('initial_epoch=old');
		expect(String(fetch.mock.calls[2][0])).toContain('initial_epoch=new');
		expect(props.onCaughtUp).not.toHaveBeenCalled();
		await view.rerender({ ...props, sessionId: 'two' });
		await vi.advanceTimersByTimeAsync(0);
		expect(String(fetch.mock.calls.at(-1)![0])).toContain('/two/sync-status');
		expect(String(fetch.mock.calls.at(-1)![0])).not.toContain('initial_seq');
	});

	it('does not label owner-wide operation counts as this chat remaining updates', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
			state: 'syncing', scope: 'owner', pending_updates: 900,
		}))));
		render(MirrorSyncGate, { sessionId: 'new', onCaughtUp: vi.fn(), children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.queryByText('900 updates remaining')).toBeNull();
		expect(screen.getByText('2 running tasks, 2 agents').closest('[aria-hidden="true"]')).not.toBeNull();
	});


	it('holds stale task/agent state, keeps the notice on poll errors, and refreshes on drain', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockResolvedValueOnce(status('syncing', 40))
			.mockRejectedValueOnce(new Error('connection lost')).mockImplementation(() => Promise.resolve(status('current')));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, { sessionId: 'remote-chat', remote: true, deviceName: 'ServerWorker', onCaughtUp, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByRole('status').textContent).toContain('Syncing from ServerWorker');
		expect(screen.getByText('40 updates remaining')).toBeTruthy();
		expect(screen.getByText('2 running tasks, 2 agents').closest('[aria-hidden="true"]')).not.toBeNull();
		await vi.advanceTimersByTimeAsync(2000);
		expect(screen.getByText('40 updates remaining')).toBeTruthy();
		expect(onCaughtUp).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(statusText()).toBe('');
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

	it('does not restart when catch-up reloads the session store without changing its id', async () => {
		vi.useFakeTimers();
		const session = writable({ sessionId: 'remote-chat', revision: 0 });
		const reactiveSession = fromStore(session);
		const fetch = vi.fn().mockImplementation(() => Promise.resolve(status('current')));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn(() => session.update(s => ({ ...s, revision: s.revision + 1 })));
		render(MirrorSyncGate, {
			get sessionId() { return reactiveSession.current.sessionId; },
			remote: true, onCaughtUp, children,
		});
		await vi.advanceTimersByTimeAsync(0);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(statusText()).toBe('');
		await vi.advanceTimersByTimeAsync(6000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(fetch).toHaveBeenCalledTimes(4);
		expect(screen.getByText('2 running tasks, 2 agents')).toBeTruthy();
		// A later backlog never unmounts the conversation, and never reloads it.
		const conversation = screen.getByText('2 running tasks, 2 agents');
		fetch.mockResolvedValueOnce(status('syncing', 3));
		await vi.advanceTimersByTimeAsync(2000);
		expect(screen.getByText('2 running tasks, 2 agents')).toBe(conversation);
		expect(screen.queryByText('Catching up before showing the current conversation, tasks and agents.')).toBeNull();
		await vi.advanceTimersByTimeAsync(2000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(statusText()).toBe('');
		await vi.advanceTimersByTimeAsync(4000);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		expect(fetch).toHaveBeenCalledTimes(8);
	});

	it('reloads only on the first catch-up across current → syncing(5) → current → syncing(3) → current', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn()
			.mockResolvedValueOnce(status('current'))
			.mockResolvedValueOnce(status('syncing', 5))
			.mockResolvedValueOnce(status('current'))
			.mockResolvedValueOnce(status('syncing', 3))
			.mockImplementation(() => Promise.resolve(status('current')));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, { sessionId: 'remote-chat', remote: true, deviceName: 'ServerWorker', onCaughtUp, children });
		await vi.advanceTimersByTimeAsync(0);
		expect(onCaughtUp).toHaveBeenCalledOnce();
		const conversation = screen.getByText('2 running tasks, 2 agents');
		for (let poll = 0; poll < 6; poll++) {
			await vi.advanceTimersByTimeAsync(2000);
			// Each backlog lasts one poll — under the grace period, so no notice.
			expect(statusText()).toBe('');
			expect(screen.getByText('2 running tasks, 2 agents')).toBe(conversation);
		}
		expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(5);
		expect(onCaughtUp).toHaveBeenCalledOnce();
	});

	it('shows the syncing notice only after a backlog outlasts the grace period, without moving the chat', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockResolvedValueOnce(status('current'))
			.mockImplementation(() => Promise.resolve(status('syncing', 12)));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, { sessionId: 'remote-chat', remote: true, deviceName: 'ServerWorker', onCaughtUp, graceMs: 3000, children });
		await vi.advanceTimersByTimeAsync(0);
		const conversation = screen.getByText('2 running tasks, 2 agents');
		await vi.advanceTimersByTimeAsync(2000); // first syncing poll arms the grace timer
		expect(statusText()).toBe('');
		await vi.advanceTimersByTimeAsync(2999);
		expect(statusText()).toBe('');
		await vi.advanceTimersByTimeAsync(1);
		expect(statusText()).toBe('Syncing updates from ServerWorker… 12 updates remaining');
		// The notice floats in its own slot; the conversation node is untouched.
		expect(screen.getByText('2 running tasks, 2 agents')).toBe(conversation);
		expect(screen.getByRole('status').classList.contains('mirror-status-slot')).toBe(true);
		// Back to current: hidden at once, and still no second reload.
		fetch.mockImplementation(() => Promise.resolve(status('current')));
		await vi.advanceTimersByTimeAsync(2000);
		expect(statusText()).toBe('');
		expect(onCaughtUp).toHaveBeenCalledOnce();
	});

	it('keeps an offline owner visible after the chat was current', async () => {
		vi.useFakeTimers();
		const fetch = vi.fn().mockResolvedValueOnce(status('current'))
			.mockImplementation(() => Promise.resolve(status('offline')));
		vi.stubGlobal('fetch', fetch);
		render(MirrorSyncGate, { sessionId: 'remote-chat', remote: true, onCaughtUp: vi.fn(), children });
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(2000);
		expect(statusText()).toBe('Owning worker offline. Showing the last saved conversation.');
		await vi.advanceTimersByTimeAsync(10000);
		expect(statusText()).toBe('Owning worker offline. Showing the last saved conversation.');
	});

	it('renders an unlisted local session once its loaded ownership arrives', async () => {
		vi.useFakeTimers();
		const session = writable<{ sessionId: string; isRemote?: boolean }>({ sessionId: 'local-child' });
		const reactiveSession = fromStore(session);
		const sidebar = fromStore(writable<{ id: string; is_remote: boolean }[]>([]));
		const fetch = vi.fn().mockResolvedValue(status('syncing', 7));
		vi.stubGlobal('fetch', fetch);
		const onCaughtUp = vi.fn();
		render(MirrorSyncGate, {
			get sessionId() { return reactiveSession.current.sessionId; },
			get remote() {
				return sidebar.current.find(s => s.id === reactiveSession.current.sessionId)?.is_remote
					?? reactiveSession.current.isRemote;
			},
			onCaughtUp, children,
		});
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('7 updates remaining')).toBeTruthy();
		session.set({ sessionId: 'local-child', isRemote: false });
		await vi.advanceTimersByTimeAsync(6000);
		expect(screen.queryByRole('status')).toBeNull();
		expect(screen.getByText('2 running tasks, 2 agents')).toBeTruthy();
		expect(fetch).toHaveBeenCalledOnce();
		expect(onCaughtUp).not.toHaveBeenCalled();
	});

	it('checks a session before its sidebar ownership row has arrived', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(status('syncing', 7)));
		render(MirrorSyncGate, { sessionId: 'not-in-sidebar', onCaughtUp: vi.fn(), children });
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByText('7 updates remaining')).toBeTruthy();
		expect(screen.getByText('2 running tasks, 2 agents').closest('[aria-hidden="true"]')).not.toBeNull();
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
