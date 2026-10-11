/**
 * Unanswered prose questions of sessions that are not open (spec §5).
 *
 * The open session's prose question is read from its transcript. Other
 * sessions' transcripts are not loaded, so when the switcher leaves a session
 * whose loaded transcript ends in a question, the session is remembered here
 * and keeps counting toward "Needs you" until the next user message (§5.2).
 *
 * Each entry keeps the session row's `last_user_message_at` as observed when
 * the switcher left (null is an observation: no user message stamped yet).
 * The server advances that field whenever a user message is persisted on the
 * executing worker — a prompt, a mid-turn steer, a command, a REST or connector
 * message — and broadcasts it with `user_session_updated`, also to tabs that
 * do not have the session open. A row update (that frame or a list refresh)
 * whose value is strictly later than the observed one, or non-null where the
 * observation was null, means the user answered from somewhere, so the entry
 * is dropped without reopening the session here. When the row was not loaded
 * at leave time the first value seen afterwards becomes the observation.
 *
 * Otherwise only a loaded transcript changes an entry: reopening the session
 * in this tab re-records (or drops) it when the switcher next leaves, and
 * deleting the session drops it. Working-time and streaming changes on the row
 * are not user messages (a permission resume changes `work_started_at` inside
 * the same turn), and peer-agent notes and autonomous wakes do not advance
 * `last_user_message_at`, so none of them clear an entry.
 *
 * Known limit: a question this tab never saw (a session not opened here since
 * page load) is not known, because the row does not say whether its last
 * prose asks a question.
 */

import { createStore } from '../store-utils';
import { lastProseAsksQuestion } from '$lib/utils/work-log';
import type { ChatMessage } from '../types';

const _store = createStore<ReadonlySet<string>>(new Set());

/**
 * `last_user_message_at` observed per remembered session, as epoch ms.
 * `null`: observed with no user message stamped. `undefined`: not observed yet
 * (the row was not loaded when the switcher left).
 */
const _observed = new Map<string, number | null | undefined>();

export const proseQuestionSessions = {
	get current() { return _store.current; },
	subscribe: _store.subscribe
};

/** Epoch ms of a row timestamp; null for null/absent, undefined when unparsable. */
function toMillis(value: unknown): number | null | undefined {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string' || !value) return undefined;
	const ms = Date.parse(value);
	return Number.isNaN(ms) ? undefined : ms;
}

function drop(sessionId: string): void {
	_observed.delete(sessionId);
	if (!_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.delete(sessionId);
	_store.set(next);
}

/**
 * Record (or clear) the prose question of a session the switcher leaves, from
 * its loaded transcript. `lastUserMessageAt` is the session row's
 * `last_user_message_at` at this moment: null when none is stamped, undefined
 * when the row is not loaded here.
 */
export function rememberLeavingProseQuestion(
	sessionId: string | null,
	messages: readonly ChatMessage[],
	lastUserMessageAt?: string | null
): void {
	if (!sessionId) return;
	const pending = lastProseAsksQuestion(messages);
	if (!pending) {
		drop(sessionId);
		return;
	}
	_observed.set(sessionId, lastUserMessageAt === undefined ? undefined : toMillis(lastUserMessageAt));
	if (_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.add(sessionId);
	_store.set(next);
}

/**
 * A session row's `last_user_message_at` as delivered by a row update (the
 * `user_session_updated` frame or a list refresh). Drops the remembered
 * question when a user message arrived after it was observed. Call only when
 * the update actually carries the field.
 */
export function observeLastUserMessage(sessionId: string | null | undefined, lastUserMessageAt: unknown): void {
	if (!sessionId || !_observed.has(sessionId)) return;
	const seen = toMillis(lastUserMessageAt);
	if (seen === undefined) return;
	const baseline = _observed.get(sessionId);
	if (baseline === undefined) {
		// First sight of a row that was not loaded at leave time.
		_observed.set(sessionId, seen);
		return;
	}
	if (seen !== null && (baseline === null || seen > baseline)) drop(sessionId);
}

/** Apply `observeLastUserMessage` to loaded session rows that carry the field. */
export function observeSessionRows(rows: readonly { id: string; last_user_message_at?: string | null }[]): void {
	if (_observed.size === 0) return;
	for (const row of rows) {
		if (_observed.has(row.id) && 'last_user_message_at' in row) {
			observeLastUserMessage(row.id, row.last_user_message_at);
		}
	}
}

/** Drop a session's entry (the session was deleted). */
export function forgetProseQuestion(sessionId: string): void {
	drop(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_observed.clear();
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
