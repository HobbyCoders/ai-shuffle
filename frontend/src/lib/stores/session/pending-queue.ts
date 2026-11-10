/**
 * pending-queue — the single arbiter of which "not seen yet" bubbles survive.
 *
 * Why this exists
 * ---------------
 * A pending bubble has TWO producers and they are not ordered against each
 * other:
 *
 * 1. The optimistic local echo appended by `message-sender` the instant the
 *    user sends into a live turn (`queued-local-*`).
 * 2. `queue_updated` — a full REPLACE-ALL snapshot of the server-side queue,
 *    which the backend emits from two independent places: the socket that
 *    handled the frame (`_v2_broadcast_queue`, a direct send) and the sync
 *    engine fan-out (`_broadcast_queue_after_start`, fired by the turn pump
 *    when a steer is absorbed). Neither carries a sequence number.
 *
 * So a snapshot taken BEFORE a message was absorbed can arrive AFTER the
 * snapshot that cleared it — e.g. the rebroadcast that answers an × click is
 * computed while the row is still queued, then lands behind the pump's clearing
 * frame. Replace-all then resurrects a row whose text is already answered in the
 * transcript, and nothing further is ever emitted: the bubble sits there
 * claiming the model has not seen the message until the page is reloaded.
 *
 * The #588 self-heal cannot catch that one: it matches a queue entry's id
 * against the turn ids of delivered `message.user` events, and a STEERED
 * message is recorded on the LIVE turn's id, never on the queued row's own
 * (app/core/session_runtime.py::_on_steer_delivered →
 * TurnRecorder.record_steered_user_message). The ids differ by construction.
 *
 * What this module adds
 * ---------------------
 * A session-scoped memory of what the client has already observed, applied on
 * top of every queue write, using an internal monotonic counter rather than
 * wall-clock time (server `created_at` and browser `Date.now()` are different
 * clocks and must never be compared):
 *
 * - Every entry id is stamped with the counter value at FIRST sighting.
 * - Every user message delivered to the LIVE transcript is remembered with the
 *   counter value at arrival.
 * - An entry is pruned when its text was delivered AFTER we already knew about
 *   the entry — i.e. the delivery answered THIS bubble. A row we meet for the
 *   first time after a delivery is a genuinely NEW pending row (the backend
 *   re-queues a message that was absorbed into a turn the user then interrupted,
 *   as a fresh turn row) and is left alone: the server list still rules what
 *   EXISTS.
 * - A prune is sticky by entry id, so a late stale snapshot cannot resurrect a
 *   bubble that was already answered.
 * - Each delivery retires at most ONE entry, so sending the same text twice
 *   never collapses both bubbles onto one answer.
 *
 * It also owns send admission and cancellation: an optimistic echo survives
 * until its own command is admitted, rejected or receipted. An × carries that
 * command id. A cancellation stays visible until terminal evidence or a bounded
 * timeout; stale snapshots cannot resurrect a receipted command.
 *
 * Switching sessions (#524)
 * -------------------------
 * The bubbles of a session the user leaves are PARKED here, keyed by session,
 * and handed back the moment the user returns, so they never blink out while
 * the return `watch_session` snapshot is in flight. They are a projection, not
 * a second authority: an available snapshot still replaces them, and a
 * transcript `message.user` carrying an entry's command id retires it. An
 * `unavailable` snapshot (the executing worker could not be read) is not an
 * empty queue, so parked bubbles stay up, marked stale, until one that is.
 */

import type { ChatMessage, PendingPeerMessage, QueuedMessage } from '../types';
import type { UploadReference } from '$lib/api/rewind';

/** Peer delivery uses durable turn ids ONLY, never the user's text/unsend arbiter. */
/** A send the owner may have taken with its answer lost: it may still run. */
export const SEND_MAYBE_APPLIED_MESSAGE =
	'The worker that runs this chat did not answer in time. Your message may still run.';

export function reconcilePendingPeerMessages(
	rows: PendingPeerMessage[], messages: ChatMessage[]
): PendingPeerMessage[] {
	// Most folds have no peer deliveries pending. There is nothing to reconcile,
	// so preserve the empty array and avoid scanning the entire transcript.
	if (rows.length === 0) return rows;
	const delivered = new Set(messages.filter(m => m.role === 'user').map(m => m.queuedTurnId ?? m.turnId));
	const seen = new Set<string>();
	return rows.filter(row => {
		if (delivered.has(row.id) || seen.has(row.id)) return false;
		seen.add(row.id);
		return true;
	});
}

export interface PendingEntry {
	id: string;
	content: string;
	queuedAt: number;
	clientMessageId?: string;
	status?: QueuedMessage['status'];
	unconfirmed?: boolean;
	uploadPaths?: string[];
	recorded?: boolean;
}

/** Legacy optimistic ids, retained for old snapshots/tests (new sends use client ids). */
export const LOCAL_PENDING_PREFIX = 'queued-local-';

/** Recent deliveries worth keeping — older ones can no longer answer a bubble. */
const MAX_TRACKED_DELIVERIES = 50;

export function isLocalPendingId(id: string): boolean {
	return id.startsWith(LOCAL_PENDING_PREFIX);
}

/** Stable command identity, carried by the send, queue, engine and receipt.
 * getRandomValues also works on LAN HTTP, unlike randomUUID. */
export function newLocalPendingId(): string {
	const bytes = crypto.getRandomValues(new Uint8Array(16));
	return `client-${Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')}`;
}

interface Delivery {
	content: string;
	seq: number;
	/** The delivered command, when the receipt carries one: it then answers only its own bubble. */
	commandId?: string;
	/** Set on the message that opened this turn: a drained row's id IS its turn id, so it answers that row only. */
	turnId?: string;
	/** Set on a steer: the running turn it joined. It is never that turn's own row. */
	joinedTurnId?: string;
	/** The entry this delivery retired — a delivery answers exactly one bubble. */
	matchedEntryId: string | null;
	createdAt?: number;
}

let sessionId: string | null = null;
let seq = 0;
/** Wall clock of the last authoritative `queue_updated` for this session. */
let lastAuthorityAt = 0;
/** Entry id → counter value when we first saw it. */
const firstSeen = new Map<string, number>();
/** Turn ids delivered on the transcript (#588: a drained queue row's id IS its turn id). */
const deliveredTurnIds = new Set<string>();
/** Entry ids already retired — sticky, so a stale snapshot cannot resurrect them. */
const prunedIds = new Set<string>();
let deliveries: Delivery[] = [];
/**
 * Command ids answered by HISTORY replayed into this feed, with the server time
 * of the answering `message.user` (NaN when unknown). Replay order says nothing
 * about when a bubble was first seen, so these are matched by identity and
 * server clock instead: a backend re-queue (an interrupted steer, run again)
 * keeps its command id but is a fresh row created AFTER that receipt.
 */
const replayedCommands = new Map<string, number>();
/** Most sessions parked at once; the oldest falls back to its snapshot alone. */
const MAX_PARKED_SESSIONS = 50;
/** Session id → the pending bubbles it showed when the user left it. */
const parked = new Map<string, PendingEntry[]>();
// Keep an unresolved cancellation visible even if a queue-cleared frame wins
// the race with its verdict. No text matching or expiring local-id map.
interface CancellationState {
	removals: Map<string, PendingEntry>;
	withdrawn: Set<string>;
	deliveredRows: Set<string>;
	terminalVerdicts: Map<string, string>;
	drafts: WithdrawnDraft[];
	timers: Map<string, ReturnType<typeof setTimeout>>;
	sends: Map<string, PendingEntry>;
	/** Command id → marks its send unconfirmed now (the admission timeout, run early). */
	unconfirm: Map<string, () => void>;
	/** Confirmed rows a snapshot dropped, still on screen until their own delivery (#1052). */
	delivering: Map<string, PendingEntry>;
	/** Row ids whose delivery hold timed out or was withdrawn: gone until a snapshot lists them. */
	releasedRows: Set<string>;
}
// Cancellation transactions belong to a session, not its currently mounted
// feed. A socket verdict can arrive after that feed has been torn down.
const cancellations = new Map<string | null, CancellationState>();
/** A withdrawn or refused send's words, with the attachments it carried. */
interface WithdrawnDraft { content: string; uploads?: SendUploads }
interface SendUploads { sessionId: string | null; paths: string[]; references: UploadReference[] }
/** What a confirmed withdrawal hands back to the composer. */
export interface WithdrawnRestore { prompt: string; uploadPaths: string[]; uploadReferences: UploadReference[] }
type RestoreSlots = { pendingRestorePrompt?: string | null; pendingRestoreUploadPaths?: string[]; pendingRestoreUploadReferences?: UploadReference[] };
/** Adds a confirmed withdrawal's words and attachments to those not yet taken. */
export function restoredDraftFields(s: RestoreSlots, restore: WithdrawnRestore | null): RestoreSlots {
	if (!restore) return {};
	const paths = [...(s.pendingRestoreUploadPaths ?? []), ...restore.uploadPaths];
	const references = [...(s.pendingRestoreUploadReferences ?? []), ...restore.uploadReferences];
	return { pendingRestorePrompt: [s.pendingRestorePrompt, restore.prompt].filter(Boolean).join('\n\n'),
		...(paths.length ? { pendingRestoreUploadPaths: paths } : {}),
		...(references.length ? { pendingRestoreUploadReferences: references } : {}) };
}
// A send's attachments, by command id (unique across chats), kept past admission
// until its receipt: the chip keeps only text, and a confirmed withdrawal must hand
// back the file chips and upload fields too, or the resend cannot transfer the file.
// Never capped: a send still waiting to run keeps them. Every terminal outcome
// removes its entry instead (receipt, delivery verdict, withdrawal verdict or
// receipt, refusal, a frame that never left, the chat's deletion; see
// forgetSendUploads, settleWithdrawalReceipt and forgetSession).
const sendUploads = new Map<string, SendUploads>();
function draftOf(key: string, content: string, recordedPaths: string[] = []): WithdrawnDraft {
	const uploads = sendUploads.get(key);
	sendUploads.delete(key);
	if (uploads) return { content, uploads };
	// A row this page did not send (reload, another device) still names the
	// paths its send recorded, so they come back as chips, not raw @paths.
	return recordedPaths.length ? { content, uploads: { sessionId: null, paths: [...recordedPaths], references: [] } } : { content };
}
// Idle transcript echoes are recoverable drafts, not queue authority. The
// command survives null -> assigned-session adoption; receipts clear it by id.
const recoverySends = new Map<string, { sessionId: string | null; content: string; expiresAt: number }>();
const MAX_RECOVERY_SENDS = 50;
const RECOVERY_SEND_TTL_MS = 30 * 60_000;
function pruneRecoverySends(): void {
	const now = Date.now();
	for (const [key, send] of recoverySends) if (send.expiresAt <= now) recoverySends.delete(key);
}
function recoverySend(id: string | null, key: string) {
	pruneRecoverySends();
	const send = recoverySends.get(key);
	return send?.sessionId === id ? send : undefined;
}
function cancellationState(id: string | null): CancellationState {
	let state = cancellations.get(id);
	if (!state) {
		state = { removals: new Map(), withdrawn: new Set(), deliveredRows: new Set(), terminalVerdicts: new Map(), drafts: [], timers: new Map(), sends: new Map(), unconfirm: new Map(), delivering: new Map(), releasedRows: new Set() };
		cancellations.set(id, state);
	}
	return state;
}
const removalKey = (entry: PendingEntry) => entry.clientMessageId ?? entry.id;
/**
 * How long a confirmed row a snapshot dropped stays up waiting for its delivery
 * (#1052). The snapshot (WebSocket) and the delivered row (events feed) take
 * separate routes; the row usually lands 50-170 ms later, but a failing feed
 * retry once took 9.7 s. Same bound as admission and cancellation.
 */
export const DELIVERY_HOLD_MS = 10_000;

function normalize(content: string): string {
	return content.trim();
}

function resetState(next: string | null): void {
	sessionId = next;
	seq = 0;
	lastAuthorityAt = 0;
	firstSeen.clear();
	deliveredTurnIds.clear();
	prunedIds.clear();
	deliveries = [];
	replayedCommands.clear();
}

/** A bubble no server snapshot has described yet: this tab's own echo. */
function isOptimistic(entry: PendingEntry): boolean {
	return entry.id === entry.clientMessageId || isLocalPendingId(entry.id);
}

/** A server re-queue is a new attempt, despite retaining the command id. */
function answeredByReceipt(entry: PendingEntry, answeredAt: number): boolean {
	// An echo cannot be a re-queue (those only exist server-side). A server row
	// is consumed when it predates the receipt on the SAME (server) clock.
	return isOptimistic(entry) || (Number.isFinite(answeredAt)
		&& Number.isFinite(entry.queuedAt) && entry.queuedAt < answeredAt);
}

/** Has replayed history already answered this bubble's command? */
function answeredByHistory(entry: PendingEntry): boolean {
	if (!entry.clientMessageId) return false;
	const answeredAt = replayedCommands.get(entry.clientMessageId);
	return answeredAt !== undefined && answeredByReceipt(entry, answeredAt);
}

/**
 * Has this page seen the message of a `recorded` row? A re-queued steer keeps
 * its command id, so any receipt for that command, live or replayed, is it.
 */
function sawRecordedMessage(id: string | null, entry: PendingEntry): boolean {
	const command = entry.clientMessageId;
	if (!command || id !== sessionId) return false;
	return replayedCommands.has(command) || deliveries.some(d => d.commandId === command);
}

/** Bind feed observations to a session; cancellation transactions are separate. */
function useSession(id: string | null): void {
	if (id !== sessionId) resetState(id);
}

/** Remember an id the first time it is seen; later sightings keep the original stamp. */
function stamp(id: string): number {
	const known = firstSeen.get(id);
	if (known !== undefined) return known;
	const value = ++seq;
	firstSeen.set(id, value);
	return value;
}

export const pendingQueue = {
	/** Reset feed observations, not outstanding cancellations. A full reset is
	 * reserved for discarding the entire client store (e.g. test isolation). */
	reset(id: string | null = null, clearCancellations = false): void {
		resetState(id);
		if (clearCancellations) {
			for (const state of cancellations.values()) for (const timer of state.timers.values()) clearTimeout(timer);
			cancellations.clear();
			parked.clear();
			recoverySends.clear();
			sendUploads.clear();
		}
	},

	/**
	 * Keep the bubbles of a session being left (#524). Statuses ride along; the
	 * cancellation transaction they mirror is session-scoped already.
	 */
	park(id: string | null, entries: PendingEntry[]): void {
		if (!id) return;
		parked.delete(id);
		if (entries.length === 0) return;
		parked.set(id, entries.map(e => ({ ...e })));
		while (parked.size > MAX_PARKED_SESSIONS) {
			const oldest = parked.keys().next().value;
			if (oldest === undefined) break;
			parked.delete(oldest);
		}
	},

	/**
	 * The bubbles to show the instant a session is re-opened, before its queue
	 * snapshot lands. Withdrawn entries stay gone; everything else waits for the
	 * server to say it was consumed or cancelled.
	 */
	unpark(id: string | null): PendingEntry[] {
		if (!id) return [];
		const entries = parked.get(id);
		parked.delete(id);
		if (!entries) return [];
		const { withdrawn } = cancellationState(id);
		return entries.filter(e => !withdrawn.has(removalKey(e)) && !withdrawn.has(e.id));
	},

	/** Test/diagnostic hook: the bubbles parked for a session. */
	parkedFor(id: string): PendingEntry[] {
		return parked.get(id) ?? [];
	},

	/** Terminal evidence survives the bubble it retires (screen readers). */
	removalVerdict(id: string | null, key: string): string | undefined {
		return cancellations.get(id)?.terminalVerdicts.get(key);
	},

	/** Consume confirmed withdrawals only when their own composer is mounted. */
	takeWithdrawnDraft(id: string | null): string | null {
		return this.takeWithdrawnRestore(id)?.prompt ?? null;
	},

	/** Like takeWithdrawnDraft, with the attachments the withdrawn sends carried. */
	takeWithdrawnRestore(id: string | null): WithdrawnRestore | null {
		const state = cancellations.get(id);
		if (!state?.drafts.length) return null;
		const drafts = state.drafts.splice(0);
		return { prompt: drafts.map(d => d.content).join('\n\n'),
			uploadPaths: drafts.flatMap(d => d.uploads?.paths ?? []),
			uploadReferences: drafts.flatMap(d => d.uploads?.references ?? []) };
	},

	/** Remember a send's attachments until its receipt or a confirmed withdrawal. */
	noteSendUploads(id: string | null, key: string, paths: string[] = [], references: UploadReference[] = []): void {
		sendUploads.delete(key);
		if (!paths.length && !references.length) return;
		sendUploads.set(key, { sessionId: id, paths: [...paths], references: [...references] });
	},

	/** The upload paths this page recorded for its own send, for its chips. */
	sendUploadPaths(key: string | undefined): string[] {
		const uploads = key ? sendUploads.get(key) : undefined;
		return uploads ? [...uploads.paths, ...uploads.references.map(reference => reference.path)] : [];
	},

	/** Test/diagnostic hook: whether a send's attachments are still kept. */
	hasSendUploads(key: string): boolean {
		return sendUploads.has(key);
	},

	/**
	 * The send reached a terminal outcome with nothing to hand back here. A
	 * withdrawal this page asked for keeps them until its own verdict takes them.
	 */
	forgetSendUploads(key: string): void {
		for (const state of cancellations.values()) if (state.removals.has(key)) return;
		sendUploads.delete(key);
	},

	/**
	 * The durable receipt that a send was withdrawn. It proves what a socket
	 * verdict would say, and the verdict can be lost after Unsend timed out, so
	 * a withdrawal this page asked for settles here: the words and files move to
	 * the draft, and a verdict arriving later finds nothing left to do. True when
	 * it settled one, so the caller can hand the draft to an open composer.
	 *
	 * A receipt for an older attempt must not end the cancellation of a newer
	 * server re-queue of the same command; it leaves that attempt's files alone.
	 */
	settleWithdrawalReceipt(key: string, createdAt: string | undefined, live: boolean): boolean {
		// Withdrawn anywhere (another tab or device too): never hold it for a delivery.
		for (const id of [...cancellations.keys()]) this.releaseHeld(id, key);
		for (const [id, state] of cancellations) {
			const removal = state.removals.get(key);
			if (!removal) continue;
			const answeredAt = createdAt ? Date.parse(createdAt) : NaN;
			if (!answeredByReceipt(removal, answeredAt) && !(live && !Number.isFinite(answeredAt))) return false;
			this.applyRemovalVerdict(id, key, 'withdrawn');
			return true;
		}
		sendUploads.delete(key);
		return false;
	},

	/** The chat was deleted: nothing it held can still run or be handed back. */
	forgetSession(id: string): void {
		for (const timer of cancellations.get(id)?.timers.values() ?? []) clearTimeout(timer);
		cancellations.delete(id);
		parked.delete(id);
		for (const [key, send] of recoverySends) if (send.sessionId === id) recoverySends.delete(key);
		for (const [key, uploads] of sendUploads) if (uploads.sessionId === id) sendUploads.delete(key);
	},

	/**
	 * True when an authoritative `queue_updated` for this session landed at or
	 * after `dispatchedAt`. The server has already described the queue for the
	 * window this send lives in, so an optimistic echo would be a second copy
	 * that no later frame is guaranteed to replace.
	 */
	authoritySpokeSince(id: string | null, dispatchedAt: number): boolean {
		if (id !== sessionId) return false;
		return lastAuthorityAt >= dispatchedAt;
	},

	/** Register the optimistic echo appended for a send into a live turn. */
	noteOptimistic(id: string | null, entryId: string): void {
		useSession(id);
		stamp(entryId);
	},

	/**
	 * Record an authoritative queue snapshot. Call BEFORE reconciling it — every
	 * row in it is "known" from this moment on, which is what makes a row that
	 * first appears AFTER a delivery (a re-queued message) immune to pruning.
	 */
	noteAuthoritative(id: string | null, rows: PendingEntry[]): void {
		useSession(id);
		lastAuthorityAt = Date.now();
		const state = cancellations.get(id);
		for (const row of rows) {
			stamp(row.id);
			// Listed again: the server says it exists, so it is neither held nor released.
			state?.releasedRows.delete(row.id);
			if (state?.delivering.has(removalKey(row))) this.endHold(id, removalKey(row));
			if (row.clientMessageId) this.acknowledgeSend(id, row.clientMessageId);
		}
	},

	/**
	 * Record a user message that landed on the transcript.
	 *
	 * `live` is false while the SSE backfill replays history: an old message is
	 * not an answer to anything pending now, and content-matching it would eat a
	 * legitimately queued repeat of the same prompt. Turn ids are harvested in
	 * both phases — that match is exact.
	 *
	 * `steered` is the recorder's own mark for a message that joined a running
	 * turn (`checkpoint_backed: false`): its turn id is that turn's, not a row's.
	 */
	noteDelivered(
		id: string | null,
		content: string,
		turnId?: string,
		live = true,
		commandId?: string,
		createdAt?: string,
		steered = false
	): void {
		useSession(id);
		// Only the message that OPENS a turn names a queue row by turn id. A steer
		// names no row of its own, so it must stay free to retire its bubble by
		// text. The recorder marks a steer; an older worker's unmarked one is a
		// later message with a turn id already seen.
		const opensTurn = !!turnId && !steered && !deliveredTurnIds.has(turnId);
		if (turnId) deliveredTurnIds.add(turnId);
		if (commandId) {
			this.acknowledgeSend(id, commandId);
			sendUploads.delete(commandId);
			const removal = cancellations.get(id)?.removals.get(commandId);
			const answeredAt = createdAt ? Date.parse(createdAt) : NaN;
			// A replayed (or late live) receipt for an interrupted attempt must
			// not end cancellation of its newer server re-queue.
			if (!removal || answeredByReceipt(removal, answeredAt) || (live && !Number.isFinite(answeredAt))) {
				this.applyRemovalVerdict(id, commandId, 'already_delivered');
			}
		}
		if (!live) {
			// The command id is an exact handle, unlike text, so history may use it.
			if (commandId) replayedCommands.set(commandId, createdAt ? Date.parse(createdAt) : NaN);
			return;
		}
		const text = normalize(content);
		if (!text && !commandId) return;
		deliveries.push({ content: text, seq: ++seq, matchedEntryId: null, createdAt: createdAt ? Date.parse(createdAt) : NaN, ...(commandId ? { commandId } : {}), ...(opensTurn ? { turnId } : turnId ? { joinedTurnId: turnId } : {}) });
		// A long session delivers a lot of messages and only the recent ones can
		// still be answering something pending. Forgetting the oldest only ever
		// makes pruning more conservative.
		if (deliveries.length > MAX_TRACKED_DELIVERIES) {
			deliveries = deliveries.slice(-MAX_TRACKED_DELIVERIES);
		}
	},

	/** An × requests a verdict; it is not evidence of withdrawal. */
	requestRemoval(id: string | null, entry: PendingEntry, onTimeout?: () => void): string {
		const key = removalKey(entry);
		stamp(entry.id);
		const state = cancellationState(id);
		this.removalFailed(id, key);
		state.removals.set(key, { ...entry, status: 'cancelling' });
		state.timers.set(key, setTimeout(() => {
			this.removalFailed(id, key);
			onTimeout?.();
		}, 10_000));
		return key;
	},

	removalFailed(id: string | null, key: string, terminal = false): void {
		const state = cancellations.get(id);
		clearTimeout(state?.timers.get(key));
		state?.timers.delete(key);
		const entry = state?.removals.get(key);
		if (terminal) state?.removals.delete(key);
		else if (entry) state!.removals.set(key, { ...entry, status: undefined });
		const rows = parked.get(id ?? '');
		if (rows) parked.set(id!, rows.map(e => removalKey(e) === key ? { ...e, status: undefined } : e));
	},

	/**
	 * #1052: an authoritative snapshot no longer lists a confirmed bubble that was
	 * on screen. The server took it out of the queue to run it, but its delivered
	 * `message.user` row comes on the events feed, a separate route that usually
	 * lands later. Keep the bubble until that row retires it in `reconcile` (by
	 * command id, turn id, or the existing text rule for rows without ids) or
	 * DELIVERY_HOLD_MS passes; `onRelease` then re-folds the queue.
	 *
	 * Only rows already confirmed and waiting are held: an echo still awaiting
	 * admission (or unconfirmed) is kept by `sends`, a cancelling row by
	 * `removals`, and a withdrawn, delivered or pruned row is gone for good.
	 * A `recorded` row's run adds no transcript row. A re-queued steer is held
	 * only until this page has seen the message it already recorded (the events
	 * feed can trail the queue), which is at once when it has: `inTranscript`
	 * answers for a transcript this page opened without replaying it. A row with
	 * no command id (a voice hand-off) has no message of its own to wait for.
	 */
	holdDelivering(
		id: string | null, previous: PendingEntry[], rows: PendingEntry[], onRelease: () => void,
		inTranscript?: (commandId: string) => boolean
	): void {
		const state = cancellationState(id);
		const listed = new Set(rows.map(removalKey));
		for (const entry of previous) {
			const key = removalKey(entry);
			if (listed.has(key) || entry.status || entry.unconfirmed || state.delivering.has(key)
				|| (entry.recorded && (!entry.clientMessageId || sawRecordedMessage(id, entry)
					|| inTranscript?.(entry.clientMessageId)))
				|| state.sends.has(key) || state.removals.has(key)
				|| state.withdrawn.has(key) || state.withdrawn.has(entry.id) || state.releasedRows.has(entry.id)
				|| state.deliveredRows.has(entry.id) || prunedIds.has(entry.id)) continue;
			state.delivering.set(key, { ...entry });
			state.timers.set(`deliver:${key}`, setTimeout(() => {
				if (this.releaseHeld(id, key)) onRelease();
			}, DELIVERY_HOLD_MS));
		}
	},

	/** Whether this bubble is held for its delivery: the server no longer has it to withdraw. */
	isHeld(id: string | null, entry: PendingEntry): boolean {
		return cancellations.get(id)?.delivering.has(removalKey(entry)) ?? false;
	},

	/** End a hold without hiding the row (its delivery or a snapshot now speaks for it). */
	endHold(id: string | null, key: string): void {
		const state = cancellations.get(id);
		clearTimeout(state?.timers.get(`deliver:${key}`));
		state?.timers.delete(`deliver:${key}`);
		state?.delivering.delete(key);
	},

	/**
	 * Take a held bubble down: its delivery did not show in time, or it was
	 * withdrawn elsewhere. True when one was held, so the caller re-folds.
	 */
	releaseHeld(id: string | null, key: string): boolean {
		const entry = cancellations.get(id)?.delivering.get(key);
		if (!entry) return false;
		this.endHold(id, key);
		cancellations.get(id)!.releasedRows.add(entry.id);
		return true;
	},

	/** Keep recoverable words until admission/refusal/receipt. Without a timeout
	 * callback this is an idle transcript draft, never an optimistic queue row. */
	trackSend(id: string | null, entry: PendingEntry, onTimeout?: () => void): void {
		const key = removalKey(entry);
		if (!onTimeout) {
			pruneRecoverySends();
			recoverySends.delete(key);
			recoverySends.set(key, { sessionId: id, content: entry.content, expiresAt: Date.now() + RECOVERY_SEND_TTL_MS });
			if (recoverySends.size > MAX_RECOVERY_SENDS) recoverySends.delete(recoverySends.keys().next().value!);
			return;
		}
		const state = cancellationState(id);
		state.sends.set(key, entry);
		const unconfirm = () => {
			clearTimeout(state.timers.get(`send:${key}`));
			state.timers.delete(`send:${key}`);
			state.unconfirm.delete(key);
			state.sends.set(key, { ...entry, unconfirmed: true });
			onTimeout();
		};
		state.unconfirm.set(key, unconfirm);
		state.timers.set(`send:${key}`, setTimeout(unconfirm, 10_000));
	},

	/**
	 * The worker that runs the chat may have taken this send, but its answer was
	 * lost. That is neither a refusal nor an admission: the send is unconfirmed
	 * now, exactly as if its admission had timed out. Its chip stays (Unsend
	 * recovers it through a verdict), its words are not restored, and a later
	 * owner snapshot, receipt or withdrawal settles it. False for a send with no
	 * queued chip (an idle transcript echo), which just stays as it is.
	 */
	answerLost(id: string | null, key: string): boolean {
		const unconfirm = cancellations.get(id)?.unconfirm.get(key);
		unconfirm?.();
		return !!unconfirm;
	},

	/** Refusals follow the command's compose, not the socket's current session. */
	sendOwner(key: string): { sessionId: string | null } | undefined {
		pruneRecoverySends();
		const send = recoverySends.get(key);
		if (send) return { sessionId: send.sessionId };
		for (const [sessionId, state] of cancellations) {
			if (state.sends.has(key) || state.removals.has(key)) return { sessionId };
		}
	},

	/** Only the commands in an authenticated new-session adoption move with it. */
	adoptSession(id: string, commandIds: (string | undefined)[]): void {
		pruneRecoverySends();
		const keys = new Set(commandIds);
		for (const [key, send] of recoverySends) {
			if (send.sessionId === null && keys.has(key)) send.sessionId = id;
		}
		for (const [key, uploads] of sendUploads) {
			if (uploads.sessionId === null && keys.has(key)) uploads.sessionId = id;
		}
	},

	acknowledgeSend(id: string | null, key: string): void {
		// Admission/receipt is command-wide; refusal recovery is ownership-scoped.
		recoverySends.delete(key);
		const state = cancellations.get(id);
		clearTimeout(state?.timers.get(`send:${key}`));
		state?.timers.delete(`send:${key}`);
		state?.unconfirm.delete(key);
		state?.sends.delete(key);
	},

	rejectSend(id: string | null, key: string, fallback?: string | null): void {
		const owner = this.sendOwner(key);
		if (owner && owner.sessionId !== id) return;
		const state = cancellationState(id);
		const content = state.sends.get(key)?.content ?? recoverySend(id, key)?.content ?? fallback;
		if (content != null && !state.withdrawn.has(key)) state.drafts.push(draftOf(key, content));
		sendUploads.delete(key);
		state.withdrawn.add(key);
		this.acknowledgeSend(id, key);
		this.removalFailed(id, key, true);
	},

	applyRemovalVerdict(id: string | null, key: string, verdict: string, messageId?: string): void {
		const { removals, withdrawn, deliveredRows, terminalVerdicts, drafts } = cancellationState(id);
		const entry = removals.get(key);
		if (verdict === 'withdrawn' || verdict === 'not_found') {
			if (withdrawn.has(key)) return;
			terminalVerdicts.set(key, verdict);
			withdrawn.add(key);
			if (entry) {
				withdrawn.add(entry.id);
				drafts.push(draftOf(key, entry.content, entry.uploadPaths));
			}
			sendUploads.delete(key);
			this.removalFailed(id, key, true);
			this.acknowledgeSend(id, key);
			this.endHold(id, key);
		} else if (verdict === 'already_delivered') {
			// Retire both names of this attempt, not future rows for the command.
			// The durable id can arrive in a verdict before its queue snapshot.
			if (messageId) deliveredRows.add(messageId);
			// A command survives re-queueing; its old attempt's late verdict
			// must not clear the newer attempt's cancellation or draft.
			if (entry && messageId && !isOptimistic(entry) && entry.id !== messageId) return;
			if (entry) deliveredRows.add(entry.id);
			sendUploads.delete(key);
			terminalVerdicts.set(key, verdict);
			this.removalFailed(id, key, true);
			this.acknowledgeSend(id, key);
		}
	},

	/**
	 * Filter a pending list against everything observed so far. Preserves the
	 * array identity when nothing changed, so folding stays a no-op in the common
	 * case.
	 */
	reconcile(id: string | null, entries: PendingEntry[]): PendingEntry[] {
		useSession(id);
		const { removals, withdrawn, deliveredRows, sends, delivering, releasedRows } = cancellationState(id);
		const keys = new Set(entries.map(removalKey));
		const unresolved = new Map([...sends, ...removals]);
		const missing = (map: Map<string, PendingEntry>) => [...map].filter(([key]) => !keys.has(key)).map(([, entry]) => entry);
		// A held row left the queue's front to run: it stays where it was, first.
		const held = missing(delivering);
		const candidates = [...held, ...entries, ...missing(unresolved).filter(e => !delivering.has(removalKey(e)))];
		// A delivery that names a bubble by turn id answers that bubble, so its text
		// must not also retire another bubble with the same words.
		for (const d of deliveries) {
			if (d.matchedEntryId === null && d.turnId
				&& (prunedIds.has(d.turnId) || candidates.some(e => e.id === d.turnId))) d.matchedEntryId = d.turnId;
		}
		const kept: PendingEntry[] = [];
		for (const entry of candidates) {
			const key = removalKey(entry);
			const bornAt = stamp(entry.id), text = normalize(entry.content);
			// Identity beats first-sighting order; server timestamps still allow a
			// genuinely new re-queue after an interrupted delivered steer.
			const delivery = deliveries.find(d => d.commandId && entry.clientMessageId
				? d.commandId === entry.clientMessageId && (isOptimistic(entry)
					|| !Number.isFinite(d.createdAt) || entry.queuedAt <= d.createdAt!)
				: d.matchedEntryId === null && d.joinedTurnId !== entry.id && bornAt < d.seq && d.content === text);
			if (withdrawn.has(key) || withdrawn.has(entry.id)
				|| deliveredRows.has(entry.id) || deliveredTurnIds.has(entry.id) || prunedIds.has(entry.id)
				|| answeredByHistory(entry) || delivery
				// A held re-queued steer waits only for the message it already recorded.
				|| (entry.recorded && delivering.get(key)?.id === entry.id && sawRecordedMessage(id, entry))) {
				if (delivery) delivery.matchedEntryId = entry.id;
				prunedIds.add(entry.id);
				this.removalFailed(id, key, true);
				if (delivering.get(key)?.id === entry.id) this.endHold(id, key);
				continue;
			}
			// A hold that ran out (or was withdrawn) leaves; a snapshot listing it again brings it back.
			if (releasedRows.has(entry.id) && !delivering.has(key)) continue;
			const removal = removals.get(key);
			if (removal) {
				const retained = { ...entry, status: removal.status };
				removals.set(key, retained);
				kept.push(retained);
			} else kept.push(sends.get(key)?.unconfirmed ? { ...entry, unconfirmed: true } : entry);
		}
		return kept.length === entries.length && kept.every((e, i) => e === entries[i]) ? entries : kept;
	},
};
