/** The running-tasks list's grouping, counts, filters and swarm folding (#1816). */
import { describe, it, expect } from 'vitest';
import type { ChatMessage, Task } from '$lib/stores/types';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import {
	buildRows,
	countRows,
	countSummary,
	formatElapsed,
	groupRows,
	plural,
	stopAllQuestion,
	stoppableRows,
	swarmKey,
	taskKey,
	taskWorker,
	visibleRows
} from './running-tasks';

function task(id: string, over: Partial<Task> = {}): Task {
	return {
		id,
		subject: `Task ${id}`,
		status: 'running',
		kind: 'agent',
		summary: null,
		toolUseId: null,
		startedAt: '2026-10-04T10:00:00Z',
		updatedAt: '2026-10-04T10:00:00Z',
		...over
	};
}

function swarm(over: Partial<SwarmSnapshot> = {}): SwarmSnapshot {
	return {
		id: 'swarm_1',
		session_id: 's1',
		title: 'Release review',
		status: 'running',
		created_at: '2026-10-04T09:00:00Z',
		updated_at: '2026-10-04T10:00:00Z',
		tasks: [
			{ id: 'code', title: 'Review code', agent: 'general-purpose', depends_on: [], status: 'running', task_id: 'm1', worker_session_id: 'w-code' },
			{ id: 'report', title: 'Write report', agent: 'Writer', depends_on: ['code'], status: 'queued' }
		],
		...over
	} as SwarmSnapshot;
}

const base = { swarms: [], messages: [], defaultWorker: 'DesktopWorker' };

describe('buildRows', () => {
	it('groups agents and shell tasks and counts each kind', () => {
		const rows = buildRows({ ...base, tasks: [task('a'), task('b', { kind: 'shell', subject: 'npm test' }), task('c')] });
		const groups = groupRows(rows);
		expect(groups.agents.map((r) => r.id)).toEqual(['a', 'c']);
		expect(groups.shell.map((r) => r.id)).toEqual(['b']);
		expect(countRows(rows)).toEqual({ running: 3, agents: 2, shell: 1 });
	});

	it('names the model each subagent runs on and finds rows by it (#1827)', () => {
		const rows = buildRows({
			...base,
			tasks: [
				task('a', { model: 'agent · sonnet (you asked for it)' }),
				task('b'),
				task('c', { kind: 'shell', model: 'agent · sonnet' })
			]
		});
		const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
		expect(byId.a.type === 'task' && byId.a.model?.label).toBe('Sonnet');
		expect(byId.b.type === 'task' && byId.b.model).toBeNull();
		expect(byId.c.type === 'task' && byId.c.model).toBeNull();
		expect(visibleRows(rows, 'all', 'sonnet').map((r) => r.id)).toEqual(['a']);
	});

	it('ignores tasks that are no longer running', () => {
		const rows = buildRows({ ...base, tasks: [task('a'), task('b', { status: 'completed' })] });
		expect(rows.map((r) => r.id)).toEqual(['a']);
	});

	it('names the worker from the task, a stop note, or the chat', () => {
		expect(taskWorker({ worker: { name: 'ServerWorker' }, summary: null }, 'DesktopWorker')).toBe('ServerWorker');
		expect(taskWorker({ summary: 'Stopped by user. Ran on ServerWorker.' }, 'DesktopWorker')).toBe('ServerWorker');
		expect(taskWorker({ summary: null }, 'DesktopWorker')).toBe('DesktopWorker');
	});

	it('links an agent row to its subagent chat through the Task card', () => {
		const card = { id: 'm', role: 'assistant', content: '', type: 'subagent', toolId: 'tool-a', agentSessionId: 'child-a' } as ChatMessage;
		const [row] = buildRows({ ...base, tasks: [task('a', { toolUseId: 'tool-a' })], messages: [card] });
		expect(row.type === 'task' && row.transcriptSessionId).toBe('child-a');
	});

	it('folds a swarm and its members into one agent row', () => {
		const rows = buildRows({
			...base,
			swarms: [swarm()],
			tasks: [task('swarm_1', { subject: 'Release review' }), task('m1', { subject: 'Review code' }), task('x', { kind: 'shell' })]
		});
		expect(rows.map((r) => r.key).sort()).toEqual([swarmKey('swarm_1'), taskKey('x')].sort());
		const row = rows.find((r) => r.type === 'swarm');
		expect(row?.type === 'swarm' && row.members.map((m) => [m.id, m.status, m.taskId, m.transcriptSessionId])).toEqual([
			['code', 'running', 'm1', 'w-code'],
			['report', 'queued', null, null]
		]);
		expect(row?.type === 'swarm' && row.runningMembers).toBe(1);
		// The swarm counts once, as an agent.
		expect(countRows(rows)).toEqual({ running: 2, agents: 1, shell: 1 });
	});

	it('drops a finished swarm whose own task has stopped', () => {
		const rows = buildRows({ ...base, swarms: [swarm({ status: 'completed' })], tasks: [] });
		expect(rows).toEqual([]);
	});

	it('keeps a row the user stopped as Stopped after it leaves the running list', () => {
		const first = buildRows({ ...base, tasks: [task('a', { worker: { name: 'ServerWorker' } })] });
		const stopped = { [taskKey('a')]: { row: first[0], at: Date.parse('2026-10-04T10:05:00Z') } };
		const after = buildRows({ ...base, tasks: [], stopped });
		expect(after).toHaveLength(1);
		expect(after[0].state).toBe('stopped');
		expect(after[0].worker).toBe('ServerWorker');
		expect(countRows(after).running).toBe(0);
	});

	it('marks a row stopping while its stop is in flight', () => {
		const rows = buildRows({ ...base, tasks: [task('a')], stopping: new Set([taskKey('a')]) });
		expect(rows[0].state).toBe('stopping');
		expect(stoppableRows(rows)).toEqual([]);
	});
});

describe('filter and search', () => {
	const rows = buildRows({
		...base,
		swarms: [swarm()],
		tasks: [task('swarm_1'), task('a', { subject: 'Fix flaky login' }), task('b', { kind: 'shell', subject: 'pytest tests/' })]
	});

	it('filters by kind', () => {
		expect(visibleRows(rows, 'agents', '').map((r) => r.id).sort()).toEqual(['a', 'swarm_1']);
		expect(visibleRows(rows, 'shell', '').map((r) => r.id)).toEqual(['b']);
		expect(visibleRows(rows, 'all', '')).toHaveLength(3);
	});

	it('searches names, workers and swarm members', () => {
		expect(visibleRows(rows, 'all', 'LOGIN').map((r) => r.id)).toEqual(['a']);
		expect(visibleRows(rows, 'all', 'write report').map((r) => r.id)).toEqual(['swarm_1']);
		expect(visibleRows(rows, 'all', 'desktopworker')).toHaveLength(3);
		expect(visibleRows(rows, 'shell', 'login')).toEqual([]);
	});
});

describe('copy', () => {
	it('pluralises correctly', () => {
		expect(plural(1, 'agent')).toBe('1 agent');
		expect(plural(2, 'agent')).toBe('2 agents');
		expect(countSummary({ agents: 1, shell: 1 })).toBe('1 agent · 1 shell task');
		expect(countSummary({ agents: 12, shell: 0 })).toBe('12 agents');
		expect(countSummary({ agents: 0, shell: 0 })).toBe('Nothing running');
	});

	it('says how many Stop all stops, in the words of the filter', () => {
		expect(stopAllQuestion(1, 'all')).toBe('Stop 1 task?');
		expect(stopAllQuestion(17, 'all')).toBe('Stop 17 tasks?');
		expect(stopAllQuestion(1, 'agents')).toBe('Stop 1 agent?');
		expect(stopAllQuestion(3, 'shell')).toBe('Stop 3 shell tasks?');
	});

	it('formats elapsed time compactly', () => {
		const t = Date.parse('2026-10-04T10:00:00Z');
		expect(formatElapsed(t, t + 45_000)).toBe('45s');
		expect(formatElapsed(t, t + 80_000)).toBe('1m 20s');
		expect(formatElapsed(t, t + 3_900_000)).toBe('1h 5m');
		expect(formatElapsed(null, t)).toBe('');
	});
});
