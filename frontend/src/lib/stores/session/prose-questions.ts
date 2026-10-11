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
 * is dropped without reopening the session here.
 *
 * The observation is fixed when the switcher leaves: the latest revision this
 * tab knows for the session at that moment. That is kept for every session
 * whatever its mark — from every loaded sidebar row, every
 * `user_session_updated` frame and, for a session opened with no row carrying
 * the field (a deep link, Spotlight, a row outside the loaded or filtered
 * page), the `/live` snapshot the session opened with. When that snapshot is
 * still in flight at leave time, revisions arriving afterwards are held until it
 * resolves: its value becomes the observation and a held revision later than it
 * clears the entry.
 * A revision that arrives after leaving never becomes the observation itself;
 * when nothing is known at all the entry waits for a reopen. Instants are
 * compared at the server's full (microsecond) precision with offsets applied.
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
 * `null`: observed with no user message stamped. `undefined`: nothing was
 * known then (see `_seeding` and `_afterLeave`).
 */
const _observed = new Map<string, Revision | null | undefined>();

/** The latest revision this tab has seen per session, marked or not. */
const _latest = new Map<string, Revision | null>();

/** Sessions whose own revision is awaited from their `/live` snapshot. */
const _seeding = new Set<string>();

/**
 * The latest revision seen after leaving, per remembered session whose
 * observation waits on an in-flight snapshot; reconciled when it resolves.
 */
const _afterLeave = new Map<string, Revision | null>();

/** Raw row value last parsed per session, so an unchanged row is not re-parsed. */
const _rowRaw = new Map<string, string | null>();

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

/** The later of a known revision and an optional one. */
function later(a: Revision | null | undefined, b: Revision | null): Revision | null {
	return a === undefined || isLater(b, a) ? b : a;
}

function drop(sessionId: string): void {
	_observed.delete(sessionId);
	_afterLeave.delete(sessionId);
	if (!_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.delete(sessionId);
	_store.set(next);
}

/**
 * Record (or clear) the prose question of a session the switcher leaves, from
 * its loaded transcript. `lastUserMessageAt` is the session row's
 * `last_user_message_at` at this moment: null when none is stamped, undefined
 * when no loaded row carries it. Whichever of it and the latest revision known
 * for the session is later becomes the observation.
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
	const known = _latest.get(sessionId);
	const observed = fromRow === undefined ? known : later(known, fromRow);
	_observed.set(sessionId, observed);
	_afterLeave.delete(sessionId);
	if (_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.add(sessionId);
	_store.set(next);
}

/**
 * Start reading an opened session's own revision, when it is not known from
 * any source and none is awaited. Returns whether a claim was opened; the
 * caller must then call `finishUserMessageRevisionSeed`.
 */
export function beginUserMessageRevisionSeed(sessionId: string | null | undefined): boolean {
	if (!sessionId || _latest.has(sessionId) || _seeding.has(sessionId)) return false;
	_seeding.add(sessionId);
	return true;
}

/**
 * The snapshot claimed by `beginUserMessageRevisionSeed` resolved. `value` is the
 * session's `last_user_message_at`, or undefined when the request failed or the
 * worker does not report it. A remembered entry left while it was in
 * flight takes it as its observation; a revision seen after leaving that is
 * later than it means the user answered, so the entry is dropped.
 */
export function finishUserMessageRevisionSeed(sessionId: string, value: unknown): void {
	_seeding.delete(sessionId);
	const seen = value === undefined ? undefined : toRevision(value);
	if (seen !== undefined) _latest.set(sessionId, later(_latest.get(sessionId), seen));
	if (!_observed.has(sessionId) || _observed.get(sessionId) !== undefined) return;
	const heldBack = _afterLeave.get(sessionId);
	_afterLeave.delete(sessionId);
	if (seen === undefined) return;
	_observed.set(sessionId, seen);
	if (heldBack !== undefined && isLater(heldBack, seen)) drop(sessionId);
}

/**
 * A session's `last_user_message_at` as delivered by a row update (the
 * `user_session_updated` frame or a list refresh). Drops the remembered
 * question when a user message arrived after it was observed. Call only when
 * the update actually carries the field.
 */
export function observeLastUserMessage(sessionId: string | null | undefined, lastUserMessageAt: unknown): void {
	if (!sessionId) return;
	const seen = toRevision(lastUserMessageAt);
	if (seen === undefined) return;
	_latest.set(sessionId, later(_latest.get(sessionId), seen));
	if (!_observed.has(sessionId)) return;
	const baseline = _observed.get(sessionId);
	if (baseline === undefined) {
		// Never the observation itself: hold it for the awaited snapshot, if any.
		if (_seeding.has(sessionId)) _afterLeave.set(sessionId, later(_afterLeave.get(sessionId), seen));
		return;
	}
	if (isLater(seen, baseline)) drop(sessionId);
}

/**
 * Apply `observeLastUserMessage` to every loaded session row that carries the
 * field, marked or not, so the revision is known if the session is opened and
 * later filtered or paged out of the list. Unchanged values are skipped.
 */
export function observeSessionRows(rows: readonly { id: string; last_user_message_at?: string | null }[]): void {
	for (const row of rows) {
		if (!('last_user_message_at' in row)) continue;
		const raw = row.last_user_message_at ?? null;
		if (_rowRaw.get(row.id) === raw && _latest.has(row.id)) continue;
		_rowRaw.set(row.id, raw);
		observeLastUserMessage(row.id, raw);
	}
}

/** Drop a session's entry (the session was deleted). */
export function forgetProseQuestion(sessionId: string): void {
	drop(sessionId);
	_latest.delete(sessionId);
	_rowRaw.delete(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_observed.clear();
	_latest.clear();
	_seeding.clear();
	_afterLeave.clear();
	_rowRaw.clear();
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
