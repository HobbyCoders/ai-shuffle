/**
 * Running tasks as list rows: what RunningTasksList renders (#1816).
 *
 * Built from what the open chat already holds: its running background tasks
 * (the session store's `activeTasks`), its swarm workflows (the swarms store)
 * and its subagent cards (for each agent's transcript). A swarm is ONE row in
 * the Agents group: its own registry task (id = swarm id) and its members'
 * tasks fold into it instead of showing as separate rows.
 */
import type { ChatMessage, Task } from '$lib/stores/types';
import { isSwarmActive, type SwarmSnapshot, type SwarmTask } from '$lib/utils/swarm';
import { linkedTranscripts, transcriptForTask } from '$lib/utils/background-work';

export type RunFilter = 'all' | 'agents' | 'shell';
export type RowState = 'running' | 'stopping' | 'stopped';

export interface SwarmMemberRow {
	key: string;
	id: string;
	title: string;
	agent: string;
	status: SwarmTask['status'];
	/** Registry task id, which the stop endpoint takes. */
	taskId: string | null;
	transcriptSessionId: string | null;
	startedAt: number | null;
	endedAt: number | null;
}

interface RowBase {
	key: string;
	id: string;
	kind: 'agent' | 'shell';
	name: string;
	worker: string;
	startedAt: number | null;
	/** When the row stopped; freezes its elapsed time. */
	endedAt: number | null;
	state: RowState;
}

export interface TaskRow extends RowBase {
	type: 'task';
	agent: string | null;
	transcriptSessionId: string | null;
}

export interface SwarmRow extends RowBase {
	type: 'swarm';
	kind: 'agent';
	members: SwarmMemberRow[];
	runningMembers: number;
}

export type RunRow = TaskRow | SwarmRow;

/** A row the user stopped from the list, kept so it can read "Stopped". */
export interface StoppedRecord {
	row: RunRow;
	at: number;
}

export interface BuildRowsInput {
	/** Running background tasks (activeTasks). */
	tasks: readonly Task[];
	swarms: readonly SwarmSnapshot[];
	messages: readonly ChatMessage[];
	/** Row keys whose stop is in flight or confirmed but not yet off the list. */
	stopping?: ReadonlySet<string>;
	/** Rows the user stopped, by key. */
	stopped?: Readonly<Record<string, StoppedRecord>>;
	/** The chat's worker, for tasks that run where the chat runs. */
	defaultWorker: string;
}

function time(value: string | null | undefined): number | null {
	if (!value) return null;
	const ms = Date.parse(value);
	return Number.isNaN(ms) ? null : ms;
}

function plain(text: string | null | undefined): string {
	return (text ?? '').replace(/\s+/g, ' ').trim();
}

const RAN_ON = /Ran on ([^.]+)\./;

/** The worker a task runs on: its own label, a stop note's, or the chat's. */
export function taskWorker(task: Pick<Task, 'worker' | 'summary'>, defaultWorker: string): string {
	const named = plain(task.worker?.name);
	if (named) return named;
	const fromNote = task.summary ? RAN_ON.exec(task.summary)?.[1] : undefined;
	return plain(fromNote) || defaultWorker;
}

export const taskKey = (id: string) => `task:${id}`;
export const swarmKey = (id: string) => `swarm:${id}`;

export function buildRows({ tasks, swarms, messages, stopping, stopped, defaultWorker }: BuildRowsInput): RunRow[] {
	const linked = linkedTranscripts(messages);
	const running = tasks.filter((t) => t.status === 'running');
	const byId = new Map(running.map((t) => [t.id, t]));
	const consumed = new Set<string>();
	const rows: RunRow[] = [];
	const seen = new Set<string>();

	// A swarm is a row while it is active, or while its own task still runs.
	for (const swarm of swarms) {
		const own = byId.get(swarm.id);
		if (!isSwarmActive(swarm) && !own) continue;
		const key = swarmKey(swarm.id);
		seen.add(key);
		if (own) consumed.add(own.id);
		const members: SwarmMemberRow[] = swarm.tasks.map((member) => {
			const live = member.task_id ? byId.get(member.task_id) : undefined;
			if (member.task_id) consumed.add(member.task_id);
			return {
				key: `${key}:${member.id}`,
				id: member.id,
				title: plain(member.title) || member.id,
				agent: plain(member.agent),
				status: member.status,
				taskId: member.task_id ?? null,
				transcriptSessionId: member.worker_session_id
					?? (live ? transcriptForTask(live, linked) : null),
				startedAt: time(member.started_at) ?? (live ? time(live.startedAt) : null),
				endedAt: time(member.finished_at)
			};
		});
		const state: RowState = stopping?.has(key) ? 'stopping' : 'running';
		rows.push({
			type: 'swarm',
			key,
			id: swarm.id,
			kind: 'agent',
			name: plain(swarm.title) || 'Swarm',
			worker: own ? taskWorker(own, defaultWorker) : defaultWorker,
			startedAt: time(own?.startedAt) ?? time(swarm.created_at),
			endedAt: null,
			state,
			members,
			runningMembers: members.filter((m) => m.status === 'running').length
		});
	}

	for (const task of running) {
		if (consumed.has(task.id)) continue;
		const key = taskKey(task.id);
		seen.add(key);
		rows.push({
			type: 'task',
			key,
			id: task.id,
			kind: task.kind === 'shell' ? 'shell' : 'agent',
			name: plain(task.subject) || plain(task.summary) || (task.kind === 'shell' ? 'Shell task' : 'Agent task'),
			agent: plain(task.agent) || null,
			worker: taskWorker(task, defaultWorker),
			startedAt: time(task.startedAt),
			endedAt: null,
			state: stopping?.has(key) ? 'stopping' : 'running',
			transcriptSessionId: transcriptForTask(task, linked)
		});
	}

	// Stopped rows the user stopped here, once they left the running list.
	for (const [key, record] of Object.entries(stopped ?? {})) {
		if (seen.has(key)) continue;
		rows.push({ ...record.row, state: 'stopped', endedAt: record.row.endedAt ?? record.at } as RunRow);
	}

	return rows.sort(compareRows);
}

/** Running first, longest-running first (rows keep their place); stopped last. */
function compareRows(a: RunRow, b: RunRow): number {
	const sa = a.state === 'stopped' ? 1 : 0;
	const sb = b.state === 'stopped' ? 1 : 0;
	if (sa !== sb) return sa - sb;
	return (a.startedAt ?? Infinity) - (b.startedAt ?? Infinity);
}

export function matchesFilter(row: RunRow, filter: RunFilter): boolean {
	if (filter === 'all') return true;
	return filter === 'agents' ? row.kind === 'agent' : row.kind === 'shell';
}

export function matchesQuery(row: RunRow, query: string): boolean {
	const q = query.trim().toLowerCase();
	if (!q) return true;
	const parts = [row.name, row.worker];
	if (row.type === 'task' && row.agent) parts.push(row.agent);
	if (row.type === 'swarm') for (const m of row.members) parts.push(m.title, m.agent);
	return parts.join(' ').toLowerCase().includes(q);
}

export function visibleRows(rows: readonly RunRow[], filter: RunFilter, query: string): RunRow[] {
	return rows.filter((row) => matchesFilter(row, filter) && matchesQuery(row, query));
}

export function groupRows(rows: readonly RunRow[]): { agents: RunRow[]; shell: RunRow[] } {
	return {
		agents: rows.filter((r) => r.kind === 'agent'),
		shell: rows.filter((r) => r.kind === 'shell')
	};
}

/** Running counts (stopped rows excluded). A swarm counts once, as an agent. */
export function countRows(rows: readonly RunRow[]): { running: number; agents: number; shell: number } {
	const live = rows.filter((r) => r.state !== 'stopped');
	return {
		running: live.length,
		agents: live.filter((r) => r.kind === 'agent').length,
		shell: live.filter((r) => r.kind === 'shell').length
	};
}

/** "1 agent", "2 agents", "1 shell task". */
export function plural(n: number, noun: string): string {
	return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

/** "2 agents · 1 shell task", omitting a zero side unless both are zero. */
export function countSummary(counts: { agents: number; shell: number }): string {
	const parts: string[] = [];
	if (counts.agents) parts.push(plural(counts.agents, 'agent'));
	if (counts.shell) parts.push(plural(counts.shell, 'shell task'));
	return parts.join(' · ') || 'Nothing running';
}

/** Rows Stop all would stop: the visible ones still running. */
export function stoppableRows(rows: readonly RunRow[]): RunRow[] {
	return rows.filter((r) => r.state === 'running');
}

/** The confirm question, naming how many it stops and what. */
export function stopAllQuestion(n: number, filter: RunFilter): string {
	if (filter === 'agents') return `Stop ${plural(n, 'agent')}?`;
	if (filter === 'shell') return `Stop ${plural(n, 'shell task')}?`;
	return `Stop ${plural(n, 'task')}?`;
}

/** "45s", "1m 20s", "1h 5m". */
export function formatElapsed(startedAt: number | null, end: number): string {
	if (startedAt === null) return '';
	const secs = Math.max(0, Math.floor((end - startedAt) / 1000));
	if (secs < 60) return `${secs}s`;
	const mins = Math.floor(secs / 60);
	const rem = secs % 60;
	if (mins < 60) return rem ? `${mins}m ${rem}s` : `${mins}m`;
	const hours = Math.floor(mins / 60);
	const remMins = mins % 60;
	return remMins ? `${hours}h ${remMins}m` : `${hours}h`;
}

const MEMBER_STATE: Record<SwarmTask['status'], string> = {
	queued: 'Queued',
	blocked: 'Waiting',
	running: 'Running',
	completed: 'Done',
	failed: 'Failed',
	cancelled: 'Stopped',
	interrupted: 'Stopped'
};

export function memberStateLabel(status: SwarmTask['status']): string {
	return MEMBER_STATE[status] ?? status;
}
