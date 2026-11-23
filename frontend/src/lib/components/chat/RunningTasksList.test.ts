/**
 * Behavior tests for RunningTasksList (#1816): grouping and counts, filter and
 * search, stop one, Stop all with a confirm limited to the filter, a swarm as
 * one expandable row with member stops, and opening agent transcripts. The
 * session, swarm and API modules are mocked; the list reads them, it keeps no
 * copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tick } from 'svelte';
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
		sessions: store<Record<string, unknown>[]>([]),
		streaming: store<Set<string>>(new Set()),
		mobile: false,
		stopSessionTask: vi.fn(),
		cancelLimitResume: vi.fn(),
		stopSession: vi.fn(),
		cancel: vi.fn(),
		hydrate: vi.fn(async () => {}),
		openSession: vi.fn(),
		notifyError: vi.fn(),
		notifyBanner: vi.fn(),
		savePreference: vi.fn()
	};
});

// The collapse is an account preference: the real persisted store, with the
// sync engine replaced so a save is observable and nothing hits the network.
vi.mock('$lib/services/preferences', () => ({
	preferenceSync: { register: vi.fn(), registerPrefix: vi.fn(), save: mocks.savePreference }
}));

vi.mock('$lib/stores/active-session', () => ({ activeSession: mocks.active, streamingSessions: mocks.streaming }));
vi.mock('$lib/stores/app-data.svelte', () => ({ sessions: mocks.sessions }));
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
vi.mock('$lib/api/sessions', () => ({ stopSessionTask: mocks.stopSessionTask, stopSession: mocks.stopSession }));
vi.mock('$lib/api/limit-resume', () => ({ cancelLimitResume: mocks.cancelLimitResume }));
// No watches here (RunningTasksList.watches.test.ts covers them).
vi.mock('$lib/api/watches', () => ({
	listWatches: vi.fn(async () => ({ watches: [], preferredWorkerId: null })),
	setWatchStatus: vi.fn(),
	moveWatch: vi.fn(),
	setWatchPreference: vi.fn()
}));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: mocks.notifyError, banner: mocks.notifyBanner } }));
/** A stop failure is an on-page error banner that also stays in the bell. */
const stopFailedWith = (message: string) =>
	expect(mocks.notifyBanner).toHaveBeenCalledWith(message, expect.objectContaining({ severity: 'error', persist: true }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession, openSessionLink: vi.fn() }));
vi.mock('$lib/utils/platform', () => ({ buildShell: () => (mocks.mobile ? 'mobile' : 'desktop') }));

import RunningTasksList from './RunningTasksList.svelte';
import { runningTasksOpen } from '$lib/stores/expansion-preferences';

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
	runningTasksOpen.set(true);
	mocks.savePreference.mockReset();
	setTasks([]);
	setSwarms([]);
	mocks.sessions.set([]);
	mocks.streaming.set(new Set());
	mocks.stopSession.mockReset().mockResolvedValue({ status: 'stopped', session_id: 'chat-a' });
	mocks.stopSessionTask.mockReset().mockResolvedValue({ ok: true });
	mocks.cancel.mockReset().mockResolvedValue(undefined);
	mocks.openSession.mockReset();
	mocks.notifyError.mockReset();
	mocks.notifyBanner.mockReset();
});

afterEach(() => cleanup());

function startSession(id: string, sessionId: string, title: string) {
	return {
		id, role: 'assistant', content: '', type: 'tool_use', toolName: 'mcp__fluid_bundled__mcp__builtins__StartSession',
		toolInput: { title, prompt: 'go' }, toolStatus: 'complete',
		toolResult: JSON.stringify({ session_id: sessionId, status: 'queued' }), timestamp: new Date(Date.now() - 90_000).toISOString()
	};
}

describe('RunningTasksList chats this one started', () => {
	it('lists a working started chat in Agents with its own icon, opens it, and stops it', async () => {
		setTasks([task('a', { subject: 'Fix login' })], [startSession('m1', 'chat-a', 'Clean worktrees'), startSession('m2', 'chat-idle', 'Done already')]);
		mocks.sessions.set([
			{ id: 'chat-a', title: 'Clean up old worktrees', remote_runtime_state: 'working', device_name: 'OfficeWorker' },
			{ id: 'chat-idle', title: 'Done already', remote_runtime_state: 'idle' }
		]);
		const { container } = render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('1 agent · 1 chat');
		const row = rowOf('Clean up old worktrees');
		expect(row.closest('section')?.querySelector('h3')?.textContent).toContain('Agents');
		expect(within(row).getByText('OfficeWorker')).toBeTruthy();
		expect(screen.queryByText('Done already')).toBeNull();
		// A chat glyph, not the subagent's bot.
		const icon = row.querySelector('.row-icon svg');
		expect(icon?.getAttribute('class')).toContain('messages-square');
		expect(rowOf('Fix login').querySelector('.row-icon svg')?.getAttribute('class')).not.toContain('messages-square');
		expect(container.querySelectorAll('li.row')).toHaveLength(2);

		await fireEvent.click(within(row).getByRole('button', { name: 'Open chat: Clean up old worktrees' }));
		expect(mocks.openSession).toHaveBeenCalledWith('chat-a');

		await fireEvent.click(within(row).getByRole('button', { name: 'Stop: Clean up old worktrees' }));
		await waitFor(() => expect(mocks.stopSession).toHaveBeenCalledWith('chat-a'));
		expect(mocks.stopSessionTask).not.toHaveBeenCalled();
	});

	it("names why a started chat did not stop, in the server's words", async () => {
		setTasks([], [startSession('m1', 'chat-a', 'Clean worktrees')]);
		mocks.sessions.set([{ id: 'chat-a', title: 'Clean worktrees', remote_runtime_state: 'working', device_name: 'OfficeWorker' }]);
		mocks.stopSession.mockRejectedValue({ status: 503, detail: 'The worker that runs this chat is offline (no route).' });
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(within(rowOf('Clean worktrees')).getByRole('button', { name: 'Stop: Clean worktrees' }));
		await waitFor(() => stopFailedWith("Couldn't stop the chat. The worker that runs this chat is offline (no route)."));
		expect(within(rowOf('Clean worktrees')).getByRole('button', { name: 'Stop: Clean worktrees' })).toBeTruthy();
	});

	it('reports a stopped chat whose background cleanup was incomplete', async () => {
		setTasks([], [startSession('m1', 'chat-a', 'Clean worktrees')]);
		mocks.sessions.set([{ id: 'chat-a', title: 'Clean worktrees', remote_runtime_state: 'working' }]);
		mocks.stopSession.mockResolvedValue({ status: 'stopped', session_id: 'chat-a', incomplete: true, message: 'One task may still run.' });
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(within(rowOf('Clean worktrees')).getByRole('button', { name: 'Stop: Clean worktrees' }));
		await waitFor(() => stopFailedWith('One task may still run.'));
	});

	it('shows a streaming started chat missing from the session list, by its StartSession title', () => {
		setTasks([], [startSession('m1', 'chat-x', 'Watch the release checks')]);
		mocks.streaming.set(new Set(['chat-x']));
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('1 running task');
		expect(rowOf('Watch the release checks')).toBeTruthy();
		// Its worker is unknown: no "Runs on" guess with the parent's worker.
		expect(rowOf('Watch the release checks').querySelector('.meta-worker')).toBeNull();
	});

	it('offers Stop again when a stopped started chat runs another turn', async () => {
		setTasks([], [startSession('m1', 'chat-a', 'Clean worktrees')]);
		mocks.sessions.set([{ id: 'chat-a', title: 'Clean worktrees', remote_runtime_state: 'working' }]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		await fireEvent.click(within(rowOf('Clean worktrees')).getByRole('button', { name: 'Stop: Clean worktrees' }));
		await waitFor(() => expect(mocks.stopSession).toHaveBeenCalledWith('chat-a'));
		// The stop lands before the idle update; then it is the only row and leaves.
		mocks.sessions.set([{ id: 'chat-a', title: 'Clean worktrees', remote_runtime_state: 'idle' }]);
		await tick();
		expect(screen.queryByText('Clean worktrees')).toBeNull();
		// Another tab sends it a new turn.
		mocks.sessions.set([{ id: 'chat-a', title: 'Clean worktrees', remote_runtime_state: 'working' }]);
		await tick();
		expect(within(rowOf('Clean worktrees')).getByRole('button', { name: 'Stop: Clean worktrees' })).toBeTruthy();
		expect(screen.queryByRole('status', { name: 'Stopping Clean worktrees' })).toBeNull();
	});

	it('does not list a started chat twice when a subagent row already opens it', () => {
		const card = { id: 'card', role: 'assistant', content: '', type: 'subagent', agentId: 'a', agentSessionId: 'chat-a' };
		setTasks([task('a', { subject: 'Fix login' })], [card, startSession('m1', 'chat-a', 'Fix login chat')]);
		mocks.streaming.set(new Set(['chat-a']));
		const { container } = render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(container.querySelectorAll('li.row')).toHaveLength(1);
		expect(screen.queryByText('Fix login chat')).toBeNull();
	});
});

describe('RunningTasksList grouping and counts', () => {
	it('renders the list as a page for a host that draws its own title and Back', () => {
		setTasks([task('a', { subject: 'Fix login' })]);
		const onClose = vi.fn();
		const { container } = render(RunningTasksList, { sessionId: 's1', variant: 'page', onClose });
		// No modal of its own: no dialog, scrim, heading or close button.
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(container.querySelector('.rt-page')).not.toBeNull();
		expect(screen.queryByRole('heading', { name: 'Running tasks' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Close running tasks' })).toBeNull();
		expect(screen.getByText('1 running · 1 agent')).toBeTruthy();
		expect(screen.getByRole('searchbox', { name: 'Search tasks' })).toBeTruthy();
		expect(rowOf('Fix login')).toBeTruthy();
	});

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
		setTasks([task('a', { subject: 'Remote job', worker: { name: 'ServerWorker' } }), task('b', { subject: 'Local job' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		expect(within(rowOf('Remote job')).getByText('ServerWorker')).toBeTruthy();
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

	it('remembers a collapsed list across a chat switch, a remount and tasks coming and going', async () => {
		setTasks([task('a'), task('b', { kind: 'shell' })]);
		const view = render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		const toggle = () => screen.getByTestId('tasks-toggle');
		expect(toggle().getAttribute('aria-expanded')).toBe('true');
		await fireEvent.click(toggle());
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		expect(mocks.savePreference).toHaveBeenLastCalledWith('ui:running_tasks_open', false);

		// A task starts, another ends: still collapsed, and the count follows.
		setTasks([task('a'), task('b', { kind: 'shell' }), task('c')]);
		await tick();
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		expect(toggle().textContent).toContain('3 running tasks');
		setTasks([task('c')]);
		await tick();
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		expect(view.container.querySelector('.inline-body')).toBeNull();

		// Nothing runs, then a new task: the list comes back collapsed.
		setTasks([]);
		await tick();
		expect(screen.queryByTestId('tasks-toggle')).toBeNull();
		setTasks([task('d')]);
		await tick();
		expect(toggle().getAttribute('aria-expanded')).toBe('false');

		// Another chat (same component) and a fresh mount keep the choice.
		mocks.active.set({ sessionId: 's2', activeTasks: [task('e')], messages: [] });
		await view.rerender({ sessionId: 's2', variant: 'inline' });
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		view.unmount();
		render(RunningTasksList, { sessionId: 's2', variant: 'inline' });
		expect(toggle().getAttribute('aria-expanded')).toBe('false');

		// Opening it again is remembered the same way.
		await fireEvent.click(toggle());
		expect(toggle().getAttribute('aria-expanded')).toBe('true');
		expect(runningTasksOpen.current).toBe(true);
		expect(mocks.savePreference).toHaveBeenLastCalledWith('ui:running_tasks_open', true);
	});

	it('follows the choice when another device changes it', async () => {
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		runningTasksOpen.set(false);
		await tick();
		expect(screen.getByTestId('tasks-toggle').getAttribute('aria-expanded')).toBe('false');
	});

	it('inline on a phone is the same collapsible list as desktop, with no Open card', async () => {
		mocks.mobile = true;
		setTasks([task('a'), task('b', { kind: 'shell' })]);
		const { container } = render(RunningTasksList, { sessionId: 's1', variant: 'inline' });
		expect(screen.queryByRole('button', { name: /Open the list/ })).toBeNull();
		expect(container.querySelector('.inline.touch')).not.toBeNull();
		const toggle = screen.getByTestId('tasks-toggle');
		expect(toggle.textContent).toContain('2 running tasks');
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(screen.getByText('Task a')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Stop all' })).toBeTruthy();
		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByText('Task a')).toBeNull();
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

	it.each([
		// 35% of a phone row, as before; a row as wide as a foldable's inner
		// screen arms at 160px, not 35% of its 800px.
		['a phone-width row', 360, 120, false],
		['a phone-width row', 360, 130, true],
		['a wide row', 800, 155, false],
		['a wide row', 800, 165, true],
	])('a left swipe on %s (%ipx) of %ipx stops the task: %s', async (_label, width, travel, stops) => {
		setTasks([task('a', { subject: 'Fix login' })]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		const inner = rowOf('Fix login').querySelector('.row-inner') as HTMLElement;
		Object.defineProperty(inner, 'offsetWidth', { configurable: true, value: width });
		const at = (clientX: number) => ({ pointerId: 1, isPrimary: true, button: 0, pointerType: 'touch', clientX, clientY: 20 });
		await fireEvent.pointerDown(inner, at(700));
		await fireEvent.pointerMove(inner, at(690));
		await fireEvent.pointerMove(inner, at(700 - travel));
		expect(rowOf('Fix login').classList.contains('armed')).toBe(stops);
		await fireEvent.pointerUp(inner, at(700 - travel));
		if (stops) expect(mocks.stopSessionTask).toHaveBeenCalledWith('s1', 'a');
		else expect(mocks.stopSessionTask).not.toHaveBeenCalled();
	});

	it('stops one task and keeps it as Stopped with its worker', async () => {
		setTasks([task('a', { subject: 'Remote job', worker: { name: 'ServerWorker' } }), task('b')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Remote job' }));
		expect(mocks.stopSessionTask).toHaveBeenCalledWith('s1', 'a');
		await waitFor(() => expect(screen.getByRole('status', { name: 'Stopping Remote job' })).toBeTruthy());
		// The next update drops it from the running list.
		setTasks([task('b')]);
		await waitFor(() => expect(within(rowOf('Remote job')).getByText('Stopped')).toBeTruthy());
		expect(within(rowOf('Remote job')).getByText('ServerWorker')).toBeTruthy();
		expect(within(rowOf('Remote job')).queryByRole('button', { name: /Stop/ })).toBeNull();
	});

	it('re-enables a row whose stop did not take', async () => {
		mocks.stopSessionTask.mockResolvedValue({ ok: false });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await waitFor(() => stopFailedWith("Couldn't stop the task. It may have already finished."));
		expect(mocks.notifyError).not.toHaveBeenCalled();
		expect(screen.getByRole('button', { name: 'Stop: Task a' })).toBeTruthy();
	});

	it("says why, in the server's words, when the chat's worker could not stop it", async () => {
		const message = "Couldn't stop the task. Couldn't reach ServerWorker from Laptop (connection refused).";
		mocks.stopSessionTask.mockResolvedValue({ ok: false, message });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await waitFor(() => stopFailedWith(message));
		expect(screen.getByRole('button', { name: 'Stop: Task a' })).toBeTruthy();
	});

	it('shows a stop failure inside the open sheet, which covers the banner, until a retry', async () => {
		const message = "Couldn't stop the task. The worker running this chat did not answer in time.";
		mocks.stopSessionTask.mockResolvedValue({ ok: false, message });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		const alert = await screen.findByTestId('sheet-stop-error');
		expect(screen.getByRole('dialog').contains(alert)).toBe(true);
		expect(alert.getAttribute('role')).toBe('alert');
		expect(alert.textContent).toContain(message);
		expect(mocks.notifyBanner).toHaveBeenCalledTimes(1);
		mocks.stopSessionTask.mockResolvedValue({ ok: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await waitFor(() => expect(screen.queryByTestId('sheet-stop-error')).toBeNull());
	});

	it('leaves a stop failure on the controls page to the banner, which shows above it', async () => {
		mocks.stopSessionTask.mockResolvedValue({ ok: false });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'page' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await waitFor(() => stopFailedWith("Couldn't stop the task. It may have already finished."));
		expect(screen.queryByTestId('sheet-stop-error')).toBeNull();
	});

	it('dismisses the in-sheet stop failure', async () => {
		mocks.stopSessionTask.mockResolvedValue({ ok: false });
		setTasks([task('a')]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		await screen.findByTestId('sheet-stop-error');
		const dismiss = screen.getByRole('button', { name: 'Dismiss' });
		dismiss.focus();
		await fireEvent.click(dismiss);
		expect(screen.queryByTestId('sheet-stop-error')).toBeNull();
		expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true);
	});

	it("keeps a late stop failure out of another chat's sheet", async () => {
		let finish!: (value: { ok: boolean }) => void;
		mocks.stopSessionTask.mockReturnValue(new Promise((resolve) => (finish = resolve)));
		setTasks([task('a')]);
		const view = render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Stop: Task a' }));
		mocks.active.set({ sessionId: 's2', activeTasks: [task('b')], messages: [] });
		await view.rerender({ sessionId: 's2', variant: 'sheet' });
		finish({ ok: false });
		await waitFor(() => expect(mocks.notifyBanner).toHaveBeenCalledTimes(1));
		expect(screen.queryByTestId('sheet-stop-error')).toBeNull();
	});

	it("says why a paused task could not be stopped when the chat's worker explains", async () => {
		const detail = "Couldn't reach ServerWorker from Laptop (connection refused).";
		mocks.cancelLimitResume.mockRejectedValue({ status: 409, detail });
		const pause = { id: 'p1', role: 'system', type: 'system', content: '', systemSubtype: 'limit_resume',
			systemData: { status: 'scheduled', resume_at: 2_000_000_000, task_id: 'a' } };
		setTasks([task('a')], [pause]);
		render(RunningTasksList, { sessionId: 's1', variant: 'sheet' });
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel: Task a' }));
		expect(mocks.cancelLimitResume).toHaveBeenCalledWith('s1', 'a');
		expect(mocks.stopSessionTask).not.toHaveBeenCalled();
		await waitFor(() => stopFailedWith(`Couldn't stop the task. ${detail}`));
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
