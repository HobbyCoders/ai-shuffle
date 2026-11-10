import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
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

	it('retires delivery-first despite receipt and stale snapshots', () => {
		const entry = row('turn-q', 'too late');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.noteDelivered(SID, entry.content, 'turn-live');
		pendingQueue.applyRemovalVerdict(SID, entry.id, 'already_delivered');
		expect(authoritative([])).toEqual([]);
		expect(authoritative([entry])).toEqual([]);
	});

	it('retires an unknown legacy id and recovers text without claiming withdrawal', () => {
		const entry = row('turn-q', 'nope');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.applyRemovalVerdict(SID, entry.id, 'not_found');
		expect(authoritative([entry])).toEqual([]);
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBe(entry.content);
		expect(pendingQueue.removalVerdict(SID, entry.id)).toBe('not_found');
	});

	it('keeps the bubble actionable after a failed cancellation send', () => {
		const entry = row('turn-q', 'nope');
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.removalFailed(SID, entry.id);
		expect(authoritative([entry])).toEqual([entry]);
		expect(pendingQueue.removalVerdict(SID, entry.id)).toBeUndefined();
	});
});

describe('pendingQueue — bounded transactions', () => {
	it('retires both optimistic and verdict durable identities before the receipt arrives', () => {
		const entry = { ...row('client-a', 'words', 5000), clientMessageId: 'client-a' };
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'already_delivered', 'turn-old');
		expect(authoritative([])).toEqual([]);
		expect(authoritative([{ ...entry, id: 'turn-old' }])).toEqual([]);
		const requeue = { ...entry, id: 'turn-new', queuedAt: 15000 };
		expect(authoritative([requeue])).toEqual([requeue]);
	});

	it('keeps the newer cancellation transaction and draft after an older delivered verdict', () => {
		const old = { ...row('turn-old', 'words', 5000), clientMessageId: 'client-a' };
		pendingQueue.requestRemoval(SID, old);
		pendingQueue.removalFailed(SID, 'client-a'); // old cancellation timed out
		const newer = { ...old, id: 'turn-new', queuedAt: 15000 };
		authoritative([newer]);
		pendingQueue.requestRemoval(SID, newer);
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'already_delivered', 'turn-old');
		expect(authoritative([newer])).toEqual([{ ...newer, status: 'cancelling' }]);
		expect(pendingQueue.removalVerdict(SID, 'client-a')).toBeUndefined();
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn', 'turn-new');
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBe('words');
	});

	it('keeps a newer server row that arrives before the older attempt verdict', () => {
		const entry = { ...row('client-a', 'words', 5000), clientMessageId: 'client-a' };
		pendingQueue.requestRemoval(SID, entry);
		const requeue = { ...entry, id: 'turn-new', queuedAt: 15000 };
		authoritative([requeue]);
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'already_delivered', 'turn-old');
		expect(authoritative([requeue])).toEqual([{ ...requeue, status: 'cancelling' }]);
	});

	it.each([false, true])('does not let an older receipt (live=%s) retire a newer cancellation attempt', (live) => {
		const requeue = { ...row('turn-new', 'words', 15000), clientMessageId: 'client-a' };
		authoritative([requeue]);
		pendingQueue.requestRemoval(SID, requeue);
		pendingQueue.noteDelivered(SID, 'words', 'turn-old', live, 'client-a', new Date(10000).toISOString());
		expect(authoritative([requeue])).toEqual([{ ...requeue, status: 'cancelling' }]);
		expect(pendingQueue.removalVerdict(SID, 'client-a')).toBeUndefined();
		pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn');
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBe('words');
	});

	it('a receipt beats cancellation memory and a first-seen stale row', () => {
		const entry = { ...row('client-a', 'words'), clientMessageId: 'client-a' };
		pendingQueue.noteOptimistic(SID, entry.id);
		pendingQueue.requestRemoval(SID, entry);
		authoritative([]);
		pendingQueue.noteDelivered(SID, 'words', 'live', true, 'client-a');
		expect(authoritative([{ ...entry, id: 'unseen-server-row' }])).toEqual([]);
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBeNull();
	});
	it('retires a first-seen stale row even without cancellation', () => {
		pendingQueue.noteDelivered(SID, 'words', 'live', true, 'client-a');
		expect(authoritative([{ ...row('unseen', 'words'), clientMessageId: 'client-a' }])).toEqual([]);
	});
	it('expires a missing verdict and allows another cancellation attempt', () => {
		vi.useFakeTimers();
		try {
			const entry = row('turn-q', 'words'), timeout = vi.fn();
			pendingQueue.requestRemoval(SID, entry, timeout);
			vi.advanceTimersByTime(10_000);
			expect(timeout).toHaveBeenCalledOnce();
			expect(authoritative([entry])).toEqual([entry]);
			pendingQueue.requestRemoval(SID, entry);
			pendingQueue.applyRemovalVerdict(SID, entry.id, 'withdrawn');
			expect(authoritative([entry])).toEqual([]);
		} finally { pendingQueue.reset(SID, true); vi.useRealTimers(); }
	});
	it('recovers the words when a withdrawal verdict arrives after its timeout', () => {
		vi.useFakeTimers();
		try {
			const entry = { ...row('turn-a', 'late recovery'), clientMessageId: 'client-a' };
			pendingQueue.requestRemoval(SID, entry);
			vi.advanceTimersByTime(10_000);
			pendingQueue.applyRemovalVerdict(SID, 'client-a', 'withdrawn');
			expect(pendingQueue.takeWithdrawnDraft(SID)).toBe('late recovery');
			expect(authoritative([entry])).toEqual([]);
		} finally { pendingQueue.reset(SID, true); vi.useRealTimers(); }
	});
	it('a delivery winning cancellation does not suppress a legitimate newer server requeue', () => {
		const entry = { ...row('client-a', 'repeat', 5000), clientMessageId: 'client-a' };
		pendingQueue.requestRemoval(SID, entry);
		pendingQueue.noteDelivered(SID, 'repeat', 'turn-old', true, 'client-a', new Date(10000).toISOString());
		expect(pendingQueue.reconcile(SID, [entry])).toEqual([]);
		const requeue = { ...row('turn-new', 'repeat', 15000), clientMessageId: 'client-a' };
		expect(authoritative([requeue])).toEqual([requeue]);
	});
	it('keeps an unadmitted echo actionable through unrelated empty snapshots', () => {
		const entry = { ...row('client-a', 'words'), clientMessageId: 'client-a' };
		pendingQueue.trackSend(SID, entry, vi.fn());
		expect(authoritative([])).toEqual([entry]);
		pendingQueue.acknowledgeSend(SID, 'client-a');
		expect(authoritative([])).toEqual([]);
	});
	it('restores a refused echo once, even after an empty snapshot', () => {
		const entry = { ...row('client-a', 'recover'), clientMessageId: 'client-a' };
		pendingQueue.trackSend(SID, entry, vi.fn());
		authoritative([]);
		pendingQueue.rejectSend(SID, 'client-a');
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBe('recover');
		pendingQueue.rejectSend(SID, 'client-a');
		expect(pendingQueue.takeWithdrawnDraft(SID)).toBeNull();
		expect(authoritative([entry])).toEqual([]);
	});
	it('bounds admission waiting and cancels the timer on an ack', () => {
		vi.useFakeTimers();
		try {
			const entry = { ...row('client-a', 'words'), clientMessageId: 'client-a' }, timeout = vi.fn();
			pendingQueue.trackSend(SID, entry, timeout);
			pendingQueue.acknowledgeSend(SID, 'client-a');
			vi.advanceTimersByTime(10_000);
			expect(timeout).not.toHaveBeenCalled();
			pendingQueue.trackSend(SID, entry, timeout);
			vi.advanceTimersByTime(10_000);
			expect(timeout).toHaveBeenCalledOnce();
		} finally { pendingQueue.reset(SID, true); vi.useRealTimers(); }
	});
});

describe('recovery-only sends', () => {
	it('does not let a refusal destination claim a New Chat command', () => {
		pendingQueue.trackSend(null, { ...row('user-b', 'B'), clientMessageId: 'client-b' });
		pendingQueue.rejectSend('s2', 'client-b');
		expect(pendingQueue.takeWithdrawnDraft('s2')).toBeNull();
		pendingQueue.rejectSend(null, 'client-b');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBe('B');
	});
	it('never projects an idle recovery record as a pending row in any session', () => {
		pendingQueue.trackSend(null, { ...row('user-a', 'A'), clientMessageId: 'client-a' });
		expect(pendingQueue.reconcile(null, [])).toEqual([]);
		expect(pendingQueue.reconcile('s1', [])).toEqual([]);
		pendingQueue.rejectSend(null, 'client-a');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBe('A');
	});
	it.each(['ack', 'receipt'] as const)('clears the first-send recovery by command on an assigned-session %s', kind => {
		pendingQueue.trackSend(null, { ...row('user-a', 'A'), clientMessageId: 'client-a' });
		pendingQueue.trackSend(null, { ...row('user-b', 'B'), clientMessageId: 'client-b' });
		if (kind === 'ack') pendingQueue.acknowledgeSend('s1', 'client-a');
		else pendingQueue.noteDelivered('s1', 'A', 'turn-a', true, 'client-a');
		pendingQueue.rejectSend(null, 'client-a');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBeNull();
		pendingQueue.rejectSend(null, 'client-b');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBe('B');
		expect(pendingQueue.reconcile(null, [])).toEqual([]);
	});
	it('does not let another session consume an existing chat recovery record', () => {
		pendingQueue.trackSend('s1', { ...row('user-a', 'A'), clientMessageId: 'client-a' });
		pendingQueue.rejectSend('s2', 'client-a');
		expect(pendingQueue.takeWithdrawnDraft('s2')).toBeNull();
		pendingQueue.rejectSend('s1', 'client-a');
		expect(pendingQueue.takeWithdrawnDraft('s1')).toBe('A');
	});
	it('bounds recovery records to the newest 50 sends', () => {
		for (let i = 0; i <= 50; i++) pendingQueue.trackSend(null, {
			...row(`user-${i}`, `words-${i}`), clientMessageId: `client-${i}`,
		});
		pendingQueue.rejectSend(null, 'client-0');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBeNull();
		pendingQueue.rejectSend(null, 'client-50');
		expect(pendingQueue.takeWithdrawnDraft(null)).toBe('words-50');
	});
	it('expires recovery-only text after 30 minutes', () => {
		vi.useFakeTimers();
		try {
			pendingQueue.trackSend(null, { ...row('user-a', 'A'), clientMessageId: 'client-a' });
			vi.advanceTimersByTime(30 * 60_000);
			pendingQueue.rejectSend(null, 'client-a');
			expect(pendingQueue.takeWithdrawnDraft(null)).toBeNull();
		} finally { vi.useRealTimers(); }
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

describe('pendingQueue — parked per session (#524)', () => {
	const CMD = 'client-' + 'c'.repeat(32);

	it('hands a left session its bubbles back once, and keeps sessions apart', () => {
		const a = [{ id: 'turn-q', clientMessageId: CMD, content: 'later', queuedAt: 1 }];
		pendingQueue.park('A', a);
		pendingQueue.park('B', []);
		expect(pendingQueue.unpark('B')).toEqual([]);
		const back = pendingQueue.unpark('A');
		expect(back).toEqual(a);
		expect(back[0]).not.toBe(a[0]); // a copy, not the live row
		expect(pendingQueue.unpark('A')).toEqual([]);
	});

	it('survives the feed resets a switch performs, but not a full store reset', () => {
		pendingQueue.park('A', [row('turn-q', 'later')]);
		pendingQueue.reset(null);
		pendingQueue.reset('B');
		expect(pendingQueue.parkedFor('A')).toHaveLength(1);
		pendingQueue.reset(null, true);
		expect(pendingQueue.parkedFor('A')).toEqual([]);
	});

	it('never hands back a bubble whose withdrawal was confirmed while parked', () => {
		const entry = { id: 'turn-q', clientMessageId: CMD, content: 'nvm', queuedAt: 1 };
		pendingQueue.park('A', [entry, row('turn-r', 'keep')]);
		pendingQueue.requestRemoval('A', entry);
		pendingQueue.applyRemovalVerdict('A', CMD, 'withdrawn');
		expect(pendingQueue.unpark('A').map(e => e.id)).toEqual(['turn-r']);
	});

	it('bounds how many sessions it remembers, dropping the oldest', () => {
		for (let i = 0; i < 55; i++) pendingQueue.park(`s-${i}`, [row(`t-${i}`, 'x')]);
		expect(pendingQueue.parkedFor('s-0')).toEqual([]);
		expect(pendingQueue.parkedFor('s-54')).toHaveLength(1);
	});
});

describe('pendingQueue — command-id receipts (#524)', () => {
	const ONE = 'client-' + '1'.repeat(32);
	const TWO = 'client-' + '2'.repeat(32);

	it('a live receipt answers only the bubble with its command id, not an identical-text sibling', () => {
		const rows = [
			{ id: 'turn-1', clientMessageId: ONE, content: 'yes', queuedAt: 1 },
			{ id: 'turn-2', clientMessageId: TWO, content: 'yes', queuedAt: 2 },
		];
		authoritative(rows);
		pendingQueue.noteDelivered(SID, 'yes', 'turn-live', true, TWO);
		expect(pendingQueue.reconcile(SID, rows).map(r => r.id)).toEqual(['turn-1']);
	});

	it('replayed history retires an echo or an older server row by command id, never by text', () => {
		const echo = { id: ONE, clientMessageId: ONE, content: 'go', queuedAt: Date.now() };
		const server = { id: 'turn-2', clientMessageId: TWO, content: 'go', queuedAt: Date.parse('2026-10-24T12:00:00Z') };
		const plain = row('turn-3', 'go');
		pendingQueue.noteDelivered(SID, 'go', 'turn-live', false, ONE, '2026-10-24T12:00:01Z');
		pendingQueue.noteDelivered(SID, 'go', 'turn-live', false, TWO, '2026-10-24T12:00:01Z');
		expect(pendingQueue.reconcile(SID, [echo, server, plain])).toEqual([plain]);
	});

	it('a replayed receipt with no server time does not retire a server row', () => {
		const server = { id: 'turn-2', clientMessageId: TWO, content: 'go', queuedAt: 5 };
		pendingQueue.noteDelivered(SID, 'go', 'turn-live', false, TWO);
		expect(pendingQueue.reconcile(SID, [server])).toEqual([server]);
	});
});

// Review round 8 S7: a send's attachments are never evicted while it may still
// run; every terminal outcome removes them, so the store cannot grow unbounded.
describe('pendingQueue — a send\'s attachments', () => {
	const uploads = (key: string) => pendingQueue.noteSendUploads('s1', key, [`/uploads/${key}.pdf`]);
	const queued = (key: string): PendingEntry => ({ id: key, clientMessageId: key, content: `Read ${key}`, queuedAt: 1 });

	it('keeps them for every send still waiting, however many', () => {
		for (let i = 0; i < 60; i++) uploads(`client-${i}`);
		expect(pendingQueue.hasSendUploads('client-0')).toBe(true);
		for (let i = 0; i < 60; i++) pendingQueue.forgetSendUploads(`client-${i}`);
	});

	it.each([
		['its receipt', (key: string) => pendingQueue.noteDelivered('s1', `Read ${key}`, 'turn-1', true, key)],
		['a delivery verdict', (key: string) => pendingQueue.applyRemovalVerdict('s1', key, 'already_delivered')],
		['a withdrawal verdict it did not ask for', (key: string) => pendingQueue.applyRemovalVerdict('s1', key, 'withdrawn')],
		['a refusal', (key: string) => pendingQueue.rejectSend('s1', key, `Read ${key}`)],
		['a withdrawal receipt from another page', (key: string) => pendingQueue.forgetSendUploads(key)],
	] as const)('drops them on %s', (_case, end) => {
		pendingQueue.reset('s1');
		uploads('client-x');
		end('client-x');
		expect(pendingQueue.hasSendUploads('client-x')).toBe(false);
		pendingQueue.takeWithdrawnRestore('s1');
	});

	it('keeps them past a withdrawal receipt until this page\'s own verdict hands them back', () => {
		pendingQueue.reset('s1');
		uploads('client-y');
		pendingQueue.requestRemoval('s1', queued('client-y'));
		pendingQueue.forgetSendUploads('client-y');
		expect(pendingQueue.hasSendUploads('client-y')).toBe(true);
		pendingQueue.applyRemovalVerdict('s1', 'client-y', 'withdrawn');
		expect(pendingQueue.hasSendUploads('client-y')).toBe(false);
		expect(pendingQueue.takeWithdrawnRestore('s1')).toEqual({ prompt: 'Read client-y', uploadPaths: ['/uploads/client-y.pdf'], uploadReferences: [] });
	});

	it('hands back a server row\'s recorded paths when this page never sent it (#1313)', () => {
		pendingQueue.reset('s1');
		pendingQueue.requestRemoval('s1', { ...queued('client-z'), uploadPaths: ['/uploads/z.txt'] });
		pendingQueue.applyRemovalVerdict('s1', 'client-z', 'withdrawn');
		expect(pendingQueue.takeWithdrawnRestore('s1')).toEqual({ prompt: 'Read client-z', uploadPaths: ['/uploads/z.txt'], uploadReferences: [] });
	});

	it('gives the queue snapshot this page\'s own recorded paths (#1313)', () => {
		pendingQueue.reset('s1');
		uploads('client-w');
		expect(pendingQueue.sendUploadPaths('client-w')).toEqual(['/uploads/client-w.pdf']);
		expect(pendingQueue.sendUploadPaths(undefined)).toEqual([]);
	});

	// Unsend timed out and the socket verdict was lost; the durable receipt still came.
	describe('when the verdict of a timed-out Unsend is lost', () => {
		const restore = { prompt: 'Read client-z', uploadPaths: ['/uploads/client-z.pdf'], uploadReferences: [] };
		beforeEach(() => {
			vi.useFakeTimers();
			pendingQueue.reset('s1', true);
			uploads('client-z');
			pendingQueue.requestRemoval('s1', queued('client-z'));
			vi.advanceTimersByTime(10_000);
		});
		afterEach(() => vi.useRealTimers());

		it('clears the entry on the receipt and hands the files back', () => {
			expect(pendingQueue.settleWithdrawalReceipt('client-z', '2026-01-01T00:00:00Z', true)).toBe(true);
			expect(pendingQueue.hasSendUploads('client-z')).toBe(false);
			expect(pendingQueue.takeWithdrawnRestore('s1')).toEqual(restore);
		});

		it('hands them back once when the delayed verdict does arrive', () => {
			pendingQueue.settleWithdrawalReceipt('client-z', '2026-01-01T00:00:00Z', true);
			pendingQueue.applyRemovalVerdict('s1', 'client-z', 'withdrawn');
			expect(pendingQueue.takeWithdrawnRestore('s1')).toEqual(restore);
			expect(pendingQueue.takeWithdrawnRestore('s1')).toBeNull();
		});

		it('still hands them back when the verdict beats the receipt', () => {
			pendingQueue.applyRemovalVerdict('s1', 'client-z', 'withdrawn');
			expect(pendingQueue.settleWithdrawalReceipt('client-z', '2026-01-01T00:00:00Z', true)).toBe(false);
			expect(pendingQueue.takeWithdrawnRestore('s1')).toEqual(restore);
		});
	});

	// Stored times keep microseconds and Date.parse keeps milliseconds. Cutting both
	// the same way can only make two moments equal, never swap their order.
	it.each([
		['a later millisecond', '2026-01-01T00:00:00.123999+00:00', '2026-01-01T00:00:00.124001+00:00', true],
		['the same millisecond', '2026-01-01T00:00:00.123100+00:00', '2026-01-01T00:00:00.123900+00:00', false],
		['an older attempt', '2026-01-01T00:00:00.124001+00:00', '2026-01-01T00:00:00.123999+00:00', false],
	])('compares a receipt to its queued row at stored precision: %s', (_name, queued, receipt, settles) => {
		pendingQueue.reset('s1', true);
		uploads('client-p');
		pendingQueue.requestRemoval('s1', { id: 'row-p', clientMessageId: 'client-p', content: 'Read', queuedAt: new Date(queued).getTime() });
		expect(pendingQueue.settleWithdrawalReceipt('client-p', receipt, true)).toBe(settles);
		expect(pendingQueue.hasSendUploads('client-p')).toBe(!settles);
		pendingQueue.reset('s1', true);
	});

	it('keeps a newer re-queue cancelling, and its files, past an older attempt\'s receipt', () => {
		pendingQueue.reset('s1', true);
		uploads('client-r');
		pendingQueue.requestRemoval('s1', { id: 'row-2', clientMessageId: 'client-r', content: 'Read', queuedAt: Date.parse('2026-01-02T00:00:00Z') });
		expect(pendingQueue.settleWithdrawalReceipt('client-r', '2026-01-01T00:00:00Z', false)).toBe(false);
		expect(pendingQueue.hasSendUploads('client-r')).toBe(true);
		expect(pendingQueue.sendOwner('client-r')).toEqual({ sessionId: 's1' });
		pendingQueue.reset('s1', true);
	});

	describe('when the chat is deleted while a send is queued', () => {
		beforeEach(() => pendingQueue.reset('s1', true));

		it('clears that chat\'s attachments and undelivered drafts, and no other chat\'s', () => {
			uploads('client-a');
			pendingQueue.noteSendUploads('s2', 'client-b', ['/uploads/b.pdf']);
			pendingQueue.rejectSend('s1', 'client-c', 'A draft kept for the next switch-in');
			pendingQueue.forgetSession('s1');
			expect(pendingQueue.hasSendUploads('client-a')).toBe(false);
			expect(pendingQueue.hasSendUploads('client-b')).toBe(true);
			expect(pendingQueue.takeWithdrawnRestore('s1')).toBeNull();
		});

		it('covers a first send adopted from a new chat', () => {
			pendingQueue.noteSendUploads(null, 'client-new', ['/uploads/new.pdf']);
			pendingQueue.adoptSession('s9', ['client-new']);
			pendingQueue.forgetSession('s9');
			expect(pendingQueue.hasSendUploads('client-new')).toBe(false);
		});

		it('stops a running Unsend timer from reporting into the deleted chat', () => {
			vi.useFakeTimers();
			try {
				const onTimeout = vi.fn();
				pendingQueue.requestRemoval('s1', queued('client-t'), onTimeout);
				pendingQueue.forgetSession('s1');
				vi.advanceTimersByTime(10_000);
				expect(onTimeout).not.toHaveBeenCalled();
			} finally {
				vi.useRealTimers();
			}
		});
	});
});
