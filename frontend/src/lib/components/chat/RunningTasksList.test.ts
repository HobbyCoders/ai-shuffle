/**
 * Behavior tests for RunningTasksList (#1816): grouping and counts, filter and
 * search, stop one, Stop all with a confirm limited to the filter, a swarm as
 * one expandable row with member stops, and opening agent transcripts. The
 * session, swarm and API modules are mocked; the list reads them, it keeps no
 * copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within, waitFor } from '@testing-library/svelte';
import type { Task } from '$lib/stores/types';
import type { SwarmSnapshot } from '$lib/utils/swarm';

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
			},
			get value() {
				return val;
			}
		};
	}
	return {
		active: store<Record<string, unknown>>({ sessionId: 's1', activeTasks: [], messages: [] }),
		swarmState: store<{ bySession: Record<string, Record<string, unknown>>; errors: Record<string, string> }>({ bySession: {}, errors: {} }),
		deviceName: store<string | null>('DesktopWorker'),
		mobile: false,
		stopSessionTask: vi.fn(),
		cancel: vi.fn(),
		hydrate: vi.fn(async () => {}),
		openSession: vi.fn(),
		notifyError: vi.fn()
	};
});

vi.mock('$lib/stores/active-session', () => ({ activeSession: mocks.active }));
vi.mock('$lib/stores/swarms', () => ({
	swarms: {
		subscribe: mocks.swarmState.subscribe,
		hydrate: mocks.hydrate,
		cancel: mocks.cancel,
		receive: vi.fn(),
		refreshKnown: vi.fn()
	}
}));
vi.mock('$lib/stores/usage-target', () => ({ usageTargetDeviceName: mocks.deviceName }));
vi.mock('$lib/api/sessions', () => ({ stopSessionTask: mocks.stopSessionTask }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: mocks.notifyError } }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession, openSessionLink: vi.fn() }));
vi.mock('$lib/utils/platform', () => ({ buildShell: () => (mocks.mobile ? 'mobile' : 'desktop') }));

import RunningTasksList from './RunningTasksList.svelte';

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

function setTasks(tasks: Task[], messages: unknown[] = []) {
	mocks.active.set({ sessionId: 's1', activeTasks: tasks, messages });
}

const swarm: SwarmSnapshot = {
	id: 'swarm_1',
	session_id: 's1',
	title: 'Release review',
	status: 'running',
	created_at: new Date(Date.now() - 120_000).toISOString(),
	updated_at: new Date().toISOString(),
	tasks: [
		{ id: 'code', title: 'Review code', agent: 'general-purpose', depends_on: [], status: 'running', task_id: 'm1', worker_session_id: 'w-code' },
		{ id: 'report', title: 'Write report', agent: 'Writer', depends_on: ['code'], status: 'queued' }
	]
} as SwarmSnapshot;

function setSwarms(list: SwarmSnapshot[]) {
	mocks.swarmState.set({ bySession: { s1: Object.fromEntries(list.map((s) => [s.id, s])) }, errors: {} });
}

const sheet = () => screen.getByRole('dialog', { name: 'Running tasks' });
const rowOf = (name: string) => screen.getByText(name).closest('li.row') as HTMLElement;

beforeEach(() => {
	mocks.mobile = false;
	setTasks([]);
	setSwarms([]);
	mocks.stopSessionTask.mockReset().mockResolvedValue({ ok: true });
	mocks.cancel.mockReset().mockResolvedValue(undefined);
	mocks.openSession.mockReset();
	mocks.notifyError.mockReset();
});

afterEach(() => cleanup());

describe('RunningTasksList grouping and counts', () => {
	it('renders nothing inline when nothing runs', () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(screen.queryByTestId('tasks-toggle')).toBeNull();
	});

	it('groups Agents and Shell with singular counts', () => {
		setTasks([task('a', { subject: 'Fix login' }), task('b', { kind: 'shell', subject: 'npm test' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		const dialog = sheet();
		expect(within(dialog).getByText('2 running · 1 agent · 1 shell task')).toBeTruthy();
		expect(within(dialog).getByRole('heading', { name: 'Agents 1' })).toBeTruthy();
		expect(within(dialog).getByRole('heading', { name: 'Shell 1' })).toBeTruthy();
		expect(within(dialog).getByRole('button', { name: 'Agents, 1' })).toBeTruthy();
		expect(within(dialog).queryByText(/1 agents/)).toBeNull();
	});

	it('shows the model each subagent runs on, once its task names it (#1827)', async () => {
		setTasks([task('a', { subject: 'Review code' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(rowOf('Review code').querySelector('.model-chip')).toBeNull();
		setTasks([task('a', { subject: 'Review code', model: 'agent · opus' })]);
		await waitFor(() => expect(within(rowOf('Review code')).getByText('Opus')).toBeTruthy());
		expect(within(rowOf('Review code')).getByText('Opus').getAttribute('title')).toBe('Model: agent · opus');
	});

	it('shows elapsed time and the worker on each row', () => {
		setTasks([task('a', { subject: 'Remote job', worker: { name: 'Unraid' } }), task('b', { subject: 'Local job' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(within(rowOf('Remote job')).getByText('Unraid')).toBeTruthy();
		expect(within(rowOf('Local job')).getByText('DesktopWorker')).toBeTruthy();
		expect(within(rowOf('Local job')).getByText(/^1m \d+s$/)).toBeTruthy();
	});

	it('inline on desktop lists the grouped rows under a toggle and caps a long list', async () => {
		setTasks(Array.from({ length: 17 }, (_, i) => task(`t${i}`, { kind: i % 3 ? 'agent' : 'shell' })));
		const { container } = render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('17 running tasks');
		expect(container.querySelector('.inline-body.capped')).not.toBeNull();
		expect(screen.getByRole('button', { name: 'Show all' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
		expect(sheet()).toBeTruthy();
	});

	it('inline on a phone is one summary row that opens the sheet', async () => {
		mocks.mobile = true;
		setTasks([task('a'), task('b', { kind: 'shell' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		const summary = screen.getByRole('button', { name: /2 running tasks: 1 agent · 1 shell task/ });
		expect(screen.queryByText('Task a')).toBeNull();
		await fireEvent.click(summary);
		expect(within(sheet()).getByText('Task a')).toBeTruthy();
		await fireEvent.click(within(sheet()).getByRole('button', { name: 'Close running tasks' }));
		expect(screen.queryByRole('dialog')).toBeNull();
	});
});

describe('RunningTasksList filter and search', () => {
	beforeEach(() => {
		setTasks([task('a', { subject: 'Fix login' }), task('b', { subject: 'Write docs' }), task('c', { kind: 'shell', subject: 'pytest' })]);
	});

	it('filters by chip', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Shell, 1' }));
		expect(screen.queryByText('Fix login')).toBeNull();
		expect(screen.getByText('pytest')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Shell, 1' }).getAttribute('aria-pressed')).toBe('true');
	});

	it('searches and says when nothing matches', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		const input = screen.getByRole('searchbox', { name: 'Search tasks' });
		await fireEvent.input(input, { target: { value: 'login' } });
		expect(screen.getByText('Fix login')).toBeTruthy();
		expect(screen.queryByText('Write docs')).toBeNull();
		await fireEvent.input(input, { target: { value: 'nope' } });
		expect(screen.getByText('No task matches “nope”')).toBeTruthy();
	});
});

describe('RunningTasksList stopping', () => {
	it('draws every row stop as a plain x, the swipe reveal included (as before #1826)', () => {
		setTasks([task('a', { subject: 'Fix login' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		const stop = screen.getByRole('button', { name: 'Stop: Fix login' });
		expect(stop.querySelector('svg.lucide-x')).not.toBeNull();
		const row = stop.closest('li')!;
		expect(row.querySelector('.row-under svg.lucide-x')).not.toBeNull();
		expect(row.querySelector('svg.lucide-square')).toBeNull();
	});

	it('stops one task and keeps it as Stopped with its worker', async () => {
		setTasks([task('a', { subject: 'Remote job', worker: { name: 'Unraid' } }), task('b')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Remote job' }));
		expect(mocks.stopSessionTask).toHaveBeenCalledWith('s1', 'a');
		await waitFor(() => expect(screen.getByRole('status', { name: 'Stopping Remote job' })).toBeTruthy());
		// The next update drops it from the running list.
		setTasks([task('b')]);
		await waitFor(() => expect(within(rowOf('Remote job')).getByText('Stopped')).toBeTruthy());
		expect(within(rowOf('Remote job')).getByText('Unraid')).toBeTruthy();
		expect(within(rowOf('Remote job')).queryByRole('button', { name: /Stop/ })).toBeNull();
	});

	it('re-enables a row whose stop did not take', async () => {
		mocks.stopSessionTask.mockResolvedValue({ ok: false });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await waitFor(() => expect(mocks.notifyError).toHaveBeenCalled());
		expect(screen.getByRole('button', { name: 'Stop: Task a' })).toBeTruthy();
	});

	it('Stop all confirms with the count and stops only the filtered rows', async () => {
		setTasks([task('a'), task('b'), task('c', { kind: 'shell', subject: 'pytest' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Agents, 2' }));
		await fireEvent.click(screen.getByRole('button', { name: /^Stop all/ }));
		const confirm = screen.getByRole('alertdialog');
		expect(confirm.textContent).toContain('Stop 2 agents?');
		expect(mocks.stopSessionTask).not.toHaveBeenCalled();
		await fireEvent.click(within(confirm).getByRole('button', { name: 'Stop 2' }));
		await waitFor(() => expect(mocks.stopSessionTask).toHaveBeenCalledTimes(2));
		expect(mocks.stopSessionTask.mock.calls.map((c) => c[1]).sort()).toEqual(['a', 'b']);
	});

	it('Stop all can be cancelled', async () => {
		setTasks([task('a'), task('b')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: /^Stop all/ }));
		await fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }));
		expect(screen.queryByRole('alertdialog')).toBeNull();
		expect(mocks.stopSessionTask).not.toHaveBeenCalled();
	});
});

describe('RunningTasksList swarms', () => {
	beforeEach(() => {
		setSwarms([swarm]);
		setTasks([task('swarm_1', { subject: 'Release review' }), task('m1', { subject: 'Review code' }), task('x', { kind: 'shell', subject: 'pytest' })]);
	});

	it('shows a swarm as one agent row that expands and collapses', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(screen.getByRole('button', { name: 'Agents, 1' })).toBeTruthy();
		// The member's own task does not show as a separate row.
		expect(screen.queryByText('Review code')).toBeNull();
		const toggle = screen.getByRole('button', { name: /^Swarm Release review, 2 tasks/ });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		const members = screen.getByRole('list', { name: 'Tasks in Release review' });
		expect(within(members).getByText('Review code')).toBeTruthy();
		expect(within(members).getByText('Queued')).toBeTruthy();
		await fireEvent.click(toggle);
		expect(screen.queryByRole('list', { name: 'Tasks in Release review' })).toBeNull();
	});

	it('stops one member through its own task', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: /^Swarm Release review/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Review code' }));
		expect(mocks.stopSessionTask).toHaveBeenCalledWith('s1', 'm1');
		expect(mocks.cancel).not.toHaveBeenCalled();
	});

	it('stops the whole swarm after a confirm', async () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop swarm: Release review' }));
		const confirm = screen.getByRole('alertdialog', { name: 'Stop swarm Release review' });
		expect(confirm.textContent).toContain('2 unfinished tasks');
		expect(mocks.cancel).not.toHaveBeenCalled();
		await fireEvent.click(within(confirm).getByRole('button', { name: 'Stop swarm' }));
		expect(mocks.cancel).toHaveBeenCalledWith('s1', 'swarm_1');
	});

	it('opens a member transcript', async () => {
		const onClose = vi.fn();
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet', onClose });
		await fireEvent.click(screen.getByRole('button', { name: /^Swarm Release review/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Open transcript: Review code' }));
		expect(mocks.openSession).toHaveBeenCalledWith('w-code');
		expect(onClose).toHaveBeenCalled();
	});

	it('keeps finished swarms reviewable in their own section', () => {
		setSwarms([{ ...swarm, id: 'swarm_done', title: 'Old review', status: 'completed' } as SwarmSnapshot]);
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(screen.getByText('Finished swarms')).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^Swarm Old review, / })).toBeNull();
	});
});

describe('RunningTasksList transcripts and closing', () => {
	it('opens an agent transcript from its row', async () => {
		const card = { id: 'card', role: 'assistant', content: '', type: 'subagent', toolId: 'tool-a', agentSessionId: 'child-a' };
		setTasks([task('a', { subject: 'Fix login', toolUseId: 'tool-a' }), task('b', { kind: 'shell', subject: 'pytest' })], [card]);
		const onClose = vi.fn();
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet', onClose });
		expect(screen.queryByRole('button', { name: 'Open transcript: pytest' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Open transcript: Fix login' }));
		expect(mocks.openSession).toHaveBeenCalledWith('child-a');
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('closes on Escape, cancelling a confirm first', async () => {
		setTasks([task('a'), task('b')]);
		const onClose = vi.fn();
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet', onClose });
		await fireEvent.click(screen.getByRole('button', { name: /^Stop all/ }));
		await fireEvent.keyDown(sheet(), { key: 'Escape' });
		expect(screen.queryByRole('alertdialog')).toBeNull();
		expect(onClose).not.toHaveBeenCalled();
		await fireEvent.keyDown(sheet(), { key: 'Escape' });
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('says nothing is running in an empty sheet', () => {
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(within(sheet()).getAllByText('Nothing running').length).toBeGreaterThan(0);
		expect(screen.getByRole('button', { name: 'Stop all' }).hasAttribute('disabled')).toBe(true);
	});
});
