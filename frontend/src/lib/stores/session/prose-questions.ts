/**
 * Unanswered prose questions of sessions that are not open (spec §5).
 *
 * The open session's prose question is read from its transcript. Other
 * sessions' transcripts are not loaded, so when the switcher leaves a session
 * whose loaded transcript ends in a question, the session is remembered here
 * and keeps counting toward "Needs you" until the next user message (§5.2).
 *
 * Answers are matched by event-log position, never by wall-clock time (#2249,
 * docs/content/api-reference/sessions.md "User-message revision"). The
 * remembered question keeps the `seq`/`epoch` its rendered message carries
 * (the LAST event that contributed to it). Every session revision this tab
 * sees — loaded sidebar rows, `user_session_updated` frames and the `/live`
 * snapshot of a visit — carries `last_user_message_seq` and
 * `last_user_message_epoch`. A revision answers the question iff
 *
 *     revision.epoch !== null && revision.epoch === question.epoch
 *       && revision.seq > question.seq
 *
 * Epochs are opaque identities, not ordered counters: a revision from another
 * log (a delayed pre-clear snapshot, or a log cleared since) never answers
 * anything. Because the rule compares against the question itself there is no
 * baseline to fix when leaving, so a late, delayed or reordered revision cannot
 * clear a mark early: a revision taken before the question has a seq at or
 * below it, including a prompt persisted in the same transaction as a voice
 * reply. The highest seq seen per epoch is kept for every session, so leaving
 * a session whose answer this tab has already seen (but whose transcript has
 * not caught up) does not mark it.
 *
 * A question without a position (legacy or ambiguous transcript), or a
 * revision without one (a legacy-only write, an older worker, a cleared log),
 * never clears on the revision alone: the mark then waits for the transcript —
 * reopening the session here re-records (or drops) it when the switcher next
 * leaves — or for the session to be deleted.
 *
 * Working-time and streaming changes on the row are not user messages, and
 * peer-agent notes and autonomous wakes do not advance the revision, so none
 * of them clear an entry.
 *
 * Known limit: a question this tab never saw (a session not opened here since
 * page load) is not known, because the row does not say whether its last
 * prose asks a question.
 */

import { createStore } from '../store-utils';
import { lastProseAsksQuestion } from '$lib/utils/work-log';
import type { ChatMessage } from '../types';

const _store = createStore<ReadonlySet<string>>(new Set());

/** An event-log position: a seq within one log epoch. */
interface Position {
	seq: number;
	epoch: string;
}

/**
 * Anything carrying a session's user-message revision: a session row, a
 * `user_session_updated` frame's `updates`, a `/live` `session_meta`.
 */
export interface UserMessageRevision {
	/** Integer seq, or null/absent when unknown. Validated here. */
	last_user_message_seq?: unknown;
	/** Opaque epoch string, or null/absent when unknown. Validated here. */
	last_user_message_epoch?: unknown;
}

/**
 * The remembered question's position per marked session. `null`: the question
 * has no position, so only the transcript or a delete changes the mark.
 */
const _questions = new Map<string, Position | null>();

/** How many log epochs to keep a highest seq for, per session. */
const EPOCHS_KEPT = 4;

/**
 * The highest user-message seq seen per epoch, per session, marked or not.
 * Max'd, never replaced: a stale lower seq of the same epoch is ignored.
 */
const _latest = new Map<string, Map<string, number>>();

/**
 * The open visit's claim per session: a token for the `/live` snapshot it
 * awaits. A newer visit replaces it. The comparison above does not depend on
 * which visit a snapshot belongs to, so a departed visit's late response is an
 * ordinary revision; the claim only scopes the request to its visit.
 */
const _claims = new Map<string, number>();
let _nextClaim = 0;

/** Raw row positions last seen per session, so an unchanged row is skipped. */
const _rowRaw = new Map<string, string>();

export const proseQuestionSessions = {
	get current() { return _store.current; },
	subscribe: _store.subscribe
};

/** A complete position, or null when either half is missing or malformed. */
function toPosition(seq: unknown, epoch: unknown): Position | null {
	if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 0) return null;
	if (typeof epoch !== 'string' || !epoch) return null;
	return { seq, epoch };
}

/** The highest user-message seq this tab has seen in `epoch`. */
function latestIn(sessionId: string, epoch: string): number | undefined {
	return _latest.get(sessionId)?.get(epoch);
}

/** Record a revision position, keeping the highest seq per epoch. */
function remember(sessionId: string, pos: Position): void {
	let epochs = _latest.get(sessionId);
	if (!epochs) {
		epochs = new Map();
		_latest.set(sessionId, epochs);
	}
	const known = epochs.get(pos.epoch);
	if (known !== undefined && known >= pos.seq) return;
	if (known === undefined && epochs.size >= EPOCHS_KEPT) {
		const oldest = epochs.keys().next().value;
		if (oldest !== undefined) epochs.delete(oldest);
	}
	epochs.set(pos.epoch, pos.seq);
}

/** Whether a user message at `seen` (highest seq of an epoch) answers `question`. */
function answers(seen: number | undefined, question: Position): boolean {
	return seen !== undefined && seen > question.seq;
}

function drop(sessionId: string): void {
	_questions.delete(sessionId);
	if (!_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.delete(sessionId);
	_store.set(next);
}

/** The trailing assistant prose message (the one `lastProseAsksQuestion` reads). */
function trailingProse(messages: readonly ChatMessage[]): ChatMessage | undefined {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.role === 'assistant' && (m.type === 'text' || !m.type) && !!m.content?.trim()) return m;
	}
	return undefined;
}

/**
 * Record (or clear) the prose question of a session the switcher leaves, from
 * its loaded transcript. `row` is the session's loaded row, when there is one;
 * its revision is observed first. Not marked when a user message later than
 * the question has already been seen here.
 */
export function rememberLeavingProseQuestion(
	sessionId: string | null,
	messages: readonly ChatMessage[],
	row?: UserMessageRevision | null
): void {
	if (!sessionId) return;
	if (row) observeLastUserMessage(sessionId, row);
	if (!lastProseAsksQuestion(messages)) {
		drop(sessionId);
		return;
	}
	const question = trailingProse(messages);
	const pos = question ? toPosition(question.seq, question.epoch) : null;
	if (pos && answers(latestIn(sessionId, pos.epoch), pos)) {
		drop(sessionId);
		return;
	}
	_questions.set(sessionId, pos);
	if (_store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	next.add(sessionId);
	_store.set(next);
}

/**
 * A visit to the session starts: claim its revision from the `/live` snapshot
 * that visit fetches. Replaces any earlier visit's claim. Returns the token to
 * pass to `finishUserMessageRevisionSeed`.
 */
export function beginUserMessageRevisionSeed(sessionId: string): number {
	const token = ++_nextClaim;
	_claims.set(sessionId, token);
	return token;
}

/**
 * The `/live` snapshot of the visit holding `token` resolved. `meta` is its
 * `session_meta`, or undefined when the request failed. A superseded visit's
 * snapshot still counts as an ordinary revision: an early or late snapshot
 * cannot answer a question its seq does not follow.
 */
export function finishUserMessageRevisionSeed(
	sessionId: string,
	token: number,
	meta: UserMessageRevision | null | undefined
): void {
	if (_claims.get(sessionId) === token) _claims.delete(sessionId);
	if (meta) observeLastUserMessage(sessionId, meta);
}

/**
 * A session's user-message revision as delivered by a row, a
 * `user_session_updated` frame or a `/live` snapshot. Drops the remembered
 * question when the revision is a later user message in the question's log.
 */
export function observeLastUserMessage(
	sessionId: string | null | undefined,
	revision: UserMessageRevision | null | undefined
): void {
	if (!sessionId || !revision) return;
	const pos = toPosition(revision.last_user_message_seq, revision.last_user_message_epoch);
	if (!pos) return;
	remember(sessionId, pos);
	const question = _questions.get(sessionId);
	if (question && question.epoch === pos.epoch && answers(latestIn(sessionId, pos.epoch), question)) {
		drop(sessionId);
	}
}

/**
 * Apply `observeLastUserMessage` to every loaded session row, marked or not,
 * so the revision is known if the session is opened and later filtered or
 * paged out of the list. Unchanged rows are skipped.
 */
export function observeSessionRows(rows: readonly ({ id: string } & UserMessageRevision)[]): void {
	for (const row of rows) {
		const raw = `${row.last_user_message_seq ?? ''}@${row.last_user_message_epoch ?? ''}`;
		if (_rowRaw.get(row.id) === raw) continue;
		_rowRaw.set(row.id, raw);
		observeLastUserMessage(row.id, row);
	}
}

/** Drop a session's entry (the session was deleted). */
export function forgetProseQuestion(sessionId: string): void {
	drop(sessionId);
	_latest.delete(sessionId);
	_rowRaw.delete(sessionId);
	_claims.delete(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_questions.clear();
	_latest.clear();
	_claims.clear();
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
