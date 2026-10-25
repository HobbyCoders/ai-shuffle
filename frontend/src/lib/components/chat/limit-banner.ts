/**
 * What the composer says about a chat a usage limit stopped (#1831).
 *
 * The worker's `GET /sessions/{id}/limit-resume` answer decides: it survives
 * a reload and knows whether this stop may still be armed. Until it answers
 * (or on a worker that predates it), the transcript's open `scheduled` /
 * `deferred` row stands in for the armed state, as before.
 */
import type { SessionLimitResume } from '$lib/api/limit-resume';
import { resumeTime, type Paused } from './limit-resume';

/** Reasons the last stop is still worth showing. Others (busy, archived,
 *  moved, a newer turn...) mean the chat has moved on. */
const SHOWN_REASONS = new Set([null, 'reset_unknown', 'already_handled', 'already_resumed', 'unsupported_provider']);

export type LimitBanner =
	| { kind: 'armed'; resumeAt: number; message: string; /** A one-shot arm: only the owner may cancel it. */ once: boolean }
	| { kind: 'stopped'; key: string; turnId: string; resetAt: number | null; canArm: boolean; message: string };

/**
 * The banner for this chat, or null.
 * `state` is the worker's answer for this chat, `transcript` the pause read
 * from its log (used only while `state` is missing). `now` is epoch seconds.
 */
export function limitBanner(
	state: SessionLimitResume | null,
	transcript: Paused | null,
	now: number = Date.now() / 1000,
	clock: Date = new Date(now * 1000)
): LimitBanner | null {
	if (!state) {
		return transcript ? armed(transcript.resumeAt, clock, false) : null;
	}
	const resumeAt = typeof state.resume_at === 'number' ? state.resume_at : pendingFireAt(state.pending);
	const isArmed = state.armed ?? state.pending != null;
	if (isArmed && resumeAt !== null) return armed(resumeAt, clock, state.pending?.once === true);

	const stop = state.limit_stopped;
	if (!stop || !SHOWN_REASONS.has(stop.reason)) return null;
	const resetAt = stop.reset_known && typeof stop.reset_at === 'number' ? stop.reset_at : null;
	const key = `${state.session_id}:${stop.turn_id}`;
	if (resetAt === null) {
		return { kind: 'stopped', key, turnId: stop.turn_id, resetAt: null, canArm: false,
			message: 'Usage limit reached. Reset time unknown' };
	}
	if (resetAt <= now) {
		// The reset has passed: arming would only wait for nothing.
		return { kind: 'stopped', key, turnId: stop.turn_id, resetAt, canArm: false,
			message: `Usage limit reset at ${resumeTime(resetAt, clock)}. Send a message to continue` };
	}
	return { kind: 'stopped', key, turnId: stop.turn_id, resetAt, canArm: stop.can_arm,
		message: `Usage limit reached. Resets at ${resumeTime(resetAt, clock)}` };
}

function armed(resumeAt: number, clock: Date, once: boolean): LimitBanner {
	return { kind: 'armed', resumeAt, once, message: `Usage limit reached. Resumes at ${resumeTime(resumeAt, clock)}` };
}

function pendingFireAt(pending: SessionLimitResume['pending']): number | null {
	const at = pending?.fire_at;
	return typeof at === 'number' && Number.isFinite(at) ? at : null;
}
