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
		preferred: null as string | null,
		installs: [] as unknown[],
		listWatches: vi.fn(),
		setWatchStatus: vi.fn(),
		moveWatch: vi.fn(),
		setWatchPreference: vi.fn(),
		listInstalls: vi.fn(),
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
vi.mock('$lib/api/watches', () => ({
	listWatches: mocks.listWatches,
	setWatchStatus: mocks.setWatchStatus,
	moveWatch: mocks.moveWatch,
	setWatchPreference: mocks.setWatchPreference
}));
vi.mock('$lib/api/installs', () => ({ listInstalls: mocks.listInstalls }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: mocks.notifyError } }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession, openSessionLink: vi.fn() }));
vi.mock('$lib/utils/platform', () => ({ buildShell: () => (mocks.mobile ? 'mobile' : 'desktop') }));

import RunningTasksList from './RunningTasksList.svelte';
import { runningTasksOpen } from '$lib/stores/expansion-preferences';
import { __resetWatches, watches as watchesStore } from '$lib/stores/watches';
import { resetWorkerPresence } from '$lib/stores/worker-presence';
import type { Install } from '$lib/api/installs';

function worker(id: string, name: string, over: Partial<Install> = {}): Install {
	return { id, device_id: id, legacy_instance_id: name, device_name: name, base_url: null, platform: 'linux', version: null, capabilities: {}, last_seen: null, online: true, approved: true, ...over };
}
/** This desktop, an always-on-by-platform Linux server, and a laptop that is off. */
const NETWORK = [
	worker('dev_desktop', 'DesktopWorker', { platform: 'windows', is_self: true }),
	worker('dev_server', 'ServerWorker'),
	worker('dev_laptop', 'LaptopWorker', { platform: 'windows', online: false })
];

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
		placement_device_id: 'dev_desktop',
		worker_id: 'DesktopWorker',
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
	resetWorkerPresence();
	mocks.mobile = false;
	mocks.preferred = null;
	mocks.installs = [NETWORK[0]];
	mocks.listInstalls.mockReset().mockImplementation(async () => ({ installs: mocks.installs, self_instance_id: 'DesktopWorker' }));
	mocks.moveWatch.mockReset();
	mocks.setWatchPreference.mockReset();
	runningTasksOpen.set(true);
	mocks.active.set({ sessionId: 's1', activeTasks: [], messages: [] });
	serve([]);
	mocks.listWatches.mockReset().mockImplementation(async () => ({ watches: mocks.list, preferredWorkerId: mocks.preferred }));
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

	describe('placement (#565)', () => {
		const moveTo = (row: HTMLElement) => within(row).getByRole('button', { name: /^Move watch:/ });

		it('names the worker each watch runs on, with whether it is always on', async () => {
			mocks.installs = NETWORK;
			serve([watch('here'), watch('there', { placement_device_id: 'dev_server', worker_id: 'ServerWorker' })]);
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			await waitFor(() => expect(within(rowOf('Watch there')).getByTestId('watch-worker').textContent).toContain('ServerWorker'));
			expect(within(rowOf('Watch here')).getByTestId('watch-worker').textContent).toContain('DesktopWorker');
			expect(within(rowOf('Watch there')).getByTestId('watch-worker').getAttribute('title')).toBe('Runs on ServerWorker · Server · online');
			expect(within(rowOf('Watch here')).getByTestId('watch-worker').getAttribute('title')).toBe('Runs on DesktopWorker · May sleep · online');
		});

		it('in the sheet each row carries its worker, tier and a Move button', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1', { placement_device_id: 'dev_server', worker_id: 'ServerWorker' })]);
			render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
			const place = await within(sheet()).findByTestId('watch-worker');
			await waitFor(() => expect(place.textContent).toContain('ServerWorker'));
			expect(within(place).getByText('Server')).toBeTruthy();
			expect(moveTo(rowOf('Watch w1'))).toBeTruthy();
		});

		it('a lone worker shows no Move and no placement choice', async () => {
			serve([watch('w1')]);
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			await waitFor(() => expect(within(rowOf('Watch w1')).getByTestId('watch-worker').textContent).toContain('DesktopWorker'));
			expect(within(rowOf('Watch w1')).queryByRole('button', { name: /^Move watch/ })).toBeNull();
			expect(screen.queryByRole('button', { name: /^New watches run on/ })).toBeNull();
		});

		it('a watch on an offline worker is its synced copy: controls wait and say why', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1', { placement_device_id: 'dev_laptop', worker_id: 'LaptopWorker' })]);
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			expect(await screen.findByText(/LaptopWorker is offline\. This is its last synced state/)).toBeTruthy();
			const row = rowOf('Watch w1');
			const pause = within(row).getByRole('button', { name: 'Pause watch: Watch w1' }) as HTMLButtonElement;
			expect(pause.disabled).toBe(true);
			expect(pause.title).toBe('LaptopWorker is offline');
			expect((within(row).getByRole('button', { name: 'Stop watch: Watch w1' }) as HTMLButtonElement).disabled).toBe(true);
			expect((moveTo(row) as HTMLButtonElement).disabled).toBe(true);
			await fireEvent.click(pause);
			expect(mocks.setWatchStatus).not.toHaveBeenCalled();
		});

		it('moves a watch after a confirm: pending, then Moved once the server answers', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1')]);
			let answer!: (v: unknown) => void;
			mocks.moveWatch.mockImplementation(() => new Promise((r) => (answer = r)));
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			await waitFor(() => expect(moveTo(rowOf('Watch w1'))).toBeTruthy());
			await fireEvent.click(moveTo(rowOf('Watch w1')));
			const picker = screen.getByRole('radiogroup', { name: 'Move Watch w1 to' });
			// Every other worker is listed; the offline one says why it cannot be picked.
			const gaming = within(picker).getByRole('radio', { name: /LaptopWorker/ }) as HTMLInputElement;
			expect(gaming.disabled).toBe(true);
			expect(within(picker).getByText('Offline')).toBeTruthy();
			expect(within(picker).queryByRole('radio', { name: /DesktopWorker/ })).toBeNull();
			await fireEvent.click(within(picker).getByRole('radio', { name: /ServerWorker/ }));
			await fireEvent.click(screen.getByRole('button', { name: 'Move to ServerWorker' }));
			expect(mocks.moveWatch).toHaveBeenCalledWith('w1', 'dev_server', false);
			const pending = await screen.findByTestId('watch-move');
			expect(pending.textContent).toContain('Moving to ServerWorker');
			expect(pending.textContent).not.toContain('Moved');
			// While it moves, nothing else may act on the watch.
			expect((within(rowOf('Watch w1')).getByRole('button', { name: 'Pause watch: Watch w1' }) as HTMLButtonElement).disabled).toBe(true);
			serve([watch('w1', { placement_device_id: 'dev_server', worker_id: 'ServerWorker' })]);
			answer(mocks.list[0]);
			await waitFor(() => expect(screen.getByTestId('watch-move').textContent).toContain('Moved to ServerWorker.'));
			await waitFor(() => expect(within(rowOf('Watch w1')).getByTestId('watch-worker').textContent).toContain('ServerWorker'));
		});

		it('an unanswered move is not confirmed and offers the same move again', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1')]);
			mocks.moveWatch.mockRejectedValueOnce({ status: 503, detail: 'Watch worker is unavailable; retry the same operation' });
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			await waitFor(() => expect(moveTo(rowOf('Watch w1'))).toBeTruthy());
			await fireEvent.click(moveTo(rowOf('Watch w1')));
			await fireEvent.click(screen.getByRole('radio', { name: /ServerWorker/ }));
			await fireEvent.click(screen.getByRole('button', { name: 'Move to ServerWorker' }));
			const note = await screen.findByText(/Move to ServerWorker not confirmed\./);
			const box = note.closest('[data-testid="watch-move"]') as HTMLElement;
			expect(box.classList.contains('warn')).toBe(true);
			expect(box.textContent).not.toMatch(/Moved to/);
			mocks.moveWatch.mockResolvedValueOnce(watch('w1'));
			await fireEvent.click(within(box).getByRole('button', { name: 'Retry move' }));
			expect(mocks.moveWatch).toHaveBeenCalledTimes(2);
			expect(mocks.moveWatch).toHaveBeenLastCalledWith('w1', 'dev_server', false);
		});

		it('an unanswered move later listed on its target is re-sent, and reads Moved only on that answer', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1')]);
			// The destination imported it but the answer was lost; the next listing shows it there.
			mocks.moveWatch.mockImplementationOnce(async () => {
				serve([watch('w1', { placement_device_id: 'dev_server', worker_id: 'ServerWorker' })]);
				throw { status: 503, detail: 'Watch worker is unavailable; retry the same operation' };
			});
			let answer!: (v: unknown) => void;
			mocks.moveWatch.mockImplementationOnce(() => new Promise((r) => (answer = r)));
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			await waitFor(() => expect(moveTo(rowOf('Watch w1'))).toBeTruthy());
			await fireEvent.click(moveTo(rowOf('Watch w1')));
			await fireEvent.click(screen.getByRole('radio', { name: /^ServerWorker/ }));
			await fireEvent.click(screen.getByRole('button', { name: 'Move to ServerWorker' }));
			// A confirm-only move goes by itself, so the old worker's receipt is closed.
			await waitFor(() => expect(mocks.moveWatch).toHaveBeenCalledTimes(2));
			expect(mocks.moveWatch).toHaveBeenLastCalledWith('w1', 'dev_server', true);
			const note = screen.getByTestId('watch-move');
			expect(note.textContent).toContain('Confirming the move to ServerWorker');
			expect(note.textContent).not.toContain('Moved');
			answer(mocks.list[0]);
			await waitFor(() => expect(screen.getByTestId('watch-move').textContent).toContain('Moved to ServerWorker.'));
			expect(mocks.moveWatch).toHaveBeenCalledTimes(2);
		});

		it('with no watches, a chosen worker that went offline can still be changed', async () => {
			mocks.installs = NETWORK;
			mocks.preferred = 'dev_laptop';
			mocks.setWatchPreference.mockImplementation(async (id: string | null) => id);
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			const toggle = await screen.findByTestId('tasks-toggle');
			await waitFor(() => expect(toggle.textContent).toContain('New watches: LaptopWorker (offline)'));
			expect(toggle.textContent).toContain('Watches');
			await fireEvent.click(screen.getByRole('button', { name: 'New watches run on: LaptopWorker (offline). Change' }));
			expect(screen.getByText(/LaptopWorker is offline\. New watches are refused/)).toBeTruthy();
			await fireEvent.click(screen.getByRole('radio', { name: /^Automatic/ }));
			expect(mocks.setWatchPreference).toHaveBeenCalledWith(null);
			// Cleared, nothing runs and no watch is live: the list goes away again.
			await waitFor(() => expect(screen.queryByTestId('tasks-toggle')).toBeNull());
		});

		it('the phone sheet opened with only a chosen worker starts on Watches with the control', async () => {
			mocks.mobile = true;
			mocks.installs = NETWORK;
			mocks.preferred = 'dev_server';
			render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
			const dialog = sheet();
			await waitFor(() => expect(within(dialog).getByRole('button', { name: /^Watches, 0/ }).getAttribute('aria-pressed')).toBe('true'));
			expect(await within(dialog).findByRole('button', { name: 'New watches run on: ServerWorker. Change' })).toBeTruthy();
		});

		it('with two workers and no watches yet, the sheet offers Watches and where new ones run', async () => {
			mocks.installs = NETWORK;
			render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
			const dialog = sheet();
			const chip = await within(dialog).findByRole('button', { name: /^Watches, 0/ });
			await fireEvent.click(chip);
			await fireEvent.click(await within(dialog).findByRole('button', { name: 'New watches run on: Automatic · ServerWorker. Change' }));
			expect(within(dialog).getByRole('radiogroup', { name: 'Where new watches run' })).toBeTruthy();
		});

		it('a refused move gives the reason and Try again', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1')]);
			mocks.moveWatch.mockRejectedValueOnce({ status: 409, detail: 'Telegram watches cannot move: receive journals are worker-local' });
			render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
			await waitFor(() => expect(moveTo(rowOf('Watch w1'))).toBeTruthy());
			await fireEvent.click(moveTo(rowOf('Watch w1')));
			await fireEvent.click(screen.getByRole('radio', { name: /ServerWorker/ }));
			await fireEvent.click(screen.getByRole('button', { name: 'Move to ServerWorker' }));
			const alert = await screen.findByRole('alert');
			expect(alert.textContent).toContain("Couldn't move to ServerWorker.");
			expect(alert.textContent).toContain('Telegram watches cannot move');
			expect(within(alert).getByRole('button', { name: 'Try again' })).toBeTruthy();
			await fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }));
			expect(screen.queryByRole('alert')).toBeNull();
		});

		it('a watch fenced mid-move says so after a reload, with no Pause or Stop, and retries the move', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1', { status: 'paused', reason: 'Transfer pending', worker_id: 'transfer:dev_server' })]);
			mocks.moveWatch.mockResolvedValueOnce(watch('w1'));
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			const note = await screen.findByText('Move to ServerWorker unfinished.');
			const box = note.closest('[data-testid="watch-move"]') as HTMLElement;
			expect(box.textContent).toContain('runs nowhere until the move completes');
			const row = rowOf('Watch w1');
			expect(within(row).getByText('Moving')).toBeTruthy();
			expect(within(row).queryByText('Paused: Transfer pending')).toBeNull();
			expect(within(row).queryByRole('button', { name: /Resume watch|Pause watch|Stop watch|^Move watch/ })).toBeNull();
			await fireEvent.click(within(box).getByRole('button', { name: 'Retry move' }));
			expect(mocks.moveWatch).toHaveBeenCalledWith('w1', 'dev_server', false);
		});

		it('says where new watches run and changes it; an offline worker cannot be picked', async () => {
			mocks.installs = NETWORK;
			serve([watch('w1')]);
			mocks.setWatchPreference.mockImplementation(async (id: string | null) => id);
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			const head = await screen.findByRole('button', { name: 'New watches run on: Automatic · ServerWorker. Change' });
			await fireEvent.click(head);
			const group = screen.getByRole('radiogroup', { name: 'Where new watches run' });
			expect((within(group).getByRole('radio', { name: /Automatic/ }) as HTMLInputElement).checked).toBe(true);
			expect(within(group).getByText(/now ServerWorker/)).toBeTruthy();
			const gaming = within(group).getByRole('radio', { name: /LaptopWorker/ }) as HTMLInputElement;
			expect(gaming.disabled).toBe(true);
			expect(within(group).getByText(/Offline: cannot be chosen until it is back/)).toBeTruthy();
			await fireEvent.click(within(group).getByRole('radio', { name: /DesktopWorker/ }));
			expect(mocks.setWatchPreference).toHaveBeenCalledWith('dev_desktop');
			expect(await screen.findByRole('button', { name: 'New watches run on: DesktopWorker. Change' })).toBeTruthy();
			expect(screen.queryByRole('radiogroup', { name: 'Where new watches run' })).toBeNull();
		});

		it('a preferred worker that is offline is a stated problem, not a silent fallback', async () => {
			mocks.installs = NETWORK;
			mocks.preferred = 'dev_laptop';
			serve([watch('w1')]);
			mocks.setWatchPreference.mockRejectedValueOnce({ status: 409, detail: 'Preferred watch worker is not an active network member' });
			render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
			const head = await screen.findByRole('button', { name: 'New watches run on: LaptopWorker (offline). Change' });
			await fireEvent.click(head);
			expect(screen.getByText(/LaptopWorker is offline\. New watches are refused until it is back/)).toBeTruthy();
			await fireEvent.click(screen.getByRole('radio', { name: /^ServerWorker/ }));
			expect(await screen.findByText('Preferred watch worker is not an active network member')).toBeTruthy();
			expect(screen.getByRole('radiogroup', { name: 'Where new watches run' })).toBeTruthy();
		});
	});
});
