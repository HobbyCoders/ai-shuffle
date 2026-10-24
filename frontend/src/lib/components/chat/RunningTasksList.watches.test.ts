/**
 * Watches in the running-tasks panel (#371 step 3): each watch is a row with
 * what it watches, when it last fired and its cost, with Pause / Resume and a
 * confirmed, terminal Stop. Watches are the owner's, so every chat lists all
 * of them; Stop all never touches one. The watches API is mocked; the store
 * between it and the list is the real one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within, waitFor } from '@testing-library/svelte';
import type { Task } from '$lib/stores/types';
import type { WatchRow } from '$lib/api/watches';

const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let val = initial;
		const subs = new Set<(v: T) => void>();
		return {
			subscribe(fn: (v: T) => void) {
				fn(val);
				subs.add(fn);
				return () => subs.delete(fn);
			},
			set(v: T) {
				val = v;
				subs.forEach((f) => f(val));
			}
		};
	}
	return {
		active: store<Record<string, unknown>>({ sessionId: 's1', activeTasks: [], messages: [] }),
		swarmState: store({ bySession: {}, errors: {} }),
		deviceName: store<string | null>('DesktopWorker'),
		mobile: false,
		list: [] as unknown[],
		listWatches: vi.fn(),
		setWatchStatus: vi.fn(),
		stopSessionTask: vi.fn(),
		openSession: vi.fn(),
		notifyError: vi.fn()
	};
});

vi.mock('$lib/services/preferences', () => ({
	preferenceSync: { register: vi.fn(), registerPrefix: vi.fn(), save: vi.fn() }
}));
vi.mock('$lib/stores/active-session', () => ({ activeSession: mocks.active }));
vi.mock('$lib/stores/swarms', () => ({
	swarms: { subscribe: mocks.swarmState.subscribe, hydrate: vi.fn(async () => {}), cancel: vi.fn(), receive: vi.fn(), refreshKnown: vi.fn() }
}));
vi.mock('$lib/stores/usage-target', () => ({ usageTargetDeviceName: mocks.deviceName }));
vi.mock('$lib/api/sessions', () => ({ stopSessionTask: mocks.stopSessionTask }));
vi.mock('$lib/api/watches', () => ({ listWatches: mocks.listWatches, setWatchStatus: mocks.setWatchStatus }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: mocks.notifyError } }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession, openSessionLink: vi.fn() }));
vi.mock('$lib/utils/platform', () => ({ buildShell: () => (mocks.mobile ? 'mobile' : 'desktop') }));

import RunningTasksList from './RunningTasksList.svelte';
import { runningTasksOpen } from '$lib/stores/expansion-preferences';
import { __resetWatches, watches as watchesStore } from '$lib/stores/watches';

function watch(id: string, over: Partial<WatchRow> = {}): WatchRow {
	const status = over.status ?? 'active';
	return {
		id,
		kind: 'watch',
		what: `Watch ${id}`,
		status,
		reason: null,
		last_fired_at: new Date(Date.now() - 5 * 60_000).toISOString(),
		cost_usd: 0.42,
		daily_budget_usd: 1,
		creating_session_id: 's1',
		can_pause: status === 'active',
		can_stop: status !== 'stopped',
		...over
	};
}

function task(id: string, over: Partial<Task> = {}): Task {
	return {
		id,
		subject: `Task ${id}`,
		status: 'running',
		kind: 'agent',
		summary: null,
		toolUseId: null,
		startedAt: new Date(Date.now() - 65_000).toISOString(),
		updatedAt: new Date().toISOString(),
		...over
	};
}

/** What the server returns from now on. */
function serve(list: WatchRow[]) {
	mocks.list = list;
}

const rowOf = (name: string) => screen.getByText(name).closest('li.row') as HTMLElement;
const sheet = () => screen.getByRole('dialog', { name: 'Running tasks' });

beforeEach(() => {
	__resetWatches();
	mocks.mobile = false;
	runningTasksOpen.set(true);
	mocks.active.set({ sessionId: 's1', activeTasks: [], messages: [] });
	serve([]);
	mocks.listWatches.mockReset().mockImplementation(async () => mocks.list);
	mocks.setWatchStatus.mockReset().mockImplementation(async (id: string, status: string) => {
		serve((mocks.list as WatchRow[]).map((w) => (w.id === id ? watch(id, { ...w, status: status as WatchRow['status'], can_pause: status === 'active', can_stop: status !== 'stopped' }) : w)));
		return (mocks.list as WatchRow[]).find((w) => w.id === id);
	});
	mocks.stopSessionTask.mockReset().mockResolvedValue({ ok: true });
	mocks.openSession.mockReset();
	mocks.notifyError.mockReset();
});

afterEach(() => cleanup());

describe('RunningTasksList watches', () => {
	it('shows a watch inline even when no task runs: what, last fired, cost, Pause and Stop', async () => {
		serve([watch('w1', { what: 'New mail from the bank' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		const toggle = await screen.findByTestId('tasks-toggle');
		expect(toggle.textContent).toContain('1 watch');
		expect(toggle.textContent).toContain('Watching for changes');
		const row = rowOf('New mail from the bank');
		// "Fired" is its own span so narrow phones can hide it visually.
		expect(row.querySelector('.meta-time')?.textContent).toBe('Fired 5m ago');
		expect(within(row).getByText('$0.42').getAttribute('title')).toBe('$0.42 spent in total · daily cap $1.00');
		expect(within(row).getByRole('button', { name: 'Pause watch: New mail from the bank' })).toBeTruthy();
		expect(within(row).getByRole('button', { name: 'Stop watch: New mail from the bank' })).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Watches 1' })).toBeTruthy();
	});

	it('says when a watch has not fired yet', async () => {
		serve([watch('w1', { last_fired_at: null, cost_usd: 0 })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await screen.findByText('Not fired yet');
		expect(within(rowOf('Watch w1')).getByText('$0.00')).toBeTruthy();
	});

	it('hides watches that are already stopped', async () => {
		serve([watch('w1', { status: 'stopped' }), watch('w2')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await screen.findByText('Watch w2');
		expect(screen.queryByText('Watch w1')).toBeNull();
	});

	it('pauses and resumes a watch', async () => {
		serve([watch('w1')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Pause watch: Watch w1' }));
		expect(mocks.setWatchStatus).toHaveBeenCalledWith('w1', 'paused');
		const resume = await screen.findByRole('button', { name: 'Resume watch: Watch w1' });
		expect(within(rowOf('Watch w1')).getByText('Paused')).toBeTruthy();
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('All paused');
		await fireEvent.click(resume);
		expect(mocks.setWatchStatus).toHaveBeenLastCalledWith('w1', 'active');
		await screen.findByRole('button', { name: 'Pause watch: Watch w1' });
	});

	it('puts a watch back and says so when a pause does not take', async () => {
		serve([watch('w1')]);
		mocks.setWatchStatus.mockRejectedValueOnce(new Error('offline'));
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Pause watch: Watch w1' }));
		await waitFor(() => expect(mocks.notifyError).toHaveBeenCalledWith("Couldn't pause the watch. Try again."));
		expect(await screen.findByRole('button', { name: 'Pause watch: Watch w1' })).toBeTruthy();
	});

	it('gives the reason a watch paused itself', async () => {
		serve([watch('w1', { status: 'paused', reason: 'It fired 40 times in an hour' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(await screen.findByText('Paused: It fired 40 times in an hour')).toBeTruthy();
	});

	it('stops a watch only after a confirm, then reads Stopped', async () => {
		serve([watch('w1')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Stop watch: Watch w1' }));
		const confirm = screen.getByRole('alertdialog', { name: 'Stop watch Watch w1' });
		await fireEvent.click(within(confirm).getByRole('button', { name: 'Keep' }));
		expect(mocks.setWatchStatus).not.toHaveBeenCalled();
		expect(screen.queryByRole('alertdialog')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: 'Stop watch: Watch w1' }));
		await fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Stop watch' }));
		expect(mocks.setWatchStatus).toHaveBeenCalledWith('w1', 'stopped');
		// The only watch is gone, so the list closes (the next case keeps one up).
		await waitFor(() => expect(screen.queryByTestId('tasks-toggle')).toBeNull());
	});

	it('keeps a stopped watch as Stopped while the list stays open', async () => {
		serve([watch('w1'), watch('w2')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Stop watch: Watch w1' }));
		await fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Stop watch' }));
		await waitFor(() => expect(rowOf('Watch w1').dataset.state).toBe('stopped'));
		expect(within(rowOf('Watch w1')).getByText('Stopped')).toBeTruthy();
		expect(within(rowOf('Watch w1')).queryByRole('button', { name: /Pause|Stop watch/ })).toBeNull();
		expect(screen.getByRole('heading', { name: 'Watches 1' })).toBeTruthy();
	});

	it('Stop all stops the running tasks and never a watch', async () => {
		serve([watch('w1')]);
		mocks.active.set({ sessionId: 's1', activeTasks: [task('a'), task('b', { kind: 'shell' })], messages: [] });
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await screen.findByText('Watch w1');
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('2 running tasks');
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('1 agent · 1 shell task · 1 watch');
		await fireEvent.click(screen.getByRole('button', { name: 'Stop all' }));
		expect(screen.getByText('Stop 2 tasks?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Stop 2' }));
		await waitFor(() => expect(mocks.stopSessionTask).toHaveBeenCalledTimes(2));
		expect(mocks.setWatchStatus).not.toHaveBeenCalled();
	});

	it('opens the chat a watch reports to, when it is another chat', async () => {
		serve([watch('here'), watch('there', { creating_session_id: 's9' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await screen.findByText('Watch there');
		expect(within(rowOf('Watch here')).queryByRole('button', { name: /Open the chat/ })).toBeNull();
		await fireEvent.click(within(rowOf('Watch there')).getByRole('button', { name: 'Open the chat for watch: Watch there' }));
		expect(mocks.openSession).toHaveBeenCalledWith('s9');
	});

	it('the sheet adds a Watches filter that shows only watches, and search finds them', async () => {
		serve([watch('w1', { what: 'CI on main' }), watch('w2', { what: 'Inbox from Alex' })]);
		mocks.active.set({ sessionId: 's1', activeTasks: [task('a', { subject: 'Fix login' })], messages: [] });
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await screen.findByText('CI on main');
		const dialog = sheet();
		expect(within(dialog).getByText('1 running · 1 agent · 2 watches')).toBeTruthy();
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Watches, 2' }));
		expect(within(dialog).queryByText('Fix login')).toBeNull();
		expect(within(dialog).getByText('CI on main')).toBeTruthy();
		// Stop all has nothing it may stop under the Watches filter.
		expect(within(dialog).getByRole('button', { name: 'Stop all' }).hasAttribute('disabled')).toBe(true);
		await fireEvent.input(within(dialog).getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'alex' } });
		expect(within(dialog).queryByText('CI on main')).toBeNull();
		expect(within(dialog).getByText('Inbox from Alex')).toBeTruthy();
	});

	it('the sheet has no Watches filter while there are none', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await waitFor(() => expect(mocks.listWatches).toHaveBeenCalled());
		expect(within(sheet()).queryByRole('button', { name: /^Watches/ })).toBeNull();
	});

	it('a failed first load shows the error and Retry, not an all-clear', async () => {
		mocks.listWatches.mockRejectedValueOnce(new Error('offline'));
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		const warn = await screen.findByTestId('watches-error');
		expect(warn.textContent).toContain('Watches could not be loaded.');
		const dialog = sheet();
		expect(within(dialog).queryByText('Nothing running')).toBeNull();
		expect(within(dialog).getByText('No tasks running. Your watches could not be loaded.')).toBeTruthy();
		expect(within(dialog).getByText('No tasks running · watches unavailable')).toBeTruthy();
		expect(dialog.querySelector('.rt-empty svg')).toBeNull();

		// Retry loads them.
		serve([watch('w1')]);
		await fireEvent.click(within(warn).getByRole('button', { name: 'Retry' }));
		expect(await within(dialog).findByText('Watch w1')).toBeTruthy();
		expect(screen.queryByTestId('watches-error')).toBeNull();
	});

	it('the phone sheet (the mobile shell opens this one) shows the same load error', async () => {
		mocks.mobile = true;
		mocks.listWatches.mockRejectedValueOnce(new Error('offline'));
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect((await screen.findByTestId('watches-error')).textContent).toContain('Watches could not be loaded.');
		expect(within(sheet()).queryByText('Nothing running')).toBeNull();
	});

	it('a confirmed empty list says Nothing running with no error', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await waitFor(() => expect(mocks.listWatches).toHaveBeenCalled());
		const dialog = sheet();
		await waitFor(() => expect(dialog.querySelector('.rt-empty svg')).not.toBeNull());
		expect(within(dialog).getAllByText('Nothing running').length).toBeGreaterThan(0);
		expect(screen.queryByTestId('watches-error')).toBeNull();
	});

	it('a failed refresh keeps the last rows and says they could not be refreshed', async () => {
		serve([watch('w1')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await screen.findByText('Watch w1');
		mocks.listWatches.mockRejectedValueOnce(new Error('offline'));
		await watchesStore.refresh();
		expect((await screen.findByTestId('watches-error')).textContent).toContain('Watches could not be refreshed.');
		expect(screen.getByText('Watch w1')).toBeTruthy();
	});

	it('on a phone a watch alone gets the summary row that opens the list', async () => {
		mocks.mobile = true;
		serve([watch('w1'), watch('w2', { status: 'paused' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		const summary = await screen.findByRole('button', { name: '2 watches: 1 paused. Open the list' });
		await fireEvent.click(summary);
		expect(within(sheet()).getByText('Watch w1')).toBeTruthy();
		expect(within(sheet()).getByText('2 watches · 1 paused')).toBeTruthy();
	});
});
