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
 * is dropped without reopening the session here. The observation is the later
 * of the sidebar row's value and the latest one this tab saw while the session
 * was open (its detail, fetched on open when no row carries the field, and any
 * `user_session_updated` frame), so a session opened from a deep link,
 * Spotlight or outside the loaded or filtered list still has a baseline. Only
 * when none of those is known does the first value seen afterwards become it.
 * Instants are compared at the server's full (microsecond) precision with
 * offsets applied.
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
 * A `last_user_message_at` instant at full precision. The server stamps
 * microseconds and `Date.parse` keeps only milliseconds, so the whole seconds
 * (epoch ms, offset applied) and the fraction digits are kept apart.
 */
interface Revision {
	/** Epoch ms of the whole second, offset applied. */
	s: number;
	/** Fraction of the second as 9 digits (nanoseconds), compared as text. */
	f: string;
}

/**
 * Revision observed per remembered session when the switcher left it.
 * `null`: observed with no user message stamped. `undefined`: not known then
 * (no row loaded and no revision seen while the session was open).
 */
const _observed = new Map<string, Revision | null | undefined>();

/**
 * The latest revision this tab has seen per session, from the open session's
 * own detail and from `user_session_updated` frames, independent of whether a
 * sidebar row is loaded (a deep link, Spotlight or a tag filter can open a
 * session the list does not hold).
 */
const _latest = new Map<string, Revision | null>();

export const proseQuestionSessions = {
	get current() { return _store.current; },
	subscribe: _store.subscribe
};

const ISO_PARTS = /^(.*T\d{2}:\d{2}:\d{2})(?:[.,](\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

/** Parse a row timestamp; null for null/absent, undefined when unparsable. */
function toRevision(value: unknown): Revision | null | undefined {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string' || !value) return undefined;
	const m = ISO_PARTS.exec(value.trim());
	if (m) {
		const s = Date.parse(m[1] + (m[3] ?? ''));
		if (Number.isNaN(s)) return undefined;
		return { s, f: (m[2] ?? '').slice(0, 9).padEnd(9, '0') };
	}
	const ms = Date.parse(value);
	if (Number.isNaN(ms)) return undefined;
	const whole = Math.floor(ms / 1000) * 1000;
	return { s: whole, f: String(ms - whole).padStart(3, '0').padEnd(9, '0') };
}

/** a > b for revisions, where null (nothing stamped) precedes every instant. */
function isLater(a: Revision | null, b: Revision | null): boolean {
	if (a === null) return false;
	if (b === null) return true;
	return a.s !== b.s ? a.s > b.s : a.f > b.f;
}

/** The later of two observations; undefined only when neither is known. */
function later(a: Revision | null | undefined, b: Revision | null | undefined): Revision | null | undefined {
	if (a === undefined) return b;
	if (b === undefined) return a;
	return isLater(b, a) ? b : a;
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
 * when the row is not loaded here. The latest revision seen for the session
 * while it was open counts too, whichever is later.
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
	const fromRow = lastUserMessageAt === undefined ? undefined : toRevision(lastUserMessageAt);
	_observed.set(sessionId, later(fromRow, _latest.get(sessionId)));
	if (_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.add(sessionId);
	_store.set(next);
}

/**
 * The open session's own `last_user_message_at`, read from its detail when no
 * sidebar row carries it. Keeps the later of this and what was seen before.
 */
export function noteUserMessageRevision(sessionId: string | null | undefined, lastUserMessageAt: unknown): void {
	if (!sessionId) return;
	const seen = toRevision(lastUserMessageAt);
	if (seen === undefined) return;
	const merged = later(_latest.get(sessionId), seen) as Revision | null;
	_latest.set(sessionId, merged);
	// A remembered entry with no baseline yet: this is the best one known.
	if (_observed.has(sessionId) && _observed.get(sessionId) === undefined) {
		_observed.set(sessionId, merged);
	}
}

/**
 * A session row's `last_user_message_at` as delivered by a row update (the
 * `user_session_updated` frame or a list refresh). Drops the remembered
 * question when a user message arrived after it was observed. Call only when
 * the update actually carries the field.
 */
export function observeLastUserMessage(sessionId: string | null | undefined, lastUserMessageAt: unknown): void {
	if (!sessionId) return;
	const seen = toRevision(lastUserMessageAt);
	if (seen === undefined) return;
	_latest.set(sessionId, later(_latest.get(sessionId), seen) as Revision | null);
	if (!_observed.has(sessionId)) return;
	const baseline = _observed.get(sessionId);
	if (baseline === undefined) {
		// Nothing was known when the switcher left: this value is the baseline.
		_observed.set(sessionId, seen);
		return;
	}
	if (isLater(seen, baseline)) drop(sessionId);
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
	_latest.delete(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_observed.clear();
	_latest.clear();
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
