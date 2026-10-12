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
 * question's epoch, so a delayed revision cannot un-answer a question.
 * Revisions are ordered by the row's `updated_at`, not by position: a list
 * row older than the one applied is ignored, so a delayed row cannot replace a
 * re-ask, while a retry that restores an earlier question (a newer write)
 * applies. Pushed frames apply in arrival order; a `/live` snapshot, which has
 * no `updated_at`, only seeds a session no row or frame has set (see
 * `isCurrent`). This
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
	/** The row's write time: orders list rows (#2263). Frames and `/live` lack it. */
	updated_at?: unknown;
}

/**
 * Where a revision came from, when it carries no `updated_at`: a pushed
 * `user_session_updated` frame (applied in arrival order) or a fetched `/live`
 * snapshot (no order at all, so it only seeds a session without row state).
 */
export interface RevisionSource {
	snapshot?: boolean;
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
 * Every question epoch delivered per session, including one only seen with a
 * null seq, so a clear can retire it.
 */
const _questionEpochs = new Map<string, Set<string>>();

/**
 * Which revision of the row the row-derived state comes from, per session:
 * `at` is the newest applied row `updated_at` (ms; null when none), and
 * `pushedSince` says a pushed frame (which carries no `updated_at`) was
 * applied after it.
 */
const _applied = new Map<string, { at: string | null; pushedSince: boolean }>();

/**
 * `updated_at` values this tab synthesised from its own clock (a turn's `done`,
 * a clear), per session. They order the sidebar but are not server versions,
 * so a row carrying one is not used to order revisions.
 */
const _localStamps = new Map<string, string[]>();
const LOCAL_STAMPS_KEPT = 8;

/** Record an `updated_at` this tab wrote onto a row from its own clock. */
export function noteLocalRowStamp(sessionId: string, stamp: string): void {
	const stamps = _localStamps.get(sessionId) ?? [];
	stamps.push(stamp);
	if (stamps.length > LOCAL_STAMPS_KEPT) stamps.shift();
	_localStamps.set(sessionId, stamps);
}

const STAMP = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

/**
 * A server `updated_at` as a key that sorts in time order without losing the
 * microseconds PostgreSQL keeps (Date.parse stops at milliseconds): whole UTC
 * seconds, then the fraction to 9 digits. Accepts `Z` or a numeric offset (a
 * missing one is UTC) and a space or `T` separator. Null when unparseable.
 */
export function versionKey(value: unknown): string | null {
	if (typeof value !== 'string') return null;
	const m = STAMP.exec(value.trim());
	if (!m) return null;
	let offset = (m[4] ?? 'Z').toUpperCase();
	if (/^[+-]\d{2}$/.test(offset)) offset += ':00';
	else if (/^[+-]\d{4}$/.test(offset)) offset = `${offset.slice(0, 3)}:${offset.slice(3)}`;
	const ms = Date.parse(`${m[1]}T${m[2]}${offset}`);
	if (Number.isNaN(ms)) return null;
	const seconds = Math.floor(ms / 1000) + 1e11; // positive, fixed width
	return `${String(seconds).padStart(13, '0')}.${(m[3] ?? '').padEnd(9, '0')}`;
}

/**
 * Prose questions the user dismissed by settling the session (#2324), as
 * position keys per session. A dismissed question no longer marks the
 * session (here or, for the open session, from its transcript); a later
 * question has another position, so it marks again. Unsettling keeps them.
 */
const _dismissed = createStore<ReadonlyMap<string, ReadonlySet<string>>>(new Map());

/** Subscribe to dismissals, for the open session's transcript-derived question. */
export const proseQuestionDismissals = {
	get current() { return _dismissed.current; },
	subscribe: _dismissed.subscribe
};

/** A question position as a dismissal key; `?` for a question without one. */
function positionKey(pos: Position | null | undefined): string {
	return pos ? `${pos.epoch}:${pos.seq}` : '?';
}

function forgetDismissals(sessionId: string): void {
	if (!_dismissed.current.has(sessionId)) return;
	const next = new Map(_dismissed.current);
	next.delete(sessionId);
	_dismissed.set(next);
}

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

/** The question that marks the session, or undefined when nothing does. */
function markingQuestion(sessionId: string): Position | null | undefined {
	const row = rowMarked(sessionId);
	if (row !== undefined) return row ? _rowQuestions.get(sessionId) ?? null : undefined;
	return _questions.has(sessionId) ? _questions.get(sessionId) ?? null : undefined;
}

/** Publish the session's mark: the row's when it has one, else the transcript's. */
function refresh(sessionId: string): void {
	const question = markingQuestion(sessionId);
	const marked = question !== undefined && !_dismissed.current.get(sessionId)?.has(positionKey(question));
	if (marked === _store.current.has(sessionId)) return;
	const next = new Set(_store.current);
	if (marked) next.add(sessionId);
	else next.delete(sessionId);
	_store.set(next);
}

function drop(sessionId: string): void {
	_questions.delete(sessionId);
	// A dismissed question without a position cannot be told from a later
	// one, so its dismissal ends once the transcript shows it answered.
	const dismissed = _dismissed.current.get(sessionId);
	if (dismissed?.has('?')) {
		const keys = new Set(dismissed);
		keys.delete('?');
		const next = new Map(_dismissed.current);
		if (keys.size) next.set(sessionId, keys); else next.delete(sessionId);
		_dismissed.set(next);
	}
	refresh(sessionId);
}

/**
 * The position keys of every prose question that currently marks the
 * session: the remembered/row-derived one and, for the open session, the
 * trailing question of its loaded transcript.
 */
export function proseQuestionKeys(
	sessionId: string,
	openMessages?: readonly ChatMessage[] | null
): string[] {
	const keys: string[] = [];
	const question = markingQuestion(sessionId);
	if (question !== undefined) keys.push(positionKey(question));
	if (openMessages && lastProseAsksQuestion(openMessages)) {
		const trailing = trailingProse(openMessages);
		keys.push(positionKey(trailing ? toPosition(trailing.seq, trailing.epoch) : null));
	}
	return keys;
}

/** Settle dismissed these prose questions (#2324): stop counting them. */
export function dismissProseQuestions(sessionId: string, keys: readonly string[]): void {
	if (!sessionId || keys.length === 0) return;
	const known = _dismissed.current.get(sessionId);
	if (known && keys.every((k) => known.has(k))) return;
	const next = new Map(_dismissed.current);
	next.set(sessionId, new Set([...(known ?? []), ...keys]));
	_dismissed.set(next);
	refresh(sessionId);
}

/** Whether the open session's trailing transcript question was dismissed. */
export function isTranscriptProseDismissed(
	dismissals: ReadonlyMap<string, ReadonlySet<string>>,
	sessionId: string | null | undefined,
	messages: readonly ChatMessage[] | null | undefined
): boolean {
	if (!sessionId || !messages) return false;
	const keys = dismissals.get(sessionId);
	if (!keys || !lastProseAsksQuestion(messages)) return false;
	const trailing = trailingProse(messages);
	return keys.has(positionKey(trailing ? toPosition(trailing.seq, trailing.epoch) : null));
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
	let epochs = _questionEpochs.get(sessionId);
	if (!epochs) {
		epochs = new Set();
		_questionEpochs.set(sessionId, epochs);
	}
	if (!epochs.has(epoch) && epochs.size >= EPOCHS_KEPT) {
		const oldest = epochs.values().next().value;
		if (oldest !== undefined) epochs.delete(oldest);
	}
	epochs.add(epoch);
	const pos = toPosition(seq, epoch);
	_rowQuestions.set(sessionId, pos);
}

/** Whether the revision carries either revision pair. */
function carriesPairs(revision: UserMessageRevision): boolean {
	return revision.last_assistant_question_seq !== undefined
		|| revision.last_user_message_seq !== undefined
		|| revision.last_user_message_epoch !== undefined;
}

/**
 * Whether the revision's pairs are current, so they may replace the row-derived
 * state. Positions cannot order revisions (a retry restores an earlier
 * question with the same user pair a delayed snapshot has), so they are ordered
 * by the row's own write time, which moves whenever either pair does:
 * - a row is applied when its `updated_at` is at least the last applied one
 *   (ties accept), and must be strictly newer after a pushed frame, which
 *   reflects a write later than the last applied row;
 * - a pushed frame or `/live` snapshot that carries `updated_at` is ordered
 *   the same way;
 * - without it, a pushed frame is applied in arrival order and a `/live`
 *   snapshot only seeds a session no row or frame has set yet;
 * - an `updated_at` this tab synthesised (`noteLocalRowStamp`) never orders.
 * Timestamps compare at full (microsecond) precision (`versionKey`).
 * Records the applied version.
 */
function isCurrent(sessionId: string, revision: UserMessageRevision, source: RevisionSource): boolean {
	if (!carriesPairs(revision)) return false;
	// This tab's own clock is not a server version: not ordered, not applied.
	// (Every pair change also reaches here as a list row or a frame.)
	if (typeof revision.updated_at === 'string' && _localStamps.get(sessionId)?.includes(revision.updated_at)) {
		return false;
	}
	const known = _applied.get(sessionId);
	const at = versionKey(revision.updated_at);
	if (at === null) {
		if (source.snapshot) return !known;
		_applied.set(sessionId, { at: known?.at ?? null, pushedSince: true });
		return true;
	}
	if (known && known.at !== null && (at < known.at || (known.pushedSince && at === known.at))) return false;
	_applied.set(sessionId, { at, pushedSince: false });
	return true;
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
	if (meta) observeLastUserMessage(sessionId, meta, { snapshot: true });
}

/**
 * A session's revision as delivered by a row, a `user_session_updated` frame
 * or a `/live` snapshot: its user-message pair and, from a worker that stamps
 * it, its trailing prose question pair (#2263). Drops the remembered question
 * when the revision is a later user message in the question's log.
 */
export function observeLastUserMessage(
	sessionId: string | null | undefined,
	revision: UserMessageRevision | null | undefined,
	source: RevisionSource = {}
): void {
	if (!sessionId || !revision) return;
	if (isCurrent(sessionId, revision, source)) {
		observeQuestion(sessionId, revision);
		observeUser(sessionId, revision);
	}
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
			row.last_assistant_question_seq, row.last_assistant_question_epoch, row.updated_at
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
		for (const epoch of _questionEpochs.get(sessionId) ?? []) retired.add(epoch);
		for (const pos of [_questions.get(sessionId), _rowQuestions.get(sessionId), _lastUser.get(sessionId)]) {
			if (pos) retired.add(pos.epoch);
		}
		if (retired.size) _retired.set(sessionId, retired);
	} else {
		_retired.delete(sessionId);
	}
	_questions.delete(sessionId);
	_rowQuestions.delete(sessionId);
	_questionEpochs.delete(sessionId);
	_applied.delete(sessionId);
	if (!opts.cleared) _localStamps.delete(sessionId);
	_lastUser.delete(sessionId);
	_latest.delete(sessionId);
	_rowRaw.delete(sessionId);
	_claims.delete(sessionId);
	forgetDismissals(sessionId);
	refresh(sessionId);
}

/** Test seam: forget everything. */
export function _resetProseQuestions(): void {
	_questions.clear();
	_latest.clear();
	_claims.clear();
	_rowRaw.clear();
	_rowQuestions.clear();
	_questionEpochs.clear();
	_applied.clear();
	_localStamps.clear();
	_lastUser.clear();
	_retired.clear();
	_dismissed.set(new Map());
	_store.set(new Set());
}

/** The set without one session (the same set when it is not in it). */
export function withoutSession(set: ReadonlySet<string>, sessionId: string | null): ReadonlySet<string> {
	if (!sessionId || !set.has(sessionId)) return set;
	const next = new Set(set);
	next.delete(sessionId);
	return next;
}
