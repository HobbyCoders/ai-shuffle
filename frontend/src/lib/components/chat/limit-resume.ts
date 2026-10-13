/**
 * A chat or worker paused by a usage limit, read from the transcript (#1831).
 *
 * The backend records every step of a resume on the chat's own log as a
 * `limit_resume` system row (app/core/limit_resume.py): `scheduled` and
 * `deferred` carry `resume_at` (epoch seconds), then `resumed`, `cancelled`
 * or `dropped` close it. A row with `task_id` is about a background worker of
 * this chat, shown on its running-tasks row. The newest row decides, and an
 * owner message after it ends the pause too: sending a message cancels the
 * scheduled resume.
 */
import type { ChatMessage } from '$lib/stores/types';

export const LIMIT_RESUME_SUBTYPE = 'limit_resume';
const OPEN = new Set(['scheduled', 'deferred']);

export interface Paused {
	/** Epoch seconds. */
	resumeAt: number;
}

function row(m: ChatMessage): { status: string; resumeAt: number | null; taskId: string | null } | null {
	if (m.systemSubtype !== LIMIT_RESUME_SUBTYPE) return null;
	const data = (m.systemData ?? {}) as Record<string, unknown>;
	const resumeAt = typeof data.resume_at === 'number' && Number.isFinite(data.resume_at) ? data.resume_at : null;
	return {
		status: typeof data.status === 'string' ? data.status : '',
		resumeAt,
		taskId: typeof data.task_id === 'string' && data.task_id ? data.task_id : null
	};
}

function isOwnerMessage(m: ChatMessage): boolean {
	return m.role === 'user' && m.type !== 'system';
}

/** The chat's own pause, or null. */
export function pausedChat(messages: readonly ChatMessage[]): Paused | null {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (isOwnerMessage(m)) return null;
		const r = row(m);
		if (!r || r.taskId) continue;
		return OPEN.has(r.status) && r.resumeAt !== null ? { resumeAt: r.resumeAt } : null;
	}
	return null;
}

/** Paused workers of this chat: task id to resume time (epoch seconds). */
export function pausedWorkers(messages: readonly ChatMessage[]): Map<string, number> {
	const decided = new Set<string>();
	const out = new Map<string, number>();
	for (let i = messages.length - 1; i >= 0; i--) {
		const r = row(messages[i]);
		if (!r || !r.taskId || decided.has(r.taskId)) continue;
		decided.add(r.taskId);
		if (OPEN.has(r.status) && r.resumeAt !== null) out.set(r.taskId, r.resumeAt);
	}
	return out;
}

/** "11:10 pm", with the weekday when it is not today: "Tue 11:10 pm". */
export function resumeTime(epochSeconds: number, now: Date = new Date()): string {
	const at = new Date(epochSeconds * 1000);
	const time = at
		.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
		.replace(/\s?(AM|PM)$/i, (_m, ap: string) => ` ${ap.toLowerCase()}`);
	if (at.toDateString() === now.toDateString()) return time;
	return `${at.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`;
}

/** "Paused: usage limit. Resumes at 11:10 pm" */
export function pausedLabel(epochSeconds: number, now?: Date): string {
	return `Paused: usage limit. Resumes at ${resumeTime(epochSeconds, now)}`;
}
