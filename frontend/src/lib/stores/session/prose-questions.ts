/**
 * Unanswered prose questions of sessions that are not open (spec §5).
 *
 * The open session's prose question is read from its transcript. Other
 * sessions' transcripts are not loaded, so when the switcher leaves a session
 * whose last prose asks something, the fact is remembered here and keeps
 * counting toward "Needs you" until that session's next user message.
 *
 * A new user message starts a turn, so an entry stops counting once the
 * session list reports a turn that started after the question was seen. A
 * question this tab never saw (a session not opened since the page loaded)
 * is not known here.
 */

import type { Session } from '$lib/api/client';
import { createStore } from '../store-utils';
import { lastProseAsksQuestion } from '$lib/utils/work-log';
import type { ChatMessage } from '../types';

export interface ProseQuestionMark {
	/** `work_started_at` of the turn the question was seen in, null when settled. */
	workStartedAt: string | null;
	/** Whether a turn was live when the question was seen. */
	streaming: boolean;
}

const _store = createStore<ReadonlyMap<string, ProseQuestionMark>>(new Map());

export const proseQuestionSessions = {
	get current() { return _store.current; },
	subscribe: _store.subscribe
};

/** Record (or clear) the prose question of a session the switcher is leaving. */
export function rememberLeavingProseQuestion(
	sessionId: string | null,
	messages: readonly ChatMessage[],
	mark: ProseQuestionMark
): void {
	if (!sessionId) return;
	const pending = lastProseAsksQuestion(messages);
	const map = _store.current;
	if (!pending && !map.has(sessionId)) return;
	const next = new Map(map);
	if (pending) next.set(sessionId, mark);
	else next.delete(sessionId);
	_store.set(next);
}

/** Drop a session's entry (the session was deleted). */
export function forgetProseQuestion(sessionId: string): void {
	if (!_store.current.has(sessionId)) return;
	const next = new Map(_store.current);
	next.delete(sessionId);
	_store.set(next);
}

/** Whether the next user message has arrived since the question was seen. */
function answered(
	mark: ProseQuestionMark,
	s: Pick<Session, 'work_started_at'>,
	streamingNow: boolean
): boolean {
	// A row without the field (an older owner) only has the live-run map.
	if (s.work_started_at === undefined) return !mark.streaming && streamingNow;
	const started = s.work_started_at;
	if (!started || started === mark.workStartedAt) return false;
	// Seen mid-turn before the list reported that turn's start: the first
	// start the list reports is that same turn (reconcile adopts it).
	return !(mark.streaming && mark.workStartedAt === null);
}

/** Whether a remembered prose question still waits on the user. */
export function proseQuestionPending(
	mark: ProseQuestionMark | undefined,
	s: Pick<Session, 'work_started_at'>,
	streamingNow: boolean
): boolean {
	return !!mark && !answered(mark, s, streamingNow);
}

/**
 * Drop entries that were answered or whose session left the list's known
 * rows, and adopt the turn start of a question seen mid-turn before the list
 * reported it. Only rows present in `sessions` are judged.
 */
export function reconcileProseQuestions(
	sessions: readonly Pick<Session, 'id' | 'work_started_at'>[],
	streaming: ReadonlyMap<string, unknown>
): void {
	const map = _store.current;
	if (map.size === 0) return;
	let next: Map<string, ProseQuestionMark> | null = null;
	for (const s of sessions) {
		const mark = map.get(s.id);
		if (!mark) continue;
		if (answered(mark, s, streaming.has(s.id))) {
			next ??= new Map(map);
			next.delete(s.id);
		} else if (mark.streaming && !mark.workStartedAt && s.work_started_at) {
			next ??= new Map(map);
			next.set(s.id, { ...mark, workStartedAt: s.work_started_at });
		}
	}
	if (next) _store.set(next);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_store.set(new Map());
}

/** The map without one session's entry (the same map when it has none). */
export function withoutSession(
	map: ReadonlyMap<string, ProseQuestionMark>,
	sessionId: string | null
): ReadonlyMap<string, ProseQuestionMark> {
	if (!sessionId || !map.has(sessionId)) return map;
	const next = new Map(map);
	next.delete(sessionId);
	return next;
}
