/**
 * Unanswered prose questions of sessions that are not open (spec §5).
 *
 * The open session's prose question is read from its transcript. Other
 * sessions' transcripts are not loaded, so when the switcher leaves a session
 * whose loaded transcript ends in a question, the session is remembered here
 * and keeps counting toward "Needs you".
 *
 * Only a loaded transcript changes an entry: reopening the session in this tab
 * re-records (or drops) it when the switcher next leaves, and deleting the
 * session drops it. Working-time and streaming changes on the session row are
 * not user messages (a steer changes neither; a permission resume changes
 * `work_started_at` inside the same turn), so they never clear an entry.
 *
 * Known limits: an answer sent from another device leaves the entry until the
 * session is opened here, and a question this tab never saw is not known.
 */

import { createStore } from '../store-utils';
import { lastProseAsksQuestion } from '$lib/utils/work-log';
import type { ChatMessage } from '../types';

const _store = createStore<ReadonlySet<string>>(new Set());

export const proseQuestionSessions = {
	get current() { return _store.current; },
	subscribe: _store.subscribe
};

/** Record (or clear) the prose question of a session the switcher leaves, from its loaded transcript. */
export function rememberLeavingProseQuestion(sessionId: string | null, messages: readonly ChatMessage[]): void {
	if (!sessionId) return;
	const pending = lastProseAsksQuestion(messages);
	const set = _store.current;
	if (pending === set.has(sessionId)) return;
	const next = new Set(set);
	if (pending) next.add(sessionId);
	else next.delete(sessionId);
	_store.set(next);
}

/** Drop a session's entry (the session was deleted). */
export function forgetProseQuestion(sessionId: string): void {
	if (!_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.delete(sessionId);
	_store.set(next);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
