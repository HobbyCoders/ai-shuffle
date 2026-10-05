/**
 * The assistant's background work, as plain lines.
 *
 * Built from what the home's view already receives: its running background
 * tasks (with the worker chat a Task card links to) and the chats it started
 * with StartSession that are still working. No task ids, agent names or model
 * names reach the line; only what the job is about and how long it has run.
 */
import type { ChatMessage, Task } from '$lib/stores/types';

export type BackgroundJobStop =
	| { kind: 'task'; taskId: string }
	| { kind: 'session'; sessionId: string };

export interface BackgroundJob {
	key: string;
	/** What the job is about, in the words it was started with. */
	label: string;
	/** Epoch ms the job started, when known. */
	startedAt: number | null;
	/** The job's own chat, when it has one. */
	sessionId: string | null;
	stop: BackgroundJobStop;
}

/** The session fields this reads; a subset of the API's Session row. */
export interface BackgroundWorkSession {
	id: string;
	title?: string | null;
	remote_runtime_state?: string | null;
	work_started_at?: string | null;
	has_running_tasks?: boolean;
	parked_task_kind?: string | null;
	created_at?: string;
}

export interface BackgroundWorkInput {
	tasks: readonly Task[];
	messages: readonly ChatMessage[];
	sessions: readonly BackgroundWorkSession[];
	/** Sessions streaming on any device (ids). */
	streaming: ReadonlySet<string> | ReadonlyMap<string, unknown>;
}

const START_SESSION = /(^|__)StartSession$/;
const SESSION_ID = /"session_id"\s*:\s*"([^"]+)"/;

function parseTime(value: string | null | undefined): number | null {
	if (!value) return null;
	const ms = Date.parse(value);
	return Number.isNaN(ms) ? null : ms;
}

function startedSessionId(message: ChatMessage): string | null {
	if (message.type !== 'tool_use' || !START_SESSION.test(message.toolName ?? '')) return null;
	if (message.toolStatus === 'error') return null;
	const result: unknown = message.toolResult;
	if (result && typeof result === 'object') {
		const id = (result as { session_id?: unknown }).session_id;
		return typeof id === 'string' && id ? id : null;
	}
	return typeof result === 'string' ? (result.match(SESSION_ID)?.[1] ?? null) : null;
}

function isWorking(session: BackgroundWorkSession, streaming: BackgroundWorkInput['streaming']): boolean {
	return streaming.has(session.id)
		|| session.remote_runtime_state === 'working'
		|| session.remote_runtime_state === 'parked'
		|| !!session.has_running_tasks
		|| !!session.parked_task_kind;
}

function plain(text: string | null | undefined): string {
	return (text ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * The worker chat each Task card points at, keyed by its launch id, agent id
 * and launch tool_use id. A running task's `toolUseId` or `id` finds its chat.
 */
export function linkedTranscripts(messages: readonly ChatMessage[]): Map<string, string> {
	const linked = new Map<string, string>();
	for (const message of messages) {
		if (message.type !== 'subagent' || !message.agentSessionId) continue;
		if (message.toolId) linked.set(message.toolId, message.agentSessionId);
		if (message.agentId) linked.set(message.agentId, message.agentSessionId);
		if (message.agentLaunchToolUseId) linked.set(message.agentLaunchToolUseId, message.agentSessionId);
	}
	return linked;
}

/** The worker chat a background task's Task card links to, if any. */
export function transcriptForTask(task: Pick<Task, 'id' | 'toolUseId'>, linked: ReadonlyMap<string, string>): string | null {
	return (task.toolUseId && linked.get(task.toolUseId)) || linked.get(task.id) || null;
}

export function buildBackgroundJobs({ tasks, messages, sessions, streaming }: BackgroundWorkInput): BackgroundJob[] {
	const linked = linkedTranscripts(messages);
	const started: { id: string; title: string; at: number | null }[] = [];
	for (const message of messages) {
		if (message.type === 'subagent') continue;
		const id = startedSessionId(message);
		if (id) {
			const input = (message.toolInput ?? {}) as { title?: unknown };
			started.push({ id, title: typeof input.title === 'string' ? input.title : '', at: parseTime(message.timestamp) });
		}
	}

	const jobs: BackgroundJob[] = [];
	const seen = new Set<string>();
	for (const task of tasks) {
		if (task.status !== 'running') continue;
		const sessionId = transcriptForTask(task, linked);
		if (sessionId) seen.add(sessionId);
		jobs.push({
			key: `task:${task.id}`,
			label: plain(task.subject) || plain(task.summary) || 'Background job',
			startedAt: parseTime(task.startedAt),
			sessionId,
			stop: { kind: 'task', taskId: task.id }
		});
	}

	const byId = new Map(sessions.map((session) => [session.id, session]));
	for (const chat of started) {
		if (seen.has(chat.id)) continue;
		seen.add(chat.id);
		// A chat missing from the loaded list still counts while it streams.
		const session = byId.get(chat.id);
		if (!(session ? isWorking(session, streaming) : streaming.has(chat.id))) continue;
		jobs.push({
			key: `chat:${chat.id}`,
			label: plain(session?.title) || plain(chat.title) || 'Background job',
			startedAt: parseTime(session?.work_started_at) ?? chat.at ?? parseTime(session?.created_at),
			sessionId: chat.id,
			stop: { kind: 'session', sessionId: chat.id }
		});
	}

	// Longest-running first, so lines keep their place as new work starts.
	return jobs.sort((a, b) => (a.startedAt ?? Infinity) - (b.startedAt ?? Infinity));
}

/** "just started", "6 min", "1 h 5 min". */
export function formatJobAge(startedAt: number | null, now: number): string | null {
	if (startedAt === null) return null;
	const minutes = Math.floor(Math.max(0, now - startedAt) / 60_000);
	if (minutes < 1) return 'just started';
	if (minutes < 60) return `${minutes} min`;
	const hours = Math.floor(minutes / 60);
	const rest = minutes % 60;
	return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
