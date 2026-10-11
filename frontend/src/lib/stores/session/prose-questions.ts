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
 * Row-derived question (#2263): a worker that stamps the row also delivers
 * `last_assistant_question_seq`/`_epoch` with the revision — the position of
 * the newest nonempty assistant prose when it asks a question, null when it
 * does not. A session is then marked iff
 *
 *     question.seq !== null && question.epoch !== null
 *       && (no user position is known
 *           || (user.epoch === question.epoch && question.seq > user.seq))
 *
 * using the latest user pair delivered and the highest user seq seen in the
 * question's epoch, so a delayed revision cannot un-answer a question. A
 * revision whose question seq is below one already seen in the same epoch,
 * and whose user seq is not newer than the one seen there, is superseded and
 * ignored, so a delayed row cannot replace a re-ask. Null (non-question prose)
 * always applies. This
 * replaces the transcript-derived mark for that session, including sessions
 * this tab never opened. An explicit null seq in a known epoch clears it.
 * When the fields are absent (an older worker) or both null (an older peer's
 * replicated row, or a cleared log), the transcript-derived mark above stays
 * the fallback. A clear retires the log's epochs here, so a delayed pre-clear
 * row cannot bring its question back.
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
	/** Trailing prose question seq; null: no question; absent: older worker. */
	last_assistant_question_seq?: unknown;
	/** Opaque epoch of `last_assistant_question_seq`. */
	last_assistant_question_epoch?: unknown;
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

/**
 * The row-derived trailing prose question per session (#2263): its position,
 * or null when the row says the newest prose asks nothing. Absent: the row
 * says nothing (older worker or peer), so the transcript-derived mark applies.
 */
const _rowQuestions = new Map<string, Position | null>();

/** The latest user-message pair delivered per session (null: none known). */
const _lastUser = new Map<string, Position | null>();

/**
 * Every question epoch delivered per session, with the highest question seq
 * seen in it (null while only "no question" was seen). Keeps the epoch of an
 * explicit null so a clear can retire it, and lets a superseded revision be
 * recognised as stale.
 */
const _questionSeen = new Map<string, Map<string, number | null>>();

/** Epochs of logs cleared since load, per session: never re-marked from. */
const _retired = new Map<string, Set<string>>();

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

/**
 * Whether the row-derived question marks the session; undefined when the row
 * says nothing, so the transcript-derived mark decides.
 */
function rowMarked(sessionId: string): boolean | undefined {
	if (!_rowQuestions.has(sessionId)) return undefined;
	const question = _rowQuestions.get(sessionId);
	if (!question) return false;
	// Marked only while the question follows every user message of its log.
	const seen = latestIn(sessionId, question.epoch);
	if (seen !== undefined && seen >= question.seq) return false;
	const user = _lastUser.get(sessionId);
	// Never compare positions across epochs: a user pair from another log
	// leaves the question's state unknown, so it is not marked.
	return !user || user.epoch === question.epoch;
}

/** Publish the session's mark: the row's when it has one, else the transcript's. */
function refresh(sessionId: string): void {
	const marked = rowMarked(sessionId) ?? _questions.has(sessionId);
	if (marked === _store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	if (marked) next.add(sessionId);
	else next.delete(sessionId);
	_store.set(next);
}

function drop(sessionId: string): void {
	_questions.delete(sessionId);
	refresh(sessionId);
}

/** Record the row-derived question pair of a revision that carries one. */
function observeQuestion(sessionId: string, revision: UserMessageRevision): void {
	if (revision.last_assistant_question_seq === undefined) return;
	const seq = revision.last_assistant_question_seq;
	const epoch = revision.last_assistant_question_epoch;
	const retired = _retired.get(sessionId);
	if (typeof epoch === 'string' && retired?.has(epoch)) return;
	if (seq === null && (epoch === null || epoch === undefined)) {
		// Nothing known (older peer, cleared log): the transcript decides.
		_rowQuestions.delete(sessionId);
		return;
	}
	if (typeof epoch !== 'string' || !epoch) {
		// A malformed pair says no question: never mark from it.
		_rowQuestions.set(sessionId, null);
		return;
	}
	let seen = _questionSeen.get(sessionId);
	if (!seen) {
		seen = new Map();
		_questionSeen.set(sessionId, seen);
	}
	const pos = toPosition(seq, epoch);
	const high = seen.get(epoch);
	if (pos && high !== undefined && high !== null && pos.seq < high && !userIsNewer(sessionId, epoch, revision)) {
		// Superseded: a later question of this log was already seen, and this
		// revision carries no newer user message (a delayed row or snapshot).
		// Non-question prose (null) and a lower question that arrives with a
		// newer user message still replace it.
		return;
	}
	if (!seen.has(epoch) && seen.size >= EPOCHS_KEPT) {
		const oldest = seen.keys().next().value;
		if (oldest !== undefined) seen.delete(oldest);
	}
	if (pos && (high === undefined || high === null || pos.seq > high)) seen.set(epoch, pos.seq);
	else if (high === undefined) seen.set(epoch, null);
	_rowQuestions.set(sessionId, pos);
}

/** Whether the revision's user message is newer than any seen in `epoch`. */
function userIsNewer(sessionId: string, epoch: string, revision: UserMessageRevision): boolean {
	const user = toPosition(revision.last_user_message_seq, revision.last_user_message_epoch);
	if (!user || user.epoch !== epoch) return false;
	const seen = latestIn(sessionId, epoch);
	return seen === undefined || user.seq > seen;
}

/** Record the latest user pair of a revision that carries one. */
function observeUser(sessionId: string, revision: UserMessageRevision): void {
	if (revision.last_user_message_seq === undefined && revision.last_user_message_epoch === undefined) return;
	const pos = toPosition(revision.last_user_message_seq, revision.last_user_message_epoch);
	if (pos && _retired.get(sessionId)?.has(pos.epoch)) return;
	_lastUser.set(sessionId, pos);
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
	refresh(sessionId);
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
 * A session's revision as delivered by a row, a `user_session_updated` frame
 * or a `/live` snapshot: its user-message pair and, from a worker that stamps
 * it, its trailing prose question pair (#2263). Drops the remembered question
 * when the revision is a later user message in the question's log.
 */
export function observeLastUserMessage(
	sessionId: string | null | undefined,
	revision: UserMessageRevision | null | undefined
): void {
	if (!sessionId || !revision) return;
	observeQuestion(sessionId, revision);
	observeUser(sessionId, revision);
	const pos = toPosition(revision.last_user_message_seq, revision.last_user_message_epoch);
	if (pos) {
		remember(sessionId, pos);
		const question = _questions.get(sessionId);
		if (question && question.epoch === pos.epoch && answers(latestIn(sessionId, pos.epoch), question)) {
			_questions.delete(sessionId);
		}
	}
	refresh(sessionId);
}

/**
 * Apply `observeLastUserMessage` to every loaded session row, marked or not,
 * so the revision is known if the session is opened and later filtered or
 * paged out of the list. Unchanged rows are skipped.
 */
export function observeSessionRows(rows: readonly ({ id: string } & UserMessageRevision)[]): void {
	for (const row of rows) {
		const raw = [
			row.last_user_message_seq, row.last_user_message_epoch,
			row.last_assistant_question_seq, row.last_assistant_question_epoch
		].map((v) => (v === undefined ? '~' : String(v))).join('@');
		if (_rowRaw.get(row.id) === raw) continue;
		_rowRaw.set(row.id, raw);
		observeLastUserMessage(row.id, row);
	}
}

/**
 * Drop a session's entry: the session was deleted, or (`cleared`) its log was
 * wiped in place. A clear retires every epoch known for it, so a delayed
 * pre-clear row or snapshot cannot mark or answer anything again.
 */
export function forgetProseQuestion(sessionId: string, opts: { cleared?: boolean } = {}): void {
	if (opts.cleared) {
		const retired = _retired.get(sessionId) ?? new Set<string>();
		for (const epoch of _latest.get(sessionId)?.keys() ?? []) retired.add(epoch);
		for (const epoch of _questionSeen.get(sessionId)?.keys() ?? []) retired.add(epoch);
		for (const pos of [_questions.get(sessionId), _rowQuestions.get(sessionId), _lastUser.get(sessionId)]) {
			if (pos) retired.add(pos.epoch);
		}
		if (retired.size) _retired.set(sessionId, retired);
	} else {
		_retired.delete(sessionId);
	}
	_questions.delete(sessionId);
	_rowQuestions.delete(sessionId);
	_questionSeen.delete(sessionId);
	_lastUser.delete(sessionId);
	_latest.delete(sessionId);
	_rowRaw.delete(sessionId);
	_claims.delete(sessionId);
	refresh(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_questions.clear();
	_latest.clear();
	_claims.clear();
	_rowRaw.clear();
	_rowQuestions.clear();
	_questionSeen.clear();
	_lastUser.clear();
	_retired.clear();
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
