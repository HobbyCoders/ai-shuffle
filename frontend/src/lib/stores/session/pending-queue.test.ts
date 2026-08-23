import { describe, expect, it, beforeEach } from 'vitest';
import { pendingQueue, isLocalPendingId, LOCAL_PENDING_PREFIX, type PendingEntry } from './pending-queue';

/**
 * The pending bubble ("Not seen yet") is written by two unordered producers: the
 * optimistic local echo and the server's replace-all `queue_updated` snapshots,
 * of which the backend has two independent emitters. These tests pin the
 * orderings that used to leave a bubble on screen for a message the model had
 * already answered.
 */

const SID = 's1';

function row(id: string, content: string, queuedAt = 1): PendingEntry {
	return { id, content, queuedAt };
}

/** The server's snapshot → what actually renders. */
function authoritative(rows: PendingEntry[]): PendingEntry[] {
	pendingQueue.noteAuthoritative(SID, rows);
	return pendingQueue.reconcile(SID, rows);
}

beforeEach(() => {
	pendingQueue.reset(SID);
});

describe('pendingQueue — optimistic echo', () => {
	it('lets a fresh send show its own bubble', () => {
		pendingQueue.noteOptimistic(SID, `${LOCAL_PENDING_PREFIX}1`);
		const local = [row(`${LOCAL_PENDING_PREFIX}1`, 'hello')];
		expect(pendingQueue.reconcile(SID, local)).toEqual(local);
	});

	it('reports the server having spoken since the send began', () => {
		const dispatchedAt = Date.now();
		expect(pendingQueue.authoritySpokeSince(SID, dispatchedAt)).toBe(false);
		authoritative([row('turn-1', 'hello')]);
		expect(pendingQueue.authoritySpokeSince(SID, dispatchedAt)).toBe(true);
	});

	it('does not report an authoritative frame from another session', () => {
		authoritative([row('turn-1', 'hello')]);
		expect(pendingQueue.authoritySpokeSince('other-session', Date.now() - 1000)).toBe(false);
	});

	it('retires the optimistic echo when its text lands on the transcript', () => {
		// No queue_updated ever arrives (the turn had already settled server-side,
		// so the send ran immediately instead of queueing).
		pendingQueue.noteOptimistic(SID, `${LOCAL_PENDING_PREFIX}1`);
		const local = [row(`${LOCAL_PENDING_PREFIX}1`, 'hello')];
		pendingQueue.noteDelivered(SID, 'hello', 'turn-live');
		expect(pendingQueue.reconcile(SID, local)).toEqual([]);
	});

	it('identifies local ids', () => {
		expect(isLocalPendingId(`${LOCAL_PENDING_PREFIX}17`)).toBe(true);
		expect(isLocalPendingId('turn-1')).toBe(false);
	});
});

describe('pendingQueue — a steered message the transcript answered', () => {
	it('drops the row and keeps it dropped when a stale snapshot re-asserts it', () => {
		// 1. The send is enqueued and the server describes the queue.
		expect(authoritative([row('turn-q', 'steer me')])).toHaveLength(1);
		// 2. The steer is absorbed: the transcript gets the user message on the
		//    LIVE turn's id (not the queued row's), and the pump broadcasts empty.
		pendingQueue.noteDelivered(SID, 'steer me', 'turn-live');
		expect(pendingQueue.reconcile(SID, [row('turn-q', 'steer me')])).toEqual([]);
		expect(authoritative([])).toEqual([]);
		// 3. A snapshot taken BEFORE the absorb (the × rebroadcast) lands last.
		expect(authoritative([row('turn-q', 'steer me')])).toEqual([]);
	});

	it('drops the row when the stale snapshot arrives before the transcript message', () => {
		expect(authoritative([row('turn-q', 'steer me')])).toHaveLength(1);
		expect(authoritative([])).toEqual([]);
		// The re-asserting snapshot beats the SSE message.user this time.
		expect(authoritative([row('turn-q', 'steer me')])).toHaveLength(1);
		pendingQueue.noteDelivered(SID, 'steer me', 'turn-live');
		expect(pendingQueue.reconcile(SID, [row('turn-q', 'steer me')])).toEqual([]);
	});

	it('still drops a drained queue row by its turn id (#588)', () => {
		authoritative([row('turn-q', 'queued text')]);
		// A drained row runs as the turn its id names, so the ids match exactly.
		pendingQueue.noteDelivered(SID, 'queued text', 'turn-q');
		expect(pendingQueue.reconcile(SID, [row('turn-q', 'queued text')])).toEqual([]);
	});

	it('ignores backfilled history — an old message is not an answer to a pending one', () => {
		// Session opens: the SSE replay carries a message with the same text the
		// user has just queued again.
		pendingQueue.noteDelivered(SID, 'continue', 'turn-old', false);
		expect(authoritative([row('turn-q', 'continue')])).toHaveLength(1);
	});
});

describe('pendingQueue — a row the server put back', () => {
	it('keeps a re-queued row whose text is already in the transcript', () => {
		authoritative([row('turn-q', 'steer me')]);
		pendingQueue.noteDelivered(SID, 'steer me', 'turn-live');
		expect(authoritative([])).toEqual([]);
		// The user interrupted the turn that swallowed it, so the backend re-queues
		// the message as a FRESH turn row. Its text is already above in the
		// transcript, and that is intended — the row must survive.
		const requeued = authoritative([row('turn-q2', 'steer me')]);
		expect(requeued.map((entry) => entry.id)).toEqual(['turn-q2']);
		// And it keeps surviving across later snapshots.
		expect(authoritative([row('turn-q2', 'steer me')]).map((e) => e.id)).toEqual(['turn-q2']);
	});

	it('retires only one bubble per delivery when the same text is queued twice', () => {
		authoritative([row('turn-a', 'again'), row('turn-b', 'again')]);
		pendingQueue.noteDelivered(SID, 'again', 'turn-live');
		const left = pendingQueue.reconcile(SID, [row('turn-a', 'again'), row('turn-b', 'again')]);
		expect(left.map((entry) => entry.id)).toEqual(['turn-b']);
	});
});

describe('pendingQueue — unsending', () => {
	it('names the server row directly when the entry has a real id', () => {
		authoritative([row('turn-q', 'nope')]);
		expect(pendingQueue.requestRemoval(SID, row('turn-q', 'nope'))).toBe('turn-q');
		expect(pendingQueue.drainRemovalRequests()).toEqual([]);
	});

	it('defers an × on a local id and re-aims it at the server row', () => {
		const localId = `${LOCAL_PENDING_PREFIX}1`;
		pendingQueue.noteOptimistic(SID, localId);
		expect(pendingQueue.requestRemoval(SID, row(localId, 'nope'))).toBeNull();
		// The real row arrives; it must not render, and the removal is re-aimed.
		expect(authoritative([row('turn-q', 'nope')])).toEqual([]);
		expect(pendingQueue.drainRemovalRequests()).toEqual(['turn-q']);
		expect(pendingQueue.drainRemovalRequests()).toEqual([]);
	});

	it('re-aims a deferred × at one row only', () => {
		const localId = `${LOCAL_PENDING_PREFIX}1`;
		pendingQueue.noteOptimistic(SID, localId);
		pendingQueue.requestRemoval(SID, row(localId, 'nope'));
		authoritative([row('turn-q', 'nope')]);
		pendingQueue.drainRemovalRequests();
		// A later, genuinely new send of the same text is untouched.
		expect(authoritative([row('turn-q2', 'nope')]).map((e) => e.id)).toEqual(['turn-q2']);
		expect(pendingQueue.drainRemovalRequests()).toEqual([]);
	});

	it('keeps a removed row gone when a stale snapshot re-asserts it', () => {
		authoritative([row('turn-q', 'nope')]);
		pendingQueue.requestRemoval(SID, row('turn-q', 'nope'));
		expect(authoritative([row('turn-q', 'nope')])).toEqual([]);
	});
});

describe('pendingQueue — session scoping', () => {
	it('forgets everything when the session changes', () => {
		authoritative([row('turn-q', 'steer me')]);
		pendingQueue.noteDelivered(SID, 'steer me', 'turn-live');
		expect(pendingQueue.reconcile(SID, [row('turn-q', 'steer me')])).toEqual([]);
		// Another session's queue is judged on its own evidence.
		const other = [row('turn-q', 'steer me')];
		expect(pendingQueue.reconcile('s2', other)).toEqual(other);
	});

	it('preserves the array identity when nothing is pruned', () => {
		const rows = [row('turn-q', 'still waiting')];
		pendingQueue.noteAuthoritative(SID, rows);
		expect(pendingQueue.reconcile(SID, rows)).toBe(rows);
	});
});
