import { describe, expect, it, beforeEach, vi } from 'vitest';
import { pendingQueue, reconcilePendingPeerMessages, isLocalPendingId, LOCAL_PENDING_PREFIX, type PendingEntry } from './pending-queue';
import type { ChatMessage, PendingPeerMessage } from '../types';

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
	pendingQueue.reset(SID, true);
});

describe('pending peer messages', () => {
	it('preserves an empty queue without inspecting transcript rows', () => {
		const rows: PendingPeerMessage[] = [];
		const role = vi.fn(() => 'user' as const);
		const messages: ChatMessage[] = [{ id: 'history', content: 'done', get role() { return role(); } }];
		expect(reconcilePendingPeerMessages(rows, messages)).toBe(rows);
		expect(role).not.toHaveBeenCalled();
	});
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
	it('does not lose cancellation when the queue acknowledgement is delayed over 15 seconds', () => {
		const clock = vi.spyOn(Date, 'now').mockReturnValue(1000);
		try {
			const local = { ...row('client-a', 'nope'), clientMessageId: 'client-a' };
			pendingQueue.noteOptimistic(SID, local.id);
			expect(pendingQueue.requestRemoval(SID, local)).toBe('client-a');
			clock.mockReturnValue(17000);
			const server = { ...local, id: 'turn-q' };
			expect(authoritative([server])).toEqual([{ ...server, status: 'cancelling' }]);
			pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn');
			expect(authoritative([server])).toEqual([]);
		} finally {
			clock.mockRestore();
		}
	});

	it('names a legacy server row directly and retains it until a verdict', () => {
		const entry = row('turn-q', 'nope');
		authoritative([entry]);
		expect(pendingQueue.requestRemoval(SID, entry)).toBe('turn-q');
		expect(authoritative([])).toEqual([{ ...entry, status: 'cancelling' }]);
	});

	it('sends the stable identity before the queue acknowledgement exists', () => {
		const local = { ...row('client-a', 'nope'), clientMessageId: 'client-a' };
		pendingQueue.noteOptimistic(SID, local.id);
		expect(pendingQueue.requestRemoval(SID, local)).toBe('client-a');
		expect(authoritative([])).toEqual([{ ...local, status: 'cancelling' }]);
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn');
		expect(authoritative([])).toHaveLength(0);
	});

	it('cancels one identity only even when the text is identical', () => {
		const first = { ...row('turn-q', 'nope'), clientMessageId: 'client-a' };
		const second = { ...row('turn-q2', 'nope'), clientMessageId: 'client-b' };
		pendingQueue.requestRemoval(SID, first);
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn');
		expect(authoritative([first, second])).toEqual([second]);
	});

	it('keeps a confirmed withdrawal gone when a stale snapshot re-asserts it', () => {
		const entry = row('turn-q', 'nope');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.applyRemovalVerdict(SID, entry.id, 'withdrawn');
		expect(authoritative([entry])).toEqual([]);
	});

	it('keeps delivery-first visible as delivered despite receipt and empty snapshot', () => {
		const entry = row('turn-q', 'too late');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.noteDelivered(SID, entry.content, 'turn-live');
		pendingQueue.applyRemovalVerdict(SID, entry.id, 'already_delivered');
		expect(authoritative([])).toEqual([{ ...entry, status: 'delivered' }]);
		expect(authoritative([entry])).toEqual([{ ...entry, status: 'delivered' }]);
	});

	it('does not claim withdrawal for an unknown id or a failed socket send', () => {
		const entry = row('turn-q', 'nope');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.applyRemovalVerdict(SID, entry.id, 'not_found');
		expect(authoritative([entry])).toHaveLength(1);
		pendingQueue.removalFailed(SID, entry.id);
		expect(authoritative([entry])).toEqual([entry]);
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
