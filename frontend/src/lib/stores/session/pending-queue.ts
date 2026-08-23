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
 * It also owns the two send-side edges: an optimistic echo is skipped outright
 * when the server has already spoken about the queue since the send began, and
 * an × on a local id is DEFERRED — the server has never heard of that id — and
 * re-aimed at the real row the moment it appears.
 */

export interface PendingEntry {
	id: string;
	content: string;
	queuedAt: number;
}

/** Prefix of the optimistic id `message-sender` mints for a send into a live turn. */
export const LOCAL_PENDING_PREFIX = 'queued-local-';

/** How long a deferred × keeps waiting for the server row it was aimed at. */
const REMOVAL_MAP_WINDOW_MS = 15_000;

/** Recent deliveries worth keeping — older ones can no longer answer a bubble. */
const MAX_TRACKED_DELIVERIES = 50;

export function isLocalPendingId(id: string): boolean {
	return id.startsWith(LOCAL_PENDING_PREFIX);
}

let localIdCounter = 0;

/** Id for an optimistic echo. Unique even for two sends in the same millisecond. */
export function newLocalPendingId(): string {
	return `${LOCAL_PENDING_PREFIX}${Date.now()}-${++localIdCounter}`;
}

interface Delivery {
	content: string;
	seq: number;
	/** The entry this delivery retired — a delivery answers exactly one bubble. */
	matchedEntryId: string | null;
}

interface DeferredRemoval {
	content: string;
	seq: number;
	requestedAt: number;
	mapped: boolean;
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
let deferredRemovals: DeferredRemoval[] = [];
let removalRequests: string[] = [];

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
	deferredRemovals = [];
	removalRequests = [];
}

/** Bind to a session, wiping everything learned about a different one. */
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
	/** Drop everything (session switch, feed start/stop). */
	reset(id: string | null = null): void {
		resetState(id);
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
		for (const row of rows) stamp(row.id);
	},

	/**
	 * Record a user message that landed on the transcript.
	 *
	 * `live` is false while the SSE backfill replays history: an old message is
	 * not an answer to anything pending now, and content-matching it would eat a
	 * legitimately queued repeat of the same prompt. Turn ids are harvested in
	 * both phases — that match is exact.
	 */
	noteDelivered(
		id: string | null,
		content: string,
		turnId?: string,
		live = true
	): void {
		useSession(id);
		if (turnId) deliveredTurnIds.add(turnId);
		if (!live) return;
		const text = normalize(content);
		if (!text) return;
		deliveries.push({ content: text, seq: ++seq, matchedEntryId: null });
		// A long session delivers a lot of messages and only the recent ones can
		// still be answering something pending. Forgetting the oldest only ever
		// makes pruning more conservative.
		if (deliveries.length > MAX_TRACKED_DELIVERIES) {
			deliveries = deliveries.slice(-MAX_TRACKED_DELIVERIES);
		}
	},

	/**
	 * Answer an × click. Returns the id to un-send server-side, or null when the
	 * click landed on an optimistic echo the server has never heard of — that
	 * removal is deferred and re-aimed at the real row by `reconcile`. Either way
	 * the caller drops the entry from view.
	 */
	requestRemoval(id: string | null, entry: PendingEntry): string | null {
		useSession(id);
		if (!isLocalPendingId(entry.id)) {
			prunedIds.add(entry.id);
			return entry.id;
		}
		prunedIds.add(entry.id);
		deferredRemovals.push({
			content: normalize(entry.content),
			seq: ++seq,
			requestedAt: Date.now(),
			mapped: false,
		});
		return null;
	},

	/**
	 * Filter a pending list against everything observed so far. Preserves the
	 * array identity when nothing changed, so folding stays a no-op in the common
	 * case.
	 */
	reconcile(id: string | null, entries: PendingEntry[]): PendingEntry[] {
		useSession(id);
		if (entries.length === 0) return entries;
		const now = Date.now();
		const kept: PendingEntry[] = [];
		for (const entry of entries) {
			// #588: a drained queue row runs as the turn its id names.
			if (deliveredTurnIds.has(entry.id) || prunedIds.has(entry.id)) continue;
			// First sighting stamps the entry; an entry we meet for the first time
			// here can never be older than a delivery, so it is never content-pruned.
			const bornAt = stamp(entry.id);
			const text = normalize(entry.content);
			const delivery = deliveries.find(
				(d) => d.matchedEntryId === null && d.content === text && bornAt < d.seq
			);
			if (delivery) {
				delivery.matchedEntryId = entry.id;
				prunedIds.add(entry.id);
				continue;
			}
			const removal = deferredRemovals.find(
				(r) =>
					!r.mapped &&
					r.content === text &&
					bornAt > r.seq &&
					now - r.requestedAt < REMOVAL_MAP_WINDOW_MS
			);
			if (removal && !isLocalPendingId(entry.id)) {
				removal.mapped = true;
				prunedIds.add(entry.id);
				removalRequests.push(entry.id);
				continue;
			}
			kept.push(entry);
		}
		return kept.length === entries.length ? entries : kept;
	},

	/**
	 * Server-side ids a deferred × has since been re-aimed at. The caller sends
	 * the `remove_queued_message` frames; the list is emptied by this call.
	 */
	drainRemovalRequests(): string[] {
		if (removalRequests.length === 0) return removalRequests;
		const out = removalRequests;
		removalRequests = [];
		return out;
	},
};
