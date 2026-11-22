/**
 * A chat's background work beyond its own tasks.
 *
 * Built from what the chat's view already receives: the worker chat each Task
 * card links to, and the chats it started with StartSession that are still
 * working. The running-tasks list shows both beside its subagents and swarms.
 */
import type { ChatMessage, Task } from '$lib/stores/types';

/** The session fields this reads; a subset of the API's Session row. */
export interface BackgroundWorkSession {
	id: string;
	title?: string | null;
	remote_runtime_state?: string | null;
	work_started_at?: string | null;
	has_running_tasks?: boolean;
	parked_task_kind?: string | null;
	created_at?: string;
	device_name?: string | null;
}

export interface StartedChatsInput {
	messages: readonly ChatMessage[];
	sessions: readonly BackgroundWorkSession[];
	/** Sessions streaming on any device (ids). */
	streaming: ReadonlySet<string> | ReadonlyMap<string, unknown>;
}

/** A chat this one started (StartSession) that is still working. */
export interface StartedChat {
	id: string;
	/** Its title, or the title it was started with. */
	title: string;
	/** Epoch ms its current work started, when known. */
	startedAt: number | null;
	/** The worker it runs on, when the session list names one. */
	worker: string | null;
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

function isWorking(session: BackgroundWorkSession, streaming: StartedChatsInput['streaming']): boolean {
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

/** The chats this one started that are still working, once each, in start order. */
export function startedChats({ messages, sessions, streaming }: StartedChatsInput): StartedChat[] {
	const byId = new Map(sessions.map((session) => [session.id, session]));
	const chats: StartedChat[] = [];
	const seen = new Set<string>();
	for (const message of messages) {
		if (message.type === 'subagent') continue;
		const id = startedSessionId(message);
		if (!id || seen.has(id)) continue;
		seen.add(id);
		// A chat missing from the loaded list still counts while it streams.
		const session = byId.get(id);
		if (!(session ? isWorking(session, streaming) : streaming.has(id))) continue;
		const input = (message.toolInput ?? {}) as { title?: unknown };
		chats.push({
			id,
			title: plain(session?.title) || plain(typeof input.title === 'string' ? input.title : '') || 'Chat',
			startedAt: parseTime(session?.work_started_at) ?? parseTime(message.timestamp) ?? parseTime(session?.created_at),
			worker: plain(session?.device_name) || null
		});
	}
	return chats;
}
